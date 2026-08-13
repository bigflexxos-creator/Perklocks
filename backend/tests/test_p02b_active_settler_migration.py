"""P0.2b — Adapter-level canonical routing tests.

Each active settlement adapter is now an INPUT RESOLVER that must
call `SettlementService.settle_from_pick(...)`.  These tests exercise
each adapter's canonical write path with a deterministic in-memory
DB and prove that:

  1.  LIVE event refuses to settle through the adapter path.
  2.  Idempotency holds through the adapter (5× identical → 1 active).
  3.  Correction (v2) supersedes v1 without destroying v1.
  4.  PUSH stays PUSH through the compat mirror (never VOID).
  5.  Dustin May pin (FINAL, actual > line, side=Over) → WON.
  6.  Wrong-identity fail-closed still blocks tampered writes.

The `_FakeDB` mirrors the P0.2a suite's collection stub so the
service is exercised with no real Mongo dependency.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

import pytest

from services.settlement_service import (
    ALREADY_SETTLED_IDENTICAL,
    CORRECTION_APPLIED,
    NEW_SETTLEMENT,
    REFUSAL_IDENTITY_MISMATCH,
    REFUSAL_LIVE,
    SettlementService,
)


# ─── Minimal in-memory fake DB (mirrors P0.2a suite) ────────────────
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
            row = dict(q)
            row.update(update.get("$set", {}))
            self.rows.append(row)


class _FakeDB:
    def __init__(self):
        self._colls: dict[str, _Coll] = defaultdict(_Coll)
    def __getitem__(self, name):
        return self._colls[name]
    def __getattr__(self, name):
        return self._colls[name]


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _svc():
    return SettlementService(_FakeDB())


# ═════════════════════════════════════════════════════════════════════
# §A — MLB player-prop adapter routing (prop_settlement pattern)
# ═════════════════════════════════════════════════════════════════════

class TestMLBPropAdapter:
    """Simulates `prop_settlement._record` post-migration."""

    _PICK = {
        "id":              "mlb-prop-1",
        "sport":           "MLB",
        "market":          "Pitcher Strikeouts Over 4.5 (Dustin May)",
        "selection":       "Dustin May",
        "side":            "Over",
        "line":            4.5,
        "event":           "SD @ MIL",
        "event_id":        "mlb-may-2026-08-13",
        "book_odds":       -115,
    }

    def test_live_event_refuses(self):
        svc = _svc()
        out = _run(svc.settle_from_pick(
            self._PICK,
            result                    = "won",
            source                    = "prop_settlement",
            actual_result             = {"actual_strikeouts": 6, "line": 4.5},
            authoritative_event_final = False,           # LIVE
            analytics_mirror          = {"units_profit": 0.87},
        ))
        assert out["status"] == REFUSAL_LIVE

    def test_dustin_may_pin_final_over_4_5_actual_6_is_won(self):
        svc = _svc()
        out = _run(svc.settle_from_pick(
            self._PICK,
            result                    = "won",
            source                    = "prop_settlement",
            actual_result             = {"actual_strikeouts": 6, "line": 4.5},
            authoritative_event_final = True,
            analytics_mirror          = {"units_profit": 0.87,
                                          "final_score": {"May Ks": 6}},
        ))
        assert out["status"] == NEW_SETTLEMENT
        assert out["event"]["result"] == "won"
        # Compat mirror carries analytics fields and stays consistent.
        row = _run(svc.db["picks"].find_one({"id": "mlb-prop-1"}))
        assert row["status"] == "won"
        assert row["units_profit"] == 0.87

    def test_idempotency_through_adapter_path(self):
        svc = _svc()
        kwargs = dict(
            result                    = "won",
            source                    = "prop_settlement",
            actual_result             = {"actual_strikeouts": 6, "line": 4.5},
            authoritative_event_final = True,
            analytics_mirror          = {"units_profit": 0.87},
        )
        first = _run(svc.settle_from_pick(self._PICK, **kwargs))
        assert first["status"] == NEW_SETTLEMENT
        for _ in range(4):
            outN = _run(svc.settle_from_pick(self._PICK, **kwargs))
            assert outN["status"] == ALREADY_SETTLED_IDENTICAL
        # Only ONE active settlement row for the pick.
        actives = [r for r in svc.db["settlement_events"].rows
                   if r["prediction_id"] == "mlb-prop-1" and r["is_active"]]
        assert len(actives) == 1

    def test_correction_through_adapter_path(self):
        svc = _svc()
        # v1: adapter incorrectly grades as LOST (say the box-score was
        # not yet final and returned actual=3).
        v1 = _run(svc.settle_from_pick(
            self._PICK, result="lost", source="prop_settlement",
            actual_result={"actual_strikeouts": 3, "line": 4.5},
            authoritative_event_final=True,
            analytics_mirror={"units_profit": -1.0},
        ))
        assert v1["status"] == NEW_SETTLEMENT
        v1_id = v1["event"]["settlement_id"]
        # v2: authoritative MLB Stats API correction — actual was 6.
        v2 = _run(svc.settle_from_pick(
            self._PICK, result="won", source="prop_settlement",
            actual_result={"actual_strikeouts": 6, "line": 4.5},
            authoritative_event_final=True,
            correction_reason="mlb_stat_correction_20260813",
            analytics_mirror={"units_profit": 0.87},
        ))
        assert v2["status"] == CORRECTION_APPLIED
        assert v2["event"]["settlement_version"] == 2
        assert v2["event"]["supersedes_settlement_id"] == v1_id
        # v1 is preserved but no longer active.
        rows = svc.db["settlement_events"].rows
        assert any(r["settlement_id"] == v1_id and not r["is_active"]
                   for r in rows)


# ═════════════════════════════════════════════════════════════════════
# §B — Soccer adapter (goalscorer PUSH-not-VOID compat mirror)
# ═════════════════════════════════════════════════════════════════════

class TestSoccerAdapterPushMirror:
    """Simulates `soccer_espn_settle` post-migration.  A PUSH result
    (e.g. 4.5-goal-line landing on exactly a 4.5-total when the market
    supports fractional pushes — using a synthetic pick to prove the
    mirror behavior) must remain PUSH on the compat mirror."""

    def test_push_through_adapter_stays_push(self):
        svc = _svc()
        soccer_pick = {
            "id":       "sok-push-1",
            "sport":    "Soccer",
            "market":   "Total Goals Over 2",
            "side":     "Over",
            "line":     2,
            "event":    "Real Madrid @ Barcelona",
            "event_id": "el-clasico-2026-08-13",
        }
        out = _run(svc.settle_from_pick(
            soccer_pick,
            result                    = "push",
            source                    = "soccer_espn_batch_v1",
            actual_result             = {"home_goals": 1, "away_goals": 1},
            authoritative_event_final = True,
            analytics_mirror          = {"units_profit": 0.0,
                                          "settled_by": "soccer_espn_batch_v1"},
        ))
        assert out["status"] == NEW_SETTLEMENT
        assert out["event"]["result"] == "push"
        row = _run(svc.db["picks"].find_one({"id": "sok-push-1"}))
        assert row["status"] == "push"
        assert row["status"] != "void"


# ═════════════════════════════════════════════════════════════════════
# §C — Tennis extra adapter (adapter LIVE-refusal + identity)
# ═════════════════════════════════════════════════════════════════════

class TestTennisExtraAdapter:
    def test_in_progress_refuses(self):
        svc = _svc()
        tennis_pick = {
            "id":       "te-1",
            "sport":    "Tennis",
            "market":   "Match Winner",
            "side":     "Sinner",
            "line":     None,
            "event":    "Sinner v Alcaraz",
            "event_id": "te-2026-08-13",
        }
        out = _run(svc.settle_from_pick(
            tennis_pick,
            result                    = "won",
            source                    = "tennis_extra_settler",
            actual_result             = {"winner": "sinner", "loser": "alcaraz"},
            authoritative_event_final = False,          # match still IN_PROGRESS
        ))
        assert out["status"] == REFUSAL_LIVE


# ═════════════════════════════════════════════════════════════════════
# §D — Wrong-identity fail-closed still fires through adapter helper
# ═════════════════════════════════════════════════════════════════════

class TestIdentityFailClosedThroughAdapter:
    def test_tampered_market_refused(self):
        svc = _svc()
        pick = {
            "id":       "wrong-1",
            "sport":    "MLB",
            "market":   "Pitcher Strikeouts Over 4.5",
            "side":     "Over",
            "line":     4.5,
            "event":    "SD @ MIL",
            "event_id": "e-1",
        }
        # Adapter (buggy) resolves the wrong pick — tamper by asking
        # SettlementService.record directly with mismatched market.
        out = _run(svc.record(
            prediction_id="wrong-1", result="won", source="test",
            actual_result={"a": 6}, authoritative_event_final=True,
            canonical_event_id="e-1", market="hits",   # ← WRONG market
            side="Over", line=4.5,
            expected_pick_id="wrong-1", expected_event_id="e-1",
            expected_market="strikeouts",              # expected != got
            expected_side="Over", expected_line=4.5,
        ))
        assert out["status"] == REFUSAL_IDENTITY_MISMATCH
        assert out["field"] == "market"


# ═════════════════════════════════════════════════════════════════════
# §E — NRFI adapter (VOID skips FINAL barrier; won path requires final)
# ═════════════════════════════════════════════════════════════════════

class TestNRFIAdapter:
    def test_nrfi_live_refuses_won(self):
        svc = _svc()
        pick = {
            "id":       "nrfi-live-1",
            "event_id": "mlb_game_pk_9999",
            "market":   "1st_inning_runs",
            "side":     "NRFI",
            "line":     0.5,
        }
        out = _run(svc.settle_from_pick(
            pick, result="won", source="mlb_stats_api_linescore",
            authoritative_event_final=False,   # still top of 1st
            actual_result={"runs_in_1st": 0},
        ))
        assert out["status"] == REFUSAL_LIVE

    def test_nrfi_final_settles(self):
        svc = _svc()
        pick = {
            "id":       "nrfi-final-1",
            "event_id": "mlb_game_pk_1234",
            "market":   "1st_inning_runs",
            "side":     "NRFI",
            "line":     0.5,
        }
        out = _run(svc.settle_from_pick(
            pick, result="won", source="mlb_stats_api_linescore",
            authoritative_event_final=True,
            actual_result={"runs_in_1st": 0},
        ))
        assert out["status"] == NEW_SETTLEMENT
        assert out["event"]["result"] == "won"


# ═════════════════════════════════════════════════════════════════════
# §F — Stuck-pick reaper adapter (VOID skips FINAL barrier)
# ═════════════════════════════════════════════════════════════════════

class TestReaperAdapter:
    def test_reaper_void_is_recorded_as_immutable_row(self):
        svc = _svc()
        stale_pick = {
            "id":         "stale-1",
            "event_id":   "e-stale",
            "market":     "Pitcher Strikeouts Over 5.5",
            "side":       "Over",
            "line":       5.5,
            "sport":      "MLB",
        }
        out = _run(svc.settle_from_pick(
            stale_pick,
            result                    = "void",
            source                    = "stuck_pick_reaper",
            authoritative_event_final = False,
            analytics_mirror          = {
                "void_reason": "auto_void_stuck_pick_reaper",
            },
        ))
        assert out["status"] == NEW_SETTLEMENT
        assert out["event"]["result"] == "void"
        row = _run(svc.db["picks"].find_one({"id": "stale-1"}))
        # Compat mirror shows VOID, and the analytics-mirror void_reason
        # made it through.
        assert row["status"] == "void"
        assert row["void_reason"] == "auto_void_stuck_pick_reaper"
