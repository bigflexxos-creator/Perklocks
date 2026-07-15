"""Unit tests for services/tennis_calibration.py — Phase 3c.

Verifies the z-score → 0-100 normalization actually differentiates
players. The "super lock CAN reach 99" property is preserved because
signal_engine.decorate_signals_bulk adds up to +3 to base lock when
all signals align — so an elite calibrated player with positive market
edge and steam alignment CAN cross into 99-territory.
"""
from __future__ import annotations

from services.tennis_calibration import _stat_score, _z_to_score


class TestZScoreMapping:
    def test_league_average_scores_50(self):
        assert _z_to_score(0.0) == 50.0

    def test_two_std_above_scores_90(self):
        assert _z_to_score(2.0) == 90.0

    def test_two_std_below_scores_10(self):
        assert _z_to_score(-2.0) == 10.0

    def test_clamps_at_100(self):
        # Extreme outlier — Isner-tier serve 95% hold vs 78% league avg = z~2.6
        assert _z_to_score(5.0) == 100.0

    def test_clamps_at_0(self):
        assert _z_to_score(-5.0) == 0.0


class TestStatScore:
    def test_hold_pct_elite(self):
        # 88% hold at league avg 78 with SD 6.5 → z = 1.54 → 80.8
        score = _stat_score(88, 78.0, 6.5)
        assert 78 <= score <= 84  # roughly 80

    def test_hold_pct_below_avg(self):
        # 68% hold → z = -1.54 → ~19
        score = _stat_score(68, 78.0, 6.5)
        assert 15 <= score <= 25

    def test_none_input(self):
        assert _stat_score(None, 78, 6.5) is None

    def test_zero_stddev(self):
        assert _stat_score(80, 78, 0) is None
