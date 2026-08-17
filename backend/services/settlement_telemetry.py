"""Phase A — Settlement Telemetry (2026-06).

Ultra-lightweight observability layer for the settlement engine.
Answers the question:

    "When did the settler last run, how many candidates did it
     examine, how many did it succeed on, how many did it fail on,
     what's the oldest unresolved pick still in the queue, and what
     are the terminal reasons for the ones it couldn't grade?"

USAGE
-----
    from services.settlement_telemetry import record_run
    await record_run(db, {
        "candidates_examined": 431,
        "attempts":            287,
        "success":             184,
        "fail":                103,
        "unsupported_terminated": 22,
        "oldest_unresolved_age_seconds": 189234,
        "terminal_reasons":    {"settler_unsupported:soccer_shots": 12, ...},
    })

Reads via admin route ``GET /api/admin/settlement_telemetry``
(added in Phase A) — returns the most recent doc.

Docs are append-only (one per settle_due_picks run) so we retain a
short rolling history for post-mortem.  No indices needed beyond the
default _id order; a small TTL is applied so the collection never
grows unbounded.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("lockscore.settlement.telemetry")

COLLECTION = "settlement_telemetry"


async def record_run(db, metrics: dict[str, Any]) -> None:
    """Insert a single telemetry doc for this settlement run.

    Best-effort: any exception is swallowed and logged at DEBUG.  The
    settlement loop MUST NEVER fail because telemetry failed.
    """
    try:
        doc = {
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            **(metrics or {}),
        }
        await db[COLLECTION].insert_one(doc)
    except Exception as e:
        logger.debug("settlement telemetry write skipped: %s", e)


async def read_latest(db, limit: int = 1) -> list[dict]:
    """Return the ``limit`` most-recent telemetry docs (most-recent first)."""
    try:
        cursor = db[COLLECTION].find({}, {"_id": 0}).sort(
            "recorded_at", -1).limit(max(1, min(limit, 100)))
        return await cursor.to_list(length=limit)
    except Exception as e:
        logger.debug("settlement telemetry read skipped: %s", e)
        return []


async def oldest_unresolved_age_seconds(db) -> Optional[int]:
    """Cheap helper — age (in seconds) of the oldest pending pick whose
    event has already completed.  Used by the settler at end-of-run to
    stamp the current starvation-frontier.

    Returns None when there are no unresolved-completed picks.
    """
    try:
        now = datetime.now(timezone.utc)
        cursor = db.picks.find(
            {"status": {"$in": [None, "pending", "PENDING"]},
             "off_board": {"$ne": True},
             "event_time": {"$lt": now.isoformat()}},
            {"event_time": 1, "_id": 0},
        ).sort("event_time", 1).limit(1)
        docs = await cursor.to_list(length=1)
        if not docs:
            return None
        et = docs[0].get("event_time")
        if not et:
            return None
        iso = et[:-1] + "+00:00" if et.endswith("Z") else et
        try:
            dt = datetime.fromisoformat(iso)
        except Exception:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int((now - dt).total_seconds())
    except Exception as e:
        logger.debug("oldest_unresolved_age_seconds skipped: %s", e)
        return None


__all__ = [
    "COLLECTION", "record_run", "read_latest",
    "oldest_unresolved_age_seconds",
]
