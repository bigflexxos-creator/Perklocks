"""Regression tests for the board_validator (Session 1 validation-first).

Coverage:
  §1 contradiction detection — Over/Under, both-team ML, team-total
  §2 batter-vs-pitcher — same-team, non-probable pitcher
  §3 immutable snapshot — locked payload attached at publish time
  §4 rollover tagging — pinned by on_rollover_at, not live thresholds
  §6 board quality — never publish below floors

Run: python -m pytest backend/tests/test_board_validator.py -q
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from board_validator import (  # noqa: E402
    remove_contradictions,
    validate_batter_pitcher,
    enforce_board_quality,
    apply_immutable_snapshot,
    tag_rollover_picks,
    validate_and_finalize,
)


# ── §1 contradictions ────────────────────────────────────────────────

def test_game_total_over_under_kept_higher_score():
    picks = [
        {"id": "a", "sport": "MLB", "event": "NYY @ BOS",
         "market": "Total Runs Over 8.5", "lock_score": 90},
        {"id": "b", "sport": "MLB", "event": "NYY @ BOS",
         "market": "Total Runs Under 8.5", "lock_score": 82},
    ]
    survivors, stats = remove_contradictions(picks)
    assert len(survivors) == 1
    assert survivors[0]["id"] == "a"
    assert stats["dropped"] == 1


def test_team_total_over_under_same_team_dropped():
    picks = [
        {"id": "a", "sport": "MLB", "event": "NYY @ BOS",
         "market": "Boston Red Sox Team Total Over 4.5", "lock_score": 88},
        {"id": "b", "sport": "MLB", "event": "NYY @ BOS",
         "market": "Boston Red Sox Team Total Under 4.5", "lock_score": 92},
    ]
    survivors, _ = remove_contradictions(picks)
    assert len(survivors) == 1
    assert survivors[0]["id"] == "b"


def test_team_total_different_teams_both_kept():
    picks = [
        {"id": "a", "sport": "MLB", "event": "NYY @ BOS",
         "market": "New York Yankees Team Total Over 4.5", "lock_score": 88},
        {"id": "b", "sport": "MLB", "event": "NYY @ BOS",
         "market": "Boston Red Sox Team Total Over 4.5", "lock_score": 88},
    ]
    survivors, _ = remove_contradictions(picks)
    assert len(survivors) == 2


def test_both_teams_moneyline_dropped():
    picks = [
        {"id": "a", "sport": "MLB", "event": "NYY @ BOS",
         "market": "New York Yankees Moneyline", "lock_score": 88},
        {"id": "b", "sport": "MLB", "event": "NYY @ BOS",
         "market": "Boston Red Sox Moneyline", "lock_score": 91},
    ]
    survivors, _ = remove_contradictions(picks)
    assert len(survivors) == 1
    assert survivors[0]["id"] == "b"


def test_player_over_under_same_prop_dropped():
    picks = [
        {"id": "a", "sport": "MLB", "event": "NYY @ BOS",
         "market": "Rafael Devers (BOS) Over 0.5 Hits", "lock_score": 89,
         "selection": "Rafael Devers"},
        {"id": "b", "sport": "MLB", "event": "NYY @ BOS",
         "market": "Rafael Devers (BOS) Under 0.5 Hits", "lock_score": 76,
         "selection": "Rafael Devers"},
    ]
    survivors, _ = remove_contradictions(picks)
    assert len(survivors) == 1
    assert survivors[0]["id"] == "a"


# ── §2 batter vs pitcher ────────────────────────────────────────────

def test_batter_team_not_in_event_rejected():
    """A Yankees batter's prop can't appear on a Dodgers vs Padres game."""
    picks = [
        {"id": "a", "sport": "MLB", "event": "San Diego Padres @ Los Angeles Dodgers",
         "market": "Aaron Judge (NYY) Over 0.5 Hits", "selection": "Aaron Judge",
         "lock_score": 80, "edge_percent": 5.0, "win_probability": 0.6},
    ]
    survivors, stats = validate_batter_pitcher(picks)
    assert len(survivors) == 0
    assert stats["reasons"].get("player_team_not_in_event") == 1


def test_batter_own_team_pitcher_rejected():
    """Batter can never face a pitcher from their own team."""
    picks = [
        {"id": "a", "sport": "MLB",
         "event": "New York Yankees @ Boston Red Sox",
         "market": "Aaron Judge (NYY) Over 0.5 Hits",
         "selection": "Aaron Judge",
         "opposing_pitcher_team": "New York Yankees"},
    ]
    survivors, stats = validate_batter_pitcher(picks)
    assert len(survivors) == 0
    assert stats["reasons"].get("batter_faces_own_team_pitcher") == 1


def test_non_mlb_ignored():
    picks = [{"id": "a", "sport": "Soccer", "event": "PSG @ Marseille",
              "market": "Mbappé - Anytime Goal Scorer"}]
    survivors, _ = validate_batter_pitcher(picks)
    assert len(survivors) == 1


def test_pitcher_not_probable_rejected():
    picks = [
        {"id": "a", "sport": "MLB",
         "event": "New York Yankees @ Boston Red Sox",
         "market": "Gerrit Cole (NYY) Over 5.5 Strikeouts",
         "selection": "Gerrit Cole",
         "is_probable_pitcher": False},
    ]
    survivors, stats = validate_batter_pitcher(picks)
    assert len(survivors) == 0
    assert stats["reasons"].get("pitcher_not_probable") == 1


# ── §6 board quality ────────────────────────────────────────────────

def test_low_lock_dropped():
    picks = [{"id": "a", "sport": "MLB", "market": "Total Runs Over 8.5",
              "lock_score": 55, "edge_percent": 1.0, "win_probability": 0.6}]
    survivors, stats = enforce_board_quality(picks)
    assert len(survivors) == 0
    assert stats["dropped"] == 1


def test_negative_edge_prop_dropped():
    picks = [{"id": "a", "sport": "MLB",
              "market": "Aaron Judge (NYY) Over 0.5 Hits",
              "lock_score": 75, "edge_percent": -1.0, "win_probability": 0.6}]
    survivors, _ = enforce_board_quality(picks)
    assert len(survivors) == 0


def test_quality_pick_kept():
    picks = [{"id": "a", "sport": "MLB",
              "market": "Aaron Judge (NYY) Over 0.5 Hits",
              "lock_score": 88, "edge_percent": 4.0, "win_probability": 0.65}]
    survivors, _ = enforce_board_quality(picks)
    assert len(survivors) == 1


# ── §3 immutable snapshot ───────────────────────────────────────────

def test_snapshot_attached():
    picks = [{"id": "a", "sport": "MLB", "event": "NYY @ BOS",
              "market": "Total Runs Over 8.5", "book_odds": -110,
              "lock_score": 88, "win_probability": 0.65}]
    picks, stats = apply_immutable_snapshot(picks)
    assert stats["applied"] == 1
    snap = picks[0]["snapshot"]
    assert snap["pick_id"] == "a"
    assert snap["line"] == "8.5"
    assert snap["book_odds"] == -110
    assert snap["published_at"]


def test_snapshot_idempotent():
    picks = [{"id": "a", "sport": "MLB", "market": "Total Runs Over 8.5",
              "snapshot": {"pick_id": "a", "existing": True}}]
    picks, stats = apply_immutable_snapshot(picks)
    assert stats["applied"] == 0
    assert stats["already"] == 1
    assert picks[0]["snapshot"].get("existing") is True


# ── §4 rollover tagging ─────────────────────────────────────────────

def test_rollover_tagged():
    picks = [{"id": "a", "sport": "MLB",
              "lock_score": 96, "win_probability": 0.85, "edge_percent": 5.0}]
    picks, stats = tag_rollover_picks(picks)
    assert stats["tagged"] == 1
    assert picks[0]["on_rollover_at"]


def test_rollover_below_floor_not_tagged():
    picks = [{"id": "a", "sport": "MLB",
              "lock_score": 93, "win_probability": 0.85, "edge_percent": 5.0}]
    picks, stats = tag_rollover_picks(picks)
    assert stats["tagged"] == 0
    assert not picks[0].get("on_rollover_at")


def test_rollover_win_prob_100_scale_accepted():
    """Some pipelines store win_probability as 0-100 instead of 0-1."""
    picks = [{"id": "a", "sport": "MLB",
              "lock_score": 96, "win_probability": 85.0, "edge_percent": 5.0}]
    picks, stats = tag_rollover_picks(picks)
    assert stats["tagged"] == 1


# ── End-to-end orchestrator ────────────────────────────────────────

def test_orchestrator_full_flow():
    picks = [
        # Kept: Cardinals Team Total Over 4.5, high lock
        {"id": "keep_1", "sport": "MLB", "event": "STL @ CHC",
         "market": "St. Louis Cardinals Team Total Over 4.5",
         "lock_score": 96, "edge_percent": 5.0, "win_probability": 0.85,
         "book_odds": -120},
        # Dropped: contradictory Cardinals TT Under
        {"id": "drop_contradict", "sport": "MLB", "event": "STL @ CHC",
         "market": "St. Louis Cardinals Team Total Under 4.5",
         "lock_score": 82, "edge_percent": 2.0, "win_probability": 0.55,
         "book_odds": -110},
        # Dropped: batter on wrong team
        {"id": "drop_wrong_team", "sport": "MLB", "event": "STL @ CHC",
         "market": "Aaron Judge (NYY) Over 0.5 Hits",
         "selection": "Aaron Judge",
         "lock_score": 86, "edge_percent": 4.0, "win_probability": 0.66,
         "book_odds": -110},
        # Dropped: quality below floor
        {"id": "drop_low_quality", "sport": "MLB", "event": "LAD @ SF",
         "market": "Total Runs Over 9.5",
         "lock_score": 55, "edge_percent": -1.0, "win_probability": 0.5,
         "book_odds": -110},
    ]
    survivors, report = validate_and_finalize(picks)
    assert len(survivors) == 1
    assert survivors[0]["id"] == "keep_1"
    assert survivors[0].get("on_rollover_at")
    assert survivors[0].get("snapshot")
    assert report["contradictions"]["dropped"] == 1
    assert report["batter_pitcher"]["dropped"] == 1
    assert report["board_quality"]["dropped"] == 1
