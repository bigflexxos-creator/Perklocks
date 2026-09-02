"""Targeted tests for Universal Totals Truth §5 — MLB Shared Run Distribution.

Verifies:
  1. P(Over) + P(Under) ≡ 1 for half-lines (conservation).
  2. μ moves in the physically-correct direction for each feature.
  3. Fair-book anchor (joint devig) is respected when features are silent.
  4. Feature caps still hold (μ shift bounded).
  5. `available=False` on missing paired odds (fail-closed).
"""
from __future__ import annotations
import math
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from services.data_driven_model import (  # noqa: E402
    mlb_shared_run_distribution,
    _phi,
    _phi_inv,
)


def _standard_odds():
    return -110, -110   # ~50/50 fair after joint devig


def test_conservation_half_line_no_features():
    d = mlb_shared_run_distribution(
        line=8.5,
        book_over_odds=-110,
        book_under_odds=-110,
        ctx={},
    )
    assert d["available"], d
    total = d["mp_over"] + d["mp_under"]
    assert abs(total - 1.0) < 1e-6, f"conservation fail: {total}"


def test_conservation_half_line_with_features():
    ctx = {
        "weather": {"temp_f": 88, "wind_mph": 12, "wind_deg": 90, "is_dome": False},
        "park_hr_factor": 118,     # Coors-like
        "starting_pitcher_home": {"stuff_plus": 92},
        "starting_pitcher_away": {"stuff_plus": 88},
        "home_team": "rockies", "away_team": "diamondbacks",
        "team_runs": {"rockies": 5.6, "diamondbacks": 4.9},
    }
    d = mlb_shared_run_distribution(8.5, -110, -110, ctx)
    assert d["available"], d
    total = d["mp_over"] + d["mp_under"]
    assert abs(total - 1.0) < 1e-6, f"conservation fail: {total}"
    # Hot weather + Coors + weak pitching + strong offense → μ shifts UP.
    assert d["mu"] > d["mu_anchor"], d
    assert d["mp_over"] > 0.5, d


def test_conservation_pitchers_duel():
    ctx = {
        "starting_pitcher_home": {"stuff_plus": 115},
        "starting_pitcher_away": {"stuff_plus": 118},
        "home_team": "dodgers", "away_team": "padres",
        "team_runs": {"dodgers": 3.4, "padres": 3.1},
    }
    d = mlb_shared_run_distribution(8.5, -110, -110, ctx)
    assert d["available"], d
    total = d["mp_over"] + d["mp_under"]
    assert abs(total - 1.0) < 1e-6, f"conservation fail: {total}"
    # Elite pitchers + weak offense → μ shifts DOWN, Under favoured.
    assert d["mu"] < d["mu_anchor"], d
    assert d["mp_under"] > 0.5, d


def test_book_anchor_respected_when_no_features():
    # Skewed book: -140/+120 → fair Over ~ 0.56.
    d = mlb_shared_run_distribution(9.0, -140, 120, ctx={})
    assert d["available"], d
    assert 0.54 <= d["fair_over"] <= 0.58, d["fair_over"]
    # No features → μ ≈ μ_anchor.
    assert abs(d["mu"] - d["mu_anchor"]) < 1e-6, d
    # mp_over recovered off the same distribution ≈ fair_over.
    assert abs(d["mp_over"] - d["fair_over"]) < 1e-3, d


def test_mu_shift_capped_at_1p2_runs():
    # Extreme features stack far past the cap; result must clamp.
    ctx = {
        "weather": {"temp_f": 95, "wind_mph": 30, "wind_deg": 90, "is_dome": False},
        "park_hr_factor": 145,
        "starting_pitcher_home": {"stuff_plus": 60},
        "starting_pitcher_away": {"stuff_plus": 60},
        "home_team": "a", "away_team": "b",
        "team_runs": {"a": 8.0, "b": 8.0},
    }
    d = mlb_shared_run_distribution(8.5, -110, -110, ctx)
    assert d["available"], d
    assert d["mu_shift"] <= 1.2 + 1e-9, d
    assert abs(d["mp_over"] + d["mp_under"] - 1.0) < 1e-6


def test_missing_paired_odds_fails_closed():
    d = mlb_shared_run_distribution(8.5, None, -110, ctx={})
    assert d["available"] is False
    d = mlb_shared_run_distribution(8.5, -110, None, ctx={})
    assert d["available"] is False


def test_phi_and_phi_inv_round_trip():
    for p in (0.05, 0.25, 0.5, 0.75, 0.95):
        z = _phi_inv(p)
        assert abs(_phi(z) - p) < 1e-4, (p, z, _phi(z))


def test_symmetry_under_line_flip():
    # For a symmetric book, swapping Over/Under labels must produce
    # exactly mirrored probs.
    ctx = {"park_hr_factor": 100}
    d = mlb_shared_run_distribution(8.5, -110, -110, ctx)
    assert abs(d["mp_over"] - d["mp_under"]) < 5e-3


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
