"""Live Repair — Rollover / Parlay / History focused tests.

Validates the three surgical fixes:
1. Rollover DB-frozen restore no longer references undefined variable
2. Parlay pick_date removed for extended windows; edge is quality/rank
3. History status mapping distinguishes VOID/UNRESOLVED from PENDING;
   settlement admission no longer excludes off_board picks.
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── Fix 1 · Rollover route crash ────────────────────────────────────
def test_rollover_route_source_no_undefined_ref():
    """The DB-frozen restore block must NOT reference
    ``sport_filter_active`` before it is assigned."""
    from routes import picks_routes
    src = open(picks_routes.__file__).read()
    # Find the frozen-restore block
    a = src.index("DB-FROZEN MEMBERSHIP RESTORE")
    block_end = src.index("DB-frozen miss — full recompute path", a)
    block_src = src[a:block_end]
    # The block must derive its own local (_sport_active_here) and
    # not read `sport_filter_active` before it's assigned further down.
    assert "_sport_active_here" in block_src, (
        "DB-frozen block must derive sport-active locally"
    )
    # Make sure the block itself never reads the pre-assignment name.
    assert "not sport_filter_active" not in block_src


# ── Fix 2 · Parlay pick_date / edge ─────────────────────────────────
def test_parlay_route_no_pick_date_gate_for_extended_windows():
    from routes import parlay_routes
    src = open(parlay_routes.__file__).read()
    # base_q must NOT unconditionally include pick_date. The old
    # contract was `"pick_date": _today_str(),` inline in the base_q
    # dict literal — that pattern must be gone.
    assert '"pick_date": _today_str(),\n        "no_bet"' not in src, (
        "base_q still hard-codes today's pick_date, blocking future events"
    )
    # And the conditional today-window gate must be present.
    assert "is_today_window" in src
    assert 'base_q["pick_date"] = _today_str()' in src


def test_parlay_optimizer_edge_is_quality_not_hard_gate_in_standard():
    """Standard admission: min_edge is None (edge is scored, not gated).
    High-risk keeps 1.0 as a sanity floor."""
    from parlay_optimizer import is_eligible_leg
    # Lock 88, edge 0 — under old contract (min_edge=3) this would
    # fail. Under new contract (min_edge=None in standard) it should
    # be admissible as long as the canonical/lock/win_p checks pass.
    pick = {
        "id": "t1", "sport": "MLB", "event": "e1", "market": "ML",
        "lock_score": 88, "edge_percent": 0.0,
        "win_probability": 70, "book_odds": -140,
        "implied_probability": 140.0/(140.0+100.0),
        "publication_state": "PUBLISHED",
        "publication_source": "canonical_pipeline",
        "identity_class": "MAPPED",
        "no_real_book_line": False, "off_board": False,
        "model_probability": 0.70, "odds_source": "the_odds_api",
    }
    ok, reason = is_eligible_leg(pick, {}, high_risk=False)
    # In old code this would have returned False("edge +0.0% < +3%").
    # New code: soft edge → eligible.
    assert ok is True, f"Standard mode should NOT hard-reject Lock 88 / Edge 0 — reason: {reason}"


def test_parlay_standard_lock_floor_is_85_not_88():
    from parlay_optimizer import is_eligible_leg
    pick = {
        "id": "t2", "sport": "MLB", "event": "e2", "market": "ML",
        "lock_score": 86, "edge_percent": 2.0,
        "win_probability": 65, "book_odds": +110,
        "implied_probability": 100.0/(110.0+100.0),
        "publication_state": "PUBLISHED",
        "publication_source": "canonical_pipeline",
        "identity_class": "MAPPED",
        "no_real_book_line": False, "off_board": False,
        "model_probability": 0.65, "odds_source": "the_odds_api",
    }
    ok, reason = is_eligible_leg(pick, {}, high_risk=False)
    assert ok is True, f"Lock 86 should clear the >=85 floor: {reason}"


def test_parlay_reason_no_stale_edge_3_string():
    from routes import parlay_routes
    src = open(parlay_routes.__file__).read()
    assert "need Lock>=88, Edge>=+3%" not in src, (
        "stale empty-parlay reason string still present"
    )
    # New truthful diagnostic must be wired.
    assert '"diagnostic":' in src
    assert "canonical_pool" in src and "optimizer_eligible" in src


# ── Fix 3 · History label + settlement admission ────────────────────
def test_history_labels_distinct_states():
    src = open("/app/frontend/app/history.tsx").read()
    for expected in ('"WON"', '"LOST"', '"PUSH"', '"VOID"',
                     '"UNRESOLVED"', '"PENDING"'):
        assert expected in src, f"missing status label {expected}"


def test_settlement_engine_no_off_board_exclusion():
    """settle_due_picks admission must be canonical publication +
    settlement_block + retry_after — NOT current off_board."""
    from settlement_engine import settle_due_picks  # noqa
    src = open("/app/backend/settlement_engine.py").read()
    fn_start = src.index("async def settle_due_picks")
    # end at next top-level def (async or sync)
    fn_end = len(src)
    for marker in ("\nasync def ", "\ndef "):
        idx = src.find(marker, fn_start + 10)
        if idx != -1 and idx < fn_end:
            fn_end = idx
    body = src[fn_start:fn_end]
    # In the old contract this exclusion sat inside the query dict.
    # In the fix, we replaced it with canonical_publication_filter().
    # Confirm the comment block that gated on off_board is gone AND
    # that canonical publication is now the admission driver.
    assert "canonical_publication_filter" in body
    # The stale off_board gate comment must be gone.
    assert "Board-visibility gate (2026-07-21)" not in body


def test_soccer_settler_no_off_board_exclusion():
    src = open("/app/backend/soccer_espn_settle.py").read()
    # Fn body must no longer excludes off_board picks
    fn_start = src.index("async def settle_soccer_picks_via_espn")
    fn_end = src.index("\ndef ", fn_start + 10) if "\ndef " in src[fn_start:] else len(src)
    body = src[fn_start:fn_end]
    assert '"off_board": {"$ne": True}' not in body, (
        "soccer settler still excludes off_board picks"
    )
    assert "canonical_publication_filter" in body


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
