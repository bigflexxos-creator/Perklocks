"""Publication Reconciliation — Session A (2026-06).

Safe retry path for picks stuck in ``PUBLICATION_PENDING`` or
``FAILED``.  Bounded and idempotent — the underlying
``PredictionPublicationService.publish`` is already idempotent (unique
key on ``prediction_id + idempotency_key``), so republishing a
successful pick is a no-op.

Scope
─────
Only picks that ALREADY passed the canonical boundary once and
subsequently entered ``FAILED`` (transient) or remain in
``PUBLICATION_PENDING`` (never completed) are retried.

Picks in ``REJECTED`` state are NEVER retried — that state is
permanent by design.  This is what enforces "no infinite retry" for
permanently invalid picks.

Bounded retry
─────────────
* Per-pick attempts are tracked in ``publication_attempts``.
* A pick that exceeds ``MAX_PUBLICATION_ATTEMPTS`` (see boundary
  module) transitions to ``REJECTED`` with reason
  ``MAX_ATTEMPTS_EXCEEDED`` — the reconciler drops it from future
  runs.

Never scheduled from this module — Session A ships the function.  A
scheduler wiring is intentionally OUT of scope so we don't quietly
start a recurring job during a delicate closure.  The final report
MUST tag this as "IMPLEMENTED BUT NOT SCHEDULED".
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

from services.canonical_publication_boundary import (
    MAX_PUBLICATION_ATTEMPTS,
    PublicationState,
)

logger = logging.getLogger("lockscore.publication_reconciler")


async def reconcile_stuck_publications(
    db: AsyncIOMotorDatabase,
    *,
    max_age_minutes: int = 5,
    limit: int = 200,
    publication_source: Optional[str] = None,
) -> dict:
    """Find PENDING/FAILED picks older than ``max_age_minutes`` and
    safely retry them through ``publish_batch``.

    Parameters
    ----------
    max_age_minutes :
        Only picks whose ``publication_last_state_at`` is older than
        this age are considered.  Small windows starve the reconciler;
        large windows delay recovery.  5-minute default is safe.
    limit :
        Cap on the number of picks retried per invocation.  Prevents
        a runaway sweep from blocking the event loop.
    publication_source :
        Optional filter — reconcile only picks from a specific
        producer.  ``None`` reconciles ALL sources.

    Returns
    -------
    dict summary with retry counters.
    """
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(minutes=max_age_minutes)).isoformat().replace(
        "+00:00", "Z",
    )

    query: dict[str, Any] = {
        "publication_state": {
            "$in": [
                PublicationState.PUBLICATION_PENDING.value,
                PublicationState.FAILED.value,
            ],
        },
        "publication_last_state_at": {"$lt": cutoff},
    }
    if publication_source:
        query["publication_source"] = publication_source

    stuck = await db.picks.find(query).limit(int(limit)).to_list(
        length=int(limit),
    )
    if not stuck:
        return {
            "ok":         True,
            "cutoff":     cutoff,
            "scanned":    0,
            "retried":    0,
            "published":  0,
            "rejected":   0,
            "failed":     0,
            "exhausted":  0,
        }

    # ── Handle exhausted attempts BEFORE retrying ──────────────────
    exhausted_ids: list[str] = []
    fresh: list[dict] = []
    for p in stuck:
        att = int(p.get("publication_attempts") or 0)
        if att >= MAX_PUBLICATION_ATTEMPTS:
            exhausted_ids.append(p.get("id"))
            continue
        fresh.append(p)

    if exhausted_ids:
        now_iso = datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z",
        )
        try:
            await db.picks.update_many(
                {"id": {"$in": exhausted_ids}},
                {"$set": {
                    "publication_state": PublicationState.REJECTED.value,
                    "publication_rejected_at": now_iso,
                    "publication_rejection_reasons":
                        ["MAX_ATTEMPTS_EXCEEDED"],
                    "off_board": True,
                    "no_bet":    True,
                }},
            )
        except Exception as e:                      # pragma: no cover
            logger.debug("exhausted mark failed: %s", e)

    if not fresh:
        return {
            "ok": True, "cutoff": cutoff,
            "scanned": len(stuck), "retried": 0,
            "published": 0, "rejected": 0, "failed": 0,
            "exhausted": len(exhausted_ids),
        }

    # ── Group by publication_source and re-run through publish_batch.
    by_src: dict[str, list[dict]] = {}
    for p in fresh:
        src = p.get("publication_source") or "reconciler"
        by_src.setdefault(src, []).append(p)

    from services.prediction_publication_service import (
        PredictionPublicationService,
    )
    publisher = PredictionPublicationService(db)
    try:
        await publisher.ensure_indices()
    except Exception:                                # pragma: no cover
        pass

    total_published = 0
    total_rejected  = 0
    total_failed    = 0
    for src, batch in by_src.items():
        try:
            summary = await publisher.publish_batch(
                batch, publication_source=src, dual_write=True,
            )
            total_published += int(summary.get("new_snapshots", 0)) + int(
                summary.get("existing_snapshots", 0),
            )
            total_rejected += int(summary.get("boundary_rejected", 0)) + int(
                summary.get("integrity_rejected", 0),
            )
            total_failed += int(summary.get("publication_failed", 0))
        except Exception as e:                      # pragma: no cover
            logger.warning("reconciler batch failure for %s: %s", src, e)

    return {
        "ok":         True,
        "cutoff":     cutoff,
        "scanned":    len(stuck),
        "retried":    len(fresh),
        "published":  total_published,
        "rejected":   total_rejected,
        "failed":     total_failed,
        "exhausted":  len(exhausted_ids),
    }


__all__ = ["reconcile_stuck_publications"]
