"""Item P0-A closure — Soccer BTTS / Double Chance / Win-or-Draw
executable settlement branches.

Verifies that the existing settler DOES grade these markets (not
just declares them in the capability registry).
"""
from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import patch

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _fake_event(status: str, hg: int, ag: int):
    return {
        "id": "ev-x",
        "competitions": [{
            "status": {"type": {"name": status}},
            "competitors": [
                {"homeAway": "home", "score": str(hg)},
                {"homeAway": "away", "score": str(ag)},
            ],
        }],
    }


async def _grade(market: str, selection: str, hg: int, ag: int,
                  status: str = "STATUS_FULL_TIME"):
    from soccer_espn_settle import settle_soccer_leg
    async def _find(_h, _a, _t):
        return ("english_premier_league", _fake_event(status, hg, ag))
    with patch("soccer_espn_settle._find_event", _find):
        return await settle_soccer_leg({
            "sport": "Soccer",
            "event": "Away @ Home",
            "market": market,
            "selection": selection,
            "event_time": "2026-06-06T15:00:00+00:00",
        })


def test_btts_yes_hits_when_both_score():
    assert asyncio.get_event_loop().run_until_complete(
        _grade("Both Teams to Score", "Yes", 2, 1)) == "won"


def test_btts_yes_loses_on_clean_sheet():
    assert asyncio.get_event_loop().run_until_complete(
        _grade("Both Teams to Score", "Yes", 3, 0)) == "lost"


def test_btts_no_wins_on_clean_sheet():
    assert asyncio.get_event_loop().run_until_complete(
        _grade("Both Teams to Score", "No", 3, 0)) == "won"


def test_double_chance_home_or_draw_wins_on_draw():
    assert asyncio.get_event_loop().run_until_complete(
        _grade("Double Chance", "Home", 1, 1)) == "won"


def test_double_chance_home_or_draw_loses_on_away_win():
    assert asyncio.get_event_loop().run_until_complete(
        _grade("Double Chance", "Home", 0, 2)) == "lost"


def test_win_or_draw_away_wins_on_draw():
    assert asyncio.get_event_loop().run_until_complete(
        _grade("Win or Draw", "Away", 1, 1)) == "won"


def test_win_or_draw_home_hits_on_home_win():
    assert asyncio.get_event_loop().run_until_complete(
        _grade("Win or Draw", "Home", 3, 1)) == "won"


def test_dnb_pushes_on_draw():
    assert asyncio.get_event_loop().run_until_complete(
        _grade("Draw No Bet", "Home", 1, 1)) == "push"


if __name__ == "__main__":
    tests = [f for f in globals() if f.startswith("test_")]
    for t in tests:
        globals()[t]()
    print(f"OK — {len(tests)} soccer settlement branches executable.")
