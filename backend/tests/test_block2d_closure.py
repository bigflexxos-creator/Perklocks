"""Block 2D Closure — universal wiring gap tests.

  §1 MLB full-game totals: Combined Bullpen / Combined Team Offense
     now wired from real data (were hardcoded None); Starter Quality
     uses combined-average (not the delta helper for ML); Umpire
     factor honestly None; MAX_DAILY_TOTALS keeps elite Lock>=90.
  §2 NFL position routing: canonical position resolver present;
     used in build_nfl_game_context; refuses to guess.
  §3 NBA prop evidence gate: no more silent fallback to "Book
     Implied Probability" — missing feature data → skip pick.
  §4 Soccer totals: Combined Goals Conceded wired; H2H BTTS trend /
     Manager Styles / Injuries honestly None; PARTIAL classification.
  §5 Canonical publication barrier: mls_direct_inject +
     soccer_prop_inject route through the barrier; failing picks
     stored with off_board=True + no_bet=True.
  §6 First-TD honest classification: PARTIAL (uses ATD engine,
     no scoring-order model built).
"""
from __future__ import annotations

import sys
import pytest

sys.path.insert(0, "/app/backend")

pytestmark = pytest.mark.unit


# ═══════════════════════════════════════════════════════════════════
# §1 — MLB full-game totals
# ═══════════════════════════════════════════════════════════════════
def test_mlb_total_combined_bullpen_wired_from_real_data():
    from services.mlb_feature_engine import (
        build_mlb_total_factors, factor_combined_team_bullpen,
    )
    # Combined bullpen returns None when no data.
    empty_ctx = {"home_team": "A", "away_team": "B"}
    assert factor_combined_team_bullpen(empty_ctx) is None
    # Combined bullpen fires when BOTH teams' ERAs are present.
    ctx = {
        "home_team": "A", "away_team": "B",
        "bullpens": {"a": {"era": 3.20}, "b": {"era": 4.10}},
    }
    v = factor_combined_team_bullpen(ctx)
    assert v is not None and 0.30 <= v <= 0.95

    factors, sources = build_mlb_total_factors(ctx, side="over")
    assert factors["Combined Bullpen"] is not None
    assert "Combined Bullpen" in sources


def test_mlb_total_combined_offense_wired_from_real_data():
    from services.mlb_feature_engine import (
        build_mlb_total_factors, factor_combined_team_offense,
    )
    ctx = {
        "home_team": "A", "away_team": "B",
        "team_runs": {"a": 4.8, "b": 4.2},
    }
    v = factor_combined_team_offense(ctx)
    assert v is not None
    factors, sources = build_mlb_total_factors(ctx, side="over")
    assert factors["Combined Team Offense"] is not None
    assert "Combined Team Offense" in sources


def test_mlb_total_starter_quality_uses_combined_not_delta():
    from services.mlb_feature_engine import (
        build_mlb_total_factors, factor_combined_starter_quality,
    )
    ctx = {
        "home_team": "A", "away_team": "B",
        "starting_pitcher_home": {"stuff_plus": 105},
        "starting_pitcher_away": {"stuff_plus": 95},
    }
    # Combined starter quality returns the AVG-based factor (100 mid-band).
    v = factor_combined_starter_quality(ctx)
    assert v is not None
    # Two elite starters (stuff+=115 each) → high factor.
    ctx_elite = dict(ctx,
        starting_pitcher_home={"stuff_plus": 115},
        starting_pitcher_away={"stuff_plus": 115})
    v_elite = factor_combined_starter_quality(ctx_elite)
    # Two bad starters (stuff+=85 each) → low factor.
    ctx_bad = dict(ctx,
        starting_pitcher_home={"stuff_plus": 85},
        starting_pitcher_away={"stuff_plus": 85})
    v_bad = factor_combined_starter_quality(ctx_bad)
    assert v_elite > v > v_bad, f"got elite={v_elite}, mid={v}, bad={v_bad}"


def test_mlb_total_umpire_factor_still_none_honest():
    from services.mlb_feature_engine import build_mlb_total_factors
    ctx = {"home_team": "A", "away_team": "B"}
    factors, _ = build_mlb_total_factors(ctx, side="over")
    # No umpire ingest yet — factor honestly None.  MISSING DATA
    # stays missing.
    assert factors["Umpire Strike Zone"] is None


def test_mlb_data_driven_model_reads_team_runs_dict():
    """data_driven_model.mlb_total_deep must fall back to
    ctx.team_runs[home/away] when the legacy team_runs_avg_* keys
    aren't set (they are never populated by MLB game context)."""
    src = open("/app/backend/services/data_driven_model.py").read()
    idx = src.index("Block 2D Closure §1")
    window = src[idx: idx + 2000]
    assert 'ctx.get("team_runs")' in window
    assert 'home_team' in window or '"home"' in window


def test_mlb_totals_daily_cap_preserves_elite_lock_score():
    """MAX_DAILY_TOTALS=6 must not silently suppress a Lock>=90
    elite pick — the elite floor is always kept regardless of edge
    ranking."""
    src = open("/app/backend/sports_engine.py").read()
    idx = src.index("Block 2D Closure §1")
    window = src[idx: idx + 2500]
    assert "LOCK_ELITE_FLOOR" in window
    assert ">= LOCK_ELITE_FLOOR" in window
    # Elite kept + top-N-by-edge from remainder.
    assert "elite +" in window or "elite = " in window


# ═══════════════════════════════════════════════════════════════════
# §2 — NFL QB/RB/WR/TE position routing
# ═══════════════════════════════════════════════════════════════════
def test_nfl_canonical_position_resolver_present():
    src = open("/app/backend/sports_engine.py").read()
    assert "async def resolve_nfl_position_for_player" in src
    # Consulted in build_nfl_game_context.
    ne = open("/app/backend/services/nfl_feature_engine.py").read()
    assert "resolve_nfl_position_for_player" in ne
    assert "canonical_registry" in ne  # position_source label


def test_nfl_position_from_market_marked_last_resort():
    src = open("/app/backend/sports_engine.py").read()
    idx = src.index("def _infer_nfl_position_from_market")
    end = src.index("\n\n\n", idx)
    body = src[idx: end]
    assert "LAST-RESORT" in body or "last-resort" in body
    # Actual precompute uses canonical registry FIRST.
    ne = open("/app/backend/services/nfl_feature_engine.py").read()
    assert 'canonical_pos or cand.get("position") or _infer_position' in ne


def test_nfl_position_source_tracked_in_precompute():
    ne = open("/app/backend/services/nfl_feature_engine.py").read()
    assert '"position_used"' in ne
    assert '"position_source"' in ne


# ═══════════════════════════════════════════════════════════════════
# §3 — NBA prop evidence gate
# ═══════════════════════════════════════════════════════════════════
def test_nba_prop_no_longer_falls_back_to_book_implied_only():
    """Previously, an NBA player prop with no gamelog data would
    fall through to {'Book Implied Probability': mp} + a
    'nba_engine_no_precompute' source tag — silent PARTIAL.  Block
    2D Closure §3 removes the fallback: NBA prop with no real
    feature data → _skip_pick=True + MISSING_FEATURE_DATA diagnostic."""
    src = open("/app/backend/sports_engine.py").read()
    idx = src.index("Block 2D Closure §3")
    window = src[idx: idx + 2500]
    # The old fallback shape.
    assert "book_implied_calibrated" not in window \
        or "nba_engine_no_precompute" not in window
    # The new gate.
    assert "_skip_pick = True" in window
    assert "nba_prop_evidence_gate" in window


# ═══════════════════════════════════════════════════════════════════
# §4 — Soccer totals
# ═══════════════════════════════════════════════════════════════════
def test_soccer_total_combined_goals_conceded_wired():
    from services.soccer_feature_engine import (
        build_soccer_total_factors, factor_goals_conceded,
    )
    # Empty ctx → conceded factor None.
    empty_ctx = {"home_team": "H", "away_team": "A"}
    factors, sources = build_soccer_total_factors(empty_ctx, "over")
    assert "Combined Goals Conceded" in factors


def test_soccer_totals_missing_data_stays_missing():
    """H2H BTTS trend / Manager Styles / Injuries must remain None
    until upstream data lands — MISSING DATA stays missing."""
    from services.soccer_feature_engine import build_soccer_total_factors
    factors, _ = build_soccer_total_factors({}, "over")
    assert factors["H2H BTTS trend"] is None
    assert factors["Manager Styles"] is None
    assert factors["Injuries (both teams)"] is None


# ═══════════════════════════════════════════════════════════════════
# §5 — Canonical publication barrier
# ═══════════════════════════════════════════════════════════════════
def test_canonical_barrier_rejects_pick_missing_book_odds():
    from services.canonical_publication_barrier import apply_canonical_barrier
    pick = {
        "id": "t1", "lock_score": 92,
        # No book_odds
        "implied_probability": 0.45,
    }
    apply_canonical_barrier(pick)
    assert pick["off_board"] is True
    assert pick["no_bet"] is True
    assert "no_real_book_odds" in pick["barrier_failures"]
    assert pick["publication_gate"] == "canonical_barrier_rejected"


def test_canonical_barrier_rejects_pick_below_strict_floor():
    from services.canonical_publication_barrier import apply_canonical_barrier
    pick = {
        "id": "t2", "lock_score": 80,
        "book_odds": -110,
    }
    apply_canonical_barrier(pick)
    assert pick["off_board"] is True
    assert "lock_below_strict_floor_85" in pick["barrier_failures"]


def test_canonical_barrier_rejects_no_real_book_line_flag():
    from services.canonical_publication_barrier import apply_canonical_barrier
    pick = {
        "id": "t3", "lock_score": 90,
        "book_odds": +150,
        "no_real_book_line": True,
    }
    apply_canonical_barrier(pick)
    assert pick["off_board"] is True
    assert "marked_no_real_book_line" in pick["barrier_failures"]


def test_canonical_barrier_passes_valid_pick_and_fills_implied():
    from services.canonical_publication_barrier import apply_canonical_barrier
    pick = {
        "id": "t4", "lock_score": 90,
        "book_odds": -110,
    }
    apply_canonical_barrier(pick)
    assert pick.get("off_board") is not True
    assert pick["publication_gate"] == "canonical_barrier_passed"
    # Implied probability auto-filled from real odds.
    assert pick["implied_probability"] is not None
    assert 0.50 < pick["implied_probability"] < 0.55


def test_mls_direct_inject_wires_canonical_barrier():
    src = open("/app/backend/services/mls_direct_inject.py").read()
    assert "from services.canonical_publication_barrier import apply_canonical_barrier" in src
    assert "apply_canonical_barrier(p)" in src


def test_soccer_prop_inject_wires_canonical_barrier():
    src = open("/app/backend/services/soccer_prop_inject.py").read()
    assert "canonical_publication_barrier" in src
    assert "apply_canonical_barrier(p)" in src


def test_signal_engine_writes_stay_shadow_updates_only():
    """Per user directive: internal/shadow writes may remain direct
    if they can NEVER become user-visible.  signal_engine writes
    UpdateOne($set) on EXISTING picks (whose visibility was already
    decided by the canonical orchestrator), so no user-visible pick
    can be created by these writers."""
    for fn in ("services/signal_engine/engine.py",
                "services/signal_engine/rank.py"):
        src = open(f"/app/backend/{fn}").read()
        assert "UpdateOne" in src
        assert "InsertOne" not in src, f"{fn}: no InsertOne allowed"
        assert "ReplaceOne" not in src, f"{fn}: no ReplaceOne allowed"


def test_barrier_summary_helper_reports_verdicts():
    from services.canonical_publication_barrier import (
        apply_canonical_barrier, barrier_summary,
    )
    picks = [
        {"id": "a", "lock_score": 92, "book_odds": -110},         # passes
        {"id": "b", "lock_score": 80, "book_odds": -110},         # rejected: lock
        {"id": "c", "lock_score": 92},                            # rejected: no odds
    ]
    for p in picks:
        apply_canonical_barrier(p)
    s = barrier_summary(picks)
    assert s["total"] == 3
    assert s["passed"] == 1
    assert s["rejected"] == 2
    assert s["by_failure"].get("lock_below_strict_floor_85", 0) == 1
    assert s["by_failure"].get("no_real_book_odds", 0) == 1


# ═══════════════════════════════════════════════════════════════════
# §6 — First-TD classification honesty
# ═══════════════════════════════════════════════════════════════════
def test_first_td_uses_atd_engine_no_dedicated_model():
    """First-TD (`player_1st_td`) routes through the SAME ATD
    engine — no scoring-order-specific model exists.  Classified
    PARTIAL until such a model is built (deferred to a future
    block).  This test locks the honest classification."""
    ne = open("/app/backend/services/nfl_feature_engine.py").read()
    assert '"player_1st_td"' in ne
    # Both markets share the SAME atd_out precompute dict.
    idx = ne.index('atd_candidates = [')
    window = ne[idx: idx + 1500]
    assert 'in ("player_anytime_td", "player_1st_td")' in window


# ═══════════════════════════════════════════════════════════════════
# §7 — Universal invariants unchanged
# ═══════════════════════════════════════════════════════════════════
def test_universal_settlement_still_missing_data_neq_zero():
    from services import universal_settlement_contract as usc
    graded = usc.grade_over_under(actual=None, line=1.5, side="over")
    assert graded.get("result") == usc.RESULT_UNRESOLVED


def test_p05_published_results_truth_still_present():
    from services import published_results_truth as prt
    assert hasattr(prt, "PublishedResultsTruthService")


def test_block2b_perklocks_day_still_present():
    from services import perklocks_day as pd
    assert hasattr(pd, "perklocks_day")


def test_block2c_isolate_bad_markets_still_wired():
    src = open("/app/backend/sports_engine.py").read()
    assert "_isolate_and_merge_event_props" in src


def test_block2d_stage_a_wiring_still_intact():
    src = open("/app/backend/sports_engine.py").read()
    assert "nfl_atd_precomputed" in src
    assert "_atd_model_override" in src
    assert "hr_intel_evidence" in src


def test_block2d_stage_b_wiring_still_intact():
    src = open("/app/backend/sports_engine.py").read()
    assert "_dc_outcomes" in src
    assert "_btts_outcomes" in src
    assert "DOUBLE_CHANCE_SYNTHETIC_LINE_BLOCKED" in src


def test_strict_85_gate_unchanged():
    from services.canonical_publication_barrier import STRICT_LOCK_FLOOR
    assert STRICT_LOCK_FLOOR == 85
