"""SettlementService — Phase 1c settlement collection owner.

The published snapshot is immutable.  Settlement decisions live in a
separate append-only collection `settlement_events`, keyed by
prediction_id + snapshot_version.

Contract
────────
- Settlement READS from `prediction_snapshots` (the immutable source
  of truth for line / odds / grade).  Never from the mutable `picks`
  document.
- Settlement WRITES to `settlement_events` (append-only).  Never to
  `prediction_snapshots`.
- Compatibility writes to `picks.status` / `picks.settled_at` are
  TRANSITIONAL — labeled with `_compat_write=True` on the event
  record so we can find and remove them in a future cleanup.

Event schema
────────────
    {
      event_id:            str (uuid),
      prediction_id:       str,
      snapshot_version:    int,
      result:              "won" | "lost" | "void" | "push" | "cancelled",
      settled_at:          iso8601 UTC,
      source:              str (e.g. "settlement_engine" | "kbo_settlement"
                                   | "prop_settlement" | "soccer_espn_settle"
                                   | "brain.nrfi_engine" | "tennis_extra.settle"),
      actual_result:       dict (freeform — final score, prop stats, etc.),
      is_active:           bool (only the LATEST event per (prediction, snap) is active),
      compat_write:        bool (True if we also mirrored to picks.status),
    }
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger("lockscore.settlement")

COLLECTION = "settlement_events"

VALID_RESULTS = ("won", "lost", "void", "push", "cancelled")


class SettlementService:
    """Single owner of settlement decisions.  All settle-* modules
    must call this service instead of mutating `picks` directly."""

    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self.db = db

    async def ensure_indices(self) -> None:
        try:
            await self.db[COLLECTION].create_index(
                [("prediction_id", 1), ("settled_at", -1)],
                name="prediction_settled_at_idx",
            )
            await self.db[COLLECTION].create_index(
                [("prediction_id", 1), ("is_active", 1)],
                name="prediction_active_idx",
            )
            await self.db[COLLECTION].create_index(
                "source", name="source_idx",
            )
            await self.db[COLLECTION].create_index(
                "settled_at", name="settled_at_idx",
            )
        except Exception as e:
            logger.debug("settlement_events index create: %s", e)

    async def record(
        self, *,
        prediction_id: str,
        result: str,
        source: str,
        actual_result: Optional[dict] = None,
        compat_write_to_picks: bool = True,
    ) -> dict:
        """Record a settlement decision.

        - Fetches the active snapshot for the prediction.
        - Appends an event to `settlement_events` with `is_active=True`.
        - Marks all prior events for this prediction as `is_active=False`.
        - Optionally mirrors `status` + `settled_at` onto the picks
          document for backwards compatibility (temporary — labeled).
        """
        if result not in VALID_RESULTS:
            raise ValueError(
                f"invalid settlement result {result!r} "
                f"(must be one of {VALID_RESULTS})")

        # Read the active snapshot to lock the settled version in.
        from services.prediction_publication_service import (
            SNAPSHOT_COLLECTION,
        )
        snap = await self.db[SNAPSHOT_COLLECTION].find_one(
            {"prediction_id": prediction_id, "is_active": True},
            {"snapshot_version": 1, "published_line": 1,
             "published_odds": 1, "_id": 0},
        )
        snap_ver = snap.get("snapshot_version") if snap else None

        # Deactivate any prior events for this prediction.
        try:
            await self.db[COLLECTION].update_many(
                {"prediction_id": prediction_id, "is_active": True},
                {"$set": {"is_active": False}},
            )
        except Exception as e:
            logger.debug("deactivate prior settlement events: %s", e)

        now = datetime.now(timezone.utc).isoformat()
        event = {
            "event_id": str(uuid.uuid4()),
            "prediction_id": prediction_id,
            "snapshot_version": snap_ver,
            "result": result,
            "settled_at": now,
            "source": source,
            "actual_result": actual_result or {},
            "is_active": True,
            "compat_write": bool(compat_write_to_picks),
        }
        await self.db[COLLECTION].insert_one(event)

        # Transitional compatibility mirror — allows the existing
        # frontend + endpoints that still read `pick.status` to work
        # unchanged.  This will be removed once every consumer has
        # migrated to reading from `settlement_events`.
        if compat_write_to_picks:
            try:
                await self.db.picks.update_one(
                    {"id": prediction_id},
                    {"$set": {
                        "status": _pick_status_from_result(result),
                        "settled_at": now,
                        "settlement_result": result,
                        "settlement_source": source,
                        "_compat_settlement": True,
                    }},
                )
            except Exception as e:
                logger.warning("settlement compat write err: %s", e)

        return event

    async def get_active_event(
        self, prediction_id: str,
    ) -> Optional[dict]:
        return await self.db[COLLECTION].find_one(
            {"prediction_id": prediction_id, "is_active": True},
            {"_id": 0},
        )


def _pick_status_from_result(result: str) -> str:
    """Map settlement result → the legacy `pick.status` string that
    the frontend expects."""
    return {
        "won":       "won",
        "lost":      "lost",
        "void":      "void",
        "push":      "void",
        "cancelled": "void",
    }.get(result, "pending")


__all__ = ["SettlementService", "COLLECTION", "VALID_RESULTS"]
