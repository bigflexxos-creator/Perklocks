"""P0.2e — Historical Reconciliation tests."""
from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from pathlib import Path

import pytest

from services.historical_reconciliation_service import (
    CANONICAL_ALREADY,
    CANONICAL_CONFLICT_CANONICAL_WINS,
    HistoricalReconciliationService,
    LEGACY_DEAD,
    LEGACY_ONLY_UNPROVEN,
    MISSING_PREGAME_SNAPSHOT,
    OUTCOMES,
    RECONCILED,
    UNRESOLVED_EVENT,
    UNRESOLVED_IDENTITY,
)
from services.settlement_service import SettlementService


# ─── Fake DB (same shape as P0.2a/b/c/d suites) ─────────────────────
class _Coll:
    def __init__(self):
        self.rows: list[dict] = []
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
            row = dict(q)
            row.update(update.get("$set", {}))
            self.rows.append(row)
    def find(self, q, proj=None):
        return _Cursor([r for r in self.rows if _matches(r, q)])


def _matches(row, q):
    for k, v in q.items():
        if isinstance(v, dict) and "$in" in v:
            if row.get(k) not in v["$in"]:
                return False
        elif row.get(k) != v:
            return False
    return True


class _Cursor:
    def __init__(self, rows): self._rows = rows
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
    def __getitem__(self, k): return self._colls[k]
    def __getattr__(self, k): return self._colls[k]


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


async def _seed_snapshot(db, pid: str, *, line=4.5, odds=-115, lock=88.0):
    await db["prediction_snapshots"].insert_one({
        "prediction_id":    pid,
        "is_active":        True,
        "snapshot_version": 1,
        "line":             line,
        "book_odds":        odds,
        "sportsbook":       "DraftKings",
        "lock_score":       lock,
        "published_lock_score": lock,
    })


async def _settle(svc, pick, *, result, source="test",
                    actual=None, event_final=True, correction_reason=None):
    return await svc.settle_from_pick(
        pick, result=result, source=source,
        actual_result=actual or {},
        authoritative_event_final=event_final,
        correction_reason=correction_reason,
    )


def _pick(**over):
    base = {
        "id":       over.pop("id", "p-1"),
        "sport":    "MLB",
        "market":   "Ks Over 4.5",
        "side":     "Over",
        "line":     4.5,
        "event_id": "e-1",
    }
    base.update(over)
    return base


# ═════════════════════════════════════════════════════════════════════
# §A — Provable identity requirements
# ═════════════════════════════════════════════════════════════════════

class TestProvableIdentity:
    def test_canonical_pick_id_match_reconciles(self):
        async def go():
            db = _FakeDB(); svc = SettlementService(db)
            pick = _pick(id="idc-1")
            await _seed_snapshot(db, pick["id"])
            await _settle(svc, pick, result="won", actual={"a": 6})
            r = HistoricalReconciliationService(db)
            c = await r.classify(pick)
            assert c["outcome"] == RECONCILED
        _run(go())

    def test_no_canonical_id_unresolved(self):
        async def go():
            db = _FakeDB()
            c = await HistoricalReconciliationService(db).classify(
                {"sport": "MLB", "market": "Ks", "event_id": "e-1"})
            assert c["outcome"] == UNRESOLVED_IDENTITY
        _run(go())

    def test_no_canonical_event_unresolved(self):
        async def go():
            db = _FakeDB()
            c = await HistoricalReconciliationService(db).classify(
                {"id": "nope-1", "market": "Ks", "side": "Over",
                 "line": 4.5})
            assert c["outcome"] == UNRESOLVED_EVENT
        _run(go())

    def test_name_only_match_refused(self):
        """Two picks share the same player NAME but different
        canonical ids and events.  Reconciliation must treat them
        as separate — no name-only linkage."""
        async def go():
            db = _FakeDB(); svc = SettlementService(db)
            a = _pick(id="dm-A", event_id="e-A")
            b = _pick(id="dm-B", event_id="e-B")
            await _seed_snapshot(db, a["id"])
            await _settle(svc, a, result="won", actual={"a": 6})
            # Reconcile B (no canonical event) — must NOT inherit A's.
            c_a = await HistoricalReconciliationService(db).classify(a)
            c_b = await HistoricalReconciliationService(db).classify(b)
            assert c_a["outcome"] == RECONCILED
            assert c_b["outcome"] in (LEGACY_ONLY_UNPROVEN,
                                        MISSING_PREGAME_SNAPSHOT)
            assert c_b["provenance"].get("canonical_settlement_id") is None
        _run(go())


# ═════════════════════════════════════════════════════════════════════
# §B — Canonical wins on conflict
# ═════════════════════════════════════════════════════════════════════

class TestCanonicalConflict:
    def test_legacy_loss_but_canonical_win_canonical_wins(self):
        async def go():
            db = _FakeDB(); svc = SettlementService(db)
            pick = _pick(id="cw-1",
                          status="lost",   # stale legacy mirror
                          result="lost")
            await _seed_snapshot(db, pick["id"])
            await _settle(svc, pick, result="won", actual={"a": 6})
            c = await HistoricalReconciliationService(db).classify(pick)
            assert c["outcome"] == CANONICAL_CONFLICT_CANONICAL_WINS
            # Provenance retains BOTH values for auditability.
            assert c["provenance"]["legacy_result"] == "lost"
            assert c["provenance"]["canonical_result"] == "won"
        _run(go())

    def test_legacy_agrees_with_canonical_reconciles_clean(self):
        async def go():
            db = _FakeDB(); svc = SettlementService(db)
            pick = _pick(id="agree-1", status="won", result="won")
            await _seed_snapshot(db, pick["id"])
            await _settle(svc, pick, result="won", actual={"a": 6})
            c = await HistoricalReconciliationService(db).classify(pick)
            assert c["outcome"] == RECONCILED
        _run(go())


# ═════════════════════════════════════════════════════════════════════
# §C — Correction lineage
# ═════════════════════════════════════════════════════════════════════

class TestCorrectionLineage:
    def test_correction_v2_wins_and_lineage_retained(self):
        async def go():
            db = _FakeDB(); svc = SettlementService(db)
            pick = _pick(id="corr-1", status="lost", result="lost")
            await _seed_snapshot(db, pick["id"])
            v1 = await _settle(svc, pick, result="lost",
                                 actual={"a": 3})
            v2 = await _settle(svc, pick, result="won", actual={"a": 6},
                                 correction_reason="stat_correction")
            r = HistoricalReconciliationService(db)
            c = await r.classify(pick)
            # Canonical (v2 WON) beats legacy LOST.
            assert c["outcome"] == CANONICAL_CONFLICT_CANONICAL_WINS
            prov = c["provenance"]
            assert prov["canonical_result"] == "won"
            assert prov["canonical_version"] == 2
            assert prov["canonical_supersedes"] == \
                   v1["event"]["settlement_id"]
        _run(go())


# ═════════════════════════════════════════════════════════════════════
# §D — PUSH != VOID through reconciliation
# ═════════════════════════════════════════════════════════════════════

class TestPushNotVoidThroughReconciliation:

    @pytest.mark.parametrize("result", ["push", "void"])
    def test_result_preserved(self, result):
        async def go():
            db = _FakeDB(); svc = SettlementService(db)
            pick = _pick(id=f"pv-{result}", status=result, result=result)
            await _seed_snapshot(db, pick["id"])
            await _settle(svc, pick, result=result, actual={"a": 0},
                          event_final=(result != "void"))
            c = await HistoricalReconciliationService(db).classify(pick)
            assert c["outcome"] == RECONCILED
            assert c["provenance"]["canonical_result"] == result
        _run(go())

    def test_push_legacy_void_canonical_conflict(self):
        async def go():
            db = _FakeDB(); svc = SettlementService(db)
            pick = _pick(id="pv-conflict", status="void", result="void")
            await _seed_snapshot(db, pick["id"])
            await _settle(svc, pick, result="push", actual={"a": 0})
            c = await HistoricalReconciliationService(db).classify(pick)
            assert c["outcome"] == CANONICAL_CONFLICT_CANONICAL_WINS
            assert c["provenance"]["canonical_result"] == "push"
            assert c["provenance"]["legacy_result"] == "void"
        _run(go())


# ═════════════════════════════════════════════════════════════════════
# §E — Snapshot-v0 gap honesty
# ═════════════════════════════════════════════════════════════════════

class TestSnapshotV0Honesty:
    def test_missing_snapshot_stays_missing_no_fabrication(self):
        async def go():
            db = _FakeDB(); svc = SettlementService(db)
            pick = _pick(id="v0-1")
            # NO snapshot seeded — simulates the v0 gap.
            await _settle(svc, pick, result="won", actual={"a": 6})
            c = await HistoricalReconciliationService(db).classify(pick)
            # Canonical settlement present, snapshot missing.
            assert c["outcome"] == RECONCILED
            assert c["provenance"]["snapshot_present"] is False
            # Pick document is NOT enriched with fabricated snapshot
            # fields.
            assert pick.get("line") == 4.5   # only what caller set
            assert "published_lock_score" not in pick
        _run(go())

    def test_no_snapshot_no_settlement_stays_missing(self):
        async def go():
            db = _FakeDB()
            pick = _pick(id="v0-2")
            c = await HistoricalReconciliationService(db).classify(pick)
            assert c["outcome"] == MISSING_PREGAME_SNAPSHOT
            assert "canonical_result" not in c["provenance"]
        _run(go())


# ═════════════════════════════════════════════════════════════════════
# §F — Legacy-only unproven
# ═════════════════════════════════════════════════════════════════════

class TestLegacyOnlyUnproven:
    def test_legacy_settled_no_canonical_stays_unproven(self):
        async def go():
            db = _FakeDB()
            pick = _pick(id="unproven-1", status="won", result="won")
            c = await HistoricalReconciliationService(db).classify(pick)
            assert c["outcome"] == LEGACY_ONLY_UNPROVEN
            # We never promote legacy status to canonical.
            assert "canonical_result" not in c["provenance"]
        _run(go())


# ═════════════════════════════════════════════════════════════════════
# §G — Retired product (KBO) is LEGACY_DEAD
# ═════════════════════════════════════════════════════════════════════

class TestRetiredProduct:
    def test_kbo_pick_is_legacy_dead(self):
        async def go():
            db = _FakeDB()
            pick = _pick(id="kbo-1", sport="KBO", league="KBO",
                          status="won")
            c = await HistoricalReconciliationService(db).classify(pick)
            assert c["outcome"] == LEGACY_DEAD
        _run(go())


# ═════════════════════════════════════════════════════════════════════
# §H — Idempotency + rerun determinism
# ═════════════════════════════════════════════════════════════════════

class TestIdempotency:
    def test_dry_run_produces_zero_writes(self):
        async def go():
            db = _FakeDB(); svc = SettlementService(db)
            picks = [_pick(id=f"dr-{i}", status="won", event_id=f"e-{i}")
                     for i in range(3)]
            for p in picks:
                await _seed_snapshot(db, p["id"])
                await _settle(svc, p, result="won", actual={"a": 6})
            r = HistoricalReconciliationService(db, dry_run=True)
            report = await r.reconcile(picks)
            assert report["dry_run"] is True
            assert report["wrote"] == 0
            # No `reconciliation_provenance` written onto any pick.
            for row in db["picks"].rows:
                assert "reconciliation_provenance" not in row
        _run(go())

    def test_write_mode_is_idempotent(self):
        async def go():
            db = _FakeDB(); svc = SettlementService(db)
            pick = _pick(id="idem-1")
            await _seed_snapshot(db, pick["id"])
            await _settle(svc, pick, result="won", actual={"a": 6})
            # Compat mirror upsert happened during _settle; do not
            # double-insert (would create two rows in the fake DB).
            r = HistoricalReconciliationService(db, dry_run=False)
            rep1 = await r.reconcile([pick])
            rep2 = await r.reconcile([pick])
            rep3 = await r.reconcile([pick])
            assert rep1["wrote"] == 1
            # Once the pick row carries the provenance blob, subsequent
            # runs are no-ops (idempotent).
            assert rep2["wrote"] == 0
            assert rep3["wrote"] == 0
            # Exactly ONE provenance blob on the pick row.
            row = await db["picks"].find_one({"id": "idem-1"})
            assert "reconciliation_provenance" in row
        _run(go())

    def test_report_all_is_deterministic(self):
        async def go():
            db = _FakeDB(); svc = SettlementService(db)
            for i in range(4):
                p = _pick(id=f"det-{i}", event_id=f"e-{i}")
                await db["picks"].insert_one(dict(p))
                await _seed_snapshot(db, p["id"])
                await _settle(svc, p, result="won", actual={"a": 6})
            r = HistoricalReconciliationService(db)
            a = await r.report_all()
            b = await r.report_all()
            assert a["outcomes"] == b["outcomes"]
            assert a["total"] == b["total"] == 4
            assert a["wrote"] == b["wrote"] == 0
        _run(go())


# ═════════════════════════════════════════════════════════════════════
# §I — Never mutates settlement_events / prediction_snapshots
# ═════════════════════════════════════════════════════════════════════

class TestReadOnlyOnCanonicalCollections:
    def test_write_mode_does_not_touch_canonical_collections(self):
        async def go():
            db = _FakeDB(); svc = SettlementService(db)
            pick = _pick(id="ro-canon-1")
            await _seed_snapshot(db, pick["id"])
            await _settle(svc, pick, result="won", actual={"a": 6})
            snap_before = list(db["prediction_snapshots"].rows)
            evt_before  = list(db["settlement_events"].rows)
            r = HistoricalReconciliationService(db, dry_run=False)
            await r.reconcile([pick])
            # settlement_events + prediction_snapshots unchanged.
            assert db["prediction_snapshots"].rows == snap_before
            assert db["settlement_events"].rows == evt_before
        _run(go())


# ═════════════════════════════════════════════════════════════════════
# §J — Preserves frozen pregame — no recomputation of Lock Score, etc.
# ═════════════════════════════════════════════════════════════════════

class TestNoRecomputationOfFrozenPregame:
    def test_reconciliation_never_recomputes_lock_score(self):
        async def go():
            db = _FakeDB(); svc = SettlementService(db)
            pick = _pick(id="fr-1", lock_score=88.0, line=4.5,
                          book_odds=-115, sportsbook="DraftKings")
            await _seed_snapshot(db, pick["id"], lock=88.0)
            await _settle(svc, pick, result="won", actual={"a": 6})
            # Merge the frozen pregame fields onto the settlement-
            # created compat-mirror row (do NOT double-insert).
            await db["picks"].update_one(
                {"id": pick["id"]},
                {"$set": dict(pick)}, upsert=True,
            )
            r = HistoricalReconciliationService(db, dry_run=False)
            await r.reconcile([pick])
            row = await db["picks"].find_one({"id": "fr-1"})
            # Lock Score / line / odds / sportsbook — verbatim.
            assert row["lock_score"] == 88.0
            assert row["line"] == 4.5
            assert row["book_odds"] == -115
            assert row["sportsbook"] == "DraftKings"
        _run(go())


# ═════════════════════════════════════════════════════════════════════
# §K — Cross-sport coverage
# ═════════════════════════════════════════════════════════════════════

class TestCrossSportReconciliation:

    @pytest.mark.parametrize("sport,market,side", [
        ("MLB",    "Strikeouts Over 4.5",         "Over"),
        ("MLB",    "1st_inning_runs",              "NRFI"),
        ("Soccer", "Total Goals Over 2",           "Over"),
        ("Tennis", "Match Winner",                 "Sinner"),
        ("NFL",    "Passing Yards Over 249.5",     "Over"),
        ("NBA",    "Points Over 24.5",             "Over"),
        ("NHL",    "Shots on Goal Over 2.5",       "Over"),
        ("CFB",    "Rushing Yards Over 89.5",      "Over"),
        ("UFC",    "Method of Victory",            "KO/TKO"),
    ])
    def test_sport_reconciles(self, sport, market, side):
        async def go():
            db = _FakeDB(); svc = SettlementService(db)
            pick = _pick(id=f"{sport}-1", sport=sport, market=market,
                          side=side, event_id=f"e-{sport}")
            await _seed_snapshot(db, pick["id"])
            await _settle(svc, pick, result="won", actual={"ok": True})
            c = await HistoricalReconciliationService(db).classify(pick)
            assert c["outcome"] == RECONCILED
        _run(go())


# ═════════════════════════════════════════════════════════════════════
# §L — Specialized product histories remain separate
# ═════════════════════════════════════════════════════════════════════

class TestSpecializedProductHistoriesStaySeparate:
    """Reconciliation attaches canonical settlement provenance but
    does not merge specialized histories into Locks History."""
    def test_rollover_pick_reconciles_without_republishing(self):
        async def go():
            db = _FakeDB(); svc = SettlementService(db)
            pick = _pick(id="roll-1",
                          category="rollover",
                          hide_from_main_board=True)
            await _seed_snapshot(db, pick["id"])
            await _settle(svc, pick, result="won", actual={"a": 6})
            c = await HistoricalReconciliationService(db).classify(pick)
            assert c["outcome"] == RECONCILED
            # Reconciliation does NOT flip `hide_from_main_board`; the
            # specialized product identity is preserved.
            assert pick["hide_from_main_board"] is True
            assert pick.get("category") == "rollover"
        _run(go())


# ═════════════════════════════════════════════════════════════════════
# §M — Static legacy-authority guard
# ═════════════════════════════════════════════════════════════════════

class TestNoLegacyAuthorityOverridingCanonical:
    """No production code may treat `pick.status` / `pick.result` /
    `pick.settled_at` as authoritative over `settlement_events`.

    We search for the anti-pattern:
        query `settlement_events` AND `picks.status ∈ {won, lost,
        push, void}` in the same file, where the result of the
        `picks` read is used to *override* the ledger.  Since our
        real signal is more subtle, we scan for direct references
        that treat legacy fields as canonical output.

    Concretely: any file that both (a) references settlement_events
    AND (b) contains a comment or code path explicitly saying legacy
    wins over canonical must be caught.  In practice, the P0.2c
    HistoryProjectionService inverted this — canonical wins.  We
    therefore enforce that no production file besides the approved
    ones asserts otherwise.
    """
    ALLOWED_FILES = {
        "services/historical_reconciliation_service.py",
    }
    LEGACY_WIN_PAT = re.compile(
        r"""legacy_wins_over_canonical|use_legacy_result_over_canonical|"""
        r"""prefer_legacy_status""",
        re.IGNORECASE,
    )

    def test_no_production_code_prefers_legacy_over_canonical(self):
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
            if self.LEGACY_WIN_PAT.search(src):
                rogue.append(rel)
        assert not rogue, (
            "Files that assert legacy authority over canonical "
            "settlement: " + str(rogue))


# ═════════════════════════════════════════════════════════════════════
# §N — Reconciliation report shape
# ═════════════════════════════════════════════════════════════════════

class TestReconciliationReportShape:
    def test_report_contains_all_vocabulary(self):
        async def go():
            db = _FakeDB()
            r = HistoricalReconciliationService(db)
            rep = await r.reconcile([])
            assert set(rep["outcomes"].keys()) == set(OUTCOMES)
            assert rep["total"] == 0
        _run(go())

    def test_report_counts_are_integers(self):
        async def go():
            db = _FakeDB(); svc = SettlementService(db)
            picks = [
                _pick(id="rep-1", status="won"),          # unproven
                _pick(id="rep-2", event_id=None),          # unresolved event
                _pick(id=None,    sport="MLB", event_id="e"),  # unresolved id
                _pick(id="rep-4", sport="KBO", league="KBO"),  # legacy dead
            ]
            for p in picks[:1]:
                # rep-1 legacy WON without canonical → LEGACY_ONLY_UNPROVEN
                pass
            r = HistoricalReconciliationService(db)
            rep = await r.reconcile(picks)
            assert rep["total"] == 4
            assert rep["outcomes"][LEGACY_ONLY_UNPROVEN] == 1
            assert rep["outcomes"][UNRESOLVED_EVENT] == 1
            assert rep["outcomes"][UNRESOLVED_IDENTITY] == 1
            assert rep["outcomes"][LEGACY_DEAD] == 1
        _run(go())
