"""Board Snapshot Cache — versioned, in-process, ETag-backed.

Session 2 (2026-09-14) — architecture correction:

    The Locks consumer contract remains a SINGLE endpoint:
    ``GET /api/picks/today?lite=true``.  The frozen snapshot lives
    **behind** that endpoint, not as a parallel route.  This module
    is a pure server-side derived cache; canonical publication in
    ``db.picks`` remains the sole scoring/publication authority.

Design
======

Cache shape:  ``(cache_key -> Snapshot)``

    cache_key   = md5 hash of (canonical query params + lite flag)
    Snapshot    = {
        board_version:   str  # deterministic hash of pick_ids + max mtime
        generated_at:    datetime
        response:        dict # the exact payload returned to the client
        payload_bytes:   int
    }

Version derivation:

    ``board_version`` is derived from the canonical set the endpoint
    would otherwise recompute: it is a stable hash of the sorted
    ``id`` values plus the maximum ``updated_at`` / ``created_at``
    across the set.  Any canonical publication that adds, removes,
    or rescores a pick changes the version.

Invalidation:

    * Any incoming request whose *computed* board_version differs
      from the cached one triggers a rebuild (atomic replace).
    * A short TTL (``_TTL_SECONDS = 15``) guarantees stalest
      possible snapshot is 15 s even under pathological cache-key
      collisions or unchanged pick set with silent field updates.
    * On rebuild failure the previous snapshot is retained (never
      served partially built state).

Atomicity:

    A per-cache-key ``asyncio.Lock`` serializes rebuilds so a
    fanout of concurrent requests never triggers duplicate expensive
    computations of the same version.

ETag / 304:

    ``board_version`` doubles as the ETag.  The handler honours
    ``If-None-Match`` and returns 304 with no body when the client's
    ETag matches the current cached version.

Guarantees:
    * No mutation of canonical truth.
    * No new "second-truth" endpoint.
    * On any cache miss the caller runs the SAME production
      pipeline; snapshot is purely derived.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Awaitable, Optional

logger = logging.getLogger("lockscore.board_snapshot_cache")

_TTL_SECONDS = int(os.environ.get("BOARD_SNAPSHOT_TTL_SECONDS", "15"))
_MAX_ENTRIES = int(os.environ.get("BOARD_SNAPSHOT_MAX_ENTRIES", "64"))


@dataclass
class Snapshot:
    board_version: str
    generated_at:  datetime
    response:      dict
    payload_bytes: int


@dataclass
class _CacheState:
    entries: dict[str, Snapshot] = field(default_factory=dict)
    locks:   dict[str, asyncio.Lock] = field(default_factory=dict)


_state = _CacheState()


def _cache_key(params: dict[str, Any]) -> str:
    """Deterministic cache key from request-shaping params.

    Only params that change the returned canonical population are
    included — auth, timestamp cache-busters (``_``), and callback
    URLs are excluded.
    """
    canon = {
        k: v for k, v in sorted(params.items())
        if v is not None
        and k not in {"_", "callback", "user"}
    }
    payload = json.dumps(canon, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


def compute_board_version(picks: list[dict]) -> str:
    """Compute a deterministic version for a canonical pick set.

    Uses the sorted list of pick ids XOR the maximum
    ``updated_at`` timestamp across the set.  This changes IFF the
    canonical publication produces a different set OR any pick's
    stored truth is refreshed.
    """
    if not picks:
        return "empty"
    _ids: list[str] = []
    _max_ts = ""
    for p in picks:
        pid = p.get("id") or p.get("canonical_pick_id")
        if pid:
            _ids.append(str(pid))
        ts = (
            p.get("updated_at")
            or p.get("last_seen_at")
            or p.get("created_at")
            or ""
        )
        if isinstance(ts, datetime):
            ts = ts.isoformat()
        if isinstance(ts, str) and ts > _max_ts:
            _max_ts = ts
    _ids.sort()
    h = hashlib.md5()
    h.update(b"v1|")
    h.update(_max_ts.encode("utf-8"))
    h.update(b"|")
    h.update("|".join(_ids).encode("utf-8"))
    return h.hexdigest()[:16]


def _evict_lru_if_needed() -> None:
    if len(_state.entries) <= _MAX_ENTRIES:
        return
    # Cheapest LRU: drop the oldest generated_at entries.
    ordered = sorted(_state.entries.items(),
                     key=lambda kv: kv[1].generated_at)
    for k, _ in ordered[: len(_state.entries) - _MAX_ENTRIES]:
        _state.entries.pop(k, None)
        _state.locks.pop(k, None)


def get_snapshot(params: dict[str, Any]) -> Optional[Snapshot]:
    """Return the cached snapshot if it is still within the TTL
    window, else None.  Caller runs the full pipeline on miss."""
    key = _cache_key(params)
    snap = _state.entries.get(key)
    if snap is None:
        return None
    age = (datetime.now(timezone.utc) - snap.generated_at).total_seconds()
    if age > _TTL_SECONDS:
        return None
    return snap


def get_lock(params: dict[str, Any]) -> asyncio.Lock:
    key = _cache_key(params)
    lock = _state.locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _state.locks[key] = lock
    return lock


def put_snapshot(params: dict[str, Any], response: dict,
                 board_version: str) -> Snapshot:
    """Store an atomically completed snapshot.  Overwrites any
    previous entry for the same cache key in a single assignment."""
    key = _cache_key(params)
    try:
        _bytes = len(json.dumps(response, default=str))
    except Exception:
        _bytes = 0
    snap = Snapshot(
        board_version=board_version,
        generated_at=datetime.now(timezone.utc),
        response=response,
        payload_bytes=_bytes,
    )
    _state.entries[key] = snap
    _evict_lru_if_needed()
    return snap


def clear_cache() -> None:
    """Test-only helper — drop every snapshot and lock."""
    _state.entries.clear()
    _state.locks.clear()


def stats() -> dict[str, Any]:
    """Diagnostic snapshot for the admin dashboard."""
    return {
        "entries":       len(_state.entries),
        "ttl_seconds":   _TTL_SECONDS,
        "max_entries":   _MAX_ENTRIES,
        "keys":          [
            {
                "cache_key":     k,
                "board_version": s.board_version,
                "age_seconds":   (datetime.now(timezone.utc) - s.generated_at).total_seconds(),
                "payload_bytes": s.payload_bytes,
            }
            for k, s in _state.entries.items()
        ],
    }
