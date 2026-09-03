"""PERKLOCKS MAIN 36 · UNIVERSAL IDENTITY GATE FIX (2026-06-30).

Locks in the mandate that every legitimate player-prop pick reaches
the main Locks board — no false ``PLAYER_EVENT_IDENTITY_MISMATCH``
when the event carries numeric team IDs while the player's team was
derived by NAME.

Root cause pre-fix:
    ``_extract_event_participants`` returned ("134", "137") when
    ``home_team_id`` / ``away_team_id`` were present.
    ``_extract_player_team`` (or the market-abbrev derivation) returned
    a NAME like "san francisco".  ``_teams_match("san francisco",
    "134", "137")`` silently returned False → every Lock 95 hitter /
    pitcher pick with real book odds was blocked from the board.

Universal fix:
    ``evaluate_identity`` now compares the player_team against BOTH
    the id-domain tuple AND the name-domain tuple (via new
    ``_extract_event_participant_names``).  A pick is VALID whenever
    EITHER matches — no fabrication, only broadened what "same team"
    already means.
"""
from __future__ import annotations

from services.player_event_identity_gate import (
    IdentityVerdict, evaluate_identity,
    _extract_event_participant_names,
)


def test_mlb_hitter_with_team_ids_and_market_abbrev_is_valid():
    """The Oneil Cruz (PIT) Over 0.5 Hits regression probe — the
    pick doc carries home_team_id / away_team_id (numeric SportDataIO
    ids) AND the market abbrev (PIT).  Must return VALID."""
    pick = {
        "sport": "MLB",
        "market": "Oneil Cruz (PIT) Over 0.5 Hits",
        "selection": "Oneil Cruz",
        "event": "San Francisco Giants @ Pittsburgh Pirates",
        "home_team": "Pittsburgh Pirates",
        "away_team": "San Francisco Giants",
        "home_team_id": 134,   # numeric — was the source of the bug
        "away_team_id": 137,
    }
    assert evaluate_identity(pick) == IdentityVerdict.VALID


def test_mlb_pitcher_away_team_ids_still_valid():
    """Blade Tidwell (SF) Over 3.5 Strikeouts — away-team case."""
    pick = {
        "sport": "MLB",
        "market": "Blade Tidwell (SF) Over 3.5 Strikeouts",
        "selection": "Blade Tidwell",
        "event": "San Francisco Giants @ Pittsburgh Pirates",
        "home_team": "Pittsburgh Pirates",
        "away_team": "San Francisco Giants",
        "home_team_id": 134,
        "away_team_id": 137,
    }
    assert evaluate_identity(pick) == IdentityVerdict.VALID


def test_no_regression_when_only_names_present():
    """Pre-fix name-only path still resolves — the id-tuple degrades
    to name-tuple when ids are absent, and the match still works."""
    pick = {
        "sport": "MLB",
        "market": "Rafael Devers (SF) Over 1.5 Hits + Runs + RBIs",
        "selection": "Rafael Devers",
        "event": "San Francisco Giants @ Pittsburgh Pirates",
        "home_team": "Pittsburgh Pirates",
        "away_team": "San Francisco Giants",
    }
    assert evaluate_identity(pick) == IdentityVerdict.VALID


def test_genuine_mismatch_still_rejected():
    """A pick where the player is genuinely on neither team must
    still be rejected — the universal fix must not over-open the gate."""
    pick = {
        "sport": "MLB",
        "market": "Aaron Judge (NYY) Over 0.5 Hits",
        "selection": "Aaron Judge",
        "event": "San Francisco Giants @ Pittsburgh Pirates",
        "home_team": "Pittsburgh Pirates",
        "away_team": "San Francisco Giants",
        "home_team_id": 134,
        "away_team_id": 137,
    }
    assert evaluate_identity(pick) == IdentityVerdict.PLAYER_EVENT_IDENTITY_MISMATCH


def test_extract_participant_names_helper_returns_display_names():
    pick = {
        "home_team": "Pittsburgh Pirates",
        "away_team": "San Francisco Giants",
        "home_team_id": 134, "away_team_id": 137,
    }
    home, away = _extract_event_participant_names(pick)
    # NAMES not IDs — even when IDs are present.
    assert "pittsburgh" in home or "pirates" in home
    assert "san francisco" in away or "giants" in away


def test_extract_participant_names_fallback_to_event_string():
    """When home/away are missing, parse the "Away @ Home" event."""
    pick = {"event": "Los Angeles Dodgers @ New York Mets"}
    home, away = _extract_event_participant_names(pick)
    assert "mets" in home
    assert "dodgers" in away


def test_participants_unresolvable_still_returns_unresolved():
    pick = {"sport": "MLB", "market": "Somebody Over 0.5 Hits",
             "selection": "Somebody"}
    assert evaluate_identity(pick) == IdentityVerdict.PLAYER_TEAM_UNRESOLVED
