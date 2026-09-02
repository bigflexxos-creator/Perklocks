"""picks_mirror_sync — settlement mirror preservation (Root Closure).

Some legacy writers (`soccer_prop_inject`, `mls_direct_inject`,
`espn_soccer_fixtures`) use `ReplaceOne({"id": p["id"]}, p, upsert=True)`
to publish/refresh picks.  Because ReplaceOne replaces the *entire*
document, any settlement mirror we previously wrote to
`picks.status` (e.g. 'won'/'lost'/'push'/'void') is silently wiped
back to the writer's default `status="pending"`.

This module provides a lightweight post-write reconciler that
re-projects the canonical settlement result from `settlement_events`
onto the `picks` compatibility mirror, so the mirror never lags the
append-only truth ledger.

Contract:
  - The `settlement_events` ledger is the immutable source of truth.
  - The `picks.status` field is a derivative, human-readable mirror.
  - This helper NEVER creates settlements, NEVER fabricates actuals,
    and NEVER touches picks that lack an active ledger row.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable, Optional

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import UpdateOne

log = logging.getLogger("lockscore.picks_mirror_sync")

_VALID_MIRROR = ("won", "lost", "push", "void")


async def reconcile_status_mirror(
    db: AsyncIOMotorDatabase,
    pick_ids: Optional[Iterable[str]] = None,
    *,
    chunk: int = 500,
) -> dict:
    """Re-project canonical settlement results from `settlement_events`
    onto `picks.status` (compatibility mirror).

    If `pick_ids` is provided, only those IDs are reconciled (fast path
    for post-write hooks).  Otherwise a full sweep of active ledger
    rows is performed.

    Returns a summary dict with counts.
    """
    now = datetime.now(timezone.utc)
    reconciled = 0
    skipped = 0

    query: dict = {"is_active": True, "result": {"$in": list(_VALID_MIRROR)}}
    if pick_ids is not None:
        ids = list({str(x) for x in pick_ids if x})
        if not ids:
            return {"reconciled": 0, "skipped": 0, "at": now.isoformat()}
        query["prediction_id"] = {"$in": ids}

    ops: list[UpdateOne] = []
    async for ev in db.settlement_events.find(query, {"prediction_id": 1, "result": 1}):
        pid = ev.get("prediction_id")
        result = ev.get("result")
        if not pid or result not in _VALID_MIRROR:
            skipped += 1
            continue
        ops.append(
            UpdateOne(
                {"id": pid, "status": {"$ne": result}},
                {"$set": {
                    "status": result,
                    "settlement_mirror_reconciled_at": now,
                }},
            )
        )
        if len(ops) >= chunk:
            r = await db.picks.bulk_write(ops, ordered=False)
            reconciled += r.modified_count or 0
            ops = []
    if ops:
        r = await db.picks.bulk_write(ops, ordered=False)
        reconciled += r.modified_count or 0

    if reconciled:
        log.info("picks_mirror_sync reconciled %s picks", reconciled)
    return {"reconciled": reconciled, "skipped": skipped, "at": now.isoformat()}


async def preserve_settlement_on_replace(
    db: AsyncIOMotorDatabase,
    pick_ids: Iterable[str],
) -> None:
    """Convenience hook: call immediately after any `ReplaceOne`-based
    bulk_write that operates on `picks`.  Restores the settlement
    mirror for the touched IDs so ReplaceOne can't silently regress
    the append-only ledger.
    """
    try:
        await reconcile_status_mirror(db, pick_ids=pick_ids)
    except Exception as e:      # never break the caller
        log.warning("preserve_settlement_on_replace failed: %s", e)
