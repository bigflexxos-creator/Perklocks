"""Live game-log ingestor runner — orchestrates MLB · NBA · NFL."""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from .common import logger, TARGET_COLLECTION
from . import mlb as _mlb
from . import nba as _nba
from . import nfl as _nfl


async def _ensure_target_index(db) -> None:
    """Ensure the canonical uniqueness index — mirrors the one from
    `services/player_history/backfill.ensure_backfill_indexes`."""
    try:
        await db[TARGET_COLLECTION].create_index(
            [("sport", 1), ("canonical_player_id", 1), ("event_id", 1)],
            name="unique_backfill_key", unique=True,
        )
    except Exception:
        pass


async def refresh_mlb_gamelogs(db, *, season: Optional[int] = None,
                                max_players: Optional[int] = None) -> dict:
    await _ensure_target_index(db)
    return await _mlb.refresh(db, season=season, max_players=max_players)


async def refresh_nba_gamelogs(db, *,
                                max_players: Optional[int] = None) -> dict:
    await _ensure_target_index(db)
    return await _nba.refresh(db, max_players=max_players)


async def refresh_nfl_gamelogs(db, *,
                                max_players: Optional[int] = None) -> dict:
    await _ensure_target_index(db)
    return await _nfl.refresh(db, max_players=max_players)


async def refresh_all_sports(db, *,
                              season: Optional[int] = None,
                              max_players_per_sport: Optional[int] = None
                              ) -> dict:
    """Refresh MLB + NBA + NFL sequentially.  Sequential (not parallel)
    so we don't stress any single provider — each sport already runs
    all its players concurrently via bounded semaphore."""
    started = time.time()
    summary: dict = {"sports": {}}
    for name, fn in (("mlb", refresh_mlb_gamelogs),
                     ("nba", refresh_nba_gamelogs),
                     ("nfl", refresh_nfl_gamelogs)):
        try:
            kwargs: dict = {"max_players": max_players_per_sport}
            if name == "mlb" and season is not None:
                kwargs["season"] = season
            summary["sports"][name] = await fn(db, **kwargs)
        except Exception as e:
            summary["sports"][name] = {"ok": False, "error": str(e)}
            logger.warning("%s live gamelog refresh crashed: %s", name, e)
    summary["elapsed_sec"] = round(time.time() - started, 1)
    logger.info("live gamelog refresh_all: %s", summary)
    return summary


__all__ = [
    "refresh_mlb_gamelogs",
    "refresh_nba_gamelogs",
    "refresh_nfl_gamelogs",
    "refresh_all_sports",
]
