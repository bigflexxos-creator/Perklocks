"""NFL Star Player + 93-99 Root Closure regression suite (2026-06-10).

Verifies:
  * Canonical name key folds sportsbook / ESPN / nflverse variants
  * Different NFL players never collide on the canonical key
  * Alt-edge cap release does not touch team markets or other sports
  * P0-H reachability ceiling matches the expected calibration curve
  * NFL_PROP_LOCK_AUTHORITY=off env flag disables the branch
"""
import asyncio
import os
import pytest


class TestCanonicalNameKey:
    def test_folds_aj_brown_variants(self):
        from services.nfl_feature_engine import _canonical_name_key
        for v in ("A.J. Brown", "AJ Brown", "A J Brown", "A. J. Brown"):
            assert _canonical_name_key(v) == "aj brown", v

    def test_folds_smith_njigba_hyphen(self):
        from services.nfl_feature_engine import _canonical_name_key
        for v in ("Jaxon Smith-Njigba", "Jaxon Smith Njigba"):
            assert _canonical_name_key(v) == "jaxon smith njigba", v

    def test_folds_jamarr_apostrophe(self):
        from services.nfl_feature_engine import _canonical_name_key
        for v in ("Ja'Marr Chase", "JaMarr Chase", "Ja\u2019Marr Chase"):
            assert _canonical_name_key(v) == "jamarr chase", v

    def test_folds_initials_only_when_consecutive_singles(self):
        # "T.J." → "tj" but "Tom J." should NOT fold to "tomj"
        from services.nfl_feature_engine import _canonical_name_key
        assert _canonical_name_key("T.J. Hockenson") == "tj hockenson"
        assert _canonical_name_key("Tom J Smith") == "tom j smith"

    def test_does_not_merge_different_players(self):
        from services.nfl_feature_engine import _canonical_name_key
        assert _canonical_name_key("A.J. Brown") != _canonical_name_key("A.J. Green")
        assert _canonical_name_key("Josh Allen") != _canonical_name_key("Keenan Allen")

    def test_strips_suffixes(self):
        from services.nfl_feature_engine import _canonical_name_key
        assert _canonical_name_key("Michael Pittman Jr.") == "michael pittman"
        assert _canonical_name_key("Amon-Ra St. Brown Jr") == "amon ra st brown"


class TestNflPropLockAuthorityReachability:
    """P0-H — feed correct WP into the safety cap ceiling."""

    def _score(self, wp, edge, *, market="Joe Burrow Over 199.5 Player Pass Yds  · ALT LOCK"):
        from sports_engine import compute_lock_score
        factors = {
            "hit_rate_l5": 0.85, "hit_rate_l3": 0.90,
            "season_avg": 0.75, "matchup": 0.72, "trend": 0.78,
        }
        pick = {
            "sport": "NFL", "market": market,
            "model_win_prob": wp, "edge_percent": edge,
            "book_odds": -500, "is_alt_line": True,
            "canonical_player_id": "00-0036442",
            "evidence_count": 5,
        }
        ls, _ = compute_lock_score(
            factors, win_prob=wp, pick=pick, edge_percent=edge,
        )
        return ls, pick

    def test_wp_80_reaches_92(self):
        ls, _ = self._score(80, 0)
        assert 91.5 <= ls <= 92.5, ls

    def test_wp_875_reaches_95(self):
        ls, _ = self._score(87.5, -1)
        assert 94.5 <= ls <= 95.5, ls

    def test_wp_95_reaches_98(self):
        ls, _ = self._score(95, -0.3)
        assert 97.5 <= ls <= 98.5, ls

    def test_wp_975_reaches_99(self):
        ls, _ = self._score(97.5, -2)
        assert 98.5 <= ls <= 99.0, ls

    def test_edge_alone_does_not_cap_high_wp(self):
        """P0-F: negative/small edge alone cannot demote a legitimate NFL 98/99."""
        ls_neg, _ = self._score(95, -3.0)
        ls_pos, _ = self._score(95, +5.0)
        assert abs(ls_neg - ls_pos) <= 0.5, (ls_neg, ls_pos)

    def test_low_wp_cannot_manufacture_98(self):
        ls, pick = self._score(50, 5)
        assert ls < 80, ls
        assert pick.get("nfl_prop_authority_applied") is False

    def test_team_market_not_affected(self):
        ls, pick = self._score(95, -0.3, market="Detroit Lions Moneyline")
        assert pick.get("nfl_prop_authority_applied") is None
        # Composite score for non-authority path (team ML doesn't get boosted)
        assert ls < 80

    def test_authority_env_flag_disable(self):
        os.environ["NFL_PROP_LOCK_AUTHORITY"] = "off"
        try:
            ls, pick = self._score(95, -0.3)
            assert pick.get("nfl_prop_authority_applied") is None, pick
            assert ls < 80, ls
        finally:
            os.environ.pop("NFL_PROP_LOCK_AUTHORITY", None)


class TestAltEdgeCapReleased:
    def test_orchestrator_no_longer_caps_negative_edge(self):
        src = open("/app/backend/services/pick_refresh_orchestrator.py").read()
        assert "nfl_alt_no_positive_edge_no_elite_authority" not in src, (
            "Old value floor reason string still present — cap not fully released"
        )
        assert "nfl_player_prop_lock_authority_is_hit_probability_not_ev" in src

    def test_integrator_no_longer_caps_negative_edge(self):
        src = open("/app/backend/services/magic/lock_score_integrator.py").read()
        assert "nfl_alt_no_positive_edge:" not in src, (
            "Old integrator cap string still present"
        )
        assert "DEPRECATED VALUE FLOOR" in src
