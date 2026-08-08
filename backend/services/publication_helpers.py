"""Shared helper for post-upsert canonical publication.

Purpose
───────
`PredictionPublicationService.publish_batch()` is the ONE write barrier
per PUBLICATION_CONTRACT.md.  Ingest paths that upsert candidates into
`db.picks` outside the main `pick_refresh_orchestrator` (e.g.
`espn_soccer_fixtures`, `soccer/pipeline`, `soccer_hot_scorers`,
`ufc_espn_ingest`) must call this helper right after their upsert
completes so an immutable snapshot exists for each pick before the
canonical board eligibility gate examines it.

This helper is a THIN wrapper — it does not modify prediction values,
does not recompute lock_score / probability / grade / confidence /
Magic Tier, and does not touch Rollover, ranking, or simulators.  It
delegates 100% of the publication payload construction to the
existing `PredictionPublicationService`.

Idempotency, atomicity, mismatch reporting, and error isolation are
all handled inside `publish_batch()`.  A publication failure never
raises — it is logged at WARNING and the upsert path continues.

Usage:
    from services.publication_helpers import publish_upserted_picks
    await publish_upserted_picks(
        db, all_picks, publication_source="ufc_espn_v1",
        caller_label="UFC ESPN sync",
    )
"""
from __future__ import annotations

import logging
from typing import Iterable

logger = logging.getLogger("lockscore.publication_helpers")


async def publish_upserted_picks(
    db,
    picks: Iterable[dict],
    *,
    publication_source: str,
    caller_label: str = "ingest",
    dual_write: bool = True,
) -> dict:
    """Publish an iterable of already-upserted picks canonically.

    Returns the publish_batch summary dict, or an empty ``{}`` when
    the publication step raised (never re-raised — degraded
    visibility is preferable to a broken ingest loop).

    Parameters
    ----------
    db :
        Motor `AsyncIOMotorDatabase` instance (same one used by the
        caller's upsert).
    picks :
        Iterable of pick dicts that were just upserted.  Every dict
        must carry the same stable `id` used in the upsert filter so
        publication's `prediction_id` matches.
    publication_source :
        The `publication_source` string that appears on the resulting
        snapshot and the dual-write.  Should identify the ingest path
        (e.g. ``"ufc_espn_v1"``, ``"espn_soccer_fixtures"``,
        ``"soccer_v1"``, ``"soccer_hot_scorers_v1"``).
    caller_label :
        Human-friendly label used only for log messages.
    dual_write :
        Passthrough — always `True` in Phase 1a per contract.
    """
    picks_list = list(picks)
    if not picks_list:
        return {}
    try:
        from services.prediction_publication_service import (
            PredictionPublicationService,
        )
        publisher = PredictionPublicationService(db)
        try:
            await publisher.ensure_indices()
        except Exception as _idx_err:  # pragma: no cover
            logger.debug("publication ensure_indices for %s: %s",
                         caller_label, _idx_err)
        summary = await publisher.publish_batch(
            picks_list,
            publication_source=publication_source,
            dual_write=dual_write,
        )
        logger.info(
            "%s publication: new=%d existing=%d errors=%d mismatches=%d "
            "board=%s",
            caller_label,
            summary.get("new_snapshots", 0),
            summary.get("existing_snapshots", 0),
            len(summary.get("errors", []) or []),
            summary.get("mismatches_logged", 0),
            summary.get("board_version"),
        )
        return summary
    except Exception as e:
        # Never let publication failure break the ingest loop.
        logger.warning(
            "%s publication step failed (non-fatal): %s",
            caller_label, e,
        )
        return {}


__all__ = ["publish_upserted_picks"]
