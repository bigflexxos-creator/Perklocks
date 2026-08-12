"""Producer Health Telemetry — Session A (2026-06).

Records per-producer publication activity so operators can detect
stuck producers, silent failures, or "no current events" states.

Collection
──────────
``producer_health`` — one document per ``publication_source`` (unique
index on ``publication_source``).  Fields:

    {
      publication_source:  "canonical_pipeline" | "espn_soccer_fixtures" |
                            "mls_direct_inject" | "soccer_prop_inject" | …,
      last_attempt_at:     ISO8601 UTC when the last publish attempt happened,
      last_success_at:     ISO8601 UTC when the last PUBLISHED verdict landed,
      last_rejection_at:   ISO8601 UTC when the last REJECTED verdict landed,
      last_failure_at:     ISO8601 UTC when the last FAILED verdict landed,
      last_no_events_at:   ISO8601 UTC when the producer ran and had 0 picks
                            (distinct from a broken/stale state),
      picks_generated:     lifetime count of picks attempted,
      picks_published:     lifetime count of picks that reached PUBLISHED,
      picks_rejected:      lifetime count of REJECTED picks,
      picks_failed:        lifetime count of FAILED picks (transient errors),
      last_rejection_reasons: { REASON: count, … } cumulative,
      last_batch: {
          attempted, published, rejected, failed, no_events_flag,
          error_message,  # optional last transient error text
          at,
      },
    }

Behaviour
─────────
* All updates are best-effort — never raise.
* Uses upsert so first call for a producer creates the doc.
* Timezone-naive strings are always ISO8601 UTC with a `Z` suffix.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger("lockscore.producer_health")

PRODUCER_HEALTH_COLLECTION = "producer_health"


def _iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


async def ensure_indices(db: AsyncIOMotorDatabase) -> None:
    """Idempotent — safe to call from ensure_indices paths."""
    try:
        await db[PRODUCER_HEALTH_COLLECTION].create_index(
            "publication_source", unique=True, name="producer_source_uniq",
        )
    except Exception as e:                          # pragma: no cover
        logger.debug("producer_health ensure_indices: %s", e)


async def record_batch(
    db: AsyncIOMotorDatabase,
    *,
    publication_source: str,
    attempted: int,
    published: int,
    rejected: int,
    failed: int,
    rejection_reasons: Optional[dict[str, int]] = None,
    error_message: Optional[str] = None,
) -> None:
    """Record a single batch's outcome for a producer."""
    if not publication_source:
        return
    now = _iso_utc()
    no_events = attempted == 0

    inc: dict[str, Any] = {
        "picks_generated": int(attempted),
        "picks_published": int(published),
        "picks_rejected":  int(rejected),
        "picks_failed":    int(failed),
    }
    if rejection_reasons:
        for r, n in rejection_reasons.items():
            inc[f"rejection_reason_counts.{r}"] = int(n)

    set_fields: dict[str, Any] = {
        "publication_source": publication_source,
        "last_attempt_at":    now,
        "last_batch": {
            "attempted": int(attempted),
            "published": int(published),
            "rejected":  int(rejected),
            "failed":    int(failed),
            "no_events_flag":  bool(no_events),
            "error_message":   error_message,
            "at":              now,
        },
    }
    if published:
        set_fields["last_success_at"] = now
    if rejected:
        set_fields["last_rejection_at"] = now
    if failed:
        set_fields["last_failure_at"] = now
    if no_events:
        set_fields["last_no_events_at"] = now

    try:
        await db[PRODUCER_HEALTH_COLLECTION].update_one(
            {"publication_source": publication_source},
            {"$set": set_fields, "$inc": inc},
            upsert=True,
        )
    except Exception as e:                          # pragma: no cover
        logger.debug("producer_health.record_batch failed for %s: %s",
                     publication_source, e)


async def summary(db: AsyncIOMotorDatabase) -> list[dict]:
    """Return a compact summary of every producer's current health.
    Never exposes secrets or provider payloads."""
    try:
        rows = await db[PRODUCER_HEALTH_COLLECTION].find(
            {}, projection={"_id": 0},
        ).to_list(length=200)
    except Exception:
        return []
    out = []
    for r in rows:
        out.append({
            "publication_source":    r.get("publication_source"),
            "last_attempt_at":       r.get("last_attempt_at"),
            "last_success_at":       r.get("last_success_at"),
            "last_rejection_at":     r.get("last_rejection_at"),
            "last_failure_at":       r.get("last_failure_at"),
            "last_no_events_at":     r.get("last_no_events_at"),
            "picks_generated":       int(r.get("picks_generated") or 0),
            "picks_published":       int(r.get("picks_published") or 0),
            "picks_rejected":        int(r.get("picks_rejected") or 0),
            "picks_failed":          int(r.get("picks_failed") or 0),
            "rejection_reason_counts":
                dict(r.get("rejection_reason_counts") or {}),
            "last_batch":            r.get("last_batch"),
        })
    return out


__all__ = [
    "PRODUCER_HEALTH_COLLECTION",
    "ensure_indices",
    "record_batch",
    "summary",
]
