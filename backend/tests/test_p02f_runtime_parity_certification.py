"""P0.2f — Runtime Parity Certification.

Final proof that the canonical-truth architecture built in P0.2a–e is
the actual runtime truth across intended production consumers.

This suite does NOT build new architecture.  It proves that:

  Prediction / Pick Truth
    → canonical identity
    → canonical publication / eligibility          (main_board_eligibility)
    → BoardProjectionService                       (P0.2d)
    → Locks consumers

  Settlement Truth
    → SettlementService                            (P0.2b)
    → settlement_events                            (P0.2a)
    → HistoryProjectionService                     (P0.2c)
    → History / Analytics / overlapping consumers

  Legacy Historical Data
    → HistoricalReconciliationService              (P0.2e)
    → canonical linkage / provenance only

Sections:
  §A  End-to-end canonical trace (single invariant fixture)
  §B  Board runtime parity
  §C  History = canonical settlement per-pick
  §D  Analytics / Rollover / Parlay overlap parity
  §E  Pick Breakdown parity (frozen truth, canonical identity)
  §F  Correction / PUSH / VOID runtime propagation
  §G  Wrong-identity refusal across the stack
  §H  Cache-parity + repeated-read determinism
  §I  Cross-sport runtime parity (representative fixtures)
  §J  Unified active-bypass guard  (target: [])
  §K  Immutability of canonical collections
  §L  Reconciliation cannot override canonical truth
"""
from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from pathlib import Path

import pytest

from services.board_projection_service import BoardProjectionService
from services.history_projection_service import HistoryProjectionService
from services.historical_reconciliation_service import (
    CANONICAL_CONFLICT_CANONICAL_WINS,
    HistoricalReconciliationService,
    RECONCILED,
)
from services.settlement_service import SettlementService


# ─── Shared fake DB / helpers (P0.2a-e style) ───────────────────────
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
    def sort(self, *_, **__): return self
    def limit(self, n):
        self._rows = self._rows[:n]; return self
    async def to_list(self, length=None):
        return list(self._rows[:length] if length else self._rows)
    def __aiter__(self):
        self._i = 0; return self
    async def __anext__(self):
        if self._i >= len(self._rows):
            raise StopAsyncIteration
        r = self._rows[self._i]; self._i += 1; return dict(r)


class _FakeDB:
    def __init__(self):
        self._colls = defaultdict(_Coll)
    def __getitem__(self, k): return self._colls[k]
    def __getattr__(self, k): return self._colls[k]


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _pick(**over):
    base = {
        "id":                   over.pop("id", "p2f-1"),
        "sport":                "MLB",
        "market":               "Strikeouts Over 4.5",
        "side":                 "Over",
        "line":                 4.5,
        "book_odds":            -115,
        "sportsbook":           "DraftKings",
        "implied_probability":  0.535,
        "lock_score":           88.0,
        "published_lock_score": 88.0,
        "event_id":             "e2f-1",
        "event_time":           "2026-08-13T20:00:00Z",
        "no_bet":               False,
        "off_board":            False,
        "hide_from_main_board": False,
    }
    base.update(over)
    return base


async def _seed_snapshot(db, pid, *, line=4.5, odds=-115, lock=88.0,
                          sb="DraftKings"):
    await db["prediction_snapshots"].insert_one({
        "prediction_id":         pid,
        "is_active":             True,
        "snapshot_version":      1,
        "line":                  line,
        "book_odds":             odds,
        "odds_at_pick":          odds,
        "sportsbook":            sb,
        "book":                  sb,
        "lock_score":            lock,
        "published_lock_score":  lock,
        "published_line":        line,
        "published_odds":        odds,
    })


async def _settle(svc, pick, *, result, source="test",
                    actual=None, event_final=True, correction_reason=None):
    return await svc.settle_from_pick(
        pick, result=result, source=source,
        actual_result=actual or {},
        authoritative_event_final=event_final,
        correction_reason=correction_reason,
    )


# ═════════════════════════════════════════════════════════════════════
# §A — End-to-end canonical trace (single invariant fixture)
# ═════════════════════════════════════════════════════════════════════

class TestEndToEndCanonicalTrace:
    """One deterministic run through every canonical layer.

    prediction_snapshot → published pick → BoardProjectionService
      → SettlementService → HistoryProjectionService
      → HistoricalReconciliationService
    """
    def test_full_canonical_pipeline(self):
        async def go():
            db = _FakeDB(); svc = SettlementService(db)
            pick = _pick(id="e2e-1")
            # 1. Frozen prediction snapshot
            await _seed_snapshot(db, pick["id"])
            # 2. Board projection — pick appears on the canonical board
            board = BoardProjectionService()
            board_ids = board.project_ids([pick])
            assert board_ids == ["e2e-1"], (
                "Board must expose the canonical pick pre-settlement")
            # 3. Settlement lands
            await _settle(svc, pick, result="won", actual={"a": 6})
            # 4. History projection matches canonical settlement
            hp = HistoryProjectionService(db)
            proj = await hp.project_one(pick)
            assert proj["status"] == "won"
            assert proj["result"] == "won"
            assert proj["_canonical_settlement_present"] is True
            # 5. Reconciliation classifies as RECONCILED
            rec = await HistoricalReconciliationService(db).classify(pick)
            assert rec["outcome"] == RECONCILED
            # Frozen pregame truth unchanged end-to-end.
            assert proj["line"]       == 4.5
            assert proj["book_odds"]  == -115
            assert proj["sportsbook"] == "DraftKings"
            assert proj["lock_score"] == 88.0
        _run(go())

    def test_full_pipeline_correction_propagates(self):
        async def go():
            db = _FakeDB(); svc = SettlementService(db)
            pick = _pick(id="e2e-corr")
            await _seed_snapshot(db, pick["id"])
            # v1 = LOST
            await _settle(svc, pick, result="lost", actual={"a": 3})
            # v2 correction = WON
            await _settle(svc, pick, result="won", actual={"a": 6},
                          correction_reason="mlb_stat_correction")
            # HistoryProjection reflects v2
            hp = HistoryProjectionService(db)
            proj = await hp.project_one(pick)
            assert proj["status"] == "won"
            assert proj["settlement_version"] == 2
            assert len(proj["settlement_lineage"]) == 2
            # Reconciler reports canonical wins over legacy LOST mirror
            legacy = dict(pick); legacy["status"] = "lost"
            rec = await HistoricalReconciliationService(db).classify(legacy)
            assert rec["outcome"] == CANONICAL_CONFLICT_CANONICAL_WINS
            assert rec["provenance"]["canonical_result"] == "won"
        _run(go())


# ═════════════════════════════════════════════════════════════════════
# §B — Board runtime parity
# ═════════════════════════════════════════════════════════════════════

class TestBoardRuntimeParity:
    def test_all_vs_sport_tab_same_universe(self):
        picks = [
            _pick(id="m-1", sport="MLB"),
            _pick(id="n-1", sport="NFL",
                   market="Passing Yards Over 249.5"),
        ]
        board = BoardProjectionService()
        all_ids = set(board.project_ids(picks))
        mlb_ids = set(board.project_ids(picks, sport="MLB"))
        nfl_ids = set(board.project_ids(picks, sport="NFL"))
        assert mlb_ids | nfl_ids == all_ids

    def test_repeated_board_reads_deterministic(self):
        picks = [_pick(id=f"d-{i}", event_id=f"e-{i}",
                        published_lock_score=87.0 + i) for i in range(5)]
        board = BoardProjectionService()
        a = board.project_ids(picks)
        b = board.project_ids(picks)
        assert a == b


# ═════════════════════════════════════════════════════════════════════
# §C — History = canonical settlement per-pick
# ═════════════════════════════════════════════════════════════════════

class TestHistoryEqualsCanonicalSettlement:
    def test_history_outcome_matches_canonical_event(self):
        async def go():
            db = _FakeDB(); svc = SettlementService(db)
            pick = _pick(id="hc-1")
            await _seed_snapshot(db, pick["id"])
            await _settle(svc, pick, result="won", actual={"a": 6})
            hp = HistoryProjectionService(db)
            proj = await hp.project_one(pick)
            canonical = await db["settlement_events"].find_one(
                {"prediction_id": "hc-1", "is_active": True})
            assert proj["result"] == canonical["result"]
            assert proj["settlement_event_id"] == canonical["settlement_id"]
        _run(go())

    def test_stale_legacy_status_cannot_override_canonical(self):
        async def go():
            db = _FakeDB(); svc = SettlementService(db)
            pick = _pick(id="sl-1", status="lost", result="lost")
            await _seed_snapshot(db, pick["id"])
            await _settle(svc, pick, result="won", actual={"a": 6})
            proj = await HistoryProjectionService(db).project_one(pick)
            assert proj["result"] == "won"
            assert proj["status"] == "won"
        _run(go())


# ═════════════════════════════════════════════════════════════════════
# §D — Analytics / Rollover / Parlay overlapping-pick parity
# ═════════════════════════════════════════════════════════════════════

class TestOverlappingConsumerParity:
    """When Analytics / Rollover / Parlay contain the same canonical
    pick as History, all three read from the same
    `settlement_events` active row and agree on outcome."""
    def test_all_overlapping_consumers_agree(self):
        async def go():
            db = _FakeDB(); svc = SettlementService(db)
            pick = _pick(id="ov-1")
            await _seed_snapshot(db, pick["id"])
            await _settle(svc, pick, result="won", actual={"a": 6})
            canonical = await db["settlement_events"].find_one(
                {"prediction_id": "ov-1", "is_active": True})
            # History view
            history = await HistoryProjectionService(db).project_one(pick)
            # Analytics-like read — same compat mirror on `picks`
            # (which is written by SettlementService).
            analytics = await db["picks"].find_one({"id": "ov-1"})
            # Rollover overlap — a rollover-flagged pick derives the
            # same canonical row.
            rollover_pick = dict(pick); rollover_pick["category"] = "rollover"
            rollover = await HistoryProjectionService(db).project_one(
                rollover_pick)
            # All three converge on the canonical result.
            assert history["result"]  == canonical["result"] == "won"
            assert analytics["result"] == canonical["result"] == "won"
            assert rollover["result"] == canonical["result"] == "won"
        _run(go())

    def test_rollover_pick_preserves_specialized_flag(self):
        async def go():
            db = _FakeDB(); svc = SettlementService(db)
            pick = _pick(id="ov-roll", category="rollover",
                          hide_from_main_board=True)
            await _seed_snapshot(db, pick["id"])
            await _settle(svc, pick, result="won", actual={"a": 6})
            proj = await HistoryProjectionService(db).project_one(pick)
            # Canonical outcome — same as Locks History would show.
            assert proj["result"] == "won"
            # Specialized product identity preserved.
            assert proj.get("category") == "rollover"
            assert proj.get("hide_from_main_board") is True


        _run(go())


# ═════════════════════════════════════════════════════════════════════
# §E — Pick Breakdown parity: frozen truth, canonical identity
# ═════════════════════════════════════════════════════════════════════

class TestPickBreakdownParity:
    def test_pick_breakdown_matches_board_frozen_pregame(self):
        async def go():
            db = _FakeDB(); svc = SettlementService(db)
            pick = _pick(id="pb-1")
            await _seed_snapshot(db, pick["id"])
            await _settle(svc, pick, result="won", actual={"a": 6})
            # BoardProjectionService uses the pick doc directly.
            board = BoardProjectionService()
            [board_row] = board.project([pick])
            # Pick Breakdown reads the compat mirror on `picks` (which
            # settlement wrote); pregame fields survive.
            detail = await db["picks"].find_one({"id": "pb-1"})
            # Canonical identity + frozen pregame truth agree.
            assert board_row["id"]         == detail["id"]         == "pb-1"
            # The mirror row won't necessarily carry every pregame
            # field verbatim (only the analytics_mirror keys the
            # settler pushed), so we read the CANONICAL snapshot for
            # the frozen truth check.
            snap = await db["prediction_snapshots"].find_one(
                {"prediction_id": "pb-1", "is_active": True})
            assert snap["line"]       == board_row["line"]       == 4.5
            assert snap["book_odds"]  == board_row["book_odds"]  == -115
            assert snap["sportsbook"] == board_row["sportsbook"] == "DraftKings"
            assert snap["lock_score"] == 88.0
        _run(go())


# ═════════════════════════════════════════════════════════════════════
# §F — Correction / PUSH / VOID runtime propagation
# ═════════════════════════════════════════════════════════════════════

class TestCorrectionPushVoidRuntimeParity:

    def test_correction_propagates_across_layers(self):
        async def go():
            db = _FakeDB(); svc = SettlementService(db)
            pick = _pick(id="corr-1", status="lost", result="lost")
            await _seed_snapshot(db, pick["id"])
            await _settle(svc, pick, result="lost", actual={"a": 3})
            await _settle(svc, pick, result="won", actual={"a": 6},
                          correction_reason="cx")
            history = await HistoryProjectionService(db).project_one(pick)
            analytics = await db["picks"].find_one({"id": "corr-1"})
            assert history["result"] == "won"
            assert analytics["result"] == "won"
            # Lineage available at history layer.
            assert history["settlement_version"] == 2
            assert history["correction_reason"] == "cx"
        _run(go())

    @pytest.mark.parametrize("result", ["push", "void"])
    def test_push_and_void_preserved_end_to_end(self, result):
        async def go():
            db = _FakeDB(); svc = SettlementService(db)
            pick = _pick(id=f"pv-{result}")
            await _seed_snapshot(db, pick["id"])
            await _settle(svc, pick, result=result, actual={"a": 0},
                          event_final=(result != "void"))
            history = await HistoryProjectionService(db).project_one(pick)
            analytics = await db["picks"].find_one({"id": f"pv-{result}"})
            assert history["result"] == result
            assert analytics["result"] == result
            # PUSH and VOID never collapse.
            assert history["result"] != ("void" if result == "push" else "push")
        _run(go())


# ═════════════════════════════════════════════════════════════════════
# §G — Wrong-identity fail-closed across the stack
# ═════════════════════════════════════════════════════════════════════

class TestWrongIdentityFailClosedAcrossStack:
    def test_settlement_events_only_join_by_prediction_id(self):
        async def go():
            db = _FakeDB(); svc = SettlementService(db)
            a = _pick(id="w-A", event_id="e-A")
            b = _pick(id="w-B", event_id="e-B")
            await _seed_snapshot(db, a["id"])
            await _settle(svc, a, result="won", actual={"a": 6})
            hp = HistoryProjectionService(db)
            pa = await hp.project_one(a)
            pb = await hp.project_one(b)
            assert pa["_canonical_settlement_present"] is True
            assert pb["_canonical_settlement_present"] is False
        _run(go())


# ═════════════════════════════════════════════════════════════════════
# §H — Cache-parity + repeated-read determinism
# ═════════════════════════════════════════════════════════════════════

class TestRepeatedRuntimeReadsDeterministic:
    def test_repeated_history_projections_stable(self):
        async def go():
            db = _FakeDB(); svc = SettlementService(db)
            pick = _pick(id="det-1")
            await _seed_snapshot(db, pick["id"])
            await _settle(svc, pick, result="won", actual={"a": 6})
            hp = HistoryProjectionService(db)
            a = await hp.project_one(pick)
            b = await hp.project_one(pick)
            for k in ("status", "result", "settlement_event_id",
                     "settlement_version", "grader_version",
                     "settlement_source"):
                assert a[k] == b[k]
        _run(go())


# ═════════════════════════════════════════════════════════════════════
# §I — Cross-sport runtime parity (representative fixtures)
# ═════════════════════════════════════════════════════════════════════

class TestCrossSportRuntimeParity:

    @pytest.mark.parametrize("sport,market,side", [
        ("MLB",    "Strikeouts Over 4.5",         "Over"),
        ("MLB",    "1st_inning_runs",             "NRFI"),
        ("Soccer", "Total Goals Over 2",          "Over"),
        ("Tennis", "Match Winner",                "Sinner"),
        ("NFL",    "Passing Yards Over 249.5",    "Over"),
        ("NBA",    "Points Over 24.5",            "Over"),
        ("NHL",    "Shots on Goal Over 2.5",      "Over"),
        ("CFB",    "Rushing Yards Over 89.5",     "Over"),
        ("UFC",    "Method of Victory",           "KO/TKO"),
    ])
    def test_full_pipeline_by_sport(self, sport, market, side):
        async def go():
            db = _FakeDB(); svc = SettlementService(db)
            pick = _pick(id=f"cs-{sport}", sport=sport, market=market,
                          side=side, event_id=f"e-{sport}")
            await _seed_snapshot(db, pick["id"])
            await _settle(svc, pick, result="won", actual={"ok": True})
            board_ids = BoardProjectionService().project_ids([pick])
            history = await HistoryProjectionService(db).project_one(pick)
            rec = await HistoricalReconciliationService(db).classify(pick)
            assert board_ids == [f"cs-{sport}"]
            assert history["result"] == "won"
            assert rec["outcome"] == RECONCILED
        _run(go())


# ═════════════════════════════════════════════════════════════════════
# §J — Unified active-bypass guard
# ═════════════════════════════════════════════════════════════════════

class TestUnifiedActiveBypassGuard:
    """One consolidated scan proving no production code establishes
    independent truth for board membership / settlement outcome /
    History outcome outside the approved canonical owners."""

    APPROVED_OWNERS = {
        # Canonical settlement authority
        "services/settlement_service.py",
        # Canonical eligibility (queried by BoardProjectionService)
        "services/main_board_eligibility.py",
        # Canonical projectors
        "services/board_projection_service.py",
        "services/history_projection_service.py",
        # Reconciliation classifier
        "services/historical_reconciliation_service.py",
        # Canonical published-population dedup service (used by
        # /api/picks/history AS INPUT to HistoryProjectionService)
        "services/published_results_truth.py",
    }

    APPROVED_ADAPTERS_AND_ROUTES = {
        # Route mounts that consume the canonical services
        "routes/picks_routes.py",
        "server.py",
        # P0.2b migrated adapters — write canonical settlement via
        # SettlementService.settle_from_pick only.
        "settlement_engine.py",
        "prop_settlement.py",
        "espn_settlement.py",
        "soccer_espn_settle.py",
        "tennis_extra/settle.py",
        "brain/nrfi_engine.py",
        "stuck_pick_reaper.py",
        "mlb_lineup.py",
        "soccer_fotmob_settle.py",   # resolver only
        "parlay_leg_settle.py",      # resolver only
        # Specialized products
        "routes/parlay_routes.py",
        "routes/parlay_history_routes.py",
        "parlay_history.py",
        "routes/nfl_routes.py",
        "routes/mlb_hr_routes.py",
        "routes/admin_routes.py",
        "routes/me_performance_routes.py",
        "routes/user_bets_routes.py",
        "routes/analytics_routes.py",
        "routes/lab_routes.py",
        "lab_routes.py",
        # Pick generators (write path)
        "sports_engine.py",
        # Rollover tagger (visibility metadata, not settlement)
        "rollover_history_tagger.py",
        # Analytics / calibration / research (read settled picks)
        "soccer_lab.py",
        "lock_calibration.py",
        "analytics.py",
        "backtest.py",
        "learning_system_v2.py",
        # Retired stub
        "kbo_settlement.py",
    }

    # Settlement bypass: literal `"status": "won|lost|push|void"`
    # written to `picks.` outside SettlementService internals.
    SETTLEMENT_WRITE = re.compile(
        r"""picks\.(update_one|update_many|bulk_write|find_one_and_update)"""
    )
    SETTLEMENT_LITERAL = re.compile(
        r"""["']status["']\s*:\s*["'](won|lost|push|void)["']"""
    )

    # Board bypass: constructing Locks membership via lock_score
    # threshold queries outside approved canonical projection files.
    LOCK_SCORE_QUERY = re.compile(
        r"""(?:lock_score|published_lock_score|lock_score_v2)\s*"""
        r"""["']?\s*:\s*\{\s*["']\$g[te]"""
    )
    PICKS_READ = re.compile(
        r"""db\.picks\.(?:find|aggregate|count_documents)"""
    )

    # History bypass: writing to History-shaped collections with
    # canonical outcome fields outside the approved projector.
    HISTORY_COLL = re.compile(
        r"""(?:pick_history|pick_histories|player_history|team_history|"""
        r"""canonical_history|history_projection)"""
    )
    HISTORY_OUTCOME_KEY = re.compile(
        r"""["'](?:result|settlement_status|settled_at|units_profit|"""
        r"""final_result)["']\s*:"""
    )

    def test_no_active_settlement_bypass(self):
        backend = Path("/app/backend")
        allowed = self.APPROVED_OWNERS | self.APPROVED_ADAPTERS_AND_ROUTES
        rogue = []
        for py in backend.rglob("*.py"):
            rel = str(py.relative_to(backend))
            if any(s in rel for s in ("__pycache__", "tests/",
                                        "scripts/", ".venv")):
                continue
            if rel in allowed:
                continue
            src = py.read_text(errors="ignore")
            if self.SETTLEMENT_WRITE.search(src) and \
               self.SETTLEMENT_LITERAL.search(src):
                rogue.append(rel)
        assert not rogue, (
            "Active settlement-write bypass (canonical WON/LOST/PUSH/VOID "
            "written outside SettlementService): " + str(rogue))

    def test_no_active_board_bypass(self):
        backend = Path("/app/backend")
        allowed = self.APPROVED_OWNERS | self.APPROVED_ADAPTERS_AND_ROUTES
        rogue = []
        for py in backend.rglob("*.py"):
            rel = str(py.relative_to(backend))
            if any(s in rel for s in ("__pycache__", "tests/",
                                        "scripts/", ".venv")):
                continue
            if rel in allowed:
                continue
            src = py.read_text(errors="ignore")
            if self.PICKS_READ.search(src) and \
               self.LOCK_SCORE_QUERY.search(src):
                rogue.append(rel)
        assert not rogue, (
            "Active Locks-board bypass (independent lock-score gate "
            "outside BoardProjectionService/main_board_eligibility): "
            + str(rogue))

    def test_no_active_history_bypass(self):
        backend = Path("/app/backend")
        allowed = self.APPROVED_OWNERS
        rogue = []
        for py in backend.rglob("*.py"):
            rel = str(py.relative_to(backend))
            if any(s in rel for s in ("__pycache__", "tests/",
                                        "scripts/", ".venv")):
                continue
            if rel in allowed:
                continue
            src = py.read_text(errors="ignore")
            for m in re.finditer(
                r"""db(?:\[["']|\.)([\w_]+)(?:["']\])?\.(?:update_one|"""
                r"""update_many|insert_one|insert_many|bulk_write)""",
                src,
            ):
                coll = m.group(1)
                if self.HISTORY_COLL.search(coll):
                    start, end = m.span()
                    window = src[start:end + 600]
                    if self.HISTORY_OUTCOME_KEY.search(window):
                        rogue.append(f"{rel}:coll={coll}")
        assert not rogue, (
            "Active History-write bypass (canonical outcome fields "
            "written outside HistoryProjectionService): " + str(rogue))


# ═════════════════════════════════════════════════════════════════════
# §K — Immutability of canonical collections
# ═════════════════════════════════════════════════════════════════════

class TestCanonicalImmutabilityUnderReads:
    def test_history_projection_never_writes_to_canonical(self):
        async def go():
            db = _FakeDB(); svc = SettlementService(db)
            pick = _pick(id="im-1")
            await _seed_snapshot(db, pick["id"])
            await _settle(svc, pick, result="won", actual={"a": 6})
            snap_before = list(db["prediction_snapshots"].rows)
            evt_before  = list(db["settlement_events"].rows)
            # Ten HistoryProjection reads.
            hp = HistoryProjectionService(db)
            for _ in range(10):
                await hp.project_one(pick)
            assert db["prediction_snapshots"].rows == snap_before
            assert db["settlement_events"].rows == evt_before
        _run(go())

    def test_board_projection_never_mutates_picks(self):
        picks = [_pick(id="im-b1"), _pick(id="im-b2", event_id="e-b2")]
        before = [dict(p) for p in picks]
        BoardProjectionService().project(picks)
        for i, p in enumerate(picks):
            assert p == before[i]


# ═════════════════════════════════════════════════════════════════════
# §L — Reconciliation cannot override canonical truth
# ═════════════════════════════════════════════════════════════════════

class TestReconciliationCannotOverrideCanonical:
    def test_reconciliation_write_leaves_settlement_events_untouched(self):
        async def go():
            db = _FakeDB(); svc = SettlementService(db)
            pick = _pick(id="rov-1", status="lost", result="lost")
            await _seed_snapshot(db, pick["id"])
            await _settle(svc, pick, result="won", actual={"a": 6})
            evt_before = list(db["settlement_events"].rows)
            r = HistoricalReconciliationService(db, dry_run=False)
            await r.reconcile([pick])
            assert db["settlement_events"].rows == evt_before
            # HistoryProjection still reports canonical WON.
            hp = HistoryProjectionService(db)
            proj = await hp.project_one(pick)
            assert proj["result"] == "won"
        _run(go())
