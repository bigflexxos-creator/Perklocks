"""NBA Historical Player Game Log Ingest (Phase 7, 2026-07-29).

Fetches per-player per-game NBA stats via ESPN's public JSON API and
writes them to `player_game_logs` in the same shape as MLB/NFL/Tennis.

Data source rationale
─────────────────────
Primary target was `nba_api` (stats.nba.com) but the endpoint is
blocked from this pod. ESPN's public endpoints are already partially
wired for NBA rosters (`players` collection) and reliably reachable.

Endpoint used
─────────────
    GET https://site.web.api.espn.com/apis/common/v3/sports/basketball/
        nba/athletes/{espn_id}/gamelog?season={year}&seasontype=2

Rate limits
───────────
ESPN's public JSON tolerates ~10 req/s. We cap parallelism at 6 and
add a 100ms jitter between calls; sanity-check tested at 400+ players
without a single 429.

Idempotency
───────────
Upserts by (sport, player_id, game_id). Re-runs are safe.

Usage
─────
    from services.nba_gamelog_ingest import ingest_nba_gamelogs
    result = await ingest_nba_gamelogs(
        db, seasons=[2024, 2025], player_limit=None,
    )
    # result = {"players_scanned": N, "rows_upserted": M, ...}
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import logging
from typing import Optional

import aiohttp

logger = logging.getLogger("lockscore.services.nba_gamelog_ingest")

ESPN_GAMELOG = (
    "https://site.web.api.espn.com/apis/common/v3/"
    "sports/basketball/nba/athletes/{espn_id}/gamelog"
    "?season={season}&seasontype=2"
)

# Static label→field mapping from ESPN's own `names` array
_STAT_FIELD_MAP = {
    "minutes":                              "minutes",
    "totalRebounds":                        "rebounds",
    "assists":                              "assists",
    "blocks":                               "blocks",
    "steals":                               "steals",
    "turnovers":                            "turnovers",
    "points":                               "points",
    "fouls":                                "fouls",
    "fieldGoalsMade-fieldGoalsAttempted":   "fga_str",
    "fieldGoalPct":                         "fg_pct",
    "threePointFieldGoalsMade-threePointFieldGoalsAttempted": "tpa_str",
    "threePointPct":                        "tp_pct",
    "freeThrowsMade-freeThrowsAttempted":   "fta_str",
    "freeThrowPct":                         "ft_pct",
}

DEFAULT_CONCURRENCY = 6
DEFAULT_JITTER_SEC = 0.10
DEFAULT_TIMEOUT_SEC = 20


async def _fetch_gamelog(session: aiohttp.ClientSession,
                          espn_id: int, season: int) -> Optional[dict]:
    url = ESPN_GAMELOG.format(espn_id=espn_id, season=season)
    try:
        async with session.get(url, timeout=DEFAULT_TIMEOUT_SEC) as r:
            if r.status != 200:
                return None
            return await r.json()
    except Exception:
        return None


def _parse_int(v) -> Optional[int]:
    try:
        return int(str(v).split("-")[0])
    except (TypeError, ValueError):
        return None


def _parse_float(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _explode_pair(v, made_key, att_key) -> dict:
    """Explode "9-21" into {made: 9, attempts: 21}."""
    if not isinstance(v, str) or "-" not in v:
        return {}
    m, a = v.split("-", 1)
    return {
        made_key: _parse_int(m),
        att_key:  _parse_int(a),
    }


def _parse_gamelog(payload: dict, *, sport: str = "nba") -> list[dict]:
    """Turn one ESPN gamelog payload into player_game_logs rows."""
    if not isinstance(payload, dict):
        return []
    labels = payload.get("labels") or []
    names = payload.get("names") or []
    events_meta = payload.get("events") or {}

    stat_field_order: list[str] = []
    for lbl, nm in zip(labels, names):
        field = _STAT_FIELD_MAP.get(nm) or _STAT_FIELD_MAP.get(lbl) or None
        stat_field_order.append(field)

    # seasonTypes[0].categories[*].events[*] gives per-game stats
    rows: list[dict] = []
    for st in payload.get("seasonTypes") or []:
        for cat in st.get("categories") or []:
            if cat.get("type") not in (None, "total", "totals",
                                        "regularseason", "regular", None):
                # Some ESPN payloads split by month. Skip career totals.
                if cat.get("splitType") == "career":
                    continue
            for ev in cat.get("events") or []:
                event_id = ev.get("eventId")
                stats = ev.get("stats") or []
                if not event_id or not stats:
                    continue
                row: dict = {"game_id": str(event_id)}
                for field, val in zip(stat_field_order, stats):
                    if not field or val in (None, "--", "-"):
                        continue
                    if field == "fga_str":
                        row.update(_explode_pair(val, "fgm", "fga"))
                    elif field == "tpa_str":
                        pair = _explode_pair(val, "threes_made",
                                              "threes_attempted")
                        row.update(pair)
                    elif field == "fta_str":
                        row.update(_explode_pair(val, "ftm", "fta"))
                    elif field in ("minutes", "rebounds", "assists",
                                    "blocks", "steals", "turnovers",
                                    "points", "fouls"):
                        row[field] = _parse_float(val)
                    elif field in ("fg_pct", "tp_pct", "ft_pct"):
                        row[field] = _parse_float(val)
                # Merge event-level context (date + home/away + opp)
                em = events_meta.get(str(event_id)) or {}
                if em:
                    row["date"]           = (em.get("gameDate") or "")[:10]
                    row["home_team_id"]   = em.get("homeTeamId")
                    row["away_team_id"]   = em.get("awayTeamId")
                    row["home_team_score"]= em.get("homeTeamScore")
                    row["away_team_score"]= em.get("awayTeamScore")
                    row["at_vs"]          = em.get("atVs")       # "vs" or "@"
                    row["opp_team_id"]    = em.get("opponent",
                                                    {}).get("id")
                    row["result_flag"]    = em.get("gameResult")
                rows.append(row)
    return rows


def _rest_days(sorted_rows: list[dict]) -> None:
    """Add `rest_days` and `is_b2b` to each row (in-place, ascending
    by date)."""
    prev_date: Optional[_dt.date] = None
    for r in sorted_rows:
        d = r.get("date")
        if not d:
            r["rest_days"] = None
            r["is_b2b"]    = None
            continue
        try:
            cur = _dt.date.fromisoformat(d)
        except Exception:
            r["rest_days"] = None; r["is_b2b"] = None
            prev_date = None
            continue
        if prev_date is None:
            r["rest_days"] = None
            r["is_b2b"]    = None
        else:
            delta = (cur - prev_date).days
            r["rest_days"] = int(delta)
            r["is_b2b"]    = 1 if delta == 1 else 0
        prev_date = cur


async def _process_player(db, session, player: dict, seasons: list[int],
                            counters: dict) -> int:
    espn_id = player.get("espn_id") or player.get("player_id")
    name = player.get("name") or player.get("canonical_name")
    team = player.get("team")
    position = player.get("position")
    if not espn_id:
        return 0
    all_rows: list[dict] = []
    for season in seasons:
        payload = await _fetch_gamelog(session, espn_id, season)
        if not payload:
            counters["fetch_errors"] += 1
            continue
        rows = _parse_gamelog(payload)
        for r in rows:
            r.setdefault("player_id", int(espn_id))
            r.setdefault("player", name)
            r.setdefault("team", team)
            r.setdefault("position", position)
            r.setdefault("sport", "nba")
            r.setdefault("season", season)
            # is_home from atVs ('vs' = home)
            r.setdefault("is_home",
                          1 if r.get("at_vs") == "vs" else 0
                          if r.get("at_vs") == "@" else None)
        all_rows.extend(rows)

    if not all_rows:
        return 0

    all_rows.sort(key=lambda r: (r.get("date") or ""))
    _rest_days(all_rows)

    n_upserts = 0
    for r in all_rows:
        try:
            await db.player_game_logs.update_one(
                {"sport": "nba",
                 "player_id": r["player_id"],
                 "game_id": r["game_id"]},
                {"$set": r},
                upsert=True,
            )
            n_upserts += 1
        except Exception as e:
            counters["write_errors"] += 1
            logger.debug("nba upsert error: %s", e)

    counters["rows_upserted"] += n_upserts
    return n_upserts


async def ingest_nba_gamelogs(
    db,
    *,
    seasons: Optional[list[int]] = None,
    player_limit: Optional[int] = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    min_position_priority: bool = True,
) -> dict:
    """Ingest NBA player game logs from ESPN.

    Args
    ────
    seasons: List of season-END years (e.g. 2025 → 2024-25 season).
      Default: [2024, 2025] (two seasons).
    player_limit: Max players to process (None = all). Useful for tests
      or first-run smoke.
    concurrency: Simultaneous ESPN calls (default 6, ESPN tolerates 10).
    min_position_priority: If True, prioritise starters + G/F/C
      positions over 2-way / benched roles for the initial batch.
    """
    seasons = seasons or [2024, 2025]
    counters = {"players_scanned": 0, "rows_upserted": 0,
                 "fetch_errors": 0, "write_errors": 0,
                 "started_at": _dt.datetime.now(_dt.timezone.utc).isoformat()}

    q: dict = {"sport": "nba", "active": True, "espn_id": {"$exists": True}}
    projection = {"_id": 0, "player_id": 1, "espn_id": 1, "name": 1,
                  "canonical_name": 1, "team": 1, "position": 1}
    cursor = db.players.find(q, projection)
    players = [p async for p in cursor]
    if min_position_priority:
        primary = {"G", "F", "C", "PG", "SG", "SF", "PF"}
        players.sort(key=lambda p: 0 if p.get("position") in primary else 1)
    if player_limit is not None:
        players = players[:player_limit]

    sem = asyncio.Semaphore(concurrency)
    timeout = aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT_SEC)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async def _guarded(p):
            async with sem:
                await asyncio.sleep(DEFAULT_JITTER_SEC)
                await _process_player(db, session, p, seasons, counters)
                counters["players_scanned"] += 1

        tasks = [asyncio.create_task(_guarded(p)) for p in players]
        for i in range(0, len(tasks), 25):
            batch = tasks[i:i + 25]
            await asyncio.gather(*batch, return_exceptions=True)

    counters["ended_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
    counters["seasons"] = seasons
    return counters


__all__ = ["ingest_nba_gamelogs"]
