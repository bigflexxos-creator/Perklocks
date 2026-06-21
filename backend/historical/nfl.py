"""NFL historical client — uses ESPN's free public scoreboard API.

No key required. Endpoints:
  • Scoreboard:    https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard
  • Box score:     https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary?event={id}

We ingest the CURRENT NFL season only (Sept → Feb). For each completed
game we store team result and per-player stats from the boxscore.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

logger = logging.getLogger("lockscore.historical.nfl")

_BASE = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
_TIMEOUT = 25.0
_PACE = 0.4  # 2.5 req/sec

_now = datetime.now(timezone.utc)
# NFL season starts in September. "Current season" = year season starts.
_CURRENT_SEASON = _now.year if _now.month >= 8 else _now.year - 1


async def _get(cx: httpx.AsyncClient, path: str, params: dict | None = None) -> dict | None:
    backoff = 1.0
    for attempt in range(1, 4):
        try:
            r = await cx.get(f"{_BASE}{path}", params=params or {})
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                await asyncio.sleep(min(backoff, 30))
                backoff *= 2
                continue
            logger.warning("NFL %s → %s", path, r.status_code)
            return None
        except Exception as e:
            logger.warning("NFL %s exception (attempt %d): %s", path, attempt, e)
            await asyncio.sleep(min(backoff, 10))
            backoff *= 2
    return None


async def backfill_current_season(db) -> dict:
    """Walk the scoreboard week-by-week for the current season."""
    games_seen = games_inserted = logs_inserted = 0
    errors: list[str] = []

    max_weeks = int(os.environ.get("HIST_NFL_MAX_WEEKS", "22"))
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers={"User-Agent": "PerksLocks/1.0"}) as cx:
        # season-type: 1=preseason, 2=regular, 3=postseason
        for season_type, weeks in [(2, range(1, 19)), (3, range(1, 6))]:
            for wk in weeks:
                if games_inserted >= max_weeks * 16:
                    break
                data = await _get(cx, "/scoreboard",
                                  {"seasontype": season_type, "week": wk, "year": _CURRENT_SEASON})
                await asyncio.sleep(_PACE)
                if not data:
                    continue
                for ev in data.get("events", []):
                    games_seen += 1
                    status = (((ev.get("status") or {}).get("type") or {}).get("completed")) or False
                    if not status:
                        continue
                    gid = ev.get("id")
                    competition = (ev.get("competitions") or [{}])[0]
                    comps = competition.get("competitors") or []
                    home = next((c for c in comps if c.get("homeAway") == "home"), {})
                    away = next((c for c in comps if c.get("homeAway") == "away"), {})
                    await db.games.update_one(
                        {"game_id": f"espn_{gid}", "sport": "nfl"},
                        {"$set": {
                            "sport": "nfl",
                            "date": ev.get("date"),
                            "home": (home.get("team") or {}).get("displayName"),
                            "away": (away.get("team") or {}).get("displayName"),
                            "result": {
                                "home": _safe_int(home.get("score")),
                                "away": _safe_int(away.get("score")),
                            },
                            "status": "Final",
                            "season": _CURRENT_SEASON,
                            "week": wk,
                        }},
                        upsert=True,
                    )
                    games_inserted += 1
                    n = await _ingest_summary(cx, db, gid)
                    logs_inserted += n
                    await asyncio.sleep(_PACE)
    return {
        "season": _CURRENT_SEASON,
        "games_seen": games_seen,
        "games_inserted": games_inserted,
        "player_logs_inserted": logs_inserted,
        "errors": errors[:10],
    }


async def incremental_sync(db, since: Optional[datetime] = None) -> dict:
    """For NFL we re-walk last 2 weeks since the league only plays weekly."""
    games_inserted = logs_inserted = 0
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers={"User-Agent": "PerksLocks/1.0"}) as cx:
        data = await _get(cx, "/scoreboard", {"limit": 100})
        for ev in (data or {}).get("events", []):
            status = (((ev.get("status") or {}).get("type") or {}).get("completed")) or False
            if not status:
                continue
            gid = ev.get("id")
            competition = (ev.get("competitions") or [{}])[0]
            comps = competition.get("competitors") or []
            home = next((c for c in comps if c.get("homeAway") == "home"), {})
            away = next((c for c in comps if c.get("homeAway") == "away"), {})
            await db.games.update_one(
                {"game_id": f"espn_{gid}", "sport": "nfl"},
                {"$set": {
                    "sport": "nfl",
                    "date": ev.get("date"),
                    "home": (home.get("team") or {}).get("displayName"),
                    "away": (away.get("team") or {}).get("displayName"),
                    "result": {
                        "home": _safe_int(home.get("score")),
                        "away": _safe_int(away.get("score")),
                    },
                    "status": "Final",
                }},
                upsert=True,
            )
            games_inserted += 1
            n = await _ingest_summary(cx, db, gid)
            logs_inserted += n
            await asyncio.sleep(_PACE)
    return {"games_inserted": games_inserted, "player_logs_inserted": logs_inserted}


def _safe_int(v) -> Optional[int]:
    try:
        return int(v)
    except Exception:
        return None


async def _ingest_summary(cx: httpx.AsyncClient, db, event_id) -> int:
    """Pull the boxscore summary for a single event and store per-player stats."""
    if not event_id:
        return 0
    data = await _get(cx, "/summary", {"event": event_id})
    if not data:
        return 0
    inserted = 0
    boxscore = data.get("boxscore") or {}
    for team_block in boxscore.get("players") or []:
        team_name = (team_block.get("team") or {}).get("displayName")
        for stat_block in team_block.get("statistics") or []:
            stat_name = (stat_block.get("name") or "").lower()  # passing/rushing/receiving/etc
            label_keys = [lbl.lower() for lbl in stat_block.get("labels") or []]
            for athlete in stat_block.get("athletes") or []:
                ath = athlete.get("athlete") or {}
                pid = ath.get("id")
                if not pid:
                    continue
                stats = athlete.get("stats") or []
                # zip labels with stats
                mapped = {k: v for k, v in zip(label_keys, stats)}
                await db.players.update_one(
                    {"player_id": f"espn_{pid}", "sport": "nfl"},
                    {"$set": {
                        "player_id": f"espn_{pid}",
                        "sport": "nfl",
                        "name": ath.get("displayName"),
                        "team": team_name,
                        "position": ((ath.get("position") or {}).get("abbreviation")),
                    }},
                    upsert=True,
                )
                log_doc = {
                    "player_id": f"espn_{pid}",
                    "game_id": f"espn_{event_id}",
                    "sport": "nfl",
                    "name": ath.get("displayName"),
                    "team": team_name,
                    "stat_block": stat_name,
                }
                log_doc.update({f"nfl_{k}": v for k, v in mapped.items()})
                await db.player_game_logs.update_one(
                    {"player_id": f"espn_{pid}", "game_id": f"espn_{event_id}", "stat_block": stat_name},
                    {"$set": log_doc},
                    upsert=True,
                )
                inserted += 1
    return inserted
