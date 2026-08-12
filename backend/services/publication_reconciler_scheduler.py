"""Publication Reconciler Scheduler — Session B (2026-06).

Wires ``services.publication_reconciliation.reconcile_stuck_publications``
into the existing runtime task registry with conservative cadence and
single-run lease protection.

Design
──────
* One asyncio loop registered under ``runtime_task_registry`` (name:
  ``"publication_reconciler_loop"``, task_type ``recurring_loop``).
* Loop interval:  60 seconds (deliberately conservative — a stuck
  pick sits at most ~65s before recovery is attempted).
* Reconciliation cutoff:  picks whose last_state_at is older than
  ``5 minutes`` are eligible.  Combined with the 60s loop this yields
  natural back-off (a pick that just entered PENDING is skipped until
  it has aged 5 min).
* Lease protection:  a module-level ``asyncio.Lock`` prevents two
  simultaneous reconciliations inside one process.  The underlying
  ``publish_batch`` idempotency (unique index on
  ``(prediction_id, idempotency_key)``) prevents duplicate canonical
  publications ACROSS processes.
* Zero duplicate canonical publications:  every retry uses the same
  ``prediction_id`` + a fresh idempotency_key computed from the
  producer payload.  When the payload_hash is stable, publish() is a
  no-op via DuplicateKeyError.
* Last-run visibility:  the last run summary is stashed on
  ``LAST_RECONCILER_STATUS`` (also queryable via
  ``/api/admin/publication/lifecycle`` in a follow-up hook).

This module does NOT perform reconciliation on import.  Only when
``start_scheduled_reconciler(db)`` is called does the loop begin.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger("lockscore.publication_reconciler_scheduler")

# Conservative cadence — see module doc.
LOOP_INTERVAL_SECONDS   = 60
RECONCILE_MAX_AGE_MIN   = 5
RECONCILE_BATCH_LIMIT   = 100

# Single-run guard (per-process).  Cross-process safety is provided
# by the underlying idempotency key on prediction_snapshots.
_RUN_LOCK: asyncio.Lock = asyncio.Lock()

# Last-run status snapshot (in-memory).  Read by the admin endpoint.
LAST_RECONCILER_STATUS: dict[str, Any] = {
    "state":              "not_started",
    "last_run_at":        None,
    "last_run_duration_s": None,
    "last_summary":       None,
    "runs_total":         0,
    "runs_ok":            0,
    "runs_failed":        0,
}


async def run_once(db: AsyncIOMotorDatabase) -> dict[str, Any]:
    """One reconciliation pass.  Idempotent + lease-guarded.

    Returns the reconciler summary or ``{"skipped": True}`` when the
    lease was already held by another concurrent pass.
    """
    if _RUN_LOCK.locked():
        return {"skipped": True, "reason": "another reconciliation in progress"}
    async with _RUN_LOCK:
        started = datetime.now(timezone.utc)
        try:
            from services.publication_reconciliation import (
                reconcile_stuck_publications,
            )
            summary = await reconcile_stuck_publications(
                db,
                max_age_minutes=RECONCILE_MAX_AGE_MIN,
                limit=RECONCILE_BATCH_LIMIT,
            )
            elapsed = (datetime.now(timezone.utc) - started).total_seconds()
            LAST_RECONCILER_STATUS.update({
                "state":               "ok",
                "last_run_at":         started.isoformat().replace(
                    "+00:00", "Z"),
                "last_run_duration_s": round(elapsed, 3),
                "last_summary":        summary,
                "runs_total":  LAST_RECONCILER_STATUS["runs_total"] + 1,
                "runs_ok":     LAST_RECONCILER_STATUS["runs_ok"] + 1,
            })
            if summary.get("retried") or summary.get("exhausted"):
                logger.info(
                    "publication reconciler: retried=%d published=%d "
                    "rejected=%d exhausted=%d",
                    summary.get("retried", 0),
                    summary.get("published", 0),
                    summary.get("rejected", 0),
                    summary.get("exhausted", 0),
                )
            return summary
        except Exception as e:
            elapsed = (datetime.now(timezone.utc) - started).total_seconds()
            LAST_RECONCILER_STATUS.update({
                "state":               "failed",
                "last_run_at":         started.isoformat().replace(
                    "+00:00", "Z"),
                "last_run_duration_s": round(elapsed, 3),
                "last_summary":        {"error": f"{type(e).__name__}: {e}"},
                "runs_total":  LAST_RECONCILER_STATUS["runs_total"] + 1,
                "runs_failed": LAST_RECONCILER_STATUS["runs_failed"] + 1,
            })
            logger.warning("publication reconciler run failed: %s", e)
            return {"ok": False, "error": str(e)}


async def _loop(db: AsyncIOMotorDatabase, interval_s: int) -> None:
    """Long-running loop — periodic reconciliation."""
    logger.info(
        "publication reconciler loop started (interval=%ds, "
        "max_age=%d min, limit=%d)",
        interval_s, RECONCILE_MAX_AGE_MIN, RECONCILE_BATCH_LIMIT,
    )
    while True:
        try:
            await run_once(db)
        except Exception as e:
            logger.warning("publication reconciler loop iter failed: %s", e)
        await asyncio.sleep(interval_s)


def register_with_task_registry(
    db: AsyncIOMotorDatabase,
    *,
    interval_s: int = LOOP_INTERVAL_SECONDS,
    registry=None,
) -> Optional[Any]:
    """Register the reconciler loop with the runtime task registry.

    Idempotent — returns the existing registration if the task name
    is already present.
    """
    if registry is None:
        from services.runtime_task_registry import get_registry
        registry = get_registry()

    name = "publication_reconciler_loop"
    # Idempotent registration.
    tasks = getattr(registry, "_tasks", {}) or {}
    if name in tasks:
        return tasks[name]

    return registry.register_and_start(
        name,
        lambda: _loop(db, interval_s),
        owner_service="publication_reconciler",
        task_type="recurring_loop",
        critical=False,
        paid_provider=False,
        cadence=f"every {interval_s} sec (age gate {RECONCILE_MAX_AGE_MIN} min)",
        restart_policy="self_managed",
        shutdown_timeout=5.0,
    )


def status() -> dict[str, Any]:
    """Return the current reconciler status.  Safe for the admin
    endpoint."""
    return dict(LAST_RECONCILER_STATUS)


__all__ = [
    "LOOP_INTERVAL_SECONDS",
    "RECONCILE_MAX_AGE_MIN",
    "RECONCILE_BATCH_LIMIT",
    "LAST_RECONCILER_STATUS",
    "run_once",
    "register_with_task_registry",
    "status",
]
