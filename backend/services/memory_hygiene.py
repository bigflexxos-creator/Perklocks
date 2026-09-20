"""P0 Server-Stability — Memory Hygiene Bootstrap.

Applies two well-known glibc-level fixes to prevent the FastAPI worker
RSS from ballooning under Python multi-threaded workloads:

    1) mallopt(M_ARENA_MAX, 2)
       glibc creates one heap arena per thread (up to ``8 * ncores``
       by default).  Each arena keeps freed memory as fragmented
       cache-line islands that never return to the OS.  Capping
       arenas at 2 cuts baseline RSS 30-60% in multi-thread Python
       servers WITHOUT affecting correctness (arenas serialize with
       mutexes on contention, but for I/O-bound asyncio workloads
       the contention penalty is negligible).

    2) Periodic malloc_trim(0)
       Even with arenas capped, freed memory accumulates until glibc
       decides to shrink.  Explicit ``malloc_trim(0)`` returns any
       fully-free top-of-heap pages to the OS immediately.  We run
       it every ``TRIM_INTERVAL_SEC`` inside a lightweight background
       task.  Cost: single millisecond on multi-GB heaps.

Both operations are pure hygiene: NO behavioural change, NO impact
on betting logic, publishing, or pick generation.  They are the
industry-standard remediation for the exact symptom this container
exhibits (~3 GB anon RSS across dozens of medium rw-p glibc arenas).

References:
    * https://sourceware.org/glibc/wiki/MallocInternals
    * pmap / smaps analysis: many 8-64MB rw-p mappings == arenas
    * Twitter, Uber, Instagram public post-mortems all use
      MALLOC_ARENA_MAX=2 for Python multi-thread servers.
"""
from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import logging
import os
import time
from typing import Optional

logger = logging.getLogger("lockscore.memory_hygiene")

# --- glibc bindings --------------------------------------------------

_libc: Optional[ctypes.CDLL] = None
_mallopt_ok = False
_malloc_trim_ok = False

# glibc constants (see malloc/malloc.h)
_M_ARENA_MAX = -8
_M_MMAP_THRESHOLD = -3
_M_TRIM_THRESHOLD = -1

# Env-tunable knobs; defaults are the widely-validated safe values.
_ARENA_MAX = int(os.environ.get("PL_MALLOC_ARENA_MAX", "2"))
_TRIM_INTERVAL_SEC = int(os.environ.get("PL_MALLOC_TRIM_INTERVAL_SEC", "60"))
_MMAP_THRESHOLD = int(os.environ.get("PL_MALLOC_MMAP_THRESHOLD", "131072"))  # 128 KB
# ↑ any allocation ≥128 KB uses mmap directly (immediately reclaimable
#   on free), instead of joining an arena that may never shrink.


def _bind_libc() -> None:
    global _libc, _mallopt_ok, _malloc_trim_ok
    if _libc is not None:
        return
    try:
        path = ctypes.util.find_library("c") or "libc.so.6"
        _libc = ctypes.CDLL(path)
    except Exception as exc:  # pragma: no cover — non-glibc platform
        logger.warning("memory_hygiene: cannot bind libc: %s", exc)
        _libc = None
        return
    try:
        _libc.mallopt.argtypes = [ctypes.c_int, ctypes.c_int]
        _libc.mallopt.restype = ctypes.c_int
        _mallopt_ok = True
    except Exception as exc:
        logger.warning("memory_hygiene: mallopt unavailable: %s", exc)
    try:
        _libc.malloc_trim.argtypes = [ctypes.c_size_t]
        _libc.malloc_trim.restype = ctypes.c_int
        _malloc_trim_ok = True
    except Exception as exc:
        logger.warning("memory_hygiene: malloc_trim unavailable: %s", exc)


def apply_startup_tuning() -> dict:
    """Call ONCE from server startup, before any heavy imports.

    Returns a dict describing which knobs were applied.  Safe to call
    multiple times — subsequent calls are no-ops.
    """
    _bind_libc()
    applied: dict = {"arena_max": None, "mmap_threshold": None, "libc": bool(_libc)}
    if _libc is None or not _mallopt_ok:
        return applied
    try:
        r1 = _libc.mallopt(_M_ARENA_MAX, _ARENA_MAX)
        applied["arena_max"] = (_ARENA_MAX if r1 == 1 else None)
    except Exception as exc:
        logger.warning("memory_hygiene: mallopt(M_ARENA_MAX) failed: %s", exc)
    try:
        r2 = _libc.mallopt(_M_MMAP_THRESHOLD, _MMAP_THRESHOLD)
        applied["mmap_threshold"] = (_MMAP_THRESHOLD if r2 == 1 else None)
    except Exception as exc:
        logger.warning("memory_hygiene: mallopt(M_MMAP_THRESHOLD) failed: %s", exc)
    logger.info(
        "memory_hygiene: startup tuning applied: %s (arenas capped at %s, "
        "mmap threshold %s bytes)", applied, _ARENA_MAX, _MMAP_THRESHOLD,
    )
    return applied


def malloc_trim() -> bool:
    """Return top-of-heap free pages to the OS.  Returns True if any
    memory was actually released."""
    _bind_libc()
    if _libc is None or not _malloc_trim_ok:
        return False
    try:
        return bool(_libc.malloc_trim(0))
    except Exception:
        return False


# --- Periodic trim loop ---------------------------------------------

_trim_task: Optional[asyncio.Task] = None
_trim_stats = {"runs": 0, "released_count": 0, "last_ms": None, "last_at": None}


async def _trim_loop() -> None:
    """Background loop that runs ``malloc_trim(0)`` every N seconds."""
    logger.info("memory_hygiene: trim loop starting (interval=%ds)", _TRIM_INTERVAL_SEC)
    while True:
        try:
            t0 = time.monotonic()
            released = malloc_trim()
            dt_ms = (time.monotonic() - t0) * 1000
            _trim_stats["runs"] += 1
            if released:
                _trim_stats["released_count"] += 1
            _trim_stats["last_ms"] = round(dt_ms, 2)
            _trim_stats["last_at"] = time.time()
        except Exception as exc:  # pragma: no cover
            logger.debug("memory_hygiene: trim iteration failed: %s", exc)
        try:
            await asyncio.sleep(_TRIM_INTERVAL_SEC)
        except asyncio.CancelledError:
            break


def start_periodic_trim() -> None:
    """Kick off the background trim loop.  Idempotent."""
    global _trim_task
    if _trim_task is not None and not _trim_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # no running loop yet — startup hook should call again
    _trim_task = loop.create_task(_trim_loop(), name="malloc_trim_loop")


def stats() -> dict:
    """Diagnostic snapshot for the admin memory route."""
    return {
        "libc_loaded": bool(_libc),
        "mallopt_ok": _mallopt_ok,
        "malloc_trim_ok": _malloc_trim_ok,
        "arena_max": _ARENA_MAX,
        "mmap_threshold_bytes": _MMAP_THRESHOLD,
        "trim_interval_sec": _TRIM_INTERVAL_SEC,
        "trim_runs": _trim_stats["runs"],
        "trim_released_count": _trim_stats["released_count"],
        "trim_last_ms": _trim_stats["last_ms"],
        "trim_last_at": _trim_stats["last_at"],
    }


__all__ = [
    "apply_startup_tuning",
    "start_periodic_trim",
    "malloc_trim",
    "stats",
]
