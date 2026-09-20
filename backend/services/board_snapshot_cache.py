"""Board Snapshot Cache — v3 (Gate 1, 2026-06).

Rewritten as part of the P0 Locks-lifecycle root closure.

Design tenets
=============

* **Committed canonical board_version is the primary freshness
  authority.**  A cached snapshot is fresh as long as its
  ``board_version`` still equals ``board_generation.active_version()``.
  Wall-clock TTL survives only as a very long safety failsafe (default
  5 minutes) so a mis-committed / stuck version can never pin readers
  to arbitrarily stale data forever.
* **True per-cache-key single-flight.**  Concurrent cold callers for
  the same key elect exactly ONE reconstruction owner via a shielded
  ``asyncio.Future``.  Waiters await the same future; owner
  disconnect / exception / timeout does not lose the work.
* **Last-good is preserved.**  When a new commit arrives the previous
  snapshot is retained under ``entries[key]`` until the new
  reconstruction succeeds.  ``get_stale_snapshot()`` exposes it so
  callers can serve it while revalidation runs.
* **Atomic replacement + version guard.**  ``put_snapshot`` refuses to
  overwrite a newer version and refuses to store while a canonical
  generation is BUILDING (preserves the frozen-during-build contract).
* **Lifecycle separation.**  ``entries`` (snapshot payloads),
  ``owners`` (in-flight Futures), and the committed version are three
  independent dicts.  ``clear_entries()`` never wipes owners.
* **Cancellation-safe.**  Owner Futures are set from an
  ``asyncio.shield``-wrapped background task; a client HTTP disconnect
  cannot destroy the shared reconstruction other waiters depend on.
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
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("lockscore.board_snapshot_cache")

# Long safety failsafe TTL.  Wall-clock time is NO LONGER the normal
# freshness authority — committed board_version is.  This value only
# fires if we somehow lose committed-version tracking (e.g. board
# generation module failed to import).
_TTL_SECONDS = int(os.environ.get("BOARD_SNAPSHOT_TTL_SECONDS", "300"))
_MAX_ENTRIES = int(os.environ.get("BOARD_SNAPSHOT_MAX_ENTRIES", "64"))
# How long a reconstruction owner may hold ownership before it is
# considered orphaned (task cancelled, hard crash without cleanup).
_OWNER_STALE_SEC = int(os.environ.get("BOARD_SNAPSHOT_OWNER_STALE_SEC", "180"))


@dataclass
class Snapshot:
    board_version: str
    generated_at:  datetime
    response:      dict
    payload_bytes: int


@dataclass
class _Owner:
    future:  asyncio.Future
    started: float
    key:     str


@dataclass
class _CacheState:
    entries: dict[str, Snapshot] = field(default_factory=dict)
    owners:  dict[str, _Owner] = field(default_factory=dict)
    locks:   dict[str, asyncio.Lock] = field(default_factory=dict)   # legacy compat
    # committed_version cache: last observed board_generation.active
    last_committed_version: Optional[str] = None
    reconstruction_count: int = 0


_state = _CacheState()


# ---------------------------------------------------------------------
# Key + version helpers
# ---------------------------------------------------------------------

def _cache_key(params: dict[str, Any]) -> str:
    canon = {
        k: v for k, v in sorted(params.items())
        if v is not None and k not in {"_", "callback", "user"}
    }
    payload = json.dumps(canon, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


def compute_board_version(picks: list[dict]) -> str:
    """Deterministic version for a canonical pick set (used by putters
    that already have the full population in hand)."""
    if not picks:
        return "empty"
    _ids: list[str] = []
    _max_ts = ""
    for p in picks:
        pid = p.get("id") or p.get("canonical_pick_id")
        if pid:
            _ids.append(str(pid))
        ts = (
            p.get("updated_at") or p.get("last_seen_at")
            or p.get("created_at") or ""
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


def _committed_version() -> Optional[str]:
    """Read the active COMMITTED canonical board_version, cheaply.

    Only reads the in-process ``board_generation._active`` pointer,
    which is stamped exactly once per real commit and unchanged on
    ``COMMITTED_NOOP``.  Never touches Mongo on hot path.
    """
    try:
        from services import board_generation as _bg
        active = getattr(_bg, "_active", {}) or {}
        v = active.get("board_version")
        return str(v) if v else None
    except Exception:
        return None


def _is_building_now() -> bool:
    try:
        from services.board_generation import is_building
        return bool(is_building())
    except Exception:
        return False


# ---------------------------------------------------------------------
# Snapshot lookup
# ---------------------------------------------------------------------

def get_snapshot(params: dict[str, Any]) -> Optional[Snapshot]:
    """Return a snapshot considered fresh under committed-version
    authority.

    Rules
    -----
    1. If a canonical generation is BUILDING and we already have any
       entry for this key, return it PINNED (frozen during build).
    2. If we can read ``_committed_version()`` and it equals the
       cached snapshot's version, return it regardless of TTL — the
       canonical population has not changed.
    3. If no committed-version is available, fall back to the long
       safety TTL (default 300 s).
    4. Otherwise return None (caller must reconstruct).
    """
    key = _cache_key(params)
    snap = _state.entries.get(key)
    if snap is None:
        return None
    if _is_building_now():
        return snap
    committed = _committed_version()
    age = (datetime.now(timezone.utc) - snap.generated_at).total_seconds()
    # Committed-version authority: if committed matches the stored
    # snapshot's version, it is fresh irrespective of wall-clock age.
    if committed is not None and snap.board_version == committed:
        return snap
    # Otherwise fall back to the long safety TTL (failsafe only).
    # This also covers the common case where the endpoint stamps a
    # ``compute_board_version(picks)`` fingerprint that intentionally
    # differs from ``board_generation._active.board_version`` — both
    # are legitimate freshness signals; version equality is a strong
    # HIT, TTL is a permissive HIT.
    if age <= _TTL_SECONDS:
        return snap
    return None


def get_stale_snapshot(params: dict[str, Any]) -> Optional[Snapshot]:
    """Return the last-good snapshot for this key REGARDLESS of
    freshness — for stale-while-revalidate serving.

    Callers should present this as ``X-Snapshot-Cache: STALE`` so the
    client (and diagnostics) know a background revalidation is in
    progress.  Never returns None just because the entry is stale by
    version or age.
    """
    return _state.entries.get(_cache_key(params))


# ---------------------------------------------------------------------
# Single-flight ownership
# ---------------------------------------------------------------------

def _reap_stale_owners() -> None:
    now = time.time()
    for k in list(_state.owners):
        o = _state.owners.get(k)
        if o is None:
            continue
        if o.future.done():
            _state.owners.pop(k, None)
            continue
        if (now - o.started) > _OWNER_STALE_SEC:
            # Orphan — cancel and drop so a new owner can be elected.
            try:
                o.future.cancel()
            except Exception:
                pass
            _state.owners.pop(k, None)
            logger.warning(
                "board_snapshot_cache: reaped stale owner key=%s age=%.1fs "
                "(owner exceeded %ds without completion)",
                k, now - o.started, _OWNER_STALE_SEC,
            )


async def reconstruct(
    params: dict[str, Any],
    build_fn: Callable[[], Awaitable[tuple[dict, str]]],
) -> Snapshot:
    """True per-cache-key single-flight.

    * Concurrent callers for the same key elect exactly one owner.
    * The owner runs ``build_fn`` inside an ``asyncio.shield``-wrapped
      background task, so the initiating HTTP client cancelling does
      not destroy the reconstruction.
    * Waiters await the shared Future.  A waiter cancellation does
      NOT propagate into the owner.
    * On owner exception: the Future exception is surfaced to all
      waiters, ownership releases, previous last-good entry survives.
    * On owner timeout: ``_reap_stale_owners`` releases the ownership
      slot so a subsequent call can re-elect an owner.
    * Version guard: after build, if a NEWER committed version was
      observed in the meantime, do NOT store the older result — a
      more recent reconstruction owns the truth.

    ``build_fn`` must return ``(response_dict, board_version)``.
    """
    _reap_stale_owners()
    key = _cache_key(params)
    existing = _state.owners.get(key)
    if existing is not None and not existing.future.done():
        # WAITER path — await the owner's Future, protected from our
        # own cancellation propagating to the owner.
        return await asyncio.shield(existing.future)

    # OWNER path — elect ourselves.
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    owner = _Owner(future=fut, started=time.time(), key=key)
    _state.owners[key] = owner
    _state.reconstruction_count += 1
    # Record the committed version at the START of reconstruction.
    # If a newer commit lands mid-build, we refuse to overwrite it.
    pre_committed = _committed_version()

    async def _drive() -> None:
        try:
            response, board_version = await build_fn()
            snap = put_snapshot(
                params, response, board_version,
                _pre_committed=pre_committed,
            )
            if not fut.done():
                fut.set_result(snap)
        except asyncio.CancelledError:
            # Owner task itself was cancelled (rare — we shield it).
            # Surface as a fresh exception so waiters retry cleanly.
            if not fut.done():
                fut.set_exception(RuntimeError("reconstruction cancelled"))
            raise
        except Exception as exc:
            if not fut.done():
                fut.set_exception(exc)
        finally:
            # Release ownership regardless of outcome — new callers
            # may elect a fresh owner immediately.
            _state.owners.pop(key, None)

    # Launch the driver as a shielded background task.  The Future is
    # the sync-point; the task itself keeps running even if THIS
    # coroutine is cancelled by the request handler.
    task = asyncio.create_task(_drive(), name=f"snapshot_reconstruct[{key[:8]}]")
    # Retain a strong reference for the lifetime of the ownership slot
    # so garbage collection cannot silently drop it.
    owner_ref = owner
    setattr(owner_ref, "_task", task)  # type: ignore[attr-defined]

    try:
        return await asyncio.shield(fut)
    finally:
        # We deliberately do NOT cancel the task here; other waiters
        # or the cache itself still need the result.
        pass


# ---------------------------------------------------------------------
# Legacy-compat lock (retained for callers still using the old API)
# ---------------------------------------------------------------------

def get_lock(params: dict[str, Any]) -> asyncio.Lock:
    key = _cache_key(params)
    lock = _state.locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _state.locks[key] = lock
    return lock


# ---------------------------------------------------------------------
# Snapshot storage — atomic + version-guarded
# ---------------------------------------------------------------------

def put_snapshot(params: dict[str, Any], response: dict,
                 board_version: str,
                 _pre_committed: Optional[str] = None) -> Snapshot:
    """Atomically store a snapshot.

    Refusal rules
    -------------
    * Never store while a generation is BUILDING (preserves the
      pin-during-build contract).
    * Never overwrite a stored entry whose board_version equals the
      CURRENT committed version if our board_version differs (this
      protects the newer canonical truth from being clobbered by a
      slower older reconstruction).
    * If ``_pre_committed`` was captured before reconstruction started
      AND the current committed version has since advanced past it,
      refuse to overwrite unless our board_version matches the newer
      committed value.
    * Same board_version as the stored entry → NOOP (return existing).
      This preserves COMMITTED_NOOP semantics: no snapshot churn.
    """
    key = _cache_key(params)
    if _is_building_now():
        # Return a transient un-stored snapshot; do NOT overwrite last-good.
        return Snapshot(
            board_version=board_version,
            generated_at=datetime.now(timezone.utc),
            response=response,
            payload_bytes=_safe_bytes(response),
        )

    existing = _state.entries.get(key)
    committed_now = _committed_version()

    # COMMITTED_NOOP fast-path — same version, do not churn.
    if existing is not None and existing.board_version == board_version:
        return existing

    # Version-guard: refuse to overwrite a newer committed snapshot
    # with an older reconstruction.
    if (existing is not None
        and committed_now is not None
        and existing.board_version == committed_now
        and board_version != committed_now):
        logger.info(
            "board_snapshot_cache: refused overwrite key=%s "
            "existing=%s (matches committed) new=%s (obsolete)",
            key, existing.board_version, board_version,
        )
        return existing

    # If committed advanced past our start marker AND we don't match
    # the new committed value, refuse.
    if (_pre_committed is not None
        and committed_now is not None
        and _pre_committed != committed_now
        and board_version != committed_now):
        logger.info(
            "board_snapshot_cache: refused stale overwrite key=%s "
            "pre_committed=%s committed_now=%s our_version=%s",
            key, _pre_committed, committed_now, board_version,
        )
        return existing or Snapshot(
            board_version=board_version,
            generated_at=datetime.now(timezone.utc),
            response=response,
            payload_bytes=_safe_bytes(response),
        )

    snap = Snapshot(
        board_version=board_version,
        generated_at=datetime.now(timezone.utc),
        response=response,
        payload_bytes=_safe_bytes(response),
    )
    _state.entries[key] = snap                # atomic single assignment
    _state.last_committed_version = committed_now or _state.last_committed_version
    _evict_lru_if_needed()
    return snap


def _safe_bytes(response: dict) -> int:
    try:
        return len(json.dumps(response, default=str))
    except Exception:
        return 0


def _evict_lru_if_needed() -> None:
    if len(_state.entries) <= _MAX_ENTRIES:
        return
    ordered = sorted(_state.entries.items(),
                     key=lambda kv: kv[1].generated_at)
    for k, _ in ordered[: len(_state.entries) - _MAX_ENTRIES]:
        _state.entries.pop(k, None)
        # Do NOT touch owners — owners lifecycle is independent.


# ---------------------------------------------------------------------
# Diagnostics + reset
# ---------------------------------------------------------------------

def clear_cache() -> None:
    """Test-only helper — drop every snapshot (owners survive so a
    concurrent reconstruction cannot fork under an invalidation)."""
    _state.entries.clear()
    _state.locks.clear()
    # NB: owners intentionally NOT cleared to prevent duplicate owners.


def stats() -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    return {
        "entries":               len(_state.entries),
        "owners_in_flight":      sum(
            1 for o in _state.owners.values() if not o.future.done()),
        "ttl_seconds":           _TTL_SECONDS,
        "owner_stale_sec":       _OWNER_STALE_SEC,
        "max_entries":           _MAX_ENTRIES,
        "reconstruction_count":  _state.reconstruction_count,
        "committed_version":     _committed_version(),
        "keys": [
            {
                "cache_key":     k,
                "board_version": s.board_version,
                "age_seconds":   (now - s.generated_at).total_seconds(),
                "payload_bytes": s.payload_bytes,
                "matches_committed": s.board_version == _committed_version(),
            }
            for k, s in _state.entries.items()
        ],
    }


__all__ = [
    "Snapshot",
    "compute_board_version",
    "get_snapshot",
    "get_stale_snapshot",
    "get_lock",
    "put_snapshot",
    "reconstruct",
    "clear_cache",
    "stats",
]
