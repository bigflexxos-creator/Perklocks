"""Regression tests for the NHL live game-log ingestor (2026-10-03).

Covers the shape-contract: NHL API gameLog rows must map to the exact
``player_game_actuals`` schema the NHL Historical Intelligence
adapter reads.  Mirrors the test posture used for the NFL ingestor:
pure unit tests with a mock HTTP response — no live network, no DB.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from services.live_gamelog_ingestor.nhl import (
    _parse_gamelog, _parse_toi, _season_year_from_season_id,
    _row_to_actuals, _nhl_id_from_player,
)


# ────────────────────────────────────────────────────────────────────
# Pure helpers
# ────────────────────────────────────────────────────────────────────

def test_parse_toi_mm_ss_to_seconds():
    assert _parse_toi("19:52") == 19 * 60 + 52
    assert _parse_toi("25:52") == 25 * 60 + 52
    assert _parse_toi("00:00") == 0.0


def test_parse_toi_honest_zero_on_bad_input():
    assert _parse_toi(None) is None
    assert _parse_toi("") is None
    assert _parse_toi("not-a-toi") is None
    assert _parse_toi(1952) is None  # ints aren't acceptable


def test_season_year_from_season_id():
    assert _season_year_from_season_id(20262027) == 2026
    assert _season_year_from_season_id("20242025") == 2024
    assert _season_year_from_season_id(None) is None
    assert _season_year_from_season_id("foo") is None


def test_nhl_id_from_player_prefers_numeric_field():
    assert _nhl_id_from_player({"nhl_id": 8478402}) == 8478402
    assert _nhl_id_from_player({"player_id": "nhl_8478402"}) == 8478402
    assert _nhl_id_from_player({"canonical_player_id": "nhl_8478402"}) == 8478402
    assert _nhl_id_from_player({"player_id": "espn_123"}) is None
    assert _nhl_id_from_player({}) is None


def test_row_to_actuals_passes_through_sog_as_shots_on_goal():
    g = {"goals": 2, "assists": 3, "shots": 4, "toi": "19:52",
         "plusMinus": 2, "pim": 0, "powerPlayGoals": 0}
    a = _row_to_actuals(g)
    assert a["goals"] == 2.0
    assert a["assists"] == 3.0
    assert a["shots_on_goal"] == 4.0
    assert a["shots"] == 4.0
    assert a["toi_seconds"] == 19 * 60 + 52
    assert a["plus_minus"] == 2.0


def test_row_to_actuals_honest_zero_on_missing_fields():
    """Missing stat must be None, not 0.  A genuine 0 (0 goals in a
    full game) must survive as 0."""
    g = {"goals": 0, "assists": None, "shots": "", "toi": None}
    a = _row_to_actuals(g)
    assert a["goals"] == 0.0           # genuine zero preserved
    assert a["assists"] is None        # missing stays missing
    assert a["shots_on_goal"] is None
    assert a["toi_seconds"] is None


# ────────────────────────────────────────────────────────────────────
# _parse_gamelog — real NHL API response shape (verified 2026-10-17)
# ────────────────────────────────────────────────────────────────────

_NHL_SAMPLE = {
    "seasonId": 20262027,
    "gameTypeId": 2,
    "gameLog": [
        {
            "gameId": 2026020133, "teamAbbrev": "EDM",
            "homeRoadFlag": "R", "gameDate": "2026-10-17",
            "opponentAbbrev": "UTA",
            "opponentCommonName": {"default": "Mammoth"},
            "goals": 2, "assists": 3, "points": 5,
            "plusMinus": 4, "powerPlayGoals": 0,
            "shots": 2, "shifts": 25, "pim": 0, "toi": "19:52",
        },
        {
            "gameId": 2026020117, "teamAbbrev": "EDM",
            "homeRoadFlag": "H", "gameDate": "2026-10-15",
            "opponentAbbrev": "UTA",
            "opponentCommonName": {"default": "Mammoth"},
            "goals": 0, "assists": 1, "points": 1, "plusMinus": 1,
            "shots": 2, "shifts": 27, "pim": 0, "toi": "25:52",
        },
    ],
}


def test_parse_gamelog_shape_matches_player_game_actuals_contract():
    out = _parse_gamelog(_NHL_SAMPLE)
    assert len(out) == 2

    first = out["nhl_2026020133"]
    assert first["event_id"] == "nhl_2026020133"
    assert first["canonical_event_id"] == "nhl_2026020133"
    # event_time is ISO-ized by `_iso` — "YYYY-MM-DD" → "YYYY-MM-DDT00:00:00Z"
    assert first["event_time"].startswith("2026-10-17")
    assert first["opponent"] == "Mammoth"
    assert first["opponent_abbrev"] == "UTA"
    assert first["canonical_opponent_id"] == "nhl_team_UTA"
    assert first["home_away"] == "away"   # "R" → away
    assert first["season"] == 2026
    assert first["team_abbrev"] == "EDM"

    actuals = first["actuals"]
    assert actuals["goals"] == 2.0
    assert actuals["assists"] == 3.0
    assert actuals["points"] == 5.0
    assert actuals["shots_on_goal"] == 2.0
    assert actuals["plus_minus"] == 4.0
    assert actuals["toi_seconds"] == 19 * 60 + 52

    # second game: H → home
    second = out["nhl_2026020117"]
    assert second["home_away"] == "home"


def test_parse_gamelog_empty_response_honest_zero():
    assert _parse_gamelog({}) == {}
    assert _parse_gamelog({"seasonId": 20262027, "gameLog": []}) == {}
    # Rows missing gameId are silently skipped (never silently zero-stamped).
    out = _parse_gamelog({"seasonId": 20262027,
                           "gameLog": [{"goals": 1}, {"gameId": 99}]})
    assert len(out) == 1
    assert "nhl_99" in out


# ────────────────────────────────────────────────────────────────────
# End-to-end wiring — ingestor exports the correct runner entry
# ────────────────────────────────────────────────────────────────────

def test_runner_exports_nhl_entrypoint():
    import services.live_gamelog_ingestor as pkg
    assert hasattr(pkg, "refresh_nhl_gamelogs")
    # Universal "refresh_all" iterates all four sports.
    from services.live_gamelog_ingestor import runner
    assert "nhl" in runner.__all__ or hasattr(runner, "refresh_nhl_gamelogs")
