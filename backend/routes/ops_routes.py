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


# ─────────────────────── Phase 2δ additions ─────────────────
@router.get("/cache/policy")
async def ops_cache_policy(
    user: Annotated[UserPublic, Depends(current_admin)],
):
    """Return the centralized Odds API cache policy in effect."""
    from services.cache_policy import POLICIES
    return {"policies": POLICIES}


@router.get("/settlement/scope")
async def ops_settlement_scope(
    user: Annotated[UserPublic, Depends(current_admin)],
    lookback_days: int = Query(14, ge=1, le=90),
):
    """List the sport_keys currently requiring settlement (i.e.
    have unsettled published picks).  Used to verify that the
    settlement loop is not fetching leagues we don't price."""
    from services.settlement_scope import scope_summary
    return await scope_summary(db, lookback_days=lookback_days)


@router.get("/lifecycle/status")
async def ops_lifecycle_status(
    user: Annotated[UserPublic, Depends(current_admin)],
):
    """Return the state of every long-running background task."""
    from server import app as _app
    lc = getattr(_app.state, "lifecycle", None)
    if lc is None:
        return {"present": False}
    return {"present": True, **lc.status()}


@router.get("/health")
async def ops_health(
    user: Annotated[UserPublic, Depends(current_admin)],
):
    """One-stop dashboard summary: budget, active leases, expired
    leases, active reservations, settlement scope size, lifecycle
    task count, gateway feature flags."""
    from services.job_coordinator import JobCoordinator
    from services.provider_budget import ProviderBudget
    from services.odds_api_gateway import _gateway_enabled, _global_refresh_mode
    from services.settlement_scope import active_sport_keys
    now = datetime.now(timezone.utc)
    coord = JobCoordinator(db)
    budget = ProviderBudget(db)
    active_leases = await db[JOB_COLL].count_documents(
        {"status": "running", "lease_until": {"$gt": now}})
    expired_leases = await db[JOB_COLL].count_documents(
        {"status": "running", "lease_until": {"$lt": now}})
    active_reservations = await db[INTENTS_COLL].count_documents(
        {"provider": "odds_api", "status": INTENT_RESERVED})
    budget_status = await budget.get_budget_status()
    from server import app as _app
    lc = getattr(_app.state, "lifecycle", None)
    lc_tasks = len(lc._tasks) if lc is not None else 0  # type: ignore[attr-defined]
    return {
        "now_utc":              now.isoformat(),
        "gateway_enabled":      _gateway_enabled(),
        "global_refresh_mode":  _global_refresh_mode(),
        "active_leases":        active_leases,
        "expired_leases":       expired_leases,
        "active_reservations":  active_reservations,
        "budget":               budget_status,
        "settlement_leagues":   len(await active_sport_keys(db)),
        "lifecycle_tasks":      lc_tasks,
    }


# ═════════════════════════════════════════════════════════════════════
# Phase 3C — Index registry admin observability
# ═════════════════════════════════════════════════════════════════════
@router.get("/indexes")
async def ops_index_summary(
    user: Annotated[UserPublic, Depends(current_admin)],
):
    """Phase 3C — high-level registry state (safe, no secrets).

    Returns:
      * total declared specs / critical / TTL / unique counts
      * per-collection totals
      * critical index health (True/False)
      * last verification timestamp
    """
    from services.index_registry import (
        safe_index_diagnostics, verify_all_indexes,
    )
    diag = safe_index_diagnostics()
    verified = await verify_all_indexes(db)
    critical_ok = all(r.critical_ok for r in verified.values())
    return {
        "diagnostics":      diag,
        "critical_ok":      critical_ok,
        "verified_at":      datetime.now(timezone.utc).isoformat(),
        "collection_status": {
            c: r.summary() for c, r in verified.items()
        },
    }


@router.get("/indexes/conflicts")
async def ops_index_conflicts(
    user: Annotated[UserPublic, Depends(current_admin)],
):
    """Phase 3C — reports same-name conflicts + equivalent duplicates
    with enough context to plan a migration.  Never returns document
    data or connection strings."""
    from services.index_registry import report_conflicts
    return await report_conflicts(db)


# ═════════════════════════════════════════════════════════════════════
# Phase 3D — Identity contracts observability (dry-run only)
# ═════════════════════════════════════════════════════════════════════
@router.get("/identity/dry-run")
async def ops_identity_dry_run(
    user: Annotated[UserPublic, Depends(current_admin)],
    collections: Optional[str] = None,
):
    """Phase 3D — scan live collections and propose canonical
    identities WITHOUT writing anything.  Reports quality counts,
    collisions, and ambiguities per collection."""
    from services.identity_resolver import dry_run_scan_all
    cols = [c.strip() for c in collections.split(",")] if collections else None
    return await dry_run_scan_all(db, cols)


@router.get("/identity/coverage")
async def ops_identity_coverage(
    user: Annotated[UserPublic, Depends(current_admin)],
):
    """Phase 3D — provider-ID coverage per critical collection.
    Reports what % of live rows carry a stable provider identifier
    versus how many would fall back to name-normalised identity.
    Uses the read-only collection list defined in the identity
    resolver (avoids referencing the immutable snapshot store
    directly here — Phase 2β guardrail)."""
    from services.identity_resolver import (
        dry_run_scan_collection,
        DRY_RUN_CRITICAL_COLLECTIONS,
    )
    out = {}
    for c in DRY_RUN_CRITICAL_COLLECTIONS:
        try:
            r = await dry_run_scan_collection(db, c, sample_size=500)
            out[c] = {
                "sampled":        r["sampled"],
                "quality_counts": r["quality_counts"],
                "collisions":     len(r.get("collisions") or {}),
            }
        except Exception as e:
            out[c] = {"error": str(e)}
    return {"critical_collections": out}


__all__ = ["router"]
