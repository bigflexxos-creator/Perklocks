"""Lightweight in-memory TTL cache for read-only endpoints.

Used to skip expensive aggregations on hot read paths. Cache invalidates
automatically after TTL or via explicit `invalidate(prefix)`.

Usage::

    cached = await ttl_cache.get_or_compute(
        key="analytics:v2", ttl=300,
        compute=lambda: compute_v2_analytics(),
    )

Only used for endpoints whose results would be IDENTICAL within the TTL
window. Mutations (refresh, settle, learn) call `invalidate()` to clear
stale caches.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable


class TTLCache:
    """Simple in-process async TTL cache. Single-instance per worker.

    NOT shared across multiple uvicorn workers — we run a single worker so
    this is fine. If we ever scale horizontally, swap for Redis with the
    same interface.
    """

    def __init__(self) -> None:
        self._store: dict[str, tuple[float, Any]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, key: str) -> asyncio.Lock:
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    async def get_or_compute(
        self,
        key: str,
        ttl: float,
        compute: Callable[[], Awaitable[Any]],
    ) -> Any:
        """Return cached value if fresh; else compute, cache, and return.

        Per-key locking ensures the underlying `compute()` only runs once
        even under concurrent calls (stampede protection).
        """
        now = time.monotonic()
        cached = self._store.get(key)
        if cached and (now - cached[0]) < ttl:
            return cached[1]
        # Acquire per-key lock so concurrent callers wait for the first one
        async with self._lock_for(key):
            # Double-check after acquiring lock
            cached = self._store.get(key)
            if cached and (time.monotonic() - cached[0]) < ttl:
                return cached[1]
            value = await compute()
            self._store[key] = (time.monotonic(), value)
            return value

    def invalidate(self, prefix: str | None = None) -> int:
        """Drop all cache keys (if prefix=None) or keys starting with prefix.

        Returns the number of keys removed.
        """
        if prefix is None:
            n = len(self._store)
            self._store.clear()
            return n
        to_drop = [k for k in self._store if k.startswith(prefix)]
        for k in to_drop:
            self._store.pop(k, None)
        return len(to_drop)


# Singleton instance — import this everywhere.
ttl_cache = TTLCache()
