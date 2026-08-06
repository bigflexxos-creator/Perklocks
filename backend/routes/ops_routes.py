"""Phase 2β admin observability routes — /api/admin/ops/*.

Read-only operational visibility into the JobCoordinator + ProviderBudget:

  • current job statuses / active leases / stale leases
  • recent job execution history
  • daily + monthly provider usage
  • remaining regular budget + emergency reserve
  • recently blocked / emergency-approved requests
  • shadow-mode decisions

Every route requires the real ``current_admin`` dependency.  No
secrets, tokens, or raw provider payloads are ever returned.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from auth import UserPublic
from deps import current_admin, db

from services.job_coordinator import (
    JobCoordinator, COLLECTION as JOB_COLL,
    EXECUTION_LOG, AUDIT_LOG,
)
from services.provider_budget import (
    ProviderBudget, INTENTS_COLL, INTENT_RESERVED,
)
from services.job_registry import list_jobs, paid_jobs

router = APIRouter(prefix="/api/admin/ops", tags=["ops"])


def _strip(doc: dict) -> dict:
    if not doc:
        return {}
    return {k: v for k, v in doc.items() if k != "_id"}


def _redact_job_row(row: dict) -> dict:
    """Strip live lease tokens before returning through the API.
    Only the active ``scheduled_jobs`` record itself may store the
    raw token — every response gives the hash instead."""
    if not row:
        return row
    tok = row.pop("lease_token", None)
    if tok:
        import hashlib
        row["lease_token_hash"] = hashlib.sha256(
            tok.encode("utf-8")).hexdigest()
    return row


# ────────────────────────── Jobs ──────────────────────────
@router.get("/jobs")
async def ops_list_jobs(
    user: Annotated[UserPublic, Depends(current_admin)],
    limit: int = Query(200, ge=1, le=500),
):
    """List every scheduled_jobs document sorted by ``updated_at``."""
    coord = JobCoordinator(db)
    rows = [_redact_job_row(r) for r in await coord.list_statuses(limit=limit)]
    return {
        "count":  len(rows),
        "jobs":   rows,
        "now_utc": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/jobs/leases/active")
async def ops_active_leases(
    user: Annotated[UserPublic, Depends(current_admin)],
):
    now = datetime.now(timezone.utc)
    rows = await db[JOB_COLL].find(
        {"status": "running", "lease_until": {"$gt": now}},
        {"_id": 0, "lease_token": 0},
    ).sort("lease_until", 1).to_list(200)
    return {"count": len(rows), "leases": rows,
            "now_utc": now.isoformat()}


@router.get("/jobs/leases/expired")
async def ops_expired_leases(
    user: Annotated[UserPublic, Depends(current_admin)],
):
    """Running leases whose deadline is already in the past."""
    now = datetime.now(timezone.utc)
    rows = await db[JOB_COLL].find(
        {"status": "running", "lease_until": {"$lt": now}},
        {"_id": 0, "lease_token": 0},
    ).sort("lease_until", 1).to_list(200)
    return {"count": len(rows), "leases": rows,
            "now_utc": now.isoformat()}

@router.post("/jobs/leases/recover")
async def ops_recover_expired(
    user: Annotated[UserPublic, Depends(current_admin)],
):
    """Manually mark all expired running leases as ``expired``."""
    coord = JobCoordinator(db)
    n = await coord.recover_expired_leases()
    return {"recovered": n}


@router.get("/jobs/executions")
async def ops_recent_executions(
    user: Annotated[UserPublic, Depends(current_admin)],
    job_name: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500),
):
    coord = JobCoordinator(db)
    rows = await coord.recent_executions(limit=limit, job_name=job_name)
    # Never expose raw lease tokens — only hashes are stored, but be
    # defensive against future schema drift.
    for r in rows:
        r.pop("lease_token", None)
    return {"count": len(rows), "executions": rows}


@router.get("/jobs/registry")
async def ops_job_registry(
    user: Annotated[UserPublic, Depends(current_admin)],
    only_paid: bool = False,
):
    """Static declarative inventory — Phase 2γ cutover source-of-truth."""
    jobs = paid_jobs() if only_paid else list_jobs()
    return {"count": len(jobs), "jobs": jobs}


# ─────────────────────────── Budget ───────────────────────
@router.get("/budget/status")
async def ops_budget_status(
    user: Annotated[UserPublic, Depends(current_admin)],
    provider: str = "odds_api",
):
    b = ProviderBudget(db, provider=provider)
    status = await b.get_budget_status()
    return status


@router.get("/budget/daily")
async def ops_budget_daily(
    user: Annotated[UserPublic, Depends(current_admin)],
    provider: str = "odds_api",
    day: Optional[str] = None,
):
    b = ProviderBudget(db, provider=provider)
    return await b.get_daily_usage(day)


@router.get("/budget/monthly")
async def ops_budget_monthly(
    user: Annotated[UserPublic, Depends(current_admin)],
    provider: str = "odds_api",
    month: Optional[str] = None,
):
    b = ProviderBudget(db, provider=provider)
    return await b.get_monthly_usage(month)


@router.get("/budget/blocked")
async def ops_budget_blocked(
    user: Annotated[UserPublic, Depends(current_admin)],
    provider: str = "odds_api",
    limit: int = Query(50, ge=1, le=500),
):
    b = ProviderBudget(db, provider=provider)
    rows = await b.recent_blocked(limit=limit)
    return {"count": len(rows), "events": rows}


@router.get("/budget/reservations/active")
async def ops_active_reservations(
    user: Annotated[UserPublic, Depends(current_admin)],
    provider: str = "odds_api",
):
    rows = await db[INTENTS_COLL].find(
        {"provider": provider, "status": INTENT_RESERVED},
        {"_id": 0},
    ).sort("created_at", -1).to_list(200)
    return {"count": len(rows), "reservations": rows}


@router.post("/budget/reservations/sweep")
async def ops_sweep_reservations(
    user: Annotated[UserPublic, Depends(current_admin)],
    provider: str = "odds_api",
):
    b = ProviderBudget(db, provider=provider)
    n = await b.sweep_expired_reservations()
    return {"expired": n}


@router.get("/budget/reconcile")
async def ops_budget_reconcile(
    user: Annotated[UserPublic, Depends(current_admin)],
    provider: str = "odds_api",
    day: Optional[str] = None,
    credits_per_request: int = Query(1, ge=1, le=100),
):
    b = ProviderBudget(db, provider=provider)
    return await b.reconcile_from_request_log(
        day_key=day, assume_credits_per_request=credits_per_request,
    )


# ─────────────────────────── Shadow decisions ────────────
@router.get("/shadow/decisions")
async def ops_shadow_decisions(
    user: Annotated[UserPublic, Depends(current_admin)],
    job_name: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500),
):
    q: dict = {"event_type": "shadow_decision"}
    if job_name:
        q["job_name"] = job_name
    rows = await db[AUDIT_LOG].find(q, {"_id": 0}).sort(
        "created_at", -1).to_list(limit)
    return {"count": len(rows), "decisions": rows}


@router.get("/audit")
async def ops_audit_log(
    user: Annotated[UserPublic, Depends(current_admin)],
    event_type: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500),
):
    q: dict = {}
    if event_type:
        q["event_type"] = event_type
    rows = await db[AUDIT_LOG].find(q, {"_id": 0}).sort(
        "created_at", -1).to_list(limit)
    return {"count": len(rows), "events": rows}


__all__ = ["router"]
