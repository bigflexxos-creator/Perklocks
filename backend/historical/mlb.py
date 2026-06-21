"""MLB historical client — uses the FREE public MLB Stats API.

Endpoint base: https://statsapi.mlb.com/api/v1
No API key required. No rate limit documented but we self-limit to 5 req/s.

This is the simplest of the per-sport clients because the MLB Stats API
gives us schedule + boxscore + player stats in one place with stable IDs.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

logger = logging.getLogger("lockscore.historical.mlb")

_BASE = "https://statsapi.mlb.com/api/v1"
_TIMEOUT = 25.0
_PACE = 0.2  # seconds between requests (5 req/s ceiling)

# Current MLB season — bumped via env or hardcoded annually.
# Backfill ONLY hits this season's games per the low-cost spec.
_CURRENT_SEASON = 2026


async def _get(cx: httpx.AsyncClient, path: str, params: dict | None = None) -> dict | None:
    try:
        r = await cx.get(f"{_BASE}{path}", params=params or {})
        if r.status_code == 200:
            return r.json()
        logger.warning("MLB %s → %s", path, r.status_code)
    except Exception as e:
        logger.warning("MLB %s exception: %s", path, e)
    return None


async def backfill_current_season(db) -> dict:
    """Walk the season schedule day-by-day, upserting completed games
    and boxscore stats. Skips games not yet Final.

    Idempotent: re-runs only fetch boxscores for games we don't already
    have logged as Final."""
    today = datetime.now(timezone.utc).date()
    season_start = datetime(_CURRENT_SEASON, 3, 20).date()  # ~ spring training end
    cutoff = min(today, datetime(_CURRENT_SEASON, 11, 5).date())  # World Series end-ish

    games_seen = games_inserted = logs_inserted = 0

    async with httpx.AsyncClient(timeout=_TIMEOUT, headers={"User-Agent": "PerksLocks/1.0"}) as cx:
        d = season_start
        while d <= cutoff:
            data = await _get(cx, "/schedule",
                              {"sportId": 1, "date": d.strftime("%Y-%m-%d")})
            await asyncio.sleep(_PACE)
            if data:
                for date_block in data.get("dates", []):
                    for g in date_block.get("games", []):
                        games_seen += 1
                        if (g.get("status") or {}).get("abstractGameState") != "Final":
                            continue
                        # Skip if we already have this game's logs.
                        existing = await db.games.find_one({"game_id": g["gamePk"], "sport": "mlb"})
                        if existing and existing.get("status") == "Final":
                            continue
                        teams = g.get("teams") or {}
                        away = (teams.get("away") or {}).get("team", {}).get("name")
                        home = (teams.get("home") or {}).get("team", {}).get("name")
                        score_a = (teams.get("away") or {}).get("score")
                        score_h = (teams.get("home") or {}).get("score")
                        await db.games.update_one(
                            {"game_id": g["gamePk"], "sport": "mlb"},
                            {"$set": {
                                "sport": "mlb",
                                "date": g.get("gameDate"),
                                "home": home,
                                "away": away,
                                "result": {"away": score_a, "home": score_h},
                                "status": "Final",
                            }},
                            upsert=True,
                        )
                        games_inserted += 1
                        # Pull boxscore for player game logs.
                        n = await _ingest_boxscore(cx, db, g["gamePk"])
                        logs_inserted += n
                        await asyncio.sleep(_PACE)
            d += timedelta(days=1)
    return {
        "season": _CURRENT_SEASON,
        "games_seen": games_seen,
        "games_inserted": games_inserted,
        "player_logs_inserted": logs_inserted,
    }


async def incremental_sync(db, since: Optional[datetime] = None) -> dict:
    """Only fetch days since `since`. If `since` is None, walk last 3 days."""
    today = datetime.now(timezone.utc).date()
    if since is None:
        start = today - timedelta(days=3)
    else:
        start = since.date()
    cutoff = today
    games_inserted = logs_inserted = 0
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers={"User-Agent": "PerksLocks/1.0"}) as cx:
        d = start
        while d <= cutoff:
            data = await _get(cx, "/schedule", {"sportId": 1, "date": d.strftime("%Y-%m-%d")})
            await asyncio.sleep(_PACE)
            for date_block in (data or {}).get("dates", []):
                for g in date_block.get("games", []):
                    if (g.get("status") or {}).get("abstractGameState") != "Final":
                        continue
                    teams = g.get("teams") or {}
                    await db.games.update_one(
                        {"game_id": g["gamePk"], "sport": "mlb"},
                        {"$set": {
                            "sport": "mlb",
                            "date": g.get("gameDate"),
                            "home": (teams.get("home") or {}).get("team", {}).get("name"),
                            "away": (teams.get("away") or {}).get("team", {}).get("name"),
                            "result": {"away": (teams.get("away") or {}).get("score"),
                                       "home": (teams.get("home") or {}).get("score")},
                            "status": "Final",
                        }},
                        upsert=True,
                    )
                    games_inserted += 1
                    n = await _ingest_boxscore(cx, db, g["gamePk"])
                    logs_inserted += n
                    await asyncio.sleep(_PACE)
            d += timedelta(days=1)
    return {"games_inserted": games_inserted, "player_logs_inserted": logs_inserted}


async def _ingest_boxscore(cx: httpx.AsyncClient, db, game_pk: int) -> int:
    """Fetch one boxscore and store per-player batting/pitching logs."""
    data = await _get(cx, f"/game/{game_pk}/boxscore")
    if not data:
        return 0
    inserted = 0
    for side in ("away", "home"):
        team = (data.get("teams") or {}).get(side, {})
        team_name = team.get("team", {}).get("name") or ""
        for _pid, pdata in (team.get("players") or {}).items():
            person = pdata.get("person") or {}
            pid = person.get("id")
            if not pid:
                continue
            stats = pdata.get("stats") or {}
            batting = stats.get("batting") or {}
            pitching = stats.get("pitching") or {}
            if not batting and not pitching:
                continue  # didn't play
            await db.players.update_one(
                {"player_id": pid, "sport": "mlb"},
                {"$set": {
                    "player_id": pid,
                    "sport": "mlb",
                    "team": team_name,
                    "name": person.get("fullName"),
                    "position": (pdata.get("position") or {}).get("abbreviation"),
                }},
                upsert=True,
            )
            log = {
                "player_id": pid,
                "game_id": game_pk,
                "sport": "mlb",
                "date": None,  # filled from games join
                "team": team_name,
                "at_bats": batting.get("atBats"),
                "hits": batting.get("hits"),
                "home_runs": batting.get("homeRuns"),
                "rbi": batting.get("rbi"),
                "strikeouts": batting.get("strikeOuts"),
                "walks": batting.get("baseOnBalls"),
                "total_bases": batting.get("totalBases"),
                # pitching
                "innings_pitched": pitching.get("inningsPitched"),
                "earned_runs": pitching.get("earnedRuns"),
                "pitcher_strikeouts": pitching.get("strikeOuts"),
                "hits_allowed": pitching.get("hits"),
            }
            await db.player_game_logs.update_one(
                {"player_id": pid, "game_id": game_pk},
                {"$set": log},
                upsert=True,
            )
            inserted += 1
    return inserted
