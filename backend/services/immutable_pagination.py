"""Immutable Board Revision Pagination — Preview surgical fix.

2026-10-02 — bug: a client pinned to revision A could receive
revision-B rows on page 2 after revision B published.  Fix: when a
new revision commits, snapshot the complete canonical pick-id list
into ``board_revision_snapshots`` (bounded to the last N revisions),
so pagination keyed on revision A continues to return revision-A
rows until that revision is pruned, at which point we return
REVISION_UNAVAILABLE and force a pagination restart.

This module is CONSUMED by the picks router; it does not run any
background workers (consistent with Preview write isolation).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("lockscore.pagination")

_SNAPSHOTS_COLL = "board_revision_snapshots"
_SNAPSHOT_RETENTION = 10


async def record_revision_snapshot(db, revision_id: str,
                                    pick_ids: list[str]) -> None:
    """Commit a revision snapshot.  Idempotent per revision_id.

    Writes are gated by the ONE DATABASE AUTHORITY module — on
    Preview this is a no-op (returns without writing).
    """
    try:
        from services.data_authority import canonical_write_enabled
        if not canonical_write_enabled():
            logger.debug("record_revision_snapshot: suppressed on Preview")
            return
    except Exception:
        pass
    now = datetime.now(timezone.utc).isoformat()
    await db[_SNAPSHOTS_COLL].update_one(
        {"revision_id": revision_id},
        {"$set": {
            "revision_id": revision_id,
            "pick_ids":    list(pick_ids),
            "count":       len(pick_ids),
            "committed_at": now,
        }, "$setOnInsert": {"first_seen": now}},
        upsert=True,
    )
    # Bounded retention: keep only the most recent N snapshots.
    try:
        cursor = db[_SNAPSHOTS_COLL].find({}, {"revision_id": 1, "committed_at": 1}
                                           ).sort("committed_at", -1)
        snaps = [row async for row in cursor]
        to_prune = [s["revision_id"] for s in snaps[_SNAPSHOT_RETENTION:]]
        if to_prune:
            await db[_SNAPSHOTS_COLL].delete_many({"revision_id": {"$in": to_prune}})
    except Exception as e:
        logger.debug("snapshot prune err: %s", e)


async def resolve_pagination(db, revision_id: Optional[str],
                              page: int, per_page: int) -> dict:
    """Return the slice of pick ids pinned to ``revision_id``.

    Verdict:
      * ``REVISION_AVAILABLE``   — snapshot found, slice returned
      * ``REVISION_UNAVAILABLE`` — snapshot pruned or never recorded
      * ``NO_REVISION_REQUESTED`` — caller did not pin a revision
    """
    if not revision_id:
        return {"state": "NO_REVISION_REQUESTED"}
    snap = await db[_SNAPSHOTS_COLL].find_one({"revision_id": revision_id})
    if not snap:
        return {"state": "REVISION_UNAVAILABLE",
                "message": "pagination restart required",
                "revision_id": revision_id}
    pick_ids = list(snap.get("pick_ids") or [])
    total    = len(pick_ids)
    page = max(1, int(page or 1))
    per_page = max(1, min(500, int(per_page or 25)))
    start = (page - 1) * per_page
    end   = min(start + per_page, total)
    return {
        "state":        "REVISION_AVAILABLE",
        "revision_id":  revision_id,
        "page":         page,
        "per_page":     per_page,
        "total":        total,
        "pick_ids":     pick_ids[start:end],
        "has_more":     end < total,
    }


__all__ = [
    "record_revision_snapshot",
    "resolve_pagination",
    "_SNAPSHOTS_COLL",
    "_SNAPSHOT_RETENTION",
]
