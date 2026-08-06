"""cold_start — Phase 2γ startup freshness helper.

At service boot, paid snapshot loops (alt_lines_feed, mls_direct_inject,
soccer_prop_inject, plus any Phase 2δ additions) must NOT fire an
immediate upstream fan-out.  Instead they should:

  1. Read the latest saved snapshot from Mongo.
  2. Calculate freshness.
  3. Do nothing when data is fresh.
  4. Request exactly one coordinated recovery job when data is
     missing OR critically stale — under lease + budget.
  5. If multiple instances boot at once, only one recovery job runs
     (JobCoordinator lease guarantees single owner).

Freshness thresholds are documented per snapshot type below.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional, Callable, Awaitable

from motor.motor_asyncio import AsyncIOMotorDatabase

from services.job_coordinator import JobCoordinator
from services.provider_budget import ProviderBudget
from services.job_registry import get_job

logger = logging.getLogger("lockscore.cold_start")

# Freshness thresholds (seconds).  Snapshot considered *fresh* when
# the last successful run finished less than ``fresh_seconds`` ago,
# *stale* when between fresh and critical, and *critically stale*
# beyond ``critical_seconds`` → warrants immediate recovery.
FRESHNESS_POLICY: dict[str, dict[str, int]] = {
    "alt_lines_feed":      {"fresh": 8 * 3600, "critical": 14 * 3600},
    "mls_direct_inject":   {"fresh": 8 * 3600, "critical": 14 * 3600},
    "soccer_prop_inject":  {"fresh": 8 * 3600, "critical": 14 * 3600},
    "picks_refresh_today": {"fresh": 6 * 3600, "critical": 12 * 3600},
}


async def _last_run_at(db: AsyncIOMotorDatabase,
                        job_name: str) -> Optional[datetime]:
    """Return the last ``last_completed_at`` for the named job from
    ``scheduled_jobs``."""
    doc = await db["scheduled_jobs"].find_one(
        {"job_name": job_name},
        {"last_completed_at": 1, "last_started_at": 1},
    )
    if not doc:
        return None
    last = doc.get("last_completed_at") or doc.get("last_started_at")
    if isinstance(last, datetime):
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return last
    return None


def _assess_freshness(job_name: str,
                       last_run_at: Optional[datetime]) -> str:
    """Return one of: ``fresh``, ``stale``, ``critical``, ``missing``."""
    policy = FRESHNESS_POLICY.get(job_name)
    if policy is None:
        return "fresh"
    if last_run_at is None:
        return "missing"
    delta = (datetime.now(timezone.utc) - last_run_at).total_seconds()
    if delta < policy["fresh"]:
        return "fresh"
    if delta < policy["critical"]:
        return "stale"
    return "critical"


async def maybe_recover_on_cold_start(
    db: AsyncIOMotorDatabase, *,
    job_name: str,
    runner: Callable[[], Awaitable[Any]],
    caller: str = "cold_start",
    emergency_reason: Optional[str] = None,
) -> dict:
    """Called at boot for each paid snapshot job.  If freshness is
    critically stale or missing, acquires a coordinator lease + budget
    reservation and invokes ``runner()`` exactly once (per fleet)."""
    last = await _last_run_at(db, job_name)
    freshness = _assess_freshness(job_name, last)
    if freshness in ("fresh", "stale"):
        logger.info(
            "cold_start[%s]: freshness=%s last=%s — skipping recovery",
            job_name, freshness,
            last.isoformat() if last else None,
        )
        return {"executed": False, "freshness": freshness,
                "last_run_at": last.isoformat() if last else None}

    # ── critical / missing → attempt coordinated recovery ──────────
    coord = JobCoordinator(db)
    budget = ProviderBudget(db)
    reg = get_job(job_name) or {}
    lease_s = int(reg.get("lease_seconds") or 600)
    min_iv  = int(reg.get("min_interval_seconds") or 1800)
    est     = int(reg.get("estimated_max_credits") or 100)
    lease = await coord.acquire(
        job_name,
        lease_seconds=lease_s,
        min_interval_seconds=min_iv,
        caller=caller,
        reason=f"cold_start:{freshness}",
        metadata={"freshness": freshness},
    )
    if not lease:
        logger.info(
            "cold_start[%s]: another instance owns recovery (%s)",
            job_name, lease.get("reason"),
        )
        return {"executed": False, "freshness": freshness,
                "reason": lease.get("reason")}
    token = lease.lease_token
    reservation = await budget.reserve(
        estimated_credits=est,
        endpoint_type="snapshot_recovery",
        caller=caller,
        job_name=job_name,
        emergency_requested=bool(emergency_reason),
        reason=emergency_reason or f"cold_start:{freshness}",
        request_key=f"cold_start:{job_name}:{token}",
        ttl_seconds=lease_s + 60,
    )
    if not reservation.get("allowed"):
        await coord.fail(job_name, token,
                          error=f"budget_denied:{reservation.get('outcome')}",
                          retry_after_seconds=300)
        logger.warning(
            "cold_start[%s]: budget denied (%s) — deferring recovery",
            job_name, reservation.get("outcome"),
        )
        return {"executed": False, "freshness": freshness,
                "budget_outcome": reservation.get("outcome")}
    intent_id = reservation.get("intent_id")
    try:
        result = await runner()
        await budget.commit(intent_id)
        await coord.complete(job_name, token,
                              result_metadata={"cold_start": True,
                                                "freshness": freshness})
        return {"executed": True, "freshness": freshness,
                "summary": str(result)[:400]}
    except Exception as e:
        await budget.release(intent_id, reason=f"cold_start_error:{e}")
        await coord.fail(job_name, token, error=str(e),
                          retry_after_seconds=300)
        logger.warning("cold_start[%s]: recovery failed: %s", job_name, e)
        return {"executed": False, "freshness": freshness,
                "error": str(e)[:400]}


__all__ = [
    "maybe_recover_on_cold_start",
    "FRESHNESS_POLICY",
    "_assess_freshness", "_last_run_at",
]
