"""Regression tests for sim_engine Session 2 additions.

§2 weighted historical data — recency + opponent + situation weighting
§4 player usage simulation — PA / minutes / usage / foul / blowout

Run: python -m pytest backend/tests/test_sim_engine_session2.py -q
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sim_engine import (  # noqa: E402
    weighted_history_hit_rate,
    player_usage_factor,
    simulate_pick,
)


def _ts(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


# ── §2 weighted history ────────────────────────────────────────────

def test_recent_outweighs_old():
    """3 recent losses should drag the weighted hit rate lower than a
    naïve mean that averages many old wins."""
    history = (
        [{"ts": _ts(1), "outcome": 0}] * 3  # 3 recent losses
        + [{"ts": _ts(365), "outcome": 1}] * 10  # 10 wins a year ago
    )
    rate, n = weighted_history_hit_rate(history, sport="MLB")
    naive = 10 / 13.0  # 0.77
    assert rate < naive, f"weighted rate {rate:.3f} should be < naïve {naive:.3f}"


def test_similar_opponent_boosts_weight():
    """3 wins vs SAME opponent should outweigh 5 losses vs random ones."""
    history = (
        [{"ts": _ts(30), "outcome": 1, "opponent": "BOS"}] * 3
        + [{"ts": _ts(30), "outcome": 0, "opponent": "TOR"}] * 5
    )
    with_opp, _ = weighted_history_hit_rate(history, sport="MLB",
                                               similar_opponent="BOS")
    without_opp, _ = weighted_history_hit_rate(history, sport="MLB")
    assert with_opp > without_opp


def test_empty_history_returns_zero():
    rate, n = weighted_history_hit_rate([], sport="MLB")
    assert rate == 0.0 and n == 0.0


# ── §4 player usage ────────────────────────────────────────────────

def test_low_pa_dampens():
    """3 PA (below neutral) → factor < 1."""
    pick = {"sport": "MLB", "pick_rationale": {
        "usage": {"expected_pa": 3}
    }}
    assert player_usage_factor(pick) < 1.0


def test_high_pa_amplifies():
    """5 PA (above neutral) → factor > 1."""
    pick = {"sport": "MLB", "pick_rationale": {
        "usage": {"expected_pa": 5}
    }}
    assert player_usage_factor(pick) > 1.0


def test_bottom_of_order_penalty():
    pick = {"sport": "MLB", "pick_rationale": {
        "usage": {"expected_pa": 4, "batting_order": 9}
    }}
    top = {"sport": "MLB", "pick_rationale": {
        "usage": {"expected_pa": 4, "batting_order": 1}
    }}
    assert player_usage_factor(pick) < player_usage_factor(top)


def test_platoon_disadvantage_dampens():
    pick = {"sport": "MLB", "pick_rationale": {
        "usage": {"expected_pa": 4, "platoon_disadvantage": True}
    }}
    control = {"sport": "MLB", "pick_rationale": {
        "usage": {"expected_pa": 4}
    }}
    assert player_usage_factor(pick) < player_usage_factor(control)


def test_nba_low_minutes_dampens():
    pick = {"sport": "NBA", "pick_rationale": {
        "usage": {"expected_minutes": 22, "usage_rate": 0.20}
    }}
    factor = player_usage_factor(pick)
    assert factor < 1.0


def test_nba_foul_and_blowout_stack():
    pick = {"sport": "NBA", "pick_rationale": {
        "usage": {"expected_minutes": 32, "usage_rate": 0.25,
                   "foul_risk": 0.6, "blowout_risk": 0.7}
    }}
    control = {"sport": "NBA", "pick_rationale": {
        "usage": {"expected_minutes": 32, "usage_rate": 0.25}
    }}
    assert player_usage_factor(pick) < player_usage_factor(control)


def test_no_usage_returns_neutral():
    pick = {"sport": "MLB", "pick_rationale": {}}
    assert player_usage_factor(pick) == 1.0


# ── Integration into simulate_pick ─────────────────────────────────

def test_sim_result_includes_new_fields():
    pick = {
        "id": "p", "sport": "MLB",
        "win_probability": 0.55, "book_odds": -110,
        "pick_rationale": {
            "usage": {"expected_pa": 4, "batting_order": 3},
            "recent_form": {
                "history": [
                    {"ts": _ts(2), "outcome": 1, "opponent": "BOS"},
                    {"ts": _ts(5), "outcome": 1, "opponent": "TB"},
                    {"ts": _ts(10), "outcome": 0, "opponent": "NYY"},
                ],
            },
        },
    }
    res = simulate_pick(pick, n_simulations=300)
    assert res.usage_factor > 0
    # hist_hit_rate can be None if history size < 2 but we passed 3
    assert res.hist_hit_rate is not None
