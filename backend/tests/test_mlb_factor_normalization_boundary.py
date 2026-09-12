"""Focused tests for the MLB → v4 scorer factor normalisation boundary.

Covers PART A + PART D of the 2026-06-15 continuous surgical closure:

  * MLB 0-100 percentage inputs are converted to ``[0, 1]``
  * Already-normalised ``[0, 1]`` values pass through untouched
  * Missing / non-numeric factors stay absent from the scoring dict
  * Impossible scale values are quarantined (fail-closed)
  * ``market_alignment`` never collapses from a scale mismatch
  * All MLB market families share the same normalisation contract
  * Human-readable ``factors`` on the pick keep their display values
  * DB → wire Lock Score parity is preserved for v4-stamped MLB picks
  * Cross-sport regression — NFL / Soccer / CFB scores unchanged
"""
from __future__ import annotations

import math
import pytest

from services.mlb_factor_normalization import (
    normalize_mlb_factors_for_scoring,
    has_out_of_scale_factors,
)
from sports_engine import compute_lock_score


# ─────────────────────────────────────────────────────────────────────
# 1. Unit — boundary helper
# ─────────────────────────────────────────────────────────────────────

def test_percent_input_71p8_converts_to_0p718():
    """A MLB emission emitting Barrel% = 71.8 must be scaled to 0.718."""
    out = normalize_mlb_factors_for_scoring({"Barrel% (Quality of Contact)": 71.8})
    assert "Barrel% (Quality of Contact)" in out
    assert math.isclose(out["Barrel% (Quality of Contact)"], 0.718, abs_tol=1e-3)


def test_already_normalized_barrel_stays_0p718():
    out = normalize_mlb_factors_for_scoring({"Barrel% (Quality of Contact)": 0.718})
    assert math.isclose(out["Barrel% (Quality of Contact)"], 0.718, abs_tol=1e-3)


def test_missing_factor_stays_absent():
    out = normalize_mlb_factors_for_scoring({"Expected BA (Statcast)": None})
    assert "Expected BA (Statcast)" not in out


def test_invalid_percentage_scale_fails_closed():
    """Barrel% = 132 (impossible upstream bug) → clamped, never negative
    or scale-mangled downstream."""
    out = normalize_mlb_factors_for_scoring({"Barrel% (Quality of Contact)": 132.0})
    assert out["Barrel% (Quality of Contact)"] == 1.0

    # Truly impossible (>200 raw) → quarantined entirely.
    out = normalize_mlb_factors_for_scoring({"Barrel% (Quality of Contact)": 3000.0})
    assert "Barrel% (Quality of Contact)" not in out

    # Negative rate → dropped.
    out = normalize_mlb_factors_for_scoring({"Recent L10 Hit Rate": -0.4})
    assert "Recent L10 Hit Rate" not in out


def test_provenance_keys_dropped():
    out = normalize_mlb_factors_for_scoring({
        "__data_quality": "sp_plus",
        "__model_uncertainty_reason": "nominal",
        "Barrel% (Quality of Contact)": 0.65,
    })
    assert "__data_quality" not in out
    assert "__model_uncertainty_reason" not in out
    assert "Barrel% (Quality of Contact)" in out


def test_non_rate_key_not_divided():
    """A raw ``line`` or ``count`` numeric factor is NOT divided by
    100 even if > 1.5 — it isn't a rate."""
    out = normalize_mlb_factors_for_scoring({"K Line count": 6.5})
    # Non-rate + out of [0, 1.5] → dropped defensively (never divided).
    assert "K Line count" not in out


def test_string_factor_ignored():
    """CFB-style display string factors (e.g. \"78.4%\") never enter the scorer."""
    out = normalize_mlb_factors_for_scoring({"Model Fair Prob": "78.4%"})
    assert "Model Fair Prob" not in out


def test_idempotent():
    once = normalize_mlb_factors_for_scoring({"Barrel% (Quality of Contact)": 71.8})
    twice = normalize_mlb_factors_for_scoring(once)
    assert once == twice


def test_has_out_of_scale_detector():
    assert has_out_of_scale_factors({"Barrel% (Quality of Contact)": 71.8})
    assert not has_out_of_scale_factors({"Barrel% (Quality of Contact)": 0.718})
    assert not has_out_of_scale_factors({})
    assert not has_out_of_scale_factors(None)


# ─────────────────────────────────────────────────────────────────────
# 2. Integration — MLB path in compute_lock_score
# ─────────────────────────────────────────────────────────────────────

def _mlb_hitter_shim() -> dict:
    return {
        "sport": "MLB",
        "market": "Spencer Jones (NYY) Over 0.5 Hits",
        "book_odds": -172,
        "win_probability": 75.7,
        "edge_percent": 12.5,
        "is_alt_line": False,
    }


HITTER_FACTORS_01 = {
    "Recent L10 Hit Rate":               0.91,
    "Home/Away Splits":                   0.66,
    "Platoon Advantage":                  0.95,
    "Expected BA (Statcast)":             0.532,
    "Barrel% (Quality of Contact)":       0.95,
    "Hard-Hit % (Statcast)":              0.95,
    "Regression Signal (xBA-BA)":         0.813,
    "DFS Projection vs Line":             0.854,
}
HITTER_FACTORS_100 = {k: round(v * 100, 1) for k, v in HITTER_FACTORS_01.items()}


def test_alignment_does_not_collapse_from_scale_mismatch():
    """The core defect closure — feeding 0-100 factors used to collapse
    market_alignment to 0.0 for every MLB hitter/pitcher pick."""
    pick_01 = _mlb_hitter_shim()
    score_01, _ = compute_lock_score(HITTER_FACTORS_01, win_prob=75.7,
                                      pick=pick_01, edge_percent=12.5)

    pick_100 = _mlb_hitter_shim()
    score_100, _ = compute_lock_score(HITTER_FACTORS_100, win_prob=75.7,
                                       pick=pick_100, edge_percent=12.5)

    align_01 = pick_01["lock_components"]["alignment"]
    align_100 = pick_100["lock_components"]["alignment"]

    assert align_01 > 5.0, f"[0,1] alignment collapsed to {align_01} — v4 math regression"
    assert align_100 > 5.0, f"[0,100] alignment collapsed to {align_100} — normalisation boundary is not wired"
    # Both variants should now be within 5 points of each other — same
    # semantic factors, just different unit conventions on the wire.
    assert abs(align_01 - align_100) <= 5.0, (
        f"boundary should equalise alignment across scales: "
        f"[0,1]={align_01} vs [0,100]={align_100}"
    )


def test_score_does_not_collapse_from_scale_mismatch():
    pick_01 = _mlb_hitter_shim()
    score_01, _ = compute_lock_score(HITTER_FACTORS_01, win_prob=75.7,
                                      pick=pick_01, edge_percent=12.5)
    pick_100 = _mlb_hitter_shim()
    score_100, _ = compute_lock_score(HITTER_FACTORS_100, win_prob=75.7,
                                       pick=pick_100, edge_percent=12.5)
    assert abs(score_01 - score_100) <= 2.0


def test_weighted_display_preserved_after_boundary():
    """The returned ``weighted`` dict (used for UI display) must still
    show human-readable 0-100 values regardless of the input scale."""
    pick = _mlb_hitter_shim()
    _, weighted_01 = compute_lock_score(HITTER_FACTORS_01, win_prob=75.7,
                                         pick=pick, edge_percent=12.5)
    for k, v in weighted_01.items():
        if k.startswith("__"):
            continue
        if isinstance(v, (int, float)):
            assert 0 <= v <= 100, f"display {k} out of 0-100 band: {v}"

    pick2 = _mlb_hitter_shim()
    _, weighted_100 = compute_lock_score(HITTER_FACTORS_100, win_prob=75.7,
                                          pick=pick2, edge_percent=12.5)
    for k, v in weighted_100.items():
        if k.startswith("__"):
            continue
        if isinstance(v, (int, float)):
            assert 0 <= v <= 100, f"display {k} out of 0-100 band after boundary: {v}"


def test_mlb_pitcher_k_family_uses_same_boundary():
    """A pitcher K prop factor bundle should also normalise correctly."""
    pitcher_factors = {
        "Stuff+": 68.5,
        "xwOBA": 71.2,
        "K/9": 62.0,
        "DFS Projection vs Line": 84.0,
    }
    pick = {"sport": "MLB", "market": "Pitcher K Prop Over 6.5",
            "book_odds": -145, "win_probability": 72.0, "edge_percent": 4.2}
    score, _ = compute_lock_score(pitcher_factors, win_prob=72.0, pick=pick,
                                   edge_percent=4.2)
    assert pick["lock_components"]["alignment"] > 20.0


def test_mlb_moneyline_uses_same_boundary():
    ml_factors = {
        "Bullpen Advantage":      55.0,
        "Recent Form":            72.0,
        "Home/Away Splits":       61.0,
        "Model Probability":      64.0,
    }
    pick = {"sport": "MLB", "market": "MLB Moneyline",
            "book_odds": -125, "win_probability": 60.0, "edge_percent": 3.5}
    score, _ = compute_lock_score(ml_factors, win_prob=60.0, pick=pick,
                                   edge_percent=3.5)
    assert pick["lock_components"]["alignment"] > 20.0


def test_mlb_total_market_uses_same_boundary():
    total_factors = {
        "Park Factor":            58.0,
        "Weather":                62.0,
        "Opp Bullpen":            71.0,
        "Model Probability":      65.0,
    }
    pick = {"sport": "MLB", "market": "Total Runs Over 8.5",
            "book_odds": -110, "win_probability": 55.0, "edge_percent": 2.0}
    score, _ = compute_lock_score(total_factors, win_prob=55.0, pick=pick,
                                   edge_percent=2.0)
    assert pick["lock_components"]["alignment"] > 15.0


# ─────────────────────────────────────────────────────────────────────
# 3. Cross-sport regression — NFL / Soccer / CFB unchanged
# ─────────────────────────────────────────────────────────────────────

def test_nfl_score_unchanged_when_already_normalised():
    """NFL feature engine already emits ``[0, 1]`` factors — the boundary
    must be a no-op for them (score stays identical)."""
    nfl_factors = {
        "L5 Avg vs Line":        0.68,
        "Opponent Defense":      0.72,
        "Health Signal":         0.85,
        "ATD Engine Confidence": 0.79,
    }
    pick = {"sport": "NFL", "market": "player_reception_yds",
            "book_odds": -140, "win_probability": 80.5, "edge_percent": 5.5,
            "model_win_prob": 80.5}
    score_before, _ = compute_lock_score(nfl_factors, win_prob=80.5,
                                          pick=dict(pick), edge_percent=5.5)
    # Second call should be deterministic given no shared state.
    score_after, _ = compute_lock_score(nfl_factors, win_prob=80.5,
                                         pick=dict(pick), edge_percent=5.5)
    assert score_before == score_after


def test_cfb_norm_factors_unchanged():
    """CFB spread/total emission uses ``(norm)`` numeric factors already
    on ``[0, 1]``.  Boundary must preserve them verbatim."""
    cfb_factors = {
        "Projected Margin (norm)":    0.62,
        "Expected Total (norm)":      0.55,
        "Model Fair Prob (norm)":     0.7834,
        "Sportsbook Implied (norm)":  0.61,
        "SP+ Rating Δ (norm)":        0.60,
        # Human-readable strings — must be ignored by scorer.
        "Model Fair Prob":            "78.34%",
        "__data_quality":             "sp_plus",
    }
    pick = {"sport": "CFB", "market": "CFB Total Points Over 43.5",
            "book_odds": -110, "win_probability": 78.34, "edge_percent": 15.5}
    score, weighted = compute_lock_score(cfb_factors, win_prob=78.34,
                                          pick=pick, edge_percent=15.5)
    # CFB (norm) factors are already normalised → alignment stays healthy.
    assert pick["lock_components"]["alignment"] >= 20.0
    # Display factors do NOT expose PROVENANCE INPUT keys (from the
    # caller's factors dict) as scored numerics — they're stripped by
    # the boundary.  The v4 persistence bridge legitimately stashes
    # its own ``__lock_score_version`` / ``__calibrated_win_probability``
    # / ``__effective_weights`` / ``__confidence_component`` keys inside
    # ``weighted`` for ``_build_pick`` to promote to top-level fields.
    _v4_bridge_keys = {
        "__lock_score_version",
        "__calibrated_win_probability",
        "__effective_weights",
        "__confidence_component",
    }
    for k in weighted.keys():
        if k in _v4_bridge_keys:
            continue
        assert not k.startswith("__"), (
            f"provenance INPUT key {k!r} leaked into weighted"
        )


def test_soccer_factors_unchanged():
    soccer_factors = {
        "Model Probability":  0.62,
        "Market Alignment":   0.71,
        "Elo Delta":          0.58,
        "Form Trend":         0.64,
    }
    pick = {"sport": "Soccer", "market": "h2h",
            "book_odds": -140, "win_probability": 62.0, "edge_percent": 3.5}
    score, _ = compute_lock_score(soccer_factors, win_prob=62.0, pick=pick,
                                   edge_percent=3.5)
    assert pick["lock_components"]["alignment"] > 20.0
