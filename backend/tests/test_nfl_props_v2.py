"""NFL Player Props 2.0 — regression suite.

Focused, cheap tests — no external providers, no historical rebuild.
"""
from __future__ import annotations

import math
import pytest

from services.nfl_props_v2.engine import (
    Distribution, ThresholdEvaluation, enforce_monotonicity,
    _normalize_market, _implied_prob, SUPPORTED_MARKETS,
)
from services.nfl_props_v2.adapters import (
    DefaultWeatherProvider, DefaultAvailabilityProvider,
    WeatherReport, AvailabilityReport,
    WEATHER_STATUS_UNAVAILABLE, WEATHER_STATUS_AVAILABLE,
    INJURY_STATUS_PARTIAL, INJURY_STATUS_UNAVAILABLE,
)


# ── Distribution / hit-probability ─────────────────────────────────

def test_distribution_hit_prob_empirical():
    d = Distribution(market="rec_yds", sample_size=10,
                     mean=60, variance=100,
                     q25=50, median=65, q75=80,
                     values=[30, 45, 50, 55, 60, 70, 75, 80, 90, 100])
    p50 = d.hit_probability_over(50)
    p90 = d.hit_probability_over(90)
    assert p50 > p90


def test_distribution_hit_prob_gaussian_fallback():
    d = Distribution(market="rec_yds", sample_size=3,
                     mean=60, variance=100)
    p50 = d.hit_probability_over(60)
    # At the mean → ~0.5
    assert abs(p50 - 0.5) < 0.05


def test_distribution_floor_distance_positive_when_line_below_q25():
    d = Distribution(market="rec_yds", sample_size=10, q25=58, median=77, q75=99,
                     values=[30, 45, 55, 60, 65, 70, 80, 90, 95, 100])
    assert d.floor_distance(40) == 18.0
    assert d.floor_distance(75) == -17.0


# ── Monotonicity ──────────────────────────────────────────────────

def test_enforce_monotonicity_never_lowers_a_higher_line_probability():
    """P(line=25) must be >= P(line=75)."""
    evals = [
        ThresholdEvaluation("rec_yds", 25, -300, "DK", 0.85, None, 33.0, 0.75, 10.0),
        ThresholdEvaluation("rec_yds", 40, -180, "DK", 0.87, None, 18.0, 0.643, 22.7),  # ← anomaly
        ThresholdEvaluation("rec_yds", 60,  120, "DK", 0.55, None,  -2.0, 0.454, 9.6),
        ThresholdEvaluation("rec_yds", 75,  180, "DK", 0.35, None, -17.0, 0.357, -0.7),
    ]
    fixed = enforce_monotonicity(evals)
    ps = [e.hit_probability_monotonic for e in fixed]
    # sorted asc by line → monotonic non-increasing probability
    for i in range(len(ps) - 1):
        assert ps[i] >= ps[i+1] - 1e-9, ps


def test_enforce_monotonicity_handles_none():
    evals = [
        ThresholdEvaluation("rec_yds", 25, -300, "DK", None, None, None, None, None),
        ThresholdEvaluation("rec_yds", 75,  180, "DK", 0.35, None, -17.0, None, None),
    ]
    fixed = enforce_monotonicity(evals)
    # None probabilities remain None.  A non-None below in the ladder
    # never fabricates a probability above.
    assert fixed[0].hit_probability_monotonic is None
    assert fixed[1].hit_probability_monotonic == 0.35


# ── Market normalization ──────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("player_pass_yds", "pass_yds"),
    ("Player Pass Yards", "pass_yds"),
    ("player_rush_yds", "rush_yds"),
    ("player_receptions", "receptions"),
    ("player_reception_yds", "rec_yds"),
    ("anytime_td", "anytime_td"),
    ("player_anytime_td", "anytime_td"),
    ("nonsense_market", "nonsense_market"),
])
def test_market_normalization(raw, expected):
    assert _normalize_market(raw) == expected


# ── Implied probability helper ────────────────────────────────────

def test_implied_probability_negative_odds():
    p = _implied_prob(-200)
    assert abs(p - 0.6667) < 0.001


def test_implied_probability_positive_odds():
    p = _implied_prob(+150)
    assert abs(p - 0.40) < 0.001


def test_implied_probability_handles_none():
    assert _implied_prob(None) is None
    assert _implied_prob(0) is None


# ── Default weather provider — no fake values ─────────────────────

@pytest.mark.asyncio
async def test_default_weather_reports_unavailable_by_default():
    p = DefaultWeatherProvider()
    r = await p.report_for_game(None, {"home_team": "DAL", "away_team": "PHI"})
    assert r.status == WEATHER_STATUS_UNAVAILABLE
    assert r.temperature_f is None
    assert r.wind_mph is None


@pytest.mark.asyncio
async def test_default_weather_consumes_indoor_flag_when_present():
    p = DefaultWeatherProvider()
    r = await p.report_for_game(None, {"is_indoor": True})
    assert r.status == WEATHER_STATUS_AVAILABLE
    assert r.is_indoor is True


# ── Adapter interface completeness ───────────────────────────────

def test_supported_markets_registered():
    for m in ("pass_yds", "rush_yds", "rec_yds", "receptions", "anytime_td"):
        assert m in SUPPORTED_MARKETS


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
