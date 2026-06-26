"""Multi-season historical backfill orchestrator.

This is Phase 1 of the Multi-Sport Historical Ingestion System. It walks
N seasons for each supported sport and populates the unified collections
(`players`, `games`, `player_game_logs`) used by the Lock Engine + Props
Engine.

Strategy:
  • Free sources only — MLB Stats API, ESPN public, Sackmann CSV. SportDB
    is reserved for current-season enrichment because of its 1k trial cap.
  • Idempotent — every per-sport client uses `update_one(..., upsert=True)`
    keyed on the upstream's stable IDs, so re-runs are safe.
  • Resumable — `ingestion_state` collection tracks what's already done per
    (sport, season). If a backfill is interrupted, we resume.
  • Polite pacing — each client self-limits to friendly request rates.

The actual per-season HTTP/parsing logic lives in the per-sport modules:
  • historical/mlb.py     → backfill_season(season)
  • historical/nba.py     → backfill_season(season)    [Phase 2]
  • historical/nfl.py     → backfill_season(season)    [Phase 2]
  • historical/soccer.py  → backfill_season(season)    [Phase 2]
  • historical/tennis.py  → backfill_season(season)    [Phase 2]
  • historical/cfb.py     → backfill_season(season)    [Phase 2]

This module is the dispatcher + state tracker.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("lockscore.historical.multi_season")

# How many seasons back from the current year to backfill by default.
DEFAULT_LOOKBACK_SEASONS = 5

# Per-sport season window — varies because some sports have cross-year
# seasons (EPL 24-25) while others use single-year (MLB 2024).
_CURRENT_YEAR = datetime.now(timezone.utc).year


def default_seasons_for(sport: str, n: int = DEFAULT_LOOKBACK_SEASONS) -> list[int]:
    """Return the last N season identifiers for a sport.

    For most sports we return [Y-n+1 ... Y]. Soccer uses a string season
    encoding but the per-sport client handles that mapping internally —
    we only deal in calendar-year ints here for the dispatch layer.
    """
    sp = (sport or "").lower()
    if sp in ("mlb", "tennis", "cfb"):
        # Calendar-year seasons. MLB 2026 = full Mar-Oct 2026.
        return [(_CURRENT_YEAR - i) for i in range(n)][::-1]
    if sp in ("nba", "nhl"):
        # Cross-year but referenced by ending year (NBA 2024-25 = "2025").
        return [(_CURRENT_YEAR - i) for i in range(n)][::-1]
    if sp == "nfl":
        # Sep-Feb. Referenced by starting year. Current "season" might be
        # last year's depending on month, but for backfill we just walk N
        # ending years.
        return [(_CURRENT_YEAR - i - 1) for i in range(n)][::-1]
    if sp == "soccer":
        # Cross-year (EPL 24-25). Per-sport client maps int → string.
        return [(_CURRENT_YEAR - i) for i in range(n)][::-1]
    return [(_CURRENT_YEAR - i) for i in range(n)][::-1]


async def _state_key(sport: str, season: int) -> str:
    return f"ingest.{(sport or '').lower()}.{int(season)}"


async def _get_state(db, sport: str, season: int) -> dict:
    if db is None:
        return {}
    doc = await db.historical_ingestion_state.find_one({"_id": await _state_key(sport, season)})
    return doc or {}


async def _mark_started(db, sport: str, season: int) -> None:
    if db is None:
        return
    await db.historical_ingestion_state.update_one(
        {"_id": await _state_key(sport, season)},
        {"$set": {
            "_id": await _state_key(sport, season),
            "sport": (sport or "").lower(),
            "season": int(season),
            "status": "in_progress",
            "started_at": datetime.now(timezone.utc),
        }},
        upsert=True,
    )


async def _mark_finished(db, sport: str, season: int, summary: dict) -> None:
    if db is None:
        return
    await db.historical_ingestion_state.update_one(
        {"_id": await _state_key(sport, season)},
        {"$set": {
            "status": "done",
            "finished_at": datetime.now(timezone.utc),
            "summary": summary,
        }},
        upsert=True,
    )


async def _mark_error(db, sport: str, season: int, err: str) -> None:
    if db is None:
        return
    await db.historical_ingestion_state.update_one(
        {"_id": await _state_key(sport, season)},
        {"$set": {
            "status": "error",
            "error": str(err)[:300],
            "errored_at": datetime.now(timezone.utc),
        }},
        upsert=True,
    )


def _client_for(sport: str):
    """Lazy-import the per-sport historical client module.

    Each module is expected to expose `backfill_season(db, season)` —
    Phase 1 wires MLB; Phase 2 fills in the rest. If a client doesn't
    yet have multi-season support, this returns None.
    """
    sp = (sport or "").lower()
    try:
        if sp == "mlb":
            from historical import mlb
            return mlb if hasattr(mlb, "backfill_season") else None
        if sp == "nba":
            from historical import nba
            return nba if hasattr(nba, "backfill_season") else None
        if sp == "nfl":
            from historical import nfl
            return nfl if hasattr(nfl, "backfill_season") else None
        if sp == "nhl":
            from historical import nhl
            return nhl if hasattr(nhl, "backfill_season") else None
        if sp == "soccer":
            from historical import soccer
            return soccer if hasattr(soccer, "backfill_season") else None
        if sp == "tennis":
            try:
                from historical import tennis
            except ImportError:
                return None
            return tennis if hasattr(tennis, "backfill_season") else None
        if sp == "cfb":
            try:
                from historical import cfb
            except ImportError:
                return None
            return cfb if hasattr(cfb, "backfill_season") else None
    except Exception as e:
        logger.warning("client %s import failed: %s", sp, e)
    return None


async def backfill_seasons(
    db,
    *,
    sports: Optional[list[str]] = None,
    seasons: Optional[list[int]] = None,
    lookback: int = DEFAULT_LOOKBACK_SEASONS,
    skip_if_done: bool = True,
) -> dict:
    """Walk N seasons of historical data for each requested sport.

    Args:
      sports: list of sport keys; default = ['mlb','nba','nfl','soccer','tennis','cfb']
      seasons: explicit season list (applied to ALL sports). If None, each
               sport uses `default_seasons_for(sport, lookback)`.
      lookback: number of recent seasons to ingest if `seasons` is None.
      skip_if_done: skip (sport, season) pairs already marked status=done.

    Returns: per-sport per-season summary dict.
    """
    sports = sports or ["mlb", "nba", "nfl", "soccer", "tennis", "cfb"]
    result: dict = {}

    for sp in sports:
        client = _client_for(sp)
        if client is None:
            result[sp] = {"skipped": "no multi-season client yet"}
            continue
        season_list = seasons or default_seasons_for(sp, n=lookback)
        per_season: dict = {}
        for season in season_list:
            try:
                state = await _get_state(db, sp, season)
                if skip_if_done and state.get("status") == "done":
                    per_season[str(season)] = {"skipped": "already_done", "summary": state.get("summary")}
                    continue
                await _mark_started(db, sp, season)
                logger.info("backfill start %s/%s", sp, season)
                summary = await client.backfill_season(db, season)
                await _mark_finished(db, sp, season, summary)
                per_season[str(season)] = summary
                logger.info("backfill done %s/%s: %s", sp, season, summary)
            except Exception as e:
                logger.exception("backfill failed %s/%s", sp, season)
                await _mark_error(db, sp, season, str(e))
                per_season[str(season)] = {"error": str(e)[:200]}
            # Inter-season pause — keep upstream APIs happy.
            await asyncio.sleep(1.5)
        result[sp] = per_season

    return result


async def get_ingestion_status(db) -> dict:
    """Snapshot of every (sport, season) we've started, in-progress, or
    finished. Powers the admin status endpoint."""
    if db is None:
        return {}
    rows: list[dict] = []
    async for doc in db.historical_ingestion_state.find({}, {"_id": 0}):
        rows.append(doc)
    rows.sort(key=lambda r: (r.get("sport", ""), -int(r.get("season") or 0)))
    return {
        "total": len(rows),
        "by_status": _group_counts(rows, "status"),
        "rows": rows,
    }


def _group_counts(rows: list[dict], key: str) -> dict:
    out: dict = {}
    for r in rows:
        v = r.get(key) or "unknown"
        out[v] = out.get(v, 0) + 1
    return out
