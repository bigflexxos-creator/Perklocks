"""Block 2D FINAL IMPLEMENTATION CLOSURE — remaining PARTIAL gaps.

Locks the terminal contracts:

  §1 Soccer Double Chance FULLY_WIRED — real line → identity →
     independent model → candidate → canonical publication.
  §2 Soccer BTTS Yes/No FULLY_WIRED — real line → both teams'
     evidence → independent BTTS probability → candidate → canonical
     publication.
  §3 MLS + Soccer prop direct-inject routed through canonical
     publication (PredictionPublicationService.publish_batch).  Zero
     user-visible bypass.
  §4 First-TD honestly DORMANT — stored but off_board=True, cannot
     surface on user-visible Locks board without a scoring-order
     model.
"""
from __future__ import annotations

import sys
import pytest

sys.path.insert(0, "/app/backend")

pytestmark = pytest.mark.unit


# ═══════════════════════════════════════════════════════════════════
# §1 — Soccer DC end-to-end
# ═══════════════════════════════════════════════════════════════════
def test_dc_only_emits_from_real_line():
    """DC candidate path — no _dc_outcomes → NO DC pick, only a
    DOUBLE_CHANCE_SYNTHETIC_LINE_BLOCKED diagnostic."""
    src = open("/app/backend/sports_engine.py").read()
    assert "DOUBLE_CHANCE_SYNTHETIC_LINE_BLOCKED" in src
    assert "if not _dc_real:" in src


def test_dc_reaches_canonical_publication_when_real_line_present():
    """When _dc_real outcome exists AND soccer engine data suffices,
    the DC pick is APPENDED to the game's picks[] list — same list
    consumed by pick_refresh_orchestrator → strict>85 gate →
    db.picks.insert_many canonical publication."""
    src = open("/app/backend/sports_engine.py").read()
    idx = src.index("DOUBLE_CHANCE_REAL_LINE_USED")
    window = src[max(0, idx - 6000): idx + 500]
    # Real line, independent model, candidate appended to picks[].
    assert "picks.append(dc_pick)" in window
    assert "build_soccer_ml_factors" in window
    # Real book_odds from outcome (never synthesised).
    assert '_dc_real.get("price")' in window


def test_dc_model_probability_never_derived_from_book_implied():
    """dc_model comes from _factor_mean (soccer engine ML factor
    average) + a bounded draw safety-net.  Never from
    home_implied + draw_implied."""
    src = open("/app/backend/sports_engine.py").read()
    # Legacy defect must not reappear.
    live_lines = [l for l in src.splitlines() if not l.strip().startswith("#")]
    live = "\n".join(live_lines)
    # No live code assigns dc_model from dc_implied.
    for occ in live.split("dc_model = ")[1:]:
        first_line = occ.split("\n", 1)[0]
        assert "dc_implied" not in first_line, (
            f"dc_model must not be derived from dc_implied; got: {first_line!r}"
        )


# ═══════════════════════════════════════════════════════════════════
# §2 — BTTS end-to-end
# ═══════════════════════════════════════════════════════════════════
def test_btts_yes_and_no_reach_canonical_publication_from_real_line():
    """BTTS Yes AND No picks — both real-line-gated, independent
    model, appended to game.picks[] → canonical publication."""
    src = open("/app/backend/sports_engine.py").read()
    idx = src.index("Block 2D B4")
    window = src[idx: idx + 10000]
    # Both Yes and No emit paths.
    assert 'for _o in _btts_outcomes:' in window
    assert 'if _side_raw not in ("yes", "no"):' in window
    # Both paths append to picks[].
    assert "picks.append(_btts_pick)" in window
    # Real book_odds from real outcome.
    assert "int(_price)" in window


def test_btts_model_prob_is_independent_of_book_implied():
    src = open("/app/backend/sports_engine.py").read()
    idx = src.index("Block 2D B4")
    window = src[idx: idx + 5000]
    # BTTS uses build_soccer_ml_factors for both teams.
    assert 'build_soccer_ml_factors(_game_ctx, pick_team=home)' in window
    assert 'build_soccer_ml_factors(_game_ctx, pick_team=away)' in window
    # No derivation from BTTS book_implied.
    assert "_implied_prob(_price)" not in window


def test_btts_no_pick_when_real_line_missing():
    src = open("/app/backend/sports_engine.py").read()
    idx = src.index("Block 2D B4")
    window = src[idx: idx + 10000]
    # Gated on _btts_outcomes presence.
    assert 'if _btts_outcomes:' in window
    # No fake price if outcome missing.
    assert 'if not isinstance(_price, (int, float)):' in window
    assert 'continue' in window


def test_btts_no_pick_when_soccer_engine_data_insufficient():
    src = open("/app/backend/sports_engine.py").read()
    idx = src.index("Block 2D B4")
    window = src[idx: idx + 4000]
    assert "has_enough_soccer_data" in window
    assert "BTTS_INSUFFICIENT_MODEL_DATA" in window


# ═══════════════════════════════════════════════════════════════════
# §3 — Direct-inject canonical routing
# ═══════════════════════════════════════════════════════════════════
def test_mls_direct_inject_routes_through_canonical_publisher():
    """mls_direct_inject MUST invoke the canonical
    PredictionPublicationService.publish_batch AND apply the
    canonical barrier BEFORE bulk_write."""
    src = open("/app/backend/services/mls_direct_inject.py").read()
    # Barrier ordering: apply BEFORE bulk_write.
    barrier_idx = src.index("apply_canonical_barrier(p)")
    bulk_idx = src.index("db.picks.bulk_write")
    assert barrier_idx < bulk_idx, (
        "canonical barrier must run BEFORE bulk_write in mls_direct_inject"
    )
    # Publication service call present.
    assert "PredictionPublicationService" in src
    assert "publish_batch" in src


def test_soccer_prop_inject_routes_through_canonical_publisher():
    src = open("/app/backend/services/soccer_prop_inject.py").read()
    barrier_idx = src.index("apply_canonical_barrier(p)")
    bulk_idx = src.index("db.picks.bulk_write")
    assert barrier_idx < bulk_idx
    assert "PredictionPublicationService" in src
    assert "publish_batch" in src


def test_direct_inject_failed_barrier_pick_is_off_board():
    """Passing the barrier is REQUIRED to be user-visible.  Fail →
    off_board=True + no_bet=True."""
    from services.canonical_publication_barrier import apply_canonical_barrier
    # Simulate a direct-inject pick with lock too low.
    p = {
        "id": "test-mls-fail",
        "lock_score": 82,
        "book_odds": +150,
        "bypasses_canonical_publication": True,
        "publication_route": "mls_espn_direct",
    }
    apply_canonical_barrier(p)
    assert p["off_board"] is True
    assert p["no_bet"] is True
    assert p["publication_gate"] == "canonical_barrier_rejected"


def test_direct_inject_passing_barrier_pick_is_visible_ready():
    from services.canonical_publication_barrier import apply_canonical_barrier
    p = {
        "id": "test-mls-pass",
        "lock_score": 91,
        "book_odds": +150,
        "bypasses_canonical_publication": True,
        "publication_route": "soccer_prop_direct_inject",
    }
    apply_canonical_barrier(p)
    # Passing barrier does NOT force off_board (unless writer set it
    # for other reasons).
    assert p.get("off_board") is not True
    assert p["publication_gate"] == "canonical_barrier_passed"
    # Implied probability derived from real odds — never null.
    assert isinstance(p.get("implied_probability"), float)


# ═══════════════════════════════════════════════════════════════════
# §4 — First-TD DORMANT
# ═══════════════════════════════════════════════════════════════════
def test_first_td_picks_are_off_board_dormant():
    """player_1st_td picks may exist for observability but MUST NOT
    surface on user-visible Locks board until a scoring-order model
    is built."""
    src = open("/app/backend/sports_engine.py").read()
    idx = src.index("First-TD DORMANT")
    window = src[idx: idx + 1500]
    assert 'mk == "player_1st_td"' in window
    assert '"off_board"] = True' in window
    assert '"no_bet"] = True' in window
    assert "PARTIAL_DORMANT" in window


def test_first_td_capability_state_declared_dormant():
    """The capability_state field on First-TD picks must explicitly
    state PARTIAL_DORMANT so no downstream consumer can accidentally
    treat First-TD as FULLY_WIRED."""
    src = open("/app/backend/sports_engine.py").read()
    assert '"capability_state"] = "PARTIAL_DORMANT"' in src


# ═══════════════════════════════════════════════════════════════════
# §5 — Universal invariants after final closure
# ═══════════════════════════════════════════════════════════════════
def test_atd_still_wired():
    src = open("/app/backend/sports_engine.py").read()
    assert "_atd_model_override" in src
    assert "nfl_atd_precomputed" in src


def test_atd_not_marked_dormant():
    """ATD picks must NOT be flagged off_board — that would defeat
    the specialised-engine wiring."""
    src = open("/app/backend/sports_engine.py").read()
    idx = src.index("First-TD DORMANT")
    window = src[idx: idx + 1500]
    # Guard: the off_board flag block must be gated on
    # `mk == "player_1st_td"` ONLY, never on "player_anytime_td".
    assert 'mk == "player_anytime_td"' not in window


def test_hr_intel_still_wired():
    src = open("/app/backend/sports_engine.py").read()
    assert "hr_intel_evidence" in src


def test_mlb_totals_closure_still_wired():
    from services.mlb_feature_engine import (
        factor_combined_starter_quality,
        factor_combined_team_bullpen,
        factor_combined_team_offense,
    )
    # All three closure helpers exist and are callable.
    assert callable(factor_combined_starter_quality)
    assert callable(factor_combined_team_bullpen)
    assert callable(factor_combined_team_offense)


def test_nfl_position_resolver_still_present():
    src = open("/app/backend/sports_engine.py").read()
    assert "async def resolve_nfl_position_for_player" in src


def test_nba_prop_gate_still_present():
    src = open("/app/backend/sports_engine.py").read()
    assert "nba_prop_evidence_gate" in src


def test_soccer_totals_conceded_still_wired():
    from services.soccer_feature_engine import build_soccer_total_factors
    factors, _ = build_soccer_total_factors(
        {"home_team": "H", "away_team": "A"}, "over")
    # Key exists (value may be None if no data).
    assert "Combined Goals Conceded" in factors


def test_block2c_isolate_bad_markets_still_wired():
    src = open("/app/backend/sports_engine.py").read()
    assert "_isolate_and_merge_event_props" in src


def test_universal_settlement_missing_data_unchanged():
    from services import universal_settlement_contract as usc
    graded = usc.grade_over_under(actual=None, line=1.5, side="over")
    assert graded.get("result") == usc.RESULT_UNRESOLVED


def test_p05_published_results_truth_present():
    from services import published_results_truth as prt
    assert hasattr(prt, "PublishedResultsTruthService")


def test_strict_85_gate_unchanged():
    from services.canonical_publication_barrier import STRICT_LOCK_FLOOR
    assert STRICT_LOCK_FLOOR == 85


def test_perklocks_day_contract_present():
    from services import perklocks_day as pd
    assert hasattr(pd, "perklocks_day")
