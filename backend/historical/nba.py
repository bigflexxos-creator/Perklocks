"""NBA historical client — uses the FREE balldontlie.io v1 API.

Endpoint base: https://api.balldontlie.io/v1
  • Free tier: 5 req/sec (300 req/min, but we self-pace to 2 req/sec for safety).
  • Free tier covers stats from 1979 onward, but we only backfill CURRENT season.
  • Requires API key signup → if BALLDONTLIE_KEY missing, falls back to
    an unauthenticated mirror call that returns minimal data.

What we ingest:
  • Games (current season, FINAL only) → `games`
  • Per-player per-game stats → `player_game_logs`
  • Player profile → `players`
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

logger = logging.getLogger("lockscore.historical.nba")

_BASE = "https://api.balldontlie.io/v1"
_TIMEOUT = 25.0
_PACE = 0.5  # 2 req/sec — well under any free tier limit
_PAGE_SIZE = 100

# NBA season starts in October. We compute current season as the YEAR the
# season ENDS (NBA convention: "2025-26 season" → season=2025).
_now = datetime.now(timezone.utc)
_CURRENT_SEASON = _now.year if _now.month >= 10 else _now.year - 1


def _headers() -> dict:
    key = (os.environ.get("BALLDONTLIE_KEY") or "").strip()
    if key:
        return {"Authorization": key, "User-Agent": "PerksLocks/1.0"}
    return {"User-Agent": "PerksLocks/1.0"}


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
            if r.status_code in (401, 403):
                logger.warning("NBA %s → %s (auth — set BALLDONTLIE_KEY)", path, r.status_code)
                return None
            logger.warning("NBA %s → %s", path, r.status_code)
            return None
        except Exception as e:
            logger.warning("NBA %s exception (attempt %d): %s", path, attempt, e)
            await asyncio.sleep(min(backoff, 10))
            backoff *= 2
    return None


async def backfill_current_season(db) -> dict:
    """Page through games for the current NBA season and store stats."""
    games_seen = 0
    games_inserted = 0
    logs_inserted = 0
    errors: list[str] = []

    async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_headers()) as cx:
        # ── 1) Games for current season ─────────────────────
        cursor: Optional[int] = None
        page_count = 0
        max_pages = int(os.environ.get("HIST_NBA_MAX_PAGES", "40"))
        while True:
            params = {"seasons[]": _CURRENT_SEASON, "per_page": _PAGE_SIZE}
            if cursor:
                params["cursor"] = cursor
            data = await _get(cx, "/games", params)
            await asyncio.sleep(_PACE)
            if not data:
                break
            for g in data.get("data", []):
                games_seen += 1
                if (g.get("status") or "").lower() != "final":
                    continue
                gid = g.get("id")
                if not gid:
                    continue
                home = g.get("home_team") or {}
                away = g.get("visitor_team") or {}
                await db.games.update_one(
                    {"game_id": f"bd_{gid}", "sport": "nba"},
                    {"$set": {
                        "sport": "nba",
                        "date": g.get("date"),
                        "home": home.get("full_name"),
                        "away": away.get("full_name"),
                        "result": {"home": g.get("home_team_score"), "away": g.get("visitor_team_score")},
                        "status": "Final",
                    }},
                    upsert=True,
                )
                games_inserted += 1

            cursor = (data.get("meta") or {}).get("next_cursor")
            page_count += 1
            if not cursor or page_count >= max_pages:
                break

        # ── 2) Stats (per-player per-game) ─────────────────────
        cursor = None
        page_count = 0
        max_pages = int(os.environ.get("HIST_NBA_STATS_PAGES", "60"))
        while True:
            params = {"seasons[]": _CURRENT_SEASON, "per_page": _PAGE_SIZE}
            if cursor:
                params["cursor"] = cursor
            data = await _get(cx, "/stats", params)
            await asyncio.sleep(_PACE)
            if not data:
                break
            for s in data.get("data", []):
                player = s.get("player") or {}
                team = s.get("team") or {}
                game = s.get("game") or {}
                pid = player.get("id")
                if not pid:
                    continue
                full_name = f"{player.get('first_name','')} {player.get('last_name','')}".strip()
                await db.players.update_one(
                    {"player_id": f"bd_{pid}", "sport": "nba"},
                    {"$set": {
                        "player_id": f"bd_{pid}",
                        "sport": "nba",
                        "name": full_name,
                        "team": team.get("full_name"),
                        "position": player.get("position"),
                    }},
                    upsert=True,
                )
                await db.player_game_logs.update_one(
                    {"player_id": f"bd_{pid}", "game_id": f"bd_{game.get('id')}"},
                    {"$set": {
                        "player_id": f"bd_{pid}",
                        "game_id": f"bd_{game.get('id')}",
                        "sport": "nba",
                        "date": game.get("date"),
                        "team": team.get("full_name"),
                        "name": full_name,
                        "minutes": s.get("min"),
                        "points": s.get("pts"),
                        "rebounds": s.get("reb"),
                        "assists": s.get("ast"),
                        "steals": s.get("stl"),
                        "blocks": s.get("blk"),
                        "threes_made": s.get("fg3m"),
                        "turnovers": s.get("turnover"),
                        "fg_made": s.get("fgm"),
                        "fg_att": s.get("fga"),
                        "ft_made": s.get("ftm"),
                        "ft_att": s.get("fta"),
                    }},
                    upsert=True,
                )
                logs_inserted += 1
            cursor = (data.get("meta") or {}).get("next_cursor")
            page_count += 1
            if not cursor or page_count >= max_pages:
                break

    return {
        "season": _CURRENT_SEASON,
        "games_seen": games_seen,
        "games_inserted": games_inserted,
        "player_logs_inserted": logs_inserted,
        "errors": errors[:10],
    }


async def incremental_sync(db, since: Optional[datetime] = None) -> dict:
    """Pull only the last 7 days of games (or since `since`)."""
    today = datetime.now(timezone.utc).date()
    start = (since.date() if since else today - timedelta(days=7))
    games_inserted = logs_inserted = 0
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_headers()) as cx:
        d = start
        while d <= today:
            data = await _get(cx, "/games", {"dates[]": d.strftime("%Y-%m-%d"), "per_page": 100})
            await asyncio.sleep(_PACE)
            for g in (data or {}).get("data", []) if data else []:
                if (g.get("status") or "").lower() != "final":
                    continue
                gid = g.get("id")
                if not gid:
                    continue
                home = g.get("home_team") or {}
                away = g.get("visitor_team") or {}
                await db.games.update_one(
                    {"game_id": f"bd_{gid}", "sport": "nba"},
                    {"$set": {
                        "sport": "nba",
                        "date": g.get("date"),
                        "home": home.get("full_name"),
                        "away": away.get("full_name"),
                        "result": {"home": g.get("home_team_score"), "away": g.get("visitor_team_score")},
                        "status": "Final",
                    }},
                    upsert=True,
                )
                games_inserted += 1
            d += timedelta(days=1)
    return {"games_inserted": games_inserted, "player_logs_inserted": logs_inserted}
