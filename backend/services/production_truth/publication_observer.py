"""Publication Observer — real-flow OBSERVE wiring (session 2).

Runs AFTER `PredictionPublicationService.publish_batch()` on every
canonical publication path.  In OBSERVE mode this observer:

* Evaluates each just-published candidate against the reachability
  contract (16 stages).
* Records structured contract violations in the in-memory
  ``enforcement`` buffer AND a persistent MongoDB collection so
  they survive process restarts.
* Freezes a hash-sealed pregame snapshot in
  ``db.pregame_snapshots`` for newly-published (or first-seen)
  canonical predictions — idempotently.
* Tags the production origin: publications coming from the
  canonical pipeline are ``DATA``; publications coming from known
  direct-inject writers are ``DIRECT_INJECT``.

Guarantees
----------
* NEVER raises — a failure inside the observer is logged at WARNING
  and swallowed so canonical publication is untouched.
* NEVER modifies the pick document — read-only access to the
  candidate dict.
* NEVER rewrites existing snapshots — freezes idempotently via
  ``freeze_pregame`` which already refuses in-place mutation.
* No changes to scoring, ranking, Lock Score, model probability,
  canonical publication eligibility, or consumer eligibility.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from .vocabulary import (
    ProductionStage,
    StageStatus,
    DropReason,
)
from .reachability import build_reachability_report
from .chain_of_custody import build_custody_record
from .pregame_snapshot import (
    freeze_pregame,
    PregameSnapshotImmutable,
    read_pregame_snapshot,
)
from .enforcement import (
    current_mode,
    record_violation,
    EnforcementMode,
)

logger = logging.getLogger("lockscore.production_truth.observer")

OBSERVATIONS_COLLECTION = "production_truth_observations"

# Known direct-inject publication_source markers.  Publications
# coming from these writers are tagged DIRECT_INJECT origin in the
# custody record.  Anything else defaults to DATA (real canonical).
_DIRECT_INJECT_SOURCES: frozenset[str] = frozenset({
    "mls_direct_inject",
    "soccer_prop_inject",
    "soccer_hot_scorers_v1",
})


def _classify_origin(publication_source: Optional[str]) -> str:
    """Map a publication_source string to a custody origin."""
    if not publication_source:
        return "UNKNOWN"
    src = publication_source.strip().lower()
    for marker in _DIRECT_INJECT_SOURCES:
        if marker in src:
            return "DIRECT_INJECT"
    return "DATA"


def _canonical_prediction_id(pick: dict) -> Optional[str]:
    """The publication service uses ``prediction_id`` (== pick.id).
    We adopt that as the canonical_prediction_id for the freeze so
    the two identifier spaces line up cleanly."""
    return (pick.get("canonical_prediction_id")
             or pick.get("prediction_id")
             or pick.get("id")
             or pick.get("external_id"))


def _observation_record(
    pick: dict,
    *,
    publication_source: Optional[str],
    origin: str,
    reachability: dict,
    snapshot_action: str,
    snapshot_hash: Optional[str],
) -> dict:
    """Assemble the observation document persisted per candidate."""
    return {
        "ts":                      datetime.now(timezone.utc).isoformat(),
        "mode":                    current_mode().value,
        "canonical_prediction_id": _canonical_prediction_id(pick),
        "pick_id":                 pick.get("id") or pick.get("external_id"),
        "sport":                   pick.get("sport"),
        "market":                  pick.get("market"),
        "publication_source":      publication_source,
        "origin":                  origin,
        "publication_gate":        pick.get("publication_gate"),
        "reachability":            reachability,
        "snapshot":                {
            "action": snapshot_action,
            "hash":   snapshot_hash,
        },
    }


async def _persist_observation(db, record: dict) -> None:
    """Store the observation record (best-effort — never raises)."""
    try:
        await db[OBSERVATIONS_COLLECTION].update_one(
            {"canonical_prediction_id": record["canonical_prediction_id"]},
            {"$set": record},
            upsert=True,
        )
    except Exception as e:                       # pragma: no cover
        logger.debug("observation persist failed: %s", e)


async def ensure_observation_indexes(db) -> None:
    """Idempotent index bootstrap."""
    try:
        coll = db[OBSERVATIONS_COLLECTION]
        await coll.create_index("canonical_prediction_id", unique=True)
        await coll.create_index("pick_id")
        await coll.create_index("ts")
    except Exception as e:                       # pragma: no cover
        logger.debug("observation index bootstrap failed: %s", e)


async def _maybe_freeze_snapshot(db, pick: dict,
                                    *, origin: str) -> tuple[str, Optional[str]]:
    """Freeze an immutable pregame snapshot when appropriate.

    Returns (action, snapshot_hash) where action is one of:

        ``FROZEN``       — a new snapshot was inserted.
        ``ALREADY_FROZEN``  — a snapshot already exists (idempotent).
        ``SKIPPED_NOT_ELIGIBLE`` — the pick is not a qualifying
                                    published prediction (e.g. barrier
                                    rejected, off_board, or missing a
                                    canonical_prediction_id).
        ``ERROR``        — freeze raised (swallowed for OBSERVE mode).

    Freeze is refused (SKIPPED_NOT_ELIGIBLE) when:
      * the pick has no canonical_prediction_id
      * publication_gate is ``canonical_barrier_rejected``
      * ``off_board`` or ``no_bet`` is True
      * the pick lacks real book_odds
    These conditions all imply the pick is NOT a real user-visible
    canonical prediction and therefore not part of the immutable
    pregame truth.  The observer still records reachability for
    them; only the freeze is skipped.
    """
    cpid = _canonical_prediction_id(pick)
    if not cpid:
        return ("SKIPPED_NOT_ELIGIBLE", None)

    if pick.get("publication_gate") == "canonical_barrier_rejected":
        return ("SKIPPED_NOT_ELIGIBLE", None)
    if pick.get("off_board") is True or pick.get("no_bet") is True:
        return ("SKIPPED_NOT_ELIGIBLE", None)
    try:
        _ = int(pick.get("book_odds"))
    except (TypeError, ValueError):
        return ("SKIPPED_NOT_ELIGIBLE", None)
    if pick.get("no_real_book_line") is True:
        return ("SKIPPED_NOT_ELIGIBLE", None)

    # Attach a synthesised canonical_prediction_id + engine_version
    # onto a COPY of the pick so the seal payload is complete but
    # the caller's dict is never mutated (§11 protection).
    payload_pick = dict(pick)
    payload_pick["canonical_prediction_id"] = cpid
    payload_pick.setdefault("provenance", origin)

    # Idempotency: if a snapshot already exists, do NOT re-freeze.
    try:
        existing = await read_pregame_snapshot(
            db, canonical_prediction_id=cpid)
    except Exception:
        existing = None
    if existing:
        return ("ALREADY_FROZEN", existing.get("snapshot_hash"))

    try:
        snap = await freeze_pregame(db, payload_pick)
        return ("FROZEN", snap.get("snapshot_hash"))
    except PregameSnapshotImmutable:
        # Race: another observer beat us here.  Read-back the hash
        # to keep the observation consistent.
        existing = await read_pregame_snapshot(
            db, canonical_prediction_id=cpid)
        return ("ALREADY_FROZEN",
                 (existing or {}).get("snapshot_hash"))
    except Exception as e:                       # pragma: no cover
        logger.debug("freeze_pregame observer failure: %s", e)
        return ("ERROR", None)


async def observe_publication(
    db,
    picks: Iterable[dict],
    *,
    publication_source: Optional[str] = None,
    caller_label: Optional[str] = None,
) -> dict:
    """Observer entry-point.

    Call this AFTER ``PredictionPublicationService.publish_batch``.
    Runs in OBSERVE mode only when ``current_mode()`` is OBSERVE —
    when ENFORCE is later enabled callers get the same behaviour
    plus violation records available on the mode diagnostic
    endpoint.  In OBSERVE the observer never rejects publication.

    Returns a lightweight summary useful for callers/tests:
        {
            "mode":     "OBSERVE" | "ENFORCE",
            "count":    int,
            "frozen":   int,      # snapshots newly frozen
            "already":  int,      # snapshots already present
            "violations": int,    # violations recorded
        }
    """
    summary = {
        "mode":     current_mode().value,
        "count":    0,
        "frozen":   0,
        "already":  0,
        "violations": 0,
    }
    origin = _classify_origin(publication_source)
    for pick in picks or []:
        summary["count"] += 1
        try:
            report = build_reachability_report(pick)
            snapshot_action, snapshot_hash = await _maybe_freeze_snapshot(
                db, pick, origin=origin)
            if snapshot_action == "FROZEN":
                summary["frozen"] += 1
            elif snapshot_action == "ALREADY_FROZEN":
                summary["already"] += 1

            # Record every FAIL as a violation.  UNKNOWN stages are
            # observed but only counted as violations when the stage
            # is a hard prerequisite (see below) — this keeps legacy
            # picks from flooding the buffer while still catching
            # every genuine drop.
            HARD_STAGES = {
                ProductionStage.REAL_MARKET_AVAILABLE.value,
                ProductionStage.CANONICAL_PUBLISHED.value,
                ProductionStage.VISIBLE_TO_CONSUMER.value,
                ProductionStage.IDENTITY_RESOLVED.value,
            }
            for stage_name, info in report.stages.items():
                st = info.get("status")
                if st == StageStatus.FAIL.value:
                    record_violation(
                        stage=stage_name,
                        reason=info.get("reason"),
                        detail=info.get("detail"),
                        pick_id=pick.get("id") or pick.get("external_id"),
                        sport=pick.get("sport"),
                        market=pick.get("market"),
                        extra={"publication_source": publication_source,
                                "origin":             origin},
                    )
                    summary["violations"] += 1
                elif st == StageStatus.UNKNOWN.value and \
                     stage_name in HARD_STAGES:
                    record_violation(
                        stage=stage_name,
                        reason=DropReason.LEGACY_MISSING_METADATA.value,
                        detail=info.get("detail")
                                or "hard-stage cannot be proven",
                        pick_id=pick.get("id") or pick.get("external_id"),
                        sport=pick.get("sport"),
                        market=pick.get("market"),
                        extra={"publication_source": publication_source,
                                "origin":             origin},
                    )
                    summary["violations"] += 1

            record = _observation_record(
                pick,
                publication_source=publication_source,
                origin=origin,
                reachability=report.to_dict(),
                snapshot_action=snapshot_action,
                snapshot_hash=snapshot_hash,
            )
            await _persist_observation(db, record)
        except Exception as e:                   # pragma: no cover
            logger.warning(
                "production_truth observer failed for pick %s (%s): %s",
                pick.get("id"), caller_label or publication_source, e,
            )
    return summary


async def read_observation(db, *,
                             canonical_prediction_id: Optional[str] = None,
                             pick_id: Optional[str] = None) -> Optional[dict]:
    """Read the latest persisted observation for a pick.  Returns
    ``None`` when no observation exists (never crashes)."""
    query: dict = {}
    if canonical_prediction_id:
        query["canonical_prediction_id"] = canonical_prediction_id
    elif pick_id:
        query["pick_id"] = pick_id
    else:
        return None
    try:
        doc = await db[OBSERVATIONS_COLLECTION].find_one(query)
    except Exception:
        return None
    if doc:
        doc.pop("_id", None)
    return doc


__all__ = [
    "OBSERVATIONS_COLLECTION",
    "observe_publication",
    "read_observation",
    "ensure_observation_indexes",
]
