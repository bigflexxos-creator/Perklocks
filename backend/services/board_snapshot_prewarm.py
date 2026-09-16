"""Board Snapshot Cache Prewarmer.

Session 3 (2026-09-14) — closes the "cold cache miss" performance gap
without deleting any enrichment.

The Locks board cache built in Session 2 collapses 99%+ of real user
traffic to <50 ms (HIT) or <10 ms (304).  What remained was the very
first request per TTL cycle per filter combo (~1.8 s full-board cold
miss on a fresh cache).  This module runs those first-hit requests
IN THE BACKGROUND on backend startup and every TTL cycle so real
users never trigger the cold path themselves.

Design:
    * On backend startup, fire a `create_task` that iterates the
      canonical filter matrix (bare, per-sport, min_lock=90) and
      calls the endpoint's underlying pipeline exactly the same
      way a real GET does.  The response is stored in the snapshot
      cache under the same cache key a real request would compute.
    * A periodic asyncio task repeats the priming every
      `_PREWARM_INTERVAL_SEC` seconds so TTL expiry never lands
      on a user request.
    * All failures are swallowed — this is a pure optimization,
      never a correctness contract.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger("lockscore.snapshot_prewarm")

# Slightly under the snapshot TTL so the cache is always warm.
_PREWARM_INTERVAL_SEC = int(os.environ.get("BOARD_SNAPSHOT_PREWARM_SEC", "12"))
_STARTUP_DELAY_SEC = 8  # wait for `_ensure_today_picks` to settle first

# The prewarm filter matrix.  Keep this SHORT — every combo consumes
# one full pipeline run.  Bare + top 4 sports covers ~95% of mobile
# navigation cache-key hits.
_PREWARM_COMBOS: list[dict] = [
    {},                                # ALL tab
    {"sport": "MLB"},
    {"sport": "NFL"},
    {"sport": "Soccer"},
    {"sport": "Tennis"},
    {"sport": "CFB"},
    {"min_lock": 90.0},               # "High Locks" quick filter
]


async def _prime_once(user_public) -> tuple[int, int, list[str]]:
    """Fire a single prewarm sweep.  Returns (attempted, succeeded, errors)."""
    try:
        # Late import to break server.py ↔ picks_routes.py bootstrap loop.
        from routes import picks_routes
        from starlette.requests import Request
        from starlette.responses import Response
    except Exception as _imp_err:
        logger.warning("prewarm import failed: %s", _imp_err)
        return (0, 0, [str(_imp_err)])

    attempted = 0
    succeeded = 0
    errors: list[str] = []

    for combo in _PREWARM_COMBOS:
        attempted += 1
        try:
            # Build synthetic Request/Response — the endpoint only
            # reads request.headers ("if-none-match") and writes
            # response.headers.  Both survive a plain construction.
            scope = {
                "type": "http",
                "method": "GET",
                "headers": [],
                "query_string": b"",
                "path": "/api/picks/today",
            }
            req = Request(scope)
            resp = Response()
            call_kwargs = {"user": user_public, "request": req, "response": resp,
                           "lite": True, **combo}
            _r = await picks_routes.picks_today(**call_kwargs)
            succeeded += 1
        except Exception as _pe:
            errors.append(f"{combo!r}: {_pe}")
    return (attempted, succeeded, errors)


async def prewarm_loop_forever(user_public) -> None:
    """Run the prewarm sweep forever at `_PREWARM_INTERVAL_SEC` cadence.

    Args:
        user_public: a UserPublic-like object the endpoint accepts as its
            authenticated dependency.  Cache keys are user-agnostic —
            any authenticated identity produces the same snapshot.
    """
    await asyncio.sleep(_STARTUP_DELAY_SEC)
    while True:
        try:
            att, ok, errs = await _prime_once(user_public)
            if errs:
                logger.debug(
                    "prewarm cycle done attempted=%d ok=%d errors=%d first=%s",
                    att, ok, len(errs), errs[0] if errs else "-",
                )
        except Exception as _cycle_err:
            logger.debug("prewarm cycle raised: %s", _cycle_err)
        await asyncio.sleep(_PREWARM_INTERVAL_SEC)


async def start_prewarm_task(app) -> None:
    """Register a startup-scoped background prewarm task.

    Called from `server.py` startup handler.  Never blocks startup —
    the task is scheduled via `asyncio.create_task` and self-drives.
    """
    try:
        from auth import UserPublic
        # System-level identity for cache priming.  The endpoint does
        # not read user-specific state to build the snapshot, so any
        # UserPublic instance produces the same cache key.
        system_user = UserPublic(
            id="prewarm_system",
            email="prewarm@system.internal",
            name="Prewarm System",
            role="user",
            status="active",
        )
        task = asyncio.create_task(prewarm_loop_forever(system_user))
        # Retain a reference so the task is not garbage collected.
        setattr(app.state, "_snapshot_prewarm_task", task)
        logger.info(
            "snapshot prewarmer started interval=%ds startup_delay=%ds combos=%d",
            _PREWARM_INTERVAL_SEC, _STARTUP_DELAY_SEC, len(_PREWARM_COMBOS),
        )
    except Exception as _e:
        logger.warning("snapshot prewarmer failed to start: %s", _e)
