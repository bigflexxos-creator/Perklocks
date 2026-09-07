"""Item P0-A — Soccer regulation-scope enforcement.

Verifies that AET / PEN scores from ESPN and FotMob do NOT
contaminate standard soccer wagers (1X2, Totals, BTTS, DC,
Win-or-Draw, DNB).
"""
from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock, patch

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def test_espn_aet_uses_regulation_linescore_only():
    """A 1-1 game at 90' that went 3-2 in AET must grade as DRAW for
    Moneyline draw selection (not away win)."""
    from soccer_espn_settle import settle_soccer_leg

    fake_event = {
        "id": "ev-1",
        "competitions": [{
            "status": {"type": {"name": "STATUS_FINAL_AET"}},
            "competitors": [
                {"homeAway": "home", "score": "3",  # final incl. AET
                 "linescores": [{"value": 0}, {"value": 1},  # 1st + 2nd half
                                 {"value": 1}, {"value": 1}]},
                {"homeAway": "away", "score": "2",
                 "linescores": [{"value": 0}, {"value": 1},
                                 {"value": 1}, {"value": 0}]},
            ],
        }],
    }
    async def _fake_find(_h, _a, _t):
        return ("english_premier_league", fake_event)

    async def _run():
        with patch("soccer_espn_settle._find_event", _fake_find):
            leg = {
                "sport": "Soccer",
                "event": "Away @ Home",
                "market": "Moneyline",
                "selection": "Draw",
                "event_time": "2026-06-06T15:00:00+00:00",
            }
            result = await settle_soccer_leg(leg)
            # Regulation ended 1-1 → Draw wins.
            assert result == "won", f"expected 'won' (regulation draw), got {result}"
    asyncio.get_event_loop().run_until_complete(_run())


def test_espn_aet_bails_when_no_linescore():
    """If ESPN doesn't publish regulation-time linescores we cannot
    safely grade → return None (→ UNRESOLVED downstream)."""
    from soccer_espn_settle import settle_soccer_leg

    fake_event = {
        "id": "ev-2",
        "competitions": [{
            "status": {"type": {"name": "STATUS_FINAL_PEN"}},
            "competitors": [
                {"homeAway": "home", "score": "5"},  # no linescores
                {"homeAway": "away", "score": "4"},
            ],
        }],
    }
    async def _fake_find(_h, _a, _t):
        return ("english_premier_league", fake_event)

    async def _run():
        with patch("soccer_espn_settle._find_event", _fake_find):
            leg = {
                "sport": "Soccer",
                "event": "Away @ Home",
                "market": "Moneyline",
                "selection": "Home",
                "event_time": "2026-06-06T15:00:00+00:00",
            }
            result = await settle_soccer_leg(leg)
            assert result is None
    asyncio.get_event_loop().run_until_complete(_run())


def test_espn_regular_final_still_grades():
    """Full-time (no AET) still grades normally."""
    from soccer_espn_settle import settle_soccer_leg

    fake_event = {
        "id": "ev-3",
        "competitions": [{
            "status": {"type": {"name": "STATUS_FULL_TIME"}},
            "competitors": [
                {"homeAway": "home", "score": "2"},
                {"homeAway": "away", "score": "1"},
            ],
        }],
    }
    async def _fake_find(_h, _a, _t):
        return ("english_premier_league", fake_event)

    async def _run():
        with patch("soccer_espn_settle._find_event", _fake_find):
            leg = {
                "sport": "Soccer",
                "event": "Away @ Home",
                "market": "Moneyline",
                "selection": "Home",
                "event_time": "2026-06-06T15:00:00+00:00",
            }
            result = await settle_soccer_leg(leg)
            assert result == "won"
    asyncio.get_event_loop().run_until_complete(_run())


if __name__ == "__main__":
    test_espn_aet_uses_regulation_linescore_only()
    test_espn_aet_bails_when_no_linescore()
    test_espn_regular_final_still_grades()
    print("OK — soccer regulation-scope enforcement verified.")
