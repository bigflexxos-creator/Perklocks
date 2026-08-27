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


async def backfill_season(db, season: int) -> dict:
    """Walk the scoreboard week-by-week for a specific NFL season.

    Pass the calendar year the season STARTS in (NFL convention).
    For past seasons we walk all 18 regular weeks + 5 postseason weeks.

    2026-08-27 — ESPN's scoreboard endpoint silently ignores ``year=``
    for prior seasons (always returns current-week events). The
    historical backfill therefore has to walk actual calendar
    ``dates=YYYYMMDD-YYYYMMDD`` ranges. We anchor each season at
    ``Sept 1 → Feb 20`` (covers preseason wk 3, regular season, and
    Super Bowl) and paginate weekly. ``games_seen`` still counts every
    event returned, so a broken date range yields 0 without silently
    inflating the summary.
    """
    from datetime import date as _date
    season = int(season)
    games_seen = games_inserted = logs_inserted = 0
    errors: list[str] = []

    max_weeks = int(os.environ.get("HIST_NFL_MAX_WEEKS", "22"))
    # Anchor season window: Sept 1 (season year) → Feb 20 (season year + 1).
    start = _date(season, 9, 1)
    end   = _date(season + 1, 2, 20)
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers={"User-Agent": "PerksLocks/1.0"}) as cx:
        cur = start
        seen_gids: set[str] = set()
        while cur <= end:
            if games_inserted >= max_weeks * 16:
                break
            wk_end = cur + timedelta(days=6)  # inclusive 7-day window
            date_arg = f"{cur.strftime('%Y%m%d')}-{wk_end.strftime('%Y%m%d')}"
            data = await _get(cx, "/scoreboard", {"dates": date_arg})
            await asyncio.sleep(_PACE)
            cur = wk_end + timedelta(days=1)
            if not data:
                continue
            for ev in data.get("events", []):
                gid = ev.get("id")
                if not gid or gid in seen_gids:
                    continue
                seen_gids.add(gid)
                games_seen += 1
                status = (((ev.get("status") or {}).get("type") or {}).get("completed")) or False
                if not status:
                    continue
                competition = (ev.get("competitions") or [{}])[0]
                comps = competition.get("competitors") or []
                home = next((c for c in comps if c.get("homeAway") == "home"), {})
                away = next((c for c in comps if c.get("homeAway") == "away"), {})
                # ESPN returns a `week.number` inside events — preserve
                # it when available so downstream week-based joins still
                # work identically to the legacy year+week walk.
                wk_num = ((ev.get("week") or {}).get("number")
                          or (competition.get("week") or {}).get("number"))
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
                        "season": season,
                        "week": wk_num,
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
    """Backward-compatible wrapper — backfills the current NFL season."""
    return await backfill_season(db, _CURRENT_SEASON)


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


async def _ingest_summary(cx: httpx.AsyncClient, db, event_id, season: Optional[int] = None) -> int:
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
                if season is not None:
                    log_doc["season"] = int(season)
                # Map ESPN labels into normalized prop-engine field names.
                # Engine reads: passing_yards, passing_tds, rushing_yards,
                # receiving_yards, receptions, any_td. We mirror BOTH the
                # nfl_* legacy fields and the normalized fields so existing
                # readers + props_engine stay in sync.
                log_doc.update({f"nfl_{k}": v for k, v in mapped.items()})
                _normalize_nfl_stats(stat_name, mapped, log_doc)
                await db.player_game_logs.update_one(
                    {"player_id": f"espn_{pid}", "game_id": f"espn_{event_id}", "stat_block": stat_name},
                    {"$set": log_doc},
                    upsert=True,
                )
                inserted += 1
    return inserted


def _normalize_nfl_stats(stat_name: str, mapped: dict, log_doc: dict) -> None:
    """Map ESPN per-block labels into normalized prop-engine fields.

    ESPN groups stats by stat_block name (passing/rushing/receiving/etc).
    The labels vary by block. This populates passing_yards, passing_tds,
    rushing_yards, receiving_yards, receptions, any_td so the props
    engine can read consistently across all games regardless of how ESPN
    laid out its boxscore that week.
    """
    def _to_int(v) -> Optional[int]:
        try:
            return int(str(v).replace(",", "").strip())
        except (TypeError, ValueError):
            return None

    def _yds_from_label(prefix: str) -> Optional[int]:
        # ESPN uses different label keys per block: "yds", "rec yds", "passing yards"
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
        # TDs label commonly: "td"
        td = _to_int(mapped.get("td"))
        if td is not None:
            log_doc["passing_tds"] = td
            log_doc["any_td"] = max(int(log_doc.get("any_td") or 0), 1 if td > 0 else 0)
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
