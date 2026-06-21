"""NHL historical client — uses the FREE public NHL Stats API.

Endpoints (no key required):
  • Schedule:  https://api-web.nhle.com/v1/schedule/{YYYY-MM-DD}
  • Boxscore:  https://api-web.nhle.com/v1/gamecenter/{game_id}/boxscore
  • Season:    https://api-web.nhle.com/v1/club-schedule-season/{team}/{season}
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

logger = logging.getLogger("lockscore.historical.nhl")

_BASE = "https://api-web.nhle.com/v1"
_TIMEOUT = 25.0
_PACE = 0.25  # 4 req/sec

_now = datetime.now(timezone.utc)
# NHL season runs Oct → June. Current season = year season starts (e.g. 2025).
_CURRENT_SEASON = _now.year if _now.month >= 9 else _now.year - 1


async def _get(cx: httpx.AsyncClient, path: str) -> dict | None:
    backoff = 1.0
    for attempt in range(1, 4):
        try:
            r = await cx.get(f"{_BASE}{path}")
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                await asyncio.sleep(min(backoff, 30))
                backoff *= 2
                continue
            logger.warning("NHL %s → %s", path, r.status_code)
            return None
        except Exception as e:
            logger.warning("NHL %s exception (attempt %d): %s", path, attempt, e)
            await asyncio.sleep(min(backoff, 10))
            backoff *= 2
    return None


async def backfill_current_season(db) -> dict:
    """Walk schedule day-by-day from season start to today."""
    today = datetime.now(timezone.utc).date()
    # Approx season start: Oct 5
    season_start = datetime(_CURRENT_SEASON, 10, 5).date()
    if season_start > today:
        # We're in off-season pre-Oct → use last completed season.
        season_start = datetime(_CURRENT_SEASON - 1, 10, 5).date()
    games_seen = games_inserted = logs_inserted = 0
    errors: list[str] = []

    async with httpx.AsyncClient(timeout=_TIMEOUT, headers={"User-Agent": "PerksLocks/1.0"}) as cx:
        d = season_start
        while d <= today:
            data = await _get(cx, f"/schedule/{d.strftime('%Y-%m-%d')}")
            await asyncio.sleep(_PACE)
            if not data:
                d += timedelta(days=1)
                continue
            for week_block in data.get("gameWeek", []) or []:
                for g in week_block.get("games", []):
                    games_seen += 1
                    if (g.get("gameState") or "").upper() not in ("OFF", "FINAL"):
                        continue
                    gid = g.get("id")
                    if not gid:
                        continue
                    home = g.get("homeTeam") or {}
                    away = g.get("awayTeam") or {}
                    await db.games.update_one(
                        {"game_id": f"nhl_{gid}", "sport": "nhl"},
                        {"$set": {
                            "sport": "nhl",
                            "date": g.get("gameDate"),
                            "home": (home.get("placeName") or {}).get("default") if isinstance(home.get("placeName"), dict) else home.get("name"),
                            "away": (away.get("placeName") or {}).get("default") if isinstance(away.get("placeName"), dict) else away.get("name"),
                            "result": {"home": home.get("score"), "away": away.get("score")},
                            "status": "Final",
                        }},
                        upsert=True,
                    )
                    games_inserted += 1
                    n = await _ingest_boxscore(cx, db, gid)
                    logs_inserted += n
                    await asyncio.sleep(_PACE)
            d += timedelta(days=1)
    return {
        "season": _CURRENT_SEASON,
        "games_seen": games_seen,
        "games_inserted": games_inserted,
        "player_logs_inserted": logs_inserted,
        "errors": errors[:10],
    }


async def incremental_sync(db, since: Optional[datetime] = None) -> dict:
    today = datetime.now(timezone.utc).date()
    start = (since.date() if since else today - timedelta(days=3))
    games_inserted = logs_inserted = 0
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers={"User-Agent": "PerksLocks/1.0"}) as cx:
        d = start
        while d <= today:
            data = await _get(cx, f"/schedule/{d.strftime('%Y-%m-%d')}")
            await asyncio.sleep(_PACE)
            if not data:
                d += timedelta(days=1)
                continue
            for week_block in data.get("gameWeek", []) or []:
                for g in week_block.get("games", []):
                    if (g.get("gameState") or "").upper() not in ("OFF", "FINAL"):
                        continue
                    gid = g.get("id")
                    if not gid:
                        continue
                    home = g.get("homeTeam") or {}
                    away = g.get("awayTeam") or {}
                    await db.games.update_one(
                        {"game_id": f"nhl_{gid}", "sport": "nhl"},
                        {"$set": {
                            "sport": "nhl",
                            "date": g.get("gameDate"),
                            "result": {"home": home.get("score"), "away": away.get("score")},
                            "status": "Final",
                        }},
                        upsert=True,
                    )
                    games_inserted += 1
                    n = await _ingest_boxscore(cx, db, gid)
                    logs_inserted += n
                    await asyncio.sleep(_PACE)
            d += timedelta(days=1)
    return {"games_inserted": games_inserted, "player_logs_inserted": logs_inserted}


async def _ingest_boxscore(cx: httpx.AsyncClient, db, game_id) -> int:
    """Pull a single boxscore and store per-player skater + goalie logs."""
    data = await _get(cx, f"/gamecenter/{game_id}/boxscore")
    if not data:
        return 0
    inserted = 0
    # NHL boxscore v1 has playerByGameStats.{awayTeam,homeTeam}.{forwards,defense,goalies}
    pbgs = (data.get("playerByGameStats") or {})
    for side_key in ("awayTeam", "homeTeam"):
        side = pbgs.get(side_key) or {}
        team_name = ((data.get(side_key.replace("Team", "")) or {}).get("name") or {}).get("default") if isinstance((data.get(side_key.replace("Team", "")) or {}).get("name"), dict) else None
        for group in ("forwards", "defense", "goalies"):
            for p in side.get(group) or []:
                pid = p.get("playerId")
                if not pid:
                    continue
                full_name = (p.get("name") or {}).get("default") if isinstance(p.get("name"), dict) else p.get("name")
                await db.players.update_one(
                    {"player_id": f"nhl_{pid}", "sport": "nhl"},
                    {"$set": {
                        "player_id": f"nhl_{pid}",
                        "sport": "nhl",
                        "name": full_name,
                        "team": team_name,
                        "position": p.get("position"),
                    }},
                    upsert=True,
                )
                log = {
                    "player_id": f"nhl_{pid}",
                    "game_id": f"nhl_{game_id}",
                    "sport": "nhl",
                    "name": full_name,
                    "team": team_name,
                    "position": p.get("position"),
                }
                if group == "goalies":
                    log.update({
                        "saves": p.get("saves"),
                        "shots_against": p.get("shotsAgainst"),
                        "save_pct": p.get("savePctg"),
                        "goals_against": p.get("goalsAgainst"),
                        "toi": p.get("toi"),
                    })
                else:
                    log.update({
                        "goals": p.get("goals"),
                        "assists": p.get("assists"),
                        "points": p.get("points"),
                        "shots": p.get("shots"),
                        "hits": p.get("hits"),
                        "blocked_shots": p.get("blockedShots"),
                        "toi": p.get("toi"),
                        "plus_minus": p.get("plusMinus"),
                    })
                await db.player_game_logs.update_one(
                    {"player_id": f"nhl_{pid}", "game_id": f"nhl_{game_id}"},
                    {"$set": log},
                    upsert=True,
                )
                inserted += 1
    return inserted
