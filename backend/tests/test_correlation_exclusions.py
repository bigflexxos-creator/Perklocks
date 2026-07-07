"""Tests for Correlation Lab exclusions (2026-07-07).

The user reported the Lab was suggesting "Strikeouts + Pitcher Outs"
for the same pitcher — those two markets settle off the same
box-score line so they cannot be parlayed. These tests lock in the
`_is_derived_same_player` filter.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lab_routes import _is_derived_same_player, _prettify_leg  # noqa: E402


# ── Same-player derived-market exclusion ──────────────────────────
class TestDerivedSamePlayer:
    def test_same_pitcher_strikeouts_and_outs_blocked(self):
        assert _is_derived_same_player(
            "Zack Wheeler", "MLB_KS",
            "Zack Wheeler", "MLB_OUTS",
        ) is True

    def test_same_pitcher_walks_and_earned_runs_blocked(self):
        assert _is_derived_same_player(
            "Cristopher Sanchez", "MLB_BB",
            "Cristopher Sanchez", "MLB_ER",
        ) is True

    def test_same_batter_hr_and_hits_blocked(self):
        assert _is_derived_same_player(
            "Aaron Judge", "MLB_HR",
            "Aaron Judge", "MLB_HITS",
        ) is True

    def test_same_batter_hr_and_tb_blocked(self):
        # A HR is a total base — mutually implied.
        assert _is_derived_same_player(
            "Shohei Ohtani", "MLB_HR",
            "Shohei Ohtani", "MLB_TB",
        ) is True

    def test_same_nba_player_pts_and_reb_blocked(self):
        # Same player volume props share game-time exposure.
        assert _is_derived_same_player(
            "Nikola Jokic", "NBA_POINTS",
            "Nikola Jokic", "NBA_REB",
        ) is True

    def test_different_players_same_market_allowed(self):
        # Two DIFFERENT batters both hitting HRs = fine (SGP staple).
        assert _is_derived_same_player(
            "Aaron Judge", "MLB_HR",
            "Juan Soto", "MLB_HR",
        ) is False

    def test_same_player_different_family_group_allowed(self):
        # A batter's HR and his TEAMMATE's HR aren't same-player, so
        # they don't collide.  But also, a batter's HR and a team ML
        # for his team is a different category so no collision.
        assert _is_derived_same_player(
            "Aaron Judge", "MLB_HR",
            "Aaron Judge", "MLB_ML",  # ML is team-level, not in HR group
        ) is False

    def test_case_insensitive_and_whitespace_tolerant(self):
        assert _is_derived_same_player(
            "  Zack Wheeler ", "MLB_KS",
            "zack wheeler", "MLB_OUTS",
        ) is True

    def test_empty_subjects_dont_collide(self):
        assert _is_derived_same_player("", "MLB_KS", "", "MLB_OUTS") is False
        assert _is_derived_same_player("Judge", "MLB_HR", "", "MLB_HITS") is False


# ── Prettified leg display ────────────────────────────────────────
class TestPrettifyLeg:
    def test_uses_raw_market_string_when_available(self):
        """When the actual market string has Over/Under, show it —
        that's what the user is really betting."""
        s = _prettify_leg(
            "Bryson Stott", "MLB_HITS",
            market="Bryson Stott (PHI) Over 0.5 Hits",
        )
        assert s == "Bryson Stott Over 0.5 Hits"

    def test_strips_team_code_suffix(self):
        s = _prettify_leg(
            "Aaron Judge", "MLB_HR",
            market="Aaron Judge (NYY) Over 0.5 Home Runs",
        )
        assert "(NYY)" not in s
        assert "Over 0.5 Home Runs" in s

    def test_falls_back_to_family_label_when_market_lacks_direction(self):
        """Team markets (Moneyline) don't have Over/Under — use label."""
        s = _prettify_leg("New York Mets", "MLB_ML", market="Moneyline")
        assert "Moneyline" in s
        assert "New York Mets" in s

    def test_team_subject_placeholder(self):
        s = _prettify_leg("TEAM", "MLB_ML")
        assert "TEAM" not in s
        assert "Moneyline" in s

    def test_soccer_scorer_still_works(self):
        s = _prettify_leg("Harry Kane", "SOC_SCORER")
        assert s == "Harry Kane Anytime Scorer"
