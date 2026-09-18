"""Live Game-Log Ingestor — MLB · NBA · NFL

Keeps ``db.player_game_actuals`` current by pulling per-game logs
directly from free public APIs on a scheduled cadence.

Design contract (mirrors services/player_history/backfill.py):
* Idempotent upsert on ``(sport, canonical_player_id, event_id)``.
* Missing stats stay as ``None`` — a genuine 0 (0 hits) is preserved.
* Provenance retained: ``source="live_gamelog_{sport}_v1"``,
  ``ingested_at`` ISO UTC.
* Bounded concurrency per sport (semaphore) so we never open >20
  sockets against one provider.
* Best-effort — HTTP / parse errors are counted, never propagated.
* Scoped to ACTIVE rostered players from ``db.players`` (already
  refreshed daily by the existing ``player_db`` ingestors).  A
  "hot list" mode also lets the loop prioritise players who appear
  in today's canonical picks so the freshness signal reaches the
  Pick Breakdown UI within one refresh cycle.
"""
from .runner import (
    refresh_mlb_gamelogs,
    refresh_nba_gamelogs,
    refresh_nfl_gamelogs,
    refresh_all_sports,
)

__all__ = [
    "refresh_mlb_gamelogs",
    "refresh_nba_gamelogs",
    "refresh_nfl_gamelogs",
    "refresh_all_sports",
]
