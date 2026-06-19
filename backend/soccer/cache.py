"""Tiny in-memory TTL cache for API-Football responses.

Avoids hammering the upstream service and keeps us under the free-tier
quota (100 req/day). Two TTLs are used by the rest of the module:

  • PREGAME_TTL_SECONDS (15 min) — fixtures, lineups, standings, scorers,
                                    injuries; all change infrequently.
  • LIVE_TTL_SECONDS    (30 s)   — internal-only live polls (never
                                    surfaced to UI per user choice 3A).

Process-local cache is fine for the current single-worker deployment.
If we ever scale to multiple workers, swap the dict for Redis without
touching consumers — the public interface (get / set) stays the same.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

PREGAME_TTL_SECONDS = 15 * 60  # 15 min
LIVE_TTL_SECONDS    = 30       # 30 s — internal only


class TTLCache:
    """Asyncio-safe TTL cache.

    Entry layout: { key: (expires_at_unix, value) }. Expired entries are
    cleaned up lazily on read (no background sweeper to keep things
    simple). `set()` writes; `get()` returns None on miss or expiry.
    """
    def __init__(self) -> None:
        self._store: dict[str, tuple[float, Any]] = {}
        self._lock = asyncio.Lock()
        # Lightweight hit/miss counters for the `/sports/soccer/health`
        # endpoint — gives the user visibility into cache effectiveness
        # without pulling in a full metrics stack.
        self.hits = 0
        self.misses = 0

    async def get(self, key: str) -> Any | None:
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self.misses += 1
                return None
            expires_at, value = entry
            if time.time() > expires_at:
                # Lazy purge — keeps the store small without a sweeper.
                del self._store[key]
                self.misses += 1
                return None
            self.hits += 1
            return value

    async def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        async with self._lock:
            self._store[key] = (time.time() + ttl_seconds, value)

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / total, 3) if total else 0.0,
            "entries": len(self._store),
        }


# Module-singleton cache. All clients share it so the same fixture/team
# lookup across multiple consumers only burns one Odds API request.
cache = TTLCache()
