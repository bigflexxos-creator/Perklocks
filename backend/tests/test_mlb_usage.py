"""Unit tests for services/mlb_usage.py — Phase 1.3 + 1.5 sanity.

Pure-logic tests only; the HTTP-based fetchers are integration-tested
manually via the CLI script in the module docstring."""
from __future__ import annotations

import pytest

from services.mlb_usage import (
    _classify_pitcher_fatigue,
    _extract_hitter_name,
    _extract_pitcher_from_market,
    _is_hitter_market,
    _pa_for_slot,
)


class TestPaForSlot:
    def test_leadoff_gets_most_pa(self):
        assert _pa_for_slot(1) == 4.65

    def test_bottom_of_order_least(self):
        assert _pa_for_slot(9) == 3.65

    def test_monotonic(self):
        # PA strictly decreases from slot 1 through slot 9
        pas = [_pa_for_slot(i) for i in range(1, 10)]
        for i in range(len(pas) - 1):
            assert pas[i] > pas[i + 1]

    def test_none(self):
        assert _pa_for_slot(None) is None
        assert _pa_for_slot(10) is None
        assert _pa_for_slot(0) is None


class TestClassifyPitcherFatigue:
    def test_fresh(self):
        assert _classify_pitcher_fatigue(7, 0) == "fresh"
        assert _classify_pitcher_fatigue(6, 0) == "fresh"

    def test_normal_starter(self):
        assert _classify_pitcher_fatigue(5, 0) == "normal"

    def test_short_rest_starter(self):
        assert _classify_pitcher_fatigue(4, 0) == "tired"

    def test_emergency_starter(self):
        assert _classify_pitcher_fatigue(3, 0) == "tired"

    def test_reliever_gassed(self):
        assert _classify_pitcher_fatigue(1, 60) == "gassed"

    def test_reliever_tired(self):
        assert _classify_pitcher_fatigue(2, 42) == "tired"

    def test_all_none(self):
        assert _classify_pitcher_fatigue(None, None) is None


class TestIsHitterMarket:
    def test_hits_prop(self):
        assert _is_hitter_market(
            {"market": "Player Over 0.5 Hits", "selection": "Trea Turner"}
        )

    def test_hr_prop(self):
        assert _is_hitter_market(
            {"market": "Anytime Home Run", "selection": "Aaron Judge"}
        )

    def test_team_total_rejected(self):
        assert not _is_hitter_market(
            {"market": "American League Team Total Over 4.5", "selection": "Over"}
        )

    def test_spread_rejected(self):
        assert not _is_hitter_market(
            {"market": "American League +1.5 Spread", "selection": "American League"}
        )

    def test_moneyline_rejected(self):
        assert not _is_hitter_market(
            {"market": "Yankees Moneyline", "selection": "Yankees"}
        )

    def test_over_under_only(self):
        # Selection is "Over" without a hitter name → not a hitter prop
        assert not _is_hitter_market(
            {"market": "Player Over 0.5 Hits", "selection": "Over"}
        )


class TestExtractPitcherFromMarket:
    def test_pitcher_k(self):
        assert _extract_pitcher_from_market(
            {"market": "Player Over 5.5 Strikeouts", "selection": "Zack Wheeler"}
        ) == "Zack Wheeler"

    def test_outs_recorded(self):
        assert _extract_pitcher_from_market(
            {"market": "Over 17.5 Outs Recorded", "selection": "Zack Wheeler"}
        ) == "Zack Wheeler"

    def test_hitter_prop_ignored(self):
        assert _extract_pitcher_from_market(
            {"market": "Player Over 0.5 Hits", "selection": "Aaron Judge"}
        ) is None


class TestExtractHitterName:
    def test_returns_selection_for_hitter_market(self):
        assert _extract_hitter_name(
            {"market": "Over 0.5 Hits", "selection": "Trea Turner"}
        ) == "Trea Turner"

    def test_returns_none_for_team_market(self):
        assert _extract_hitter_name(
            {"market": "Team Total Over 4.5", "selection": "Yankees"}
        ) is None

    def test_returns_none_for_pitcher_market(self):
        assert _extract_hitter_name(
            {"market": "Strikeouts Over 5.5", "selection": "Zack Wheeler"}
        ) is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
