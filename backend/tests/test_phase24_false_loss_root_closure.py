"""Root Closure 2026-06 — Universal Over/Under invariants + regression
=====================================================================

User-mandated §10 UNIVERSAL CONTRACT TESTS:
    Over 1.5 actual 4 → WIN
    Over 1.5 actual 2 → WIN
    Over 1.5 actual 1 → LOSS
    Under 1.5 actual 1 → WIN
    Under 1.5 actual 2 → LOSS
    Over 2.0 actual 2 → PUSH
    Under 2.0 actual 2 → PUSH
    missing required combo component → UNRESOLVED (never zero)
    canonical correction supersedes wrong historical settlement
    displayed actual cannot mathematically contradict displayed result
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)
load_dotenv(os.path.join(_BACKEND, ".env"))


# ── §10 Universal grader math ────────────────────────────────────────
@pytest.mark.parametrize("actual,line,side,expected", [
    (4,   1.5, "over",  "won"),
    (2,   1.5, "over",  "won"),
    (1,   1.5, "over",  "lost"),
    (1,   1.5, "under", "won"),
    (2,   1.5, "under", "lost"),
    (2,   2.0, "over",  "push"),
    (2,   2.0, "under", "push"),
])
def test_grade_over_under_math(actual, line, side, expected):
    from services.universal_settlement_contract import grade_over_under
    r = grade_over_under(actual=actual, line=line, side=side)
    assert r["result"] == expected, f"{side} line={line} actual={actual} → {r['result']} (want {expected})"


def test_grade_missing_component_yields_unresolved_not_zero():
    """Combo markets where ANY required component is None must return
    unresolved — NEVER treat missing as zero."""
    from services.universal_settlement_contract import grade_derived
    # All three present → sums
    v = grade_derived({"hits": 1, "runs": 1, "rbi": 2})
    assert v == 4
    # One missing → unresolved sentinel (None per contract)
    v2 = grade_derived({"hits": 1, "runs": None, "rbi": 2})
    assert v2 is None, f"missing 'runs' must produce None, got {v2}"


# ── History projection SETTLEMENT_RESULT_ACTUAL_CONTRADICTION invariant
def test_history_projection_flags_actual_contradiction():
    """A pick displaying `Actual 4 · Line 1.5 · Over · LOST` is
    mathematically contradictory.  The projector must flag it and
    suppress the misleading mirror rather than paint the impossible
    pairing."""
    from services.history_projection_service import project_pick
    pick = {
        "id":     "test-hrb-contradiction",
        "market": "Michael Harris II (ATL) Over 1.5 Hits + Runs + RBIs",
        "line":   1.5,
        "status": "lost",
        "final_score": {"Michael Harris Ii Hits+Runs+Rbi": 4, "Line": 1.5},
    }
    proj = project_pick(pick, active_event={"result": "lost", "settled_at": "x", "settlement_version": 2},
                         prior_events=[])
    assert proj.get("_actual_contradiction") is True
    assert proj.get("final_score_suppressed") is True
    assert proj.get("final_score") is None


def test_history_projection_passes_consistent_actual():
    """A consistent pairing (Under 1.5 · Actual 1 · WON) must NOT
    be flagged."""
    from services.history_projection_service import project_pick
    pick = {
        "id":     "test-consistent",
        "market": "James Wood (WSH) Under 1.5 Hits",
        "line":   1.5,
        "status": "won",
        "final_score": {"James Wood Hits": 1, "Line": 1.5},
    }
    proj = project_pick(pick, active_event={"result": "won", "settled_at": "x", "settlement_version": 1},
                         prior_events=[])
    assert proj.get("_actual_contradiction") is not True
    assert proj.get("final_score") is not None


# ── LIVE ACCEPTANCE — the exact user-reported picks ──────────────────
def test_michael_harris_matt_olson_final_score_corrected_live():
    """Runtime evidence: the two user-reported pick.final_score fields
    now carry the authoritative MLB StatsAPI actuals (1 for Harris,
    0 for Olson) — not the stale (4 / 2) from the buggy v1 grader."""
    async def go():
        c = AsyncIOMotorClient(os.environ["MONGO_URL"])
        db = c[os.environ["DB_NAME"]]
        harris = await db.picks.find_one({"id": "4b0a8cd8-a9c5-5721-91e5-0c7e2071b50e"})
        olson  = await db.picks.find_one({"id": "554e2108-b4e0-5d00-9754-15f97f8770cc"})
        assert harris and olson
        # Both were auto-corrected to LOST (canonical settlement truth).
        assert harris["status"] == "lost"
        assert olson["status"]  == "lost"
        # Both now carry the AUTHORITATIVE H+R+RBI mirror.
        def _extract(fs):
            for k, v in (fs or {}).items():
                if k == "Line":
                    continue
                try:
                    return float(v)
                except Exception:
                    continue
            return None
        h_val = _extract(harris.get("final_score"))
        o_val = _extract(olson.get("final_score"))
        # 1 < 1.5 → LOSS matches; 0 < 1.5 → LOSS matches.
        assert h_val == 1.0, f"Harris final_score mirror still stale: {h_val}"
        assert o_val == 0.0, f"Olson final_score mirror still stale: {o_val}"

    asyncio.get_event_loop().run_until_complete(go())


# ── Correction supersedes on the ledger (immutable audit trail) ─────
def test_correction_ledger_supersedes_v1_wrong_result():
    """The v-active settlement event for each corrected pick must be
    v2 with `is_active=True` and `result` matching the truthful grade,
    while the buggy v1 remains on the ledger `is_active=False`
    (append-only — never deleted)."""
    async def go():
        c = AsyncIOMotorClient(os.environ["MONGO_URL"])
        db = c[os.environ["DB_NAME"]]
        for pid in ("4b0a8cd8-a9c5-5721-91e5-0c7e2071b50e",
                    "554e2108-b4e0-5d00-9754-15f97f8770cc"):
            evs = [ev async for ev in db.settlement_events.find({"prediction_id": pid})]
            active = [ev for ev in evs if ev.get("is_active")]
            assert len(active) == 1, f"pid={pid} must have exactly 1 active event"
            assert active[0]["result"] == "lost"
            # Prior v1 preserved for audit.
            prior = [ev for ev in evs if not ev.get("is_active")]
            assert prior, f"pid={pid} must retain the superseded v1 event"

    asyncio.get_event_loop().run_until_complete(go())
