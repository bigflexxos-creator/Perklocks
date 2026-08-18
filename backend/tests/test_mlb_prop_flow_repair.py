"""MLB Prop Flow Surgical Repair — focused tests.

Validates all five fixes at source-level:
 1. Balanced-book pair-selection now passes both sides for hitter families + Outs
 2. Shared hitter hydration path is present + gated to unknown lineup
 3. K rejection reasons are mapped to full taxonomy
 4. Pitcher Outs routes through workload-oriented factor builder
 5. Funnel telemetry (record_funnel_step / snapshot) exists
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _src(p): return open(p).read()


# ── FIX 1 — pre-model balanced-book starvation removed ──────────────
def test_balanced_book_keep_both_families_wired():
    src = _src("/app/backend/sports_engine.py")
    assert "_KEEP_BOTH_ON_BALANCED_FAMILIES" in src
    for k in ("batter_hits", "batter_total_bases",
              "batter_hits_runs_rbis", "pitcher_outs"):
        assert f'"{k}"' in src
    # Winner sentinel "both" → allowed_sides = {over, under}
    assert '_winner == "both"' in src
    assert '{"over", "under"}' in src


def test_balanced_book_non_target_family_still_drops():
    """A non-target family with balanced book must still drop both
    (existing contract preserved for K family + other pairs)."""
    src = _src("/app/backend/sports_engine.py")
    # The else branch of the family gate still sets _winner = None.
    assert 'balanced({_o_imp' in src


# ── FIX 2 — hitter hydration (already landed in prior μ-closure) ────
def test_hitter_hydration_helper_present():
    from services.mlb_early_hitter_hydrate import hydrate_missing_hitter
    assert callable(hydrate_missing_hitter)


def test_hitter_hydration_hooked_for_all_three_families():
    src = _src("/app/backend/sports_engine.py")
    assert "_HITTER_FAMILY_MK" in src
    for mk in ("batter_hits", "batter_hits_alternate",
               "batter_total_bases", "batter_total_bases_alternate",
               "batter_hits_runs_rbis", "batter_hits_runs_rbis_alternate"):
        assert f'"{mk}"' in src


def test_hitter_hydration_only_runs_when_unknown():
    src = _src("/app/backend/sports_engine.py")
    idx = src.index("_HITTER_FAMILY_MK")
    assert '_lu_status == "unknown"' in src[idx:idx + 2000]


# ── FIX 3 — K rejection telemetry alignment ─────────────────────────
def test_k_reason_map_covers_full_taxonomy():
    src = _src("/app/backend/sports_engine.py")
    for reason in ("no_pitcher_data",
                   "insufficient_signals",
                   "odds_too_chalky",
                   "insufficient_edge",
                   "edge_too_low",
                   "model_win_prob_low",
                   "model_prob_too_low",
                   "under_but_expected_over",
                   "over_but_expected_under",
                   "under_self_contradict",
                   "book_odds_chalk_trap"):
        assert f'"{reason}"' in src, f"K reason {reason} unmapped"


# ── FIX 4 — Pitcher Outs routes through workload builder ────────────
def test_pitcher_outs_uses_outs_factor_builder():
    from services.mlb_feature_engine import build_mlb_pitcher_outs_factors
    assert callable(build_mlb_pitcher_outs_factors)
    src = _src("/app/backend/sports_engine.py")
    assert "build_mlb_pitcher_outs_factors" in src
    assert "_is_outs_prop" in src
    assert '"pitcher_outs", "pitcher_outs_alternate"' in src


def test_outs_factors_favor_workload_not_pure_k():
    """The Outs factor dict must include workload signals and NOT
    treat this as strictly a K market."""
    from services.mlb_feature_engine import build_mlb_pitcher_outs_factors
    factors, sources = build_mlb_pitcher_outs_factors(
        {"hitters": {}, "starting_pitcher_home": {},
         "starting_pitcher_away": {}}, "test_player", "over", 15.5,
    )
    # Keys must include the workload primaries.
    ks = list(factors.keys())
    assert any("Workload" in k for k in ks)
    assert any("Pitch Count" in k or "Workload" in k for k in ks)
    assert any("Park" in k for k in ks)


# ── FIX 5 — funnel telemetry ────────────────────────────────────────
def test_funnel_step_api_present():
    from services.mlb_gates import (
        record_funnel_step, snapshot, FUNNEL_STEPS,
    )
    for step in ("provider_received", "candidate_created",
                 "model_evaluated", "passed_model",
                 "published", "board_visible"):
        assert step in FUNNEL_STEPS
    # Round-trip: incrementing a step is visible in snapshot.
    from services.mlb_gates import reset
    reset()
    record_funnel_step("candidate_created", market_key="batter_hits")
    record_funnel_step("model_evaluated", market_key="batter_hits")
    snap = snapshot()
    assert snap["funnel_by_market"]["batter_hits"]["candidate_created"] == 1
    assert snap["funnel_by_market"]["batter_hits"]["model_evaluated"] == 1


def test_funnel_wired_in_pitcher_path():
    src = _src("/app/backend/sports_engine.py")
    assert "record_funnel_step" in src
    assert "_mlb_funnel(" in src


# ── Non-negotiables ─────────────────────────────────────────────────
def test_board_floor_unchanged():
    # ≥85 canonical Board floor still in place.
    src = _src("/app/backend/parlay_optimizer.py")
    assert "min_lock = 75.0 if high_risk else 85.0" in src


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
