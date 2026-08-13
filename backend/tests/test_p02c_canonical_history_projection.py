"""P0.2c — Canonical History Projection tests.

Proves History is now a deterministic projection over
`settlement_events` + `prediction_snapshots` and that no active
independent History settlement authority remains.
"""
from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from pathlib import Path

import pytest

from services.history_projection_service import (
    HistoryProjectionService,
    project_pick,
)
from services.settlement_service import SettlementService


# ─── Minimal in-memory fake DB (matches the P0.2a/b test contract) ─
class _Coll:
    def __init__(self):
        self.rows = []
    async def find_one(self, q, proj=None):
        for r in self.rows:
            if all(r.get(k) == v for k, v in q.items()):
                return dict(r)
        return None
    async def insert_one(self, doc):
        self.rows.append(dict(doc))
    async def update_many(self, q, update):
        n = 0
        for r in self.rows:
            if all(r.get(k) == v for k, v in q.items()):
                r.update(update.get("$set", {}))
                n += 1
        return n
    async def update_one(self, q, update, upsert=False):
        for r in self.rows:
            if all(r.get(k) == v for k, v in q.items()):
                r.update(update.get("$set", {}))
                return
        if upsert:
            row = dict(q); row.update(update.get("$set", {}))
            self.rows.append(row)
    def find(self, q, proj=None):
        return _Cursor([r for r in self.rows
                        if _matches(r, q)])


def _matches(row: dict, q: dict) -> bool:
    for k, v in q.items():
        if k == "$or":
            if not any(_matches(row, sub) for sub in v):
                return False
        elif isinstance(v, dict) and "$in" in v:
            if row.get(k) not in v["$in"]:
                return False
        else:
            if row.get(k) != v:
                return False
    return True


class _Cursor:
    def __init__(self, rows):
        self._rows = list(rows)
    def sort(self, *_, **__): return self
    def limit(self, n):
        self._rows = self._rows[:n]
        return self
    async def to_list(self, length=None):
        return list(self._rows[:length] if length else self._rows)
    def __aiter__(self):
        self._i = 0
        return self
    async def __anext__(self):
        if self._i >= len(self._rows):
            raise StopAsyncIteration
        r = self._rows[self._i]; self._i += 1
        return dict(r)


class _FakeDB:
    def __init__(self):
        self._colls: dict[str, _Coll] = defaultdict(_Coll)
    def __getitem__(self, name):
        return self._colls[name]
    def __getattr__(self, name):
        return self._colls[name]


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


async def _seed_snapshot(db, pid: str, *, line=4.5, odds=-115, lock=88.0):
    await db["prediction_snapshots"].insert_one({
        "prediction_id":    pid,
        "is_active":        True,
        "snapshot_version": 1,
        "line":             line,
        "book_odds":        odds,
        "odds_at_pick":     odds,
        "sportsbook":       "DraftKings",
        "book":             "DraftKings",
        "lock_score":       lock,
        "published_lock_score": lock,
        "published_line":   line,
        "published_odds":   odds,
    })


async def _settle(svc, pick, *, result, source, actual, event_final=True,
                    correction_reason=None):
    return await svc.settle_from_pick(
        pick, result=result, source=source, actual_result=actual,
        authoritative_event_final=event_final,
        correction_reason=correction_reason,
    )


# ═════════════════════════════════════════════════════════════════════
# §A — WIN / LOSS / PUSH / VOID project correctly
# ═════════════════════════════════════════════════════════════════════

class TestOutcomesProjectCorrectly:

    @pytest.mark.parametrize("result,expected_status", [
        ("won",   "won"),
        ("lost",  "lost"),
        ("push",  "push"),
        ("void",  "void"),
    ])
    def test_outcome_projects(self, result, expected_status):
        async def go():
            db = _FakeDB()
            svc = SettlementService(db)
            pick = {"id": f"p-{result}", "market": "Strikeouts",
                     "side": "Over", "line": 4.5,
                     "event_id": "e-1"}
            await _seed_snapshot(db, pick["id"])
            await _settle(
                svc, pick, result=result, source="test",
                actual={"actual": 6},
                event_final=(result != "void"),
            )
            proj = await HistoryProjectionService(db).project_one(pick)
            assert proj["status"] == expected_status
            assert proj["result"] == result
            # Provenance surfaced
            assert proj["_canonical_settlement_present"] is True
            assert proj["settlement_event_id"]
            assert proj["settlement_version"] == 1
            assert proj["grader_version"] == "settlement_service.v2.0"
        _run(go())


# ═════════════════════════════════════════════════════════════════════
# §B — PUSH != VOID even after full projection
# ═════════════════════════════════════════════════════════════════════

class TestPushNotVoidThroughProjection:
    def test_push_stays_push_in_history_view(self):
        async def go():
            db = _FakeDB()
            svc = SettlementService(db)
            pick = {"id": "hp-push", "market": "Total Goals Over 2",
                     "side": "Over", "line": 2, "event_id": "e-1"}
            await _seed_snapshot(db, pick["id"])
            await _settle(svc, pick, result="push", source="test",
                          actual={"home": 1, "away": 1})
            proj = await HistoryProjectionService(db).project_one(pick)
            assert proj["status"] == "push"
            assert proj["result"] == "push"
            assert proj["status"] != "void"
        _run(go())


# ═════════════════════════════════════════════════════════════════════
# §C — Idempotency: repeated projection has no side effects
# ═════════════════════════════════════════════════════════════════════

class TestProjectionIdempotency:
    def test_multiple_projections_identical(self):
        async def go():
            db = _FakeDB()
            svc = SettlementService(db)
            pick = {"id": "hi-1", "market": "Strikeouts",
                     "side": "Over", "line": 4.5, "event_id": "e-1"}
            await _seed_snapshot(db, pick["id"])
            await _settle(svc, pick, result="won", source="test",
                          actual={"actual": 6})
            hp = HistoryProjectionService(db)
            proj1 = await hp.project_one(pick)
            proj2 = await hp.project_one(pick)
            proj3 = await hp.project_one(pick)
            # Canonical fields identical.
            for k in ("status", "result", "settlement_event_id",
                     "settlement_version", "grader_version"):
                assert proj1[k] == proj2[k] == proj3[k]
            # No duplicate rows in either canonical collection.
            assert len(db["settlement_events"].rows) == 1
            assert len([r for r in db["settlement_events"].rows
                        if r["is_active"]]) == 1
        _run(go())


# ═════════════════════════════════════════════════════════════════════
# §D — Correction: LOSS → WIN updates current view, no duplicate pick,
#      full lineage retained.
# ═════════════════════════════════════════════════════════════════════

class TestCorrectionProjection:
    def test_v2_supersedes_v1_history_reflects_correction(self):
        async def go():
            db = _FakeDB()
            svc = SettlementService(db)
            pick = {"id": "hc-1", "market": "Strikeouts",
                     "side": "Over", "line": 4.5, "event_id": "e-1"}
            await _seed_snapshot(db, pick["id"])
            # v1: LOST (bad stat feed)
            v1 = await _settle(svc, pick, result="lost", source="test",
                                actual={"actual": 3})
            v1_id = v1["event"]["settlement_id"]
            # v2: authoritative correction — WON
            v2 = await _settle(svc, pick, result="won", source="test",
                                actual={"actual": 6},
                                correction_reason="mlb_stat_correction_20260813")
            # Current History view = WON
            proj = await HistoryProjectionService(db).project_one(pick)
            assert proj["status"] == "won"
            assert proj["result"] == "won"
            assert proj["settlement_version"] == 2
            assert proj["supersedes_settlement_id"] == v1_id
            assert proj["correction_reason"] == "mlb_stat_correction_20260813"
            assert proj["old_result"] == "lost"
            assert proj["new_result"] == "won"
            # Lineage preserved — two entries, one active, one prior.
            lineage = proj["settlement_lineage"]
            assert len(lineage) == 2
            versions = sorted(e["settlement_version"] for e in lineage)
            assert versions == [1, 2]
            actives  = [e for e in lineage if e["is_active"]]
            inactives = [e for e in lineage if not e["is_active"]]
            assert len(actives) == 1 and actives[0]["result"] == "won"
            assert len(inactives) == 1 and inactives[0]["result"] == "lost"

            # The pick_id in the History view is UNCHANGED — no duplicate.
            proj2 = await HistoryProjectionService(db).project_one(pick)
            assert proj2["id"] == proj["id"] == "hc-1"
        _run(go())


# ═════════════════════════════════════════════════════════════════════
# §E — Frozen pregame values SURVIVE settlement.  Nothing about the
#      canonical settlement path can rewrite line/odds/lock_score/etc.
# ═════════════════════════════════════════════════════════════════════

class TestFrozenPregameTruth:
    def test_settlement_never_overwrites_frozen_fields(self):
        async def go():
            db = _FakeDB()
            svc = SettlementService(db)
            pick = {"id": "fz-1", "market": "Strikeouts",
                     "side": "Over", "line": 4.5, "event_id": "e-1",
                     # Pregame frozen values as they were AT publication:
                     "line":       4.5,
                     "book_odds":  -115,
                     "sportsbook": "DraftKings",
                     "lock_score": 88.0}
            await _seed_snapshot(db, pick["id"], line=4.5, odds=-115, lock=88.0)
            await _settle(svc, pick, result="won", source="test",
                          actual={"actual": 6})
            proj = await HistoryProjectionService(db).project_one(pick)
            # Frozen pregame values equal snapshot values, NOT settlement values.
            assert proj["line"]       == 4.5
            assert proj["book_odds"]  == -115
            assert proj["sportsbook"] == "DraftKings"
            assert proj["lock_score"] == 88.0
            assert proj["published_lock_score"] == 88.0
            assert proj["published_line"]      == 4.5
            assert proj["published_odds"]      == -115
        _run(go())

    def test_missing_snapshot_fields_remain_unavailable_not_fabricated(self):
        async def go():
            db = _FakeDB()
            svc = SettlementService(db)
            # NO snapshot seeded — simulates the v0 backfill gap.
            pick = {"id": "fz-2", "market": "Strikeouts", "side": "Over",
                     "line": 4.5, "event_id": "e-1"}
            await _settle(svc, pick, result="won", source="test",
                          actual={"actual": 6})
            proj = await HistoryProjectionService(db).project_one(pick)
            # Snapshot fields we didn't publish must stay unavailable.
            assert proj.get("published_lock_score") is None
            assert proj.get("magic_evidence") is None
            assert proj.get("apex_status") is None
            # But canonical settlement is still projected correctly.
            assert proj["status"] == "won"
            assert proj["result"] == "won"
        _run(go())


# ═════════════════════════════════════════════════════════════════════
# §F — LIVE picks (no canonical event) MUST NOT project as settled
# ═════════════════════════════════════════════════════════════════════

class TestLiveNotSettled:
    def test_no_canonical_event_does_not_project_won(self):
        pick = {"id": "live-1", "status": "won"}   # legacy mirror w/o ledger
        proj = project_pick(pick, active_event=None, snapshot=None)
        # We refuse to trust legacy status without a canonical event.
        assert proj["status"] == "unresolved"
        assert proj["result"] is None
        assert proj["_legacy_status_without_canonical_event"] == "won"
        assert proj["_canonical_settlement_present"] is False


# ═════════════════════════════════════════════════════════════════════
# §G — Reaper VOID + auto-void 14d + NRFI + cross-sport parity
# ═════════════════════════════════════════════════════════════════════

class TestVoidProjection:
    def test_reaper_void_projects(self):
        async def go():
            db = _FakeDB()
            svc = SettlementService(db)
            pick = {"id": "rp-1", "market": "Ks Over 5.5", "side": "Over",
                     "line": 5.5, "event_id": "e-stale"}
            await _settle(svc, pick, result="void", source="stuck_pick_reaper",
                          actual={}, event_final=False)
            proj = await HistoryProjectionService(db).project_one(pick)
            assert proj["status"] == "void"
            assert proj["result"] == "void"
            assert proj["settlement_source"] == "stuck_pick_reaper"
        _run(go())

    def test_auto_void_14d_projects(self):
        async def go():
            db = _FakeDB()
            svc = SettlementService(db)
            pick = {"id": "av-1", "market": "ML", "side": "home",
                     "line": None, "event_id": "e-old"}
            await _settle(svc, pick, result="void",
                          source="settlement_engine:auto_void_stale_14d",
                          actual={}, event_final=False)
            proj = await HistoryProjectionService(db).project_one(pick)
            assert proj["status"] == "void"
            assert proj["settlement_source"] == "settlement_engine:auto_void_stale_14d"
        _run(go())


class TestNRFIProjection:
    def test_nrfi_won_projects(self):
        async def go():
            db = _FakeDB()
            svc = SettlementService(db)
            pick = {"id": "nrfi-final-1", "market": "1st_inning_runs",
                     "side": "NRFI", "line": 0.5,
                     "event_id": "mlb_game_pk_1234"}
            await _settle(svc, pick, result="won",
                          source="mlb_stats_api_linescore",
                          actual={"runs_in_1st": 0})
            proj = await HistoryProjectionService(db).project_one(pick)
            assert proj["status"] == "won"
            assert proj["settlement_source"] == "mlb_stats_api_linescore"
        _run(go())


# ═════════════════════════════════════════════════════════════════════
# §H — History / Analytics / canonical parity invariant
# ═════════════════════════════════════════════════════════════════════

class TestHistoryAnalyticsCanonicalParity:
    def test_history_matches_canonical_settlement(self):
        async def go():
            db = _FakeDB()
            svc = SettlementService(db)
            picks = [
                {"id": "hp1", "market": "M1", "side": "Over", "line": 4,
                 "event_id": "e1"},
                {"id": "hp2", "market": "M2", "side": "Under", "line": 6,
                 "event_id": "e2"},
                {"id": "hp3", "market": "M3", "side": "Home", "line": None,
                 "event_id": "e3"},
            ]
            expected = ["won", "lost", "push"]
            for p, r in zip(picks, expected):
                await _seed_snapshot(db, p["id"])
                await _settle(svc, p, result=r, source="test",
                              actual={"actual": r})
            projections = await HistoryProjectionService(db).project_many(picks)
            for proj, r in zip(projections, expected):
                # History = canonical settlement.  No divergence.
                canonical = await db["settlement_events"].find_one(
                    {"prediction_id": proj["id"], "is_active": True})
                assert proj["result"] == canonical["result"] == r
                assert proj["settlement_event_id"] == canonical["settlement_id"]
        _run(go())


# ═════════════════════════════════════════════════════════════════════
# §I — Wrong-identity: does not attach settlement to unrelated pick
# ═════════════════════════════════════════════════════════════════════

class TestWrongIdentity:
    def test_settlement_only_joins_by_prediction_id(self):
        async def go():
            db = _FakeDB()
            svc = SettlementService(db)
            pick_a = {"id": "ida-1", "market": "M", "side": "Over",
                       "line": 4.5, "event_id": "e-A"}
            pick_b = {"id": "idb-1", "market": "M", "side": "Over",
                       "line": 4.5, "event_id": "e-B"}
            # Settle only A.
            await _settle(svc, pick_a, result="won", source="test",
                          actual={"actual": 6})
            projections = await HistoryProjectionService(db).project_many(
                [pick_a, pick_b])
            proj_a, proj_b = projections
            # A got the settlement.
            assert proj_a["status"] == "won"
            assert proj_a["_canonical_settlement_present"] is True
            # B did NOT — even though market/side/line match A.
            assert proj_b["_canonical_settlement_present"] is False
            assert proj_b["settlement_lineage"] == []
        _run(go())


# ═════════════════════════════════════════════════════════════════════
# §J — Stale legacy `status` cannot override newer canonical event
# ═════════════════════════════════════════════════════════════════════

class TestStaleLegacyDoesNotOverride:
    def test_legacy_status_overridden_by_active_settlement(self):
        async def go():
            db = _FakeDB()
            svc = SettlementService(db)
            pick = {"id": "sl-1", "market": "M", "side": "Over",
                     "line": 4.5, "event_id": "e-1",
                     # Legacy mirror set to WRONG value.
                     "status": "lost"}
            await _settle(svc, pick, result="won", source="test",
                          actual={"actual": 6})
            proj = await HistoryProjectionService(db).project_one(pick)
            # Canonical wins; legacy mirror is overwritten in the view.
            assert proj["status"] == "won"
            assert proj["result"] == "won"
        _run(go())


# ═════════════════════════════════════════════════════════════════════
# §K — Rogue History-writer static guard
# ═════════════════════════════════════════════════════════════════════

class TestNoRogueHistoryWriters:
    """Any code that writes History settlement-outcome fields
    (`result`, `settlement_status`, `settled_at`, `units_profit`,
    `final_result`) into a HISTORY-typed collection outside the
    approved projection/migration boundary must be flagged.

    The scan targets History-shaped collection names (case-insensitive):
        pick_history, pick_histories,
        player_history, team_history,
        canonical_history, history_projection.

    The `picks` compat mirror is EXEMPT because it is owned by
    ``SettlementService`` (locked by the P0.2b static guard).
    User-bet propagation (`user_bets`) is a distinct product surface;
    it is exempt but its own writes remain subject to the P0.2b rule
    that they must be driven by canonical `pick.status`.

    ALLOWED_FILES: modules that are the CANONICAL History projection
    or a legitimate migration/backfill helper.
    """
    ALLOWED_FILES = {
        "services/history_projection_service.py",
    }

    HISTORY_COLL_PAT = re.compile(
        r"""(?:pick_history|pick_histories|player_history|team_history|"""
        r"""canonical_history|history_projection)"""
    )
    OUTCOME_FIELD_PAT = re.compile(
        r"""["'](?:result|settlement_status|settled_at|units_profit|"""
        r"""final_result)["']\s*:"""
    )

    def test_scan_backend_for_direct_history_settlement_writers(self):
        backend = Path("/app/backend")
        rogue = []
        for py in backend.rglob("*.py"):
            rel = str(py.relative_to(backend))
            if any(s in rel for s in ("__pycache__", "tests/",
                                        "scripts/", ".venv")):
                continue
            if rel in self.ALLOWED_FILES:
                continue
            src = py.read_text(errors="ignore")
            # Look for `db.<history_coll>.update|insert` writes that
            # ALSO set a canonical settlement field.
            for m in re.finditer(
                r"""db\[["']([\w_]+)["']\]\.(?:update_one|update_many|"""
                r"""insert_one|insert_many|bulk_write)""", src,
            ):
                coll = m.group(1)
                if self.HISTORY_COLL_PAT.search(coll):
                    # Take a window around the match to check for
                    # outcome-field writes.
                    start, end = m.span()
                    window = src[start:end + 600]
                    if self.OUTCOME_FIELD_PAT.search(window):
                        rogue.append(f"{rel}:coll={coll}")
            for m in re.finditer(
                r"""db\.([\w_]+)\.(?:update_one|update_many|"""
                r"""insert_one|insert_many|bulk_write)""", src,
            ):
                coll = m.group(1)
                if self.HISTORY_COLL_PAT.search(coll):
                    start, end = m.span()
                    window = src[start:end + 600]
                    if self.OUTCOME_FIELD_PAT.search(window):
                        rogue.append(f"{rel}:coll={coll}")
        assert not rogue, (
            "Rogue History settlement writers (bypass "
            "HistoryProjectionService): " + str(rogue))


# ═════════════════════════════════════════════════════════════════════
# §L — Projection rebuild is deterministic
# ═════════════════════════════════════════════════════════════════════

class TestDeterministicRebuild:
    def test_two_full_rebuilds_produce_identical_output(self):
        async def go():
            db = _FakeDB()
            svc = SettlementService(db)
            picks = [
                {"id": f"det-{i}", "market": "M", "side": "Over",
                 "line": 4.5, "event_id": f"e-{i}"}
                for i in range(5)
            ]
            for i, p in enumerate(picks):
                await _seed_snapshot(db, p["id"])
                r = ["won", "lost", "push", "void", "won"][i]
                await _settle(svc, p, result=r, source="test",
                              actual={"i": i},
                              event_final=(r != "void"))
            hp = HistoryProjectionService(db)
            run_a = await hp.project_many(picks)
            run_b = await hp.project_many(picks)
            # Same canonical fields on every rebuild.
            for a, b in zip(run_a, run_b):
                for k in ("status", "result", "settlement_event_id",
                         "settlement_version", "grader_version",
                         "settlement_source"):
                    assert a[k] == b[k]
        _run(go())
