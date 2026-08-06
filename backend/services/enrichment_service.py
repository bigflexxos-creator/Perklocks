"""EnrichmentService — Phase 1c enrichment side-car collection owner.

Post-publication enrichment (xG, H2H, lineup, injuries, form, matchup
notes, hot-scorer signals, steam markers) must never mutate the
immutable published prediction.  This service is the append-write
owner of the `pick_enrichment` collection.

Contract
────────
- Enrichment READS from `prediction_snapshots` when it needs to
  reference the published prediction values.
- Enrichment WRITES ONLY to `pick_enrichment`.
- The publication snapshot is NEVER modified.

Enrichment record schema
────────────────────────
    {
      enrichment_id:   str (uuid),
      prediction_id:   str,
      snapshot_version: int | None,
      enrichment_type: str  (e.g. "xg" | "h2h" | "lineup" | "injury"
                             | "form" | "matchup" | "hot_scorer"
                             | "steam" | "signal" | "notes"),
      data:            dict (freeform, enrichment-type-specific),
      source:          str (which module wrote the record),
      created_at:      iso8601 UTC,
      updated_at:      iso8601 UTC,
      is_active:       bool (latest enrichment of this type is active),
    }

Uniqueness
──────────
Latest-active enrichment per (prediction_id, enrichment_type) is
enforced by (a) deactivating prior records of the same type before
inserting the new one, and (b) an upsert-style `record()` API.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger("lockscore.enrichment")

COLLECTION = "pick_enrichment"

# Curated enrichment-type vocabulary — additions require a code review.
KNOWN_ENRICHMENT_TYPES = (
    "xg",
    "h2h",
    "lineup",
    "injury",
    "form",
    "matchup",
    "hot_scorer",
    "steam",
    "signal",
    "notes",
    "market_signal",
    "prop_context",
)


class EnrichmentService:
    """Single owner of post-publication enrichment writes."""

    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self.db = db

    async def ensure_indices(self) -> None:
        try:
            await self.db[COLLECTION].create_index(
                [("prediction_id", 1), ("enrichment_type", 1),
                 ("is_active", 1)],
                name="pred_type_active_idx",
            )
            await self.db[COLLECTION].create_index(
                [("prediction_id", 1), ("updated_at", -1)],
                name="pred_updated_idx",
            )
            await self.db[COLLECTION].create_index(
                "enrichment_type", name="type_idx",
            )
            await self.db[COLLECTION].create_index(
                "source", name="source_idx",
            )
        except Exception as e:
            logger.debug("pick_enrichment index create: %s", e)

    async def record(
        self, *,
        prediction_id: str,
        enrichment_type: str,
        data: dict,
        source: str,
        deactivate_prior: bool = True,
    ) -> dict:
        """Record an enrichment payload for a prediction.

        By default, prior records of the same (prediction, type) are
        marked `is_active=False` before the new record is inserted so
        callers reading with `{is_active: True}` always get the
        latest.
        """
        if enrichment_type not in KNOWN_ENRICHMENT_TYPES:
            # Not fatal — accept but log so we can grow the vocabulary
            # deliberately.
            logger.debug(
                "enrichment_type=%r not in KNOWN_ENRICHMENT_TYPES",
                enrichment_type)

        # Look up active snapshot version (for provenance only).
        from services.prediction_publication_service import (
            SNAPSHOT_COLLECTION,
        )
        try:
            snap = await self.db[SNAPSHOT_COLLECTION].find_one(
                {"prediction_id": prediction_id, "is_active": True},
                {"snapshot_version": 1, "_id": 0},
            )
            snap_ver = snap.get("snapshot_version") if snap else None
        except Exception:
            snap_ver = None

        if deactivate_prior:
            try:
                await self.db[COLLECTION].update_many(
                    {"prediction_id": prediction_id,
                     "enrichment_type": enrichment_type,
                     "is_active": True},
                    {"$set": {"is_active": False}},
                )
            except Exception as e:
                logger.debug("deactivate prior enrichment: %s", e)

        now = datetime.now(timezone.utc).isoformat()
        doc = {
            "enrichment_id": str(uuid.uuid4()),
            "prediction_id": prediction_id,
            "snapshot_version": snap_ver,
            "enrichment_type": enrichment_type,
            "data": data or {},
            "source": source,
            "created_at": now,
            "updated_at": now,
            "is_active": True,
        }
        await self.db[COLLECTION].insert_one(doc)
        return doc

    async def get_active(
        self, prediction_id: str,
        enrichment_type: Optional[str] = None,
    ) -> list[dict]:
        q: dict = {"prediction_id": prediction_id, "is_active": True}
        if enrichment_type is not None:
            q["enrichment_type"] = enrichment_type
        return await self.db[COLLECTION].find(
            q, {"_id": 0}).to_list(50)


__all__ = ["EnrichmentService", "COLLECTION", "KNOWN_ENRICHMENT_TYPES"]
