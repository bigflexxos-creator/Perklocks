"""Game-ID resolver — maps (sport, date, home_team, away_team) → provider game ID.

Enrichment services (umpires, lineups) need a stable game identifier
to fetch data from MLB StatsAPI / football-data.org, but the odds-API
picks stored in Mongo don't currently carry those IDs. Rather than
modify the ingestion pipeline (risky, touches many surfaces), we
resolve the ID on-demand from the provider's daily schedule endpoint.

Cached per (sport, date) so a full slate refresh (~20 MLB games,
~30 soccer matches) fires exactly two upstream calls per sport per day.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

import aiohttp

logger = logging.getLogger("lockscore.game_resolver")

_TTL = 30 * 60  # 30-minute daily schedule cache
_MLB_SCHED: dict[str, tuple[float, dict[tuple[str, str], int]]] = {}
_SOC_SCHED: dict[str, tuple[float, dict[tuple[str, str], int]]] = {}

_MLB_SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={date}"
_FD_MATCHES_URL = "https://api.football-data.org/v4/matches?dateFrom={date}&dateTo={date}"


def _norm(s: str) -> str:
    return (s or "").lower().replace(" fc", "").replace("fc ", "").replace(" cf", "").strip()


async def _fetch_mlb_schedule(date: str) -> dict[tuple[str, str], int]:
    """(home_team_lower, away_team_lower) → game_pk."""
    now = time.time()
    cached = _MLB_SCHED.get(date)
    if cached and (now - cached[0]) < _TTL:
        return cached[1]
    url = _MLB_SCHEDULE_URL.format(date=date)
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(url) as r:
                if r.status != 200:
                    _MLB_SCHED[date] = (now, {})
                    return {}
                data = await r.json()
    except Exception as e:
        logger.debug("MLB schedule fetch failed: %s", e)
        _MLB_SCHED[date] = (now, {})
        return {}
    out: dict[tuple[str, str], int] = {}
    for d in data.get("dates") or []:
        for g in d.get("games") or []:
            pk = g.get("gamePk")
            teams = g.get("teams") or {}
            home = _norm(((teams.get("home") or {}).get("team") or {}).get("name"))
            away = _norm(((teams.get("away") or {}).get("team") or {}).get("name"))
            if pk and home and away:
                out[(home, away)] = pk
                # Also stash reverse (some picks flip home/away)
                out[(away, home)] = pk
    _MLB_SCHED[date] = (now, out)
    return out


async def resolve_mlb_game_pk(pick: dict) -> Optional[int]:
    """Return MLB gamePk for a pick, or None if unresolvable."""
    if pick.get("game_pk"):
        try:
            return int(pick["game_pk"])
        except (TypeError, ValueError):
            pass
    date = pick.get("pick_date")
    home = _norm(pick.get("home_team") or "")
    away = _norm(pick.get("away_team") or "")
    if not (date and home and away):
        return None
    sched = await _fetch_mlb_schedule(date)
    return sched.get((home, away))


async def _fetch_soccer_schedule(date: str) -> dict[tuple[str, str], int]:
    key = os.environ.get("FOOTBALL_DATA_ORG_KEY", "").strip()
    if not key:
        return {}
    now = time.time()
    cached = _SOC_SCHED.get(date)
    if cached and (now - cached[0]) < _TTL:
        return cached[1]
    url = _FD_MATCHES_URL.format(date=date)
    headers = {"X-Auth-Token": key}
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(url, headers=headers) as r:
                if r.status != 200:
                    _SOC_SCHED[date] = (now, {})
                    return {}
                data = await r.json()
    except Exception as e:
        logger.debug("football-data schedule fetch failed: %s", e)
        _SOC_SCHED[date] = (now, {})
        return {}
    out: dict[tuple[str, str], int] = {}
    for m in data.get("matches") or []:
        mid = m.get("id")
        home = _norm(((m.get("homeTeam") or {}).get("name")) or "")
        away = _norm(((m.get("awayTeam") or {}).get("name")) or "")
        if mid and home and away:
            out[(home, away)] = mid
            out[(away, home)] = mid
    _SOC_SCHED[date] = (now, out)
    return out


async def resolve_soccer_match_id(pick: dict) -> Optional[int]:
    if pick.get("football_data_match_id"):
        try:
            return int(pick["football_data_match_id"])
        except (TypeError, ValueError):
            pass
    date = pick.get("pick_date")
    home = _norm(pick.get("home_team") or "")
    away = _norm(pick.get("away_team") or "")
    if not (date and home and away):
        return None
    sched = await _fetch_soccer_schedule(date)
    return sched.get((home, away))
