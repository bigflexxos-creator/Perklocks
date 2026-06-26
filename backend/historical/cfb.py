"""College Football (CFB) historical client — uses ESPN's free public
scoreboard API. Same shape as the NFL client, just a different sport
path. CFB seasons start in late August, so we treat the season key as
the calendar year it STARTS in.

Endpoints (no key required):
  • Scoreboard: https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard
  • Summary:    https://site.api.espn.com/apis/site/v2/sports/football/college-football/summary?event={id}

For CFB we walk weeks 1..16 regular season + bowl games. CFB has 130+
FBS teams playing 12-13 games each, so we cap walls via
HIST_CFB_MAX_WEEKS to keep the initial backfill bounded.

CollegeFootballData.com is optionally used for Returning Production /
Strength of Schedule enrichment (Phase 3+); this Phase-2 client sticks
to the free ESPN feed which doesn't require any key.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger("lockscore.historical.cfb")

_BASE = "https://site.api.espn.com/apis/site/v2/sports/football/college-football"
_TIMEOUT = 25.0
_PACE = 0.5  # 2 req/sec — ESPN is sensitive

_now = datetime.now(timezone.utc)
# CFB season starts late August. Treat "current season" = year season starts.
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
            logger.warning("CFB %s → %s", path, r.status_code)
            return None
        except Exception as e:
            logger.warning("CFB %s exception (attempt %d): %s", path, attempt, e)
            await asyncio.sleep(min(backoff, 10))
            backoff *= 2
    return None


def _safe_int(v) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _to_int(v) -> Optional[int]:
    try:
        if v in (None, ""):
            return None
        return int(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _normalize_cfb_stats(stat_name: str, mapped: dict, log_doc: dict) -> None:
    """Map ESPN per-block labels into normalized prop-engine fields.

    Mirrors the NFL helper — CFB and NFL share the ESPN boxscore shape.
    """
    def _yds_from_label(prefix: str) -> Optional[int]:
        for k in ("yds", "yards", f"{prefix} yds", f"{prefix} yards"):
            if k in mapped:
                v = _to_int(mapped[k])
                if v is not None:
                    return v
        return None

    if stat_name == "passing":
        py = _yds_from_label("passing")
        if py is not None:
            log_doc["passing_yards"] = py
        td = _to_int(mapped.get("td"))
        if td is not None:
            log_doc["passing_tds"] = td
            if td > 0:
                log_doc["any_td"] = 1
    elif stat_name == "rushing":
        ry = _yds_from_label("rushing")
        if ry is not None:
            log_doc["rushing_yards"] = ry
        td = _to_int(mapped.get("td"))
        if td is not None and td > 0:
            log_doc["any_td"] = 1
    elif stat_name == "receiving":
        rcy = _yds_from_label("receiving")
        if rcy is not None:
            log_doc["receiving_yards"] = rcy
        rec = _to_int(mapped.get("rec"))
        if rec is not None:
            log_doc["receptions"] = rec
        td = _to_int(mapped.get("td"))
        if td is not None and td > 0:
            log_doc["any_td"] = 1


async def _ingest_summary(cx: httpx.AsyncClient, db, event_id, season: Optional[int] = None) -> int:
    """Pull boxscore summary for one CFB event and store per-player stats."""
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
            stat_name = (stat_block.get("name") or "").lower()
            label_keys = [lbl.lower() for lbl in stat_block.get("labels") or []]
            for athlete in stat_block.get("athletes") or []:
                ath = athlete.get("athlete") or {}
                pid = ath.get("id")
                if not pid:
                    continue
                stats = athlete.get("stats") or []
                mapped = {k: v for k, v in zip(label_keys, stats)}
                await db.players.update_one(
                    {"player_id": f"espn_cfb_{pid}", "sport": "cfb"},
                    {"$set": {
                        "player_id": f"espn_cfb_{pid}",
                        "sport": "cfb",
                        "name": ath.get("displayName"),
                        "team": team_name,
                        "position": ((ath.get("position") or {}).get("abbreviation")),
                    }},
                    upsert=True,
                )
                log_doc = {
                    "player_id": f"espn_cfb_{pid}",
                    "game_id": f"espn_cfb_{event_id}",
                    "sport": "cfb",
                    "name": ath.get("displayName"),
                    "team": team_name,
                    "stat_block": stat_name,
                }
                if season is not None:
                    log_doc["season"] = int(season)
                log_doc.update({f"cfb_{k}": v for k, v in mapped.items()})
                _normalize_cfb_stats(stat_name, mapped, log_doc)
                await db.player_game_logs.update_one(
                    {"player_id": f"espn_cfb_{pid}", "game_id": f"espn_cfb_{event_id}", "stat_block": stat_name},
                    {"$set": log_doc},
                    upsert=True,
                )
                inserted += 1
    return inserted


async def backfill_season(db, season: int) -> dict:
    """Walk the CFB scoreboard week-by-week for a specific season.

    Pass the calendar year the season STARTS in (2024 = 2024 season).
    """
    season = int(season)
    games_seen = games_inserted = logs_inserted = 0
    errors: list[str] = []

    max_weeks = int(os.environ.get("HIST_CFB_MAX_WEEKS", "20"))
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers={"User-Agent": "PerksLocks/1.0"}) as cx:
        for season_type, weeks in [(2, range(1, 17)), (3, range(1, 6))]:
            for wk in weeks:
                if games_inserted >= max_weeks * 30:
                    break
                data = await _get(cx, "/scoreboard",
                                  {"seasontype": season_type, "week": wk, "year": season,
                                   "groups": 80, "limit": 200})
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
                        {"game_id": f"espn_cfb_{gid}", "sport": "cfb"},
                        {"$set": {
                            "sport": "cfb",
                            "date": ev.get("date"),
                            "home": (home.get("team") or {}).get("displayName"),
                            "away": (away.get("team") or {}).get("displayName"),
                            "result": {
                                "home": _safe_int(home.get("score")),
                                "away": _safe_int(away.get("score")),
                            },
                            "status": "Final",
                            "season": season,
                            "week": wk,
                        }},
                        upsert=True,
                    )
                    games_inserted += 1
                    n = await _ingest_summary(cx, db, gid, season=season)
                    logs_inserted += n
                    await asyncio.sleep(_PACE)
    return {
        "season": season,
        "games_seen": games_seen,
        "games_inserted": games_inserted,
        "player_logs_inserted": logs_inserted,
        "errors": errors[:10],
    }


async def backfill_current_season(db) -> dict:
    """Backward-compatible wrapper — backfills the current CFB season."""
    return await backfill_season(db, _CURRENT_SEASON)


async def incremental_sync(db, since=None) -> dict:
    """Re-walk last 2 CFB weeks (Saturday-heavy schedule)."""
    games_inserted = logs_inserted = 0
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers={"User-Agent": "PerksLocks/1.0"}) as cx:
        data = await _get(cx, "/scoreboard", {"limit": 200})
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
                {"game_id": f"espn_cfb_{gid}", "sport": "cfb"},
                {"$set": {
                    "sport": "cfb",
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
