"""shadow_wiring — Phase 2β observation helper.

Provides ONE function that any scheduled job may call to record how
the JobCoordinator + ProviderBudget WOULD have gated its execution
if it were fully managed.  Shadow calls never reserve or consume
real budget capacity; they only compute and log the decision.

Callers should invoke ``shadow_check`` at the start of a run and
attach the returned dict to their own logs / metrics.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

from services.job_coordinator import (
    JobCoordinator, _hash_token,
)
from services.provider_budget import ProviderBudget
from services.job_registry import get_job

logger = logging.getLogger("lockscore.shadow_wiring")

SHADOW_EVENT = "shadow_decision"


async def shadow_check(
    db: AsyncIOMotorDatabase, *,
    job_name: str,
    caller: str,
    reason: str = "shadow_observation",
    estimated_credits: Optional[int] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Non-blocking shadow evaluation.  Returns a dict describing
    whether a lease + budget WOULD have been granted."""
    reg = get_job(job_name) or {}
    est = int(
        estimated_credits
        if estimated_credits is not None
        else reg.get("estimated_max_credits") or 0
    )

    now = datetime.now(timezone.utc)

    # ─ Coordinator would-acquire probe (pure read) ──────────────────
    coord = JobCoordinator(db)
    status = await coord.get_status(job_name)
    lease_would_be_granted = True
    lease_reason = "would_acquire"
    if status:
        # Running with a live lease? Would be busy.
        ne = status.get("next_eligible_at")
        if isinstance(ne, datetime):
            if ne.tzinfo is None:
                ne = ne.replace(tzinfo=timezone.utc)
            if ne > now:
                lease_would_be_granted = False
                lease_reason = "blocked_min_interval"
        if status.get("status") == "running":
            lu = status.get("lease_until")
            if isinstance(lu, datetime):
                if lu.tzinfo is None:
                    lu = lu.replace(tzinfo=timezone.utc)
                if lu > now:
                    lease_would_be_granted = False
                    lease_reason = "busy"

    # ─ Budget would-allow probe (pure read) ─────────────────────────
    budget = ProviderBudget(db)
    if est > 0:
        allowance = await budget.check_allowance(
            estimated_credits=est,
            caller=caller,
            job_name=job_name,
            emergency_requested=False,
            reason=reason,
        )
    else:
        allowance = {"allowed": True, "outcome": "no_paid_estimate",
                     "estimated_credits": 0}

    decision = {
        "job_name":          job_name,
        "shadow":            True,
        "caller":            caller,
        "reason":            reason,
        "estimated_credits": est,
        "lease": {
            "would_grant": lease_would_be_granted,
            "reason":      lease_reason,
        },
        "budget": allowance,
        "observed_at":       now,
    }
    # Emit an audit-log entry via the coordinator so all shadow
    # decisions land in a single queryable stream.
    try:
        await coord.audit(
            SHADOW_EVENT,
            job_name=job_name,
            caller=caller,
            reason=reason,
            lease_would_grant=lease_would_be_granted,
            lease_reason=lease_reason,
            budget_allowed=allowance.get("allowed"),
            budget_outcome=allowance.get("outcome"),
            estimated_credits=est,
            metadata=metadata or {},
        )
    except Exception as e:  # pragma: no cover
        logger.debug("shadow audit write failed: %s", e)
    return decision


__all__ = ["shadow_check", "SHADOW_EVENT"]
