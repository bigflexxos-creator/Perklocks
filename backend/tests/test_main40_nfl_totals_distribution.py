"""Item 1 — NFL Totals independent distribution.

Verifies that the NFL game simulator centers its scoring
distribution on the MODEL's ``expected_total``, not on the sportsbook
line, while still using the sportsbook line as the O/U threshold.

Also verifies P(Over) + P(Under) + P(Push) = 1 for the totals side.
"""
from __future__ import annotations

import os
import random
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from services.platinum_nfl.game_markets import simulate_game_market


def test_distribution_mean_tracks_expected_total_not_book_line():
    """Model expected_total = 52 must produce a distribution whose
    mean is near 52 regardless of a wildly different book line (44.5).
    """
    pick = {
        "market": "total",
        "side": "Over",
        "line": 44.5,
        "home_team": "H",
        "away_team": "A",
    }
    sim = simulate_game_market(
        pick,
        expected_margin_home=0.0,
        total_line=44.5,           # sportsbook threshold
        expected_total=52.0,       # model prediction
        seed=random.Random(42),
        n_sims=4000,
    )
    assert sim["ran"] is True
    assert abs(sim["distribution_mean"] - 52.0) < 1.0, (
        f"Distribution mean {sim['distribution_mean']!r} must center "
        f"on model expected_total=52, not book line 44.5"
    )
    # And the book line stays as the threshold on the emitted summary.
    assert abs(sim["market_threshold"] - 44.5) < 1e-9


def test_over_edge_preserved_when_model_above_book():
    """When the model predicts higher scoring than the book, Over
    should be materially > 50%.
    """
    pick = {"market": "total", "side": "Over", "line": 44.5}
    sim = simulate_game_market(
        pick,
        expected_margin_home=0.0,
        total_line=44.5,
        expected_total=52.0,
        seed=random.Random(7),
        n_sims=4000,
    )
    assert sim["sim_probability"] > 0.55, sim


def test_under_edge_preserved_when_model_below_book():
    """When the model predicts lower scoring than the book, Under
    should be materially > 50%.
    """
    pick = {"market": "total", "side": "Under", "line": 52.5}
    sim = simulate_game_market(
        pick,
        expected_margin_home=0.0,
        total_line=52.5,
        expected_total=42.0,
        seed=random.Random(13),
        n_sims=4000,
    )
    assert sim["sim_probability"] > 0.55, sim


def test_over_under_push_sum_to_one():
    """Directly evaluate P(Over) + P(Under) + P(Push) from the sample
    used inside the simulator via two calls on the same seed.
    """
    from services.platinum_nfl.football_core import (
        sample_game_script, p_over, p_under, p_push,
    )
    scripts = sample_game_script(
        expected_margin_home=0.0,
        total_line=44.5,
        expected_total=50.0,
        seed=random.Random(99), n=4000,
    )
    totals = [s["total_points"] for s in scripts]
    line = 44.5
    p_o = p_over(totals, line)
    p_u = p_under(totals, line)
    p_p = p_push(totals, line)
    total = p_o + p_u + p_p
    assert abs(total - 1.0) < 1e-9, (p_o, p_u, p_p, total)


def test_legacy_fallback_uses_total_line_when_expected_total_missing():
    """If callers don't pass expected_total the sim must still run
    (backward compat), but this collapses edge — we merely assert
    that ran=True.
    """
    pick = {"market": "total", "side": "Over", "line": 44.5}
    sim = simulate_game_market(
        pick,
        expected_margin_home=0.0,
        total_line=44.5,
        seed=random.Random(1),
        n_sims=500,
    )
    assert sim["ran"] is True


if __name__ == "__main__":
    test_distribution_mean_tracks_expected_total_not_book_line()
    test_over_edge_preserved_when_model_above_book()
    test_under_edge_preserved_when_model_below_book()
    test_over_under_push_sum_to_one()
    test_legacy_fallback_uses_total_line_when_expected_total_missing()
    print("OK — NFL totals distribution independence verified.")
