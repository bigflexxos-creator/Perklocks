"""Phase 2δ — infrastructure hardening tests.

Covers:
  • Cache policy sanity (fresh < stale < max for every endpoint).
  • Settlement scope filtering.
  • BackgroundLifecycle startup lease recovery.
  • BackgroundLifecycle graceful shutdown.
  • Scheduler-registry completeness (every scheduled snapshot job
    is present in services.job_registry).
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

from motor.motor_asyncio import AsyncIOMotorClient


def _run(coro):
    return asyncio.run(coro)


def _fresh_db():
    return AsyncIOMotorClient(os.environ["MONGO_URL"])[
        os.environ.get("DB_NAME", "lockscore_db")
    ]


# ═════════════════════════════════════════════════════════════════════
# Cache policy
# ═════════════════════════════════════════════════════════════════════
def test_cache_policy_windows_are_ordered():
    from services.cache_policy import POLICIES, get_policy, is_fresh, is_stale, is_max_stale
    for name, p in POLICIES.items():
        assert p["fresh_seconds"] < p["stale_seconds"], name
        assert p["stale_seconds"] < p["max_seconds"], name
    # Predicates line up.
    assert is_fresh(60, "bulk_odds")
    assert not is_fresh(600, "bulk_odds")
    assert is_stale(600, "bulk_odds")
    assert is_max_stale(10_000, "bulk_odds")


def test_cache_policy_unknown_falls_back_to_generic():
    from services.cache_policy import get_policy
    assert get_policy("nonexistent_endpoint") == get_policy("generic")


# ═════════════════════════════════════════════════════════════════════
# Settlement scope
# ═════════════════════════════════════════════════════════════════════
def test_settlement_scope_returns_active_keys_only():
    async def go():
        db = _fresh_db()
        # Seed a few disposable picks.
        from services.settlement_scope import active_sport_keys
        tag = f"phase2d_test_{uuid.uuid4().hex[:8]}"
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        rows = [
            {"id": f"{tag}_a", "pick_date": today, "sport_key": f"{tag}_soccer_epl",
             "status": "pending", "tag": tag},
            {"id": f"{tag}_b", "pick_date": today, "sport_key": f"{tag}_soccer_epl",
             "status": "won", "tag": tag},   # already settled
            {"id": f"{tag}_c", "pick_date": today, "sport_key": f"{tag}_tennis_wimbledon",
             "status": "pending", "tag": tag},
        ]
        try:
            await db.picks.insert_many(rows)
            keys = await active_sport_keys(db)
            assert f"{tag}_soccer_epl" in keys
            assert f"{tag}_tennis_wimbledon" in keys
        finally:
            await db.picks.delete_many({"tag": tag})
    _run(go())


def test_settlement_scope_summary_shape():
    async def go():
        db = _fresh_db()
        from services.settlement_scope import scope_summary
        summary = await scope_summary(db)
        assert "as_of" in summary
        assert "distinct_leagues" in summary
        assert "sport_keys" in summary
        assert isinstance(summary["sport_keys"], list)
    _run(go())


# ═════════════════════════════════════════════════════════════════════
# BackgroundLifecycle
# ═════════════════════════════════════════════════════════════════════
def test_lifecycle_startup_recovers_expired_leases():
    async def go():
        db = _fresh_db()
        from services.background_lifecycle import BackgroundLifecycle
        from services.job_coordinator import JobCoordinator, COLLECTION
        # Seed an expired lease.
        job = f"p2d_recover_{uuid.uuid4().hex[:8]}"
        await db[COLLECTION].insert_one({
            "job_name": job,
            "status":  "running",
            "lease_until": datetime.now(timezone.utc) - timedelta(seconds=60),
            "owner_instance": "stale-worker",
            "updated_at": datetime.now(timezone.utc),
        })
        try:
            lc = BackgroundLifecycle(db)
            summary = await lc.on_startup()
            assert summary["recovered_leases"] >= 1
            doc = await db[COLLECTION].find_one({"job_name": job})
            assert doc["status"] == "expired"
        finally:
            await db[COLLECTION].delete_one({"job_name": job})
    _run(go())


def test_lifecycle_graceful_shutdown_cancels_registered_tasks():
    async def go():
        db = _fresh_db()
        from services.background_lifecycle import BackgroundLifecycle
        lc = BackgroundLifecycle(db)

        async def _forever():
            try:
                while True:
                    await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                return
        t = asyncio.create_task(_forever())
        lc.register("test_forever", t)
        summary = await lc.on_shutdown(timeout=2.0)
        assert summary["cancelled"] >= 1
        assert t.done() or t.cancelled()
    _run(go())


def test_lifecycle_status_reports_task_state():
    async def go():
        db = _fresh_db()
        from services.background_lifecycle import BackgroundLifecycle
        lc = BackgroundLifecycle(db)

        async def _quick():
            await asyncio.sleep(0.05)
        t = asyncio.create_task(_quick())
        lc.register("test_quick", t)
        st = lc.status()
        assert any(x["name"] == "test_quick" for x in st["tasks"])
        await t
    _run(go())


# ═════════════════════════════════════════════════════════════════════
# Scheduler / registry completeness
# ═════════════════════════════════════════════════════════════════════
def test_every_paid_scheduled_job_is_registered():
    """Every scheduled snapshot job the server arms must appear in
    services.job_registry so Phase 2δ observability is complete."""
    from services import job_registry
    required = {
        "alt_lines_feed", "mls_direct_inject", "soccer_prop_inject",
        "picks_refresh_today",
        "mlb_pregame_refresh_today", "mlb_pregame_refresh_tomorrow",
    }
    registered = {j["job_name"] for j in job_registry.list_jobs()}
    missing = required - registered
    assert not missing, f"missing registry entries: {missing}"


def test_paid_jobs_have_lease_and_budget_metadata():
    from services import job_registry
    for j in job_registry.paid_jobs():
        assert j.get("lease_seconds"), j
        assert j.get("min_interval_seconds"), j
        assert j.get("estimated_max_credits") is not None, j
        assert j.get("migration_status") in {
            "shadow", "leased", "budgeted", "fully_managed",
        }, j


# ═════════════════════════════════════════════════════════════════════
# Phase 1 immutability preserved
# ═════════════════════════════════════════════════════════════════════
def test_phase1_snapshots_not_mutated_by_phase2d():
    """No Phase 2δ module opens prediction_snapshots for writes."""
    from pathlib import Path
    for f in (
        "/app/backend/services/cache_policy.py",
        "/app/backend/services/settlement_scope.py",
        "/app/backend/services/background_lifecycle.py",
    ):
        src = Path(f).read_text()
        assert "prediction_snapshots" not in src, f
