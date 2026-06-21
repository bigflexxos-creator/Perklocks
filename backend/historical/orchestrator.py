"""Orchestrator — entry points the rest of the app calls.

Implements:
  • `backfill_current_season()`   — one-time bootstrap on first deploy.
  • `incremental_sync()`          — runs nightly. Only fetches games
                                     completed since `meta.last_sync`.
  • `refresh_player_form()`       — recomputes last3/5/10 + season_total.
  • `refresh_team_form()`         — recomputes per-team rolling rates.
  • `generate_profiles()`         — derives archetypes from stored data.

Each sport has its own client module — soccer/mlb/nfl/nba/tennis. The
orchestrator dispatches based on `sport` and merges results into the
unified collections (`players`, `games`, `player_form`, `team_form`,
`season_totals`).

This file ONLY contains glue. All HTTP / parsing logic lives in
`/app/backend/historical/<sport>.py`.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("lockscore.historical")

# Cached singleton DB handle. Set by `_set_db(db)` from server.py on
# startup so we don't re-import motor on every call.
_db = None


def _set_db(db) -> None:
    """Called once at app startup from server.py."""
    global _db
    _db = db


async def _last_sync(sport: str) -> Optional[datetime]:
    """Return the timestamp of the last completed sync for `sport`."""
    if _db is None:
        return None
    doc = await _db.historical_meta.find_one({"_id": f"sync.{sport}"})
    if doc and doc.get("last_sync"):
        try:
            return datetime.fromisoformat(doc["last_sync"])
        except Exception:
            return None
    return None


async def _set_last_sync(sport: str, ts: datetime) -> None:
    if _db is None:
        return
    await _db.historical_meta.update_one(
        {"_id": f"sync.{sport}"},
        {"$set": {"last_sync": ts.isoformat()}},
        upsert=True,
    )


# ─────────────────────────── public API ───────────────────────────


async def backfill_current_season(sports: list[str] | None = None) -> dict:
    """One-time backfill of the CURRENT SEASON ONLY for each sport.

    Safe to re-run — each client is idempotent (uses external IDs as
    keys, $setOnInsert for player/game docs).

    Returns a per-sport summary dict suitable for /api/admin/backfill
    response.
    """
    sports = sports or ["soccer", "mlb", "nfl", "nba", "tennis"]
    out: dict = {}
    for sport in sports:
        try:
            client = _client_for(sport)
            if client is None:
                out[sport] = {"skipped": "no client"}
                continue
            summary = await client.backfill_current_season(_db)
            await _set_last_sync(sport, datetime.now(timezone.utc))
            out[sport] = summary
            logger.info("backfill %s: %s", sport, summary)
        except Exception as e:
            logger.exception("backfill %s failed", sport)
            out[sport] = {"error": str(e)[:200]}
    return out


async def incremental_sync(sports: list[str] | None = None) -> dict:
    """Nightly sync — fetch only what changed since last run."""
    sports = sports or ["soccer", "mlb", "nfl", "nba", "tennis"]
    out: dict = {}
    for sport in sports:
        client = _client_for(sport)
        if client is None:
            out[sport] = {"skipped": "no client"}
            continue
        since = await _last_sync(sport)
        try:
            summary = await client.incremental_sync(_db, since=since)
            await _set_last_sync(sport, datetime.now(timezone.utc))
            out[sport] = summary
        except Exception as e:
            logger.exception("incremental %s failed", sport)
            out[sport] = {"error": str(e)[:200]}
    return out


async def refresh_player_form(sport: str | None = None) -> dict:
    """Recompute rolling form windows (last3/5/10) + season_total from
    the existing `player_game_logs` collection. Cheap — no HTTP."""
    # Skeleton — implementation in Phase 2.
    return {"note": "phase 2"}


async def refresh_team_form(sport: str | None = None) -> dict:
    """Recompute per-team rolling rates from `games` collection."""
    return {"note": "phase 2"}


async def generate_profiles(sport: str | None = None) -> dict:
    """Derive player archetypes (positive_regression / regression_risk
    / high_floor / boom_bust) from stored data."""
    return {"note": "phase 2"}


# ─────────────────────────── dispatch ───────────────────────────


def _client_for(sport: str):
    """Lazy import the per-sport client module to avoid loading every
    HTTP library at module-import time."""
    try:
        if sport == "soccer":
            from . import soccer
            return soccer
        if sport == "mlb":
            from . import mlb
            return mlb
        if sport == "nfl":
            from . import nfl
            return nfl
        if sport == "nba":
            from . import nba
            return nba
        if sport == "tennis":
            from . import tennis
            return tennis
    except ImportError as e:
        logger.warning("client %s not yet implemented: %s", sport, e)
    return None
