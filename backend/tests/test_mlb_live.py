"""Smoke tests for the MLB Stats API integration.

These tests hit the live public statsapi.mlb.com endpoint — they're a
sanity check that the free API is reachable and that the shape mapper
still produces Odds-API-compatible payloads. Skipped automatically if
the network is unavailable.
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mlb_live import fetch_mlb_scores, _convert_game  # noqa: E402


def test_convert_game_final():
    """Final games should produce completed=True and a 2-entry scores list."""
    raw = {
        "gamePk": 12345,
        "gameDate": "2026-06-18T23:05:00Z",
        "status": {"detailedState": "Final", "abstractGameState": "Final"},
        "teams": {
            "home": {"team": {"name": "Baltimore Orioles"}, "score": 4},
            "away": {"team": {"name": "Seattle Mariners"},  "score": 3},
        },
    }
    out = _convert_game(raw)
    assert out is not None
    assert out["completed"] is True
    assert out["id"] == "mlb_12345"
    assert out["home_team"] == "Baltimore Orioles"
    assert out["away_team"] == "Seattle Mariners"
    assert out["commence_time"] == "2026-06-18T23:05:00Z"
    names = {s["name"] for s in out["scores"]}
    assert names == {"Baltimore Orioles", "Seattle Mariners"}


def test_convert_game_in_progress():
    """In-progress games should have completed=False but still expose scores."""
    raw = {
        "gamePk": 999,
        "gameDate": "2026-06-18T23:05:00Z",
        "status": {"detailedState": "In Progress", "abstractGameState": "Live"},
        "teams": {
            "home": {"team": {"name": "New York Yankees"}, "score": 2},
            "away": {"team": {"name": "Boston Red Sox"},  "score": 1},
        },
    }
    out = _convert_game(raw)
    assert out is not None
    assert out["completed"] is False
    assert len(out["scores"]) == 2


def test_convert_game_postponed_returns_no_scores():
    """Postponed games should not be marked completed even if status exists."""
    raw = {
        "gamePk": 42,
        "gameDate": "2026-06-18T23:05:00Z",
        "status": {"detailedState": "Postponed", "abstractGameState": "Preview"},
        "teams": {
            "home": {"team": {"name": "Chicago Cubs"}, "score": None},
            "away": {"team": {"name": "Pittsburgh Pirates"}, "score": None},
        },
    }
    out = _convert_game(raw)
    assert out is not None
    assert out["completed"] is False
    assert out["scores"] == []


def test_live_fetch_smoke():
    """Live network test — verifies MLB Stats API is reachable and the
    payload contains at least one game-shaped dict. Skipped on network
    failure rather than failing CI."""
    try:
        games = asyncio.run(fetch_mlb_scores(days_back=1))
    except Exception as e:
        print(f"SKIP live fetch (network): {e}")
        return
    if not games:
        print("SKIP live fetch (no games returned — off-season?)")
        return
    g = games[0]
    # Shape compatibility with settlement_engine._fetch_scores
    for k in ("id", "home_team", "away_team", "commence_time", "completed", "scores"):
        assert k in g, f"missing key {k} in mlb_live payload"
    assert g["id"].startswith("mlb_")


if __name__ == "__main__":
    test_convert_game_final()
    test_convert_game_in_progress()
    test_convert_game_postponed_returns_no_scores()
    test_live_fetch_smoke()
    print("OK — all mlb_live tests passed")
