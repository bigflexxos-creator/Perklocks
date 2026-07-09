"""Tests for hot_hitters — stats-driven best-bets discovery.

Locks in the composite heat score so a future refactor can't
regress the ranking (which is what causes the Otto Lopez / Rincones
"where's the data-driven pick?" complaint).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from hot_hitters import _heat_score, _reasons  # noqa: E402


class TestHeatScore:
    def test_eight_game_gate(self):
        """< 8 games in window → score is 0 (insufficient sample)."""
        assert _heat_score(0.350, 1.000, 0.410, 5, games=7) == 0
        assert _heat_score(0.350, 1.000, 0.410, 5, games=8) > 0

    def test_hot_hitter_scores_high(self):
        # Judge-tier profile: .360 avg, .450 obp, 1.100 ops, 8-game streak
        score = _heat_score(0.360, 1.100, 0.450, hit_streak=8, games=15)
        assert score >= 70, f"Elite profile should score ≥70, got {score}"

    def test_cold_hitter_scores_low(self):
        # Below-avg profile
        score = _heat_score(0.240, 0.680, 0.290, hit_streak=0, games=15)
        assert score <= 5, f"Cold profile should score ≤5, got {score}"

    def test_streak_saturates_at_ten(self):
        no_streak = _heat_score(0.300, 0.850, 0.360, hit_streak=0, games=15)
        streak10 = _heat_score(0.300, 0.850, 0.360, hit_streak=10, games=15)
        streak20 = _heat_score(0.300, 0.850, 0.360, hit_streak=20, games=15)
        assert streak10 > no_streak
        # Streak component caps at 25 points regardless of length.
        assert streak10 == streak20

    def test_score_is_monotonic_in_avg(self):
        low = _heat_score(0.260, 0.750, 0.320, hit_streak=3, games=15)
        mid = _heat_score(0.310, 0.750, 0.320, hit_streak=3, games=15)
        high = _heat_score(0.360, 0.750, 0.320, hit_streak=3, games=15)
        assert low < mid < high


class TestReasons:
    def test_streak_reason_surfaces(self):
        rs = _reasons(0.300, 0.850, hit_streak=6, multi_hits=2,
                      l15_games=15, next_pitcher=None)
        assert any("6-game hit streak" in r for r in rs), rs

    def test_avg_reason_uses_baseball_format(self):
        rs = _reasons(0.351, 0.850, hit_streak=0, multi_hits=2,
                      l15_games=15, next_pitcher=None)
        # Should format as ".351" not "0.351"
        assert any(".351" in r for r in rs), rs

    def test_elite_ops_reason(self):
        rs = _reasons(0.300, 0.950, hit_streak=0, multi_hits=1,
                      l15_games=15, next_pitcher=None)
        assert any("OPS" in r and ".950" in r for r in rs), rs

    def test_pitcher_context_surfaces(self):
        rs = _reasons(0.290, 0.800, hit_streak=0, multi_hits=1,
                      l15_games=15, next_pitcher="Chris Sale")
        assert any("Chris Sale" in r for r in rs), rs

    def test_cold_hitter_gets_no_hype_reasons(self):
        rs = _reasons(0.250, 0.680, hit_streak=0, multi_hits=0,
                      l15_games=15, next_pitcher=None)
        # No streak, no elite avg, no elite OPS — should be empty.
        assert rs == []
