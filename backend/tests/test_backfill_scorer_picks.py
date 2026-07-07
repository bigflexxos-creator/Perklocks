"""Regression tests for the goal-scorer backfill v2.

These tests guard against the Jun-2026 bug where Kane was being
credited with "New England Revolution" MLS losses because the alias
"England" was substring-matching "newenglandrevolution".
"""
import sys
from pathlib import Path

# Allow importing the script directly.
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from scripts.backfill_scorer_picks import (
    _team_matches_alias,
    _name_match,
    _rosters_from_summary,
)


# ── Team alias matching ────────────────────────────────────────────
class TestTeamMatchesAlias:
    """Single-word aliases must be EXACT — no substring match."""

    def test_single_word_alias_exact_match(self):
        assert _team_matches_alias("England", "England") is True
        assert _team_matches_alias("France", "France") is True
        assert _team_matches_alias("Brazil", "Brazil") is True

    def test_single_word_alias_no_substring_false_positive(self):
        """The critical bug: 'England' must NOT match 'New England Revolution'."""
        assert _team_matches_alias("New England Revolution", "England") is False
        assert _team_matches_alias("England U21", "England") is False
        assert _team_matches_alias("England Women", "England") is False
        assert _team_matches_alias("Île-de-France", "France") is False

    def test_multi_word_alias_token_subset(self):
        assert _team_matches_alias("Bayern Munich", "Bayern Munich") is True
        assert _team_matches_alias("FC Bayern Munich", "Bayern Munich") is True
        assert _team_matches_alias("Real Madrid", "Real Madrid") is True
        assert _team_matches_alias("Inter Miami CF", "Inter Miami") is True

    def test_multi_word_alias_no_partial_match(self):
        assert _team_matches_alias("Real Sociedad", "Real Madrid") is False
        assert _team_matches_alias("Manchester United", "Manchester City") is False
        assert _team_matches_alias("Bayer Leverkusen", "Bayern Munich") is False

    def test_accent_stripping(self):
        assert _team_matches_alias("FC Bayern München", "Bayern Munchen") is True
        assert _team_matches_alias("Al-Nassr", "Al Nassr") is True


# ── Roster extraction ─────────────────────────────────────────────
class TestRostersFromSummary:
    def test_reads_rosters_path(self):
        summary = {
            "rosters": [
                {"roster": [
                    {"athlete": {"displayName": "Harry Kane"}},
                    {"athlete": {"displayName": "Kyle Walker"}},
                ]},
                {"roster": [
                    {"athlete": {"displayName": "Erling Haaland"}},
                ]},
            ]
        }
        names = _rosters_from_summary(summary)
        assert "Harry Kane" in names
        assert "Kyle Walker" in names
        assert "Erling Haaland" in names

    def test_empty_summary(self):
        assert _rosters_from_summary({}) == []
        assert _rosters_from_summary({"rosters": []}) == []
        assert _rosters_from_summary({"rosters": [{"roster": []}]}) == []


# ── Player name matching ──────────────────────────────────────────
class TestNameMatch:
    def test_last_name_match(self):
        # ESPN keyEvents returns full names, so we test with that.
        assert _name_match("Harry Kane", ["Harry Kane", "Marcus Rashford"]) is True
        assert _name_match("Erling Haaland", ["Erling Haaland"]) is True
        # Long surname fallback works (Rashford ≥ 5 chars).
        assert _name_match("Marcus Rashford", ["M. Rashford"]) is True

    def test_exact_full_name(self):
        assert _name_match("Erling Haaland", ["Erling Braut Haaland"]) is True

    def test_no_match(self):
        assert _name_match("Harry Kane", ["Kylian Mbappe", "Vinicius Junior"]) is False
        assert _name_match("Harry Kane", []) is False
