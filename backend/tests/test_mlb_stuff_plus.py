"""Unit tests for services/mlb_stuff_plus.py — Phase 1.2.

Focus on the pure math: RV→Stuff+ / xwOBA→Location+ mapping and the
usage-weighted aggregation. Network calls to Baseball Savant are
mocked out — this module needs to run offline in CI.
"""
from __future__ import annotations

import pytest

from services.mlb_stuff_plus import (
    _aggregate_arsenal,
    _extract_pitcher_from_pick,
    _rv_to_stuff_plus,
    _xwoba_to_location_plus,
)


class TestScaleMappings:
    def test_stuff_plus_neutral(self):
        # RV/100 = 0 → league avg (100)
        assert _rv_to_stuff_plus(0.0) == 100.0

    def test_stuff_plus_elite(self):
        # RV/100 = -2.0 (elite) → 120+
        assert _rv_to_stuff_plus(-2.0) == 120.0

    def test_stuff_plus_terrible(self):
        # RV/100 = +2.0 → 80
        assert _rv_to_stuff_plus(2.0) == 80.0

    def test_stuff_plus_clamped(self):
        # Extreme values are clamped to Fangraphs' observed range 60-150
        assert _rv_to_stuff_plus(-100.0) == 150.0
        assert _rv_to_stuff_plus(+100.0) == 60.0

    def test_location_plus_neutral(self):
        # xwOBA of 0.320 (league avg anchor) → 100
        assert _xwoba_to_location_plus(0.320) == 100.0

    def test_location_plus_elite(self):
        # xwOBA of 0.270 (elite) → 100 + 0.05*200 = 110
        assert _xwoba_to_location_plus(0.270) == 110.0

    def test_location_plus_bad(self):
        # xwOBA of 0.370 → 100 - 0.05*200 = 90
        assert _xwoba_to_location_plus(0.370) == 90.0


class TestAggregation:
    def test_ignores_below_min_pitches(self):
        # 40 pitches total across two pitch types — < _MIN_TOTAL_PITCHES=100
        # so pitcher shouldn't appear in output.
        rows = [
            {
                "player_id": "999", "last_name, first_name": "Test, Guy",
                "team_name_alt": "TEST", "pitch_type": "FF",
                "pitch_name": "4-Seam", "pitches": "20",
                "pitch_usage": "50", "run_value_per_100": "0",
                "est_woba": "0.320", "whiff_percent": "20",
                "k_percent": "20", "hard_hit_percent": "35",
                "put_away": "18",
            },
            {
                "player_id": "999", "last_name, first_name": "Test, Guy",
                "team_name_alt": "TEST", "pitch_type": "SL",
                "pitch_name": "Slider", "pitches": "20",
                "pitch_usage": "50", "run_value_per_100": "0",
                "est_woba": "0.320", "whiff_percent": "20",
                "k_percent": "20", "hard_hit_percent": "35",
                "put_away": "18",
            },
        ]
        docs = _aggregate_arsenal(rows, 2025)
        assert docs == []

    def test_composite_pitching_plus_60_40(self):
        # Pitcher with 800 pitches total, elite Stuff+/avg Location+.
        # RV/100 = -1.0 (Stuff+ ≈ 110), xwOBA = 0.320 (Loc+ = 100).
        # Pitching+ = 110*0.6 + 100*0.4 = 106.
        rows = [
            {
                "player_id": "A", "last_name, first_name": "Ace, Elite",
                "team_name_alt": "TEST", "pitch_type": "FF",
                "pitch_name": "4-Seam", "pitches": "800",
                "pitch_usage": "60", "run_value_per_100": "-1.0",
                "est_woba": "0.320", "whiff_percent": "25",
                "k_percent": "28", "hard_hit_percent": "40",
                "put_away": "22",
            },
            {
                "player_id": "A", "last_name, first_name": "Ace, Elite",
                "team_name_alt": "TEST", "pitch_type": "SL",
                "pitch_name": "Slider", "pitches": "400",
                "pitch_usage": "40", "run_value_per_100": "-1.0",
                "est_woba": "0.320", "whiff_percent": "35",
                "k_percent": "34", "hard_hit_percent": "30",
                "put_away": "28",
            },
        ]
        docs = _aggregate_arsenal(rows, 2025)
        assert len(docs) == 1
        doc = docs[0]
        assert doc["name"] == "elite ace"
        assert doc["stuff_plus"] == 110.0
        assert doc["location_plus"] == 100.0
        assert doc["pitching_plus"] == 106.0
        # arsenal sorted by usage descending
        assert doc["arsenal"][0]["pitch_type"] == "FF"

    def test_usage_weighted(self):
        # If most usage is on a bad pitch, composite should reflect that.
        rows = [
            {
                "player_id": "B", "last_name, first_name": "Weak, Bill",
                "team_name_alt": "TST", "pitch_type": "FF",
                "pitch_name": "4-Seam", "pitches": "1000",
                "pitch_usage": "90", "run_value_per_100": "1.5",
                "est_woba": "0.360", "whiff_percent": "12",
                "k_percent": "12", "hard_hit_percent": "50",
                "put_away": "10",
            },
            {
                "player_id": "B", "last_name, first_name": "Weak, Bill",
                "team_name_alt": "TST", "pitch_type": "CU",
                "pitch_name": "Curveball", "pitches": "100",
                "pitch_usage": "10", "run_value_per_100": "-2.0",
                "est_woba": "0.260", "whiff_percent": "35",
                "k_percent": "40", "hard_hit_percent": "20",
                "put_away": "28",
            },
        ]
        docs = _aggregate_arsenal(rows, 2025)
        assert len(docs) == 1
        # Weighted RV = (0.9 * 1.5 + 0.1 * -2.0) = 1.15
        # Stuff+ = 100 - 1.15*10 = 88.5
        assert docs[0]["stuff_plus"] == 88.5
        # Weighted xwOBA = 0.9*0.360 + 0.1*0.260 = 0.350
        # Location+ = 100 + (0.320-0.350)*200 = 94.0
        assert docs[0]["location_plus"] == 94.0


class TestPitcherExtraction:
    def test_strikeouts_pick(self):
        pick = {
            "sport": "MLB",
            "market": "Pitcher Strikeouts - Over/Under",
            "selection": "Kevin Gausman",
        }
        assert _extract_pitcher_from_pick(pick) == "Kevin Gausman"

    def test_outs_recorded(self):
        pick = {
            "sport": "MLB",
            "market": "Outs Recorded Over 17.5",
            "selection": "Framber Valdez",
        }
        assert _extract_pitcher_from_pick(pick) == "Framber Valdez"

    def test_hitter_market_ignored(self):
        pick = {
            "sport": "MLB",
            "market": "Home Runs Over 0.5",
            "selection": "Aaron Judge",
        }
        assert _extract_pitcher_from_pick(pick) is None

    def test_team_market_ignored(self):
        pick = {
            "sport": "MLB",
            "market": "Team Total Over 4.5",
            "selection": "Over",
        }
        assert _extract_pitcher_from_pick(pick) is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
