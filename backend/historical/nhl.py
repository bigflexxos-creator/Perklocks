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


async def _authoritative_season_start(cx: httpx.AsyncClient, season: int) -> "datetime.date":
    """2026-10-02 — NHL SEASON DISCOVERY P0.

    Replaces the hard-coded ``Oct 5`` assumption with authoritative
    season boundaries pulled from NHL's own schedule endpoint.  We
    ask the standings-season endpoint for the current season window
    (``start_date`` field is published by NHL in ISO form).  If the
    endpoint is unreachable we fall back to the earliest schedule
    day with any game listed in the ``{season}{season+1}`` cycle
    rather than guessing.
    """
    from datetime import date as _date
    try:
        data = await _get(cx, f"/standings-season")
        for row in (data or {}).get("seasons", []):
            sid = int(row.get("id") or 0)
            # season id is "20252026" etc.
            if sid // 10000 == season:
                sd = row.get("standingsStart") or row.get("regularSeasonStartDate")
                if isinstance(sd, str) and len(sd) >= 10:
                    y, m, d = int(sd[:4]), int(sd[5:7]), int(sd[8:10])
                    return _date(y, m, d)
    except Exception as e:
        logger.warning("NHL authoritative season probe failed: %s", e)
    # Fallback — scan the schedule from Sept 1 forward until the
    # first day with at least one real game.  Avoids blind October
    # guessing while never fabricating a date.
    start = _date(season, 9, 1)
    for i in range(0, 60):
        d = start + timedelta(days=i)
        probe = await _get(cx, f"/schedule/{d.strftime('%Y-%m-%d')}")
        if probe:
            for wk in probe.get("gameWeek", []) or []:
                if wk.get("games"):
                    return d
        await asyncio.sleep(_PACE)
    # Final fallback: Oct 1 of the season year (legitimate for every
    # modern NHL season — never earlier than Sept 20).
    return _date(season, 10, 1)


async def backfill_current_season(db) -> dict:
    """Walk schedule day-by-day from the AUTHORITATIVE season start
    to today.  No hard-coded ``Oct 5`` assumption — the season
    boundary is pulled from NHL's own standings-season endpoint.
    """
    today = datetime.now(timezone.utc).date()
    games_seen = games_inserted = logs_inserted = 0
    errors: list[str] = []

    async with httpx.AsyncClient(timeout=_TIMEOUT, headers={"User-Agent": "PerksLocks/1.0"}) as cx:
        season_start = await _authoritative_season_start(cx, _CURRENT_SEASON)
        if season_start > today:
            # True pre-season — walk the previous NHL year.
            season_start = await _authoritative_season_start(cx, _CURRENT_SEASON - 1)
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
                            "home_team_id": str(home.get("id") or ""),
                            "away_team_id": str(away.get("id") or ""),
                            "home_abbrev":  home.get("abbrev") or "",
                            "away_abbrev":  away.get("abbrev") or "",
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
        "season_start_detected": season_start.isoformat(),
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
                            # 2026-08-23 H2H_DATA_COMPLETION — write
                            # canonical team identity so NHL H2H can
                            # resolve opponent (bug: prior incremental
                            # dropped these fields, breaking NHL H2H).
                            "home": (home.get("placeName") or {}).get("default") if isinstance(home.get("placeName"), dict) else home.get("name"),
                            "away": (away.get("placeName") or {}).get("default") if isinstance(away.get("placeName"), dict) else away.get("name"),
                            "home_team_id": str(home.get("id") or ""),
                            "away_team_id": str(away.get("id") or ""),
                            "home_abbrev":  home.get("abbrev") or "",
                            "away_abbrev":  away.get("abbrev") or "",
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
    # 2026-08-23 H2H_DATA_COMPLETION — extract canonical team ids up front
    # so player logs carry opponent identity for H2H.
    home_block = data.get("homeTeam") or {}
    away_block = data.get("awayTeam") or {}
    home_tid = str(home_block.get("id") or "")
    away_tid = str(away_block.get("id") or "")
    home_name = (home_block.get("placeName") or {}).get("default") if isinstance(home_block.get("placeName"), dict) else home_block.get("name")
    away_name = (away_block.get("placeName") or {}).get("default") if isinstance(away_block.get("placeName"), dict) else away_block.get("name")
    # NHL boxscore v1 has playerByGameStats.{awayTeam,homeTeam}.{forwards,defense,goalies}
    pbgs = (data.get("playerByGameStats") or {})
    for side_key in ("awayTeam", "homeTeam"):
        side = pbgs.get(side_key) or {}
        is_home_side = (side_key == "homeTeam")
        team_name = home_name if is_home_side else away_name
        team_tid  = home_tid  if is_home_side else away_tid
        opp_tid   = away_tid  if is_home_side else home_tid
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
                    "team_id": team_tid,
                    "opp_team_id": opp_tid,
                    "is_home": is_home_side,
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
                        # ── 2026-06-28 · P0-A NHL ingestion fix ──
                        # NHL public API field is ``sog`` (shots on
                        # goal), not ``shots``.  Previously stored a
                        # null for every skater which blocked the
                        # SOG model.  Reading ``sog`` with ``shots``
                        # as a legacy fallback.
                        "shots": p.get("sog", p.get("shots")),
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


async def enrich_player_log_opponents(db) -> dict:
    """One-shot enrichment — populate ``opp_team_id`` / ``team_id`` /
    ``is_home`` on existing ``player_game_logs`` sport='nhl' rows using
    the canonical ``games`` collection (2026-08-23 H2H_DATA_COMPLETION).

    Uses ONLY existing stored data — no external API calls.  Rows whose
    game_id has no games row with team ids remain honestly unresolved.
    """
    enriched = 0
    unresolved = 0
    scanned = 0
    async for log in db.player_game_logs.find(
        {"sport": "nhl",
         "$or": [{"opp_team_id": {"$in": [None, ""]}},
                  {"opp_team_id": {"$exists": False}}]},
        {"_id": 1, "game_id": 1, "team": 1, "name": 1},
    ):
        scanned += 1
        gid = log.get("game_id")
        if not gid:
            unresolved += 1
            continue
        game = await db.games.find_one(
            {"game_id": gid, "sport": "nhl"},
            {"_id": 0, "home": 1, "away": 1,
             "home_team_id": 1, "away_team_id": 1,
             "home_abbrev": 1, "away_abbrev": 1},
        )
        if not game:
            unresolved += 1
            continue
        home_tid = str(game.get("home_team_id") or "")
        away_tid = str(game.get("away_team_id") or "")
        if not (home_tid and away_tid):
            unresolved += 1
            continue
        # Determine which side the player was on.  If ``team`` on the log
        # is the home name/abbrev, opp = away id; else opp = home id.
        team = (log.get("team") or "").strip().lower()
        home_id_match = any(
            team == (v or "").strip().lower()
            for v in (game.get("home"), game.get("home_abbrev"), home_tid)
        )
        away_id_match = any(
            team == (v or "").strip().lower()
            for v in (game.get("away"), game.get("away_abbrev"), away_tid)
        )
        if home_id_match:
            team_tid, opp_tid, is_home = home_tid, away_tid, True
        elif away_id_match:
            team_tid, opp_tid, is_home = away_tid, home_tid, False
        else:
            # Team side unresolved from stored log.team — honest miss.
            unresolved += 1
            continue
        await db.player_game_logs.update_one(
            {"_id": log["_id"]},
            {"$set": {
                "team_id":     team_tid,
                "opp_team_id": opp_tid,
                "is_home":     is_home,
            }},
        )
        enriched += 1
    return {"scanned": scanned, "enriched": enriched, "unresolved": unresolved}

