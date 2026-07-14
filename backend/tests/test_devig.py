"""Unit tests for services/devig.py — Phase 0.1 sanity."""
from __future__ import annotations

import pytest

from services.devig import (
    american_to_prob,
    devig_pick,
    edge_vs_no_vig,
    fair_from_prob,
    no_vig_three_way,
    no_vig_two_way,
    steam_detected,
)


class TestAmericanToProb:
    def test_even(self):
        assert abs(american_to_prob(100) - 0.5) < 1e-9

    def test_negative(self):
        # -110 → 110/(110+100) = 0.5238
        p = american_to_prob(-110)
        assert p is not None
        assert abs(p - 110 / 210) < 1e-9

    def test_positive(self):
        p = american_to_prob(150)
        assert p is not None
        assert abs(p - 100 / 250) < 1e-9

    def test_zero(self):
        assert american_to_prob(0) is None

    def test_none(self):
        assert american_to_prob(None) is None


class TestFairFromProb:
    def test_favorite(self):
        # 0.60 → −150
        assert fair_from_prob(0.60) == -150

    def test_underdog(self):
        # 0.40 → +150
        assert fair_from_prob(0.40) == 150

    def test_edge_cases(self):
        assert fair_from_prob(0.0) is None
        assert fair_from_prob(1.0) is None
        assert fair_from_prob(None) is None


class TestNoVigTwoWay:
    def test_pick_em(self):
        # -110/-110 has ~4.5% hold; fair is ~50/50
        result = no_vig_two_way(-110, -110)
        assert result is not None
        _, _, pa, pb, hold = result
        assert abs(pa - 0.5) < 1e-3
        assert abs(pb - 0.5) < 1e-3
        assert 4 < hold < 6

    def test_moderate_fav(self):
        # -180/+160 → holder ~2.7pp
        result = no_vig_two_way(-180, 160)
        assert result is not None
        _, _, pa, pb, hold = result
        assert abs((pa + pb) - 1.0) < 1e-6
        assert 2 < hold < 4
        # Favorite should have >50% fair prob
        assert pa > 0.55

    def test_missing_side(self):
        assert no_vig_two_way(-110, None) is None
        assert no_vig_two_way(None, 100) is None


class TestNoVigThreeWay:
    def test_typical_soccer(self):
        # City -180, Draw +320, Liv +450
        result = no_vig_three_way(-180, 320, 450)
        assert result is not None
        _, _, _, ph, pd, pa, hold = result
        assert abs((ph + pd + pa) - 1.0) < 1e-6
        assert ph > pd > pa  # City > Draw > Liv
        # 1X2 markets typically carry 5-8% hold
        assert 4 < hold < 10

    def test_missing_leg(self):
        assert no_vig_three_way(-180, None, 450) is None


class TestDevigPick:
    def test_no_counterpart_falls_back(self):
        p = {"book_odds": -180, "sport": "MLB"}
        devig_pick(p)
        assert p["no_vig_implied_pct"] is not None
        assert p["no_vig_source"] == "proportional_sport_default"
        # Fair implied % should be LOWER than raw (vig removed)
        raw = american_to_prob(-180) * 100
        assert p["no_vig_implied_pct"] < raw

    def test_two_way_with_counterpart(self):
        p = {"book_odds": -180, "counterpart_odds": 160, "sport": "MLB"}
        devig_pick(p)
        assert p["no_vig_source"] == "two_way"
        assert 60 < p["no_vig_implied_pct"] < 65

    def test_three_way_home_pick(self):
        p = {
            "book_odds": -180,
            "three_way_odds": {"home": -180, "draw": 320, "away": 450},
            "selection": "Manchester City",
            "event": "Liverpool @ Manchester City",
            "sport": "Soccer",
        }
        devig_pick(p)
        assert p["no_vig_source"] == "three_way"
        assert 55 < p["no_vig_implied_pct"] < 65

    def test_three_way_draw_pick(self):
        p = {
            "book_odds": 320,
            "three_way_odds": {"home": -180, "draw": 320, "away": 450},
            "selection": "Draw",
            "event": "Liverpool @ Manchester City",
            "sport": "Soccer",
        }
        devig_pick(p)
        assert p["no_vig_source"] == "three_way"
        assert 20 < p["no_vig_implied_pct"] < 28

    def test_missing_book_odds_no_op(self):
        p = {"sport": "MLB"}
        devig_pick(p)
        assert "no_vig_implied_pct" not in p


class TestEdgeVsNoVig:
    def test_positive_edge(self):
        # Model thinks 65%, fair is 60% → +5pp edge
        edge = edge_vs_no_vig(65.0, 60.0)
        assert edge == 5.0

    def test_negative_edge(self):
        edge = edge_vs_no_vig(58.0, 63.0)
        assert edge == -5.0

    def test_missing(self):
        assert edge_vs_no_vig(None, 60.0) is None
        assert edge_vs_no_vig(60.0, 0.0) is None


class TestSteamDetected:
    def test_steam_toward_pick(self):
        # Our book: -150 (60% imp). Pinnacle: -180 (64.3% imp).
        # Sharp is 4.3pp higher → steam toward pick.
        assert steam_detected(-150, -180) is True

    def test_no_steam(self):
        # Our book: -150 (60%). Pinnacle: -155 (60.8%). ~0.8pp gap.
        assert steam_detected(-150, -155, threshold_pct=2.5) is False

    def test_steam_against(self):
        # Our book -180 (64.3%). Pinnacle -150 (60%). Sharp is LOWER →
        # market says we overpaid. Not "steam toward" in our definition.
        assert steam_detected(-180, -150, threshold_pct=2.5) is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
