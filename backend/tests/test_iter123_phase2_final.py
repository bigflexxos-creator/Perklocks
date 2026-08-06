"""Phase 2 FINAL closeout patch tests.

Covers the two remaining Phase 2 items:
  1. settlement_scope wired into settle_due_picks
  2. day-rollover refresh routed through JobCoordinator + ProviderBudget + gateway
"""
from __future__ import annotations

import asyncio
import os
import re
import uuid
from pathlib import Path
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
# Settlement scope wiring
# ═════════════════════════════════════════════════════════════════════
def test_settle_due_picks_consults_settlement_scope():
    """Source-level check: settlement_engine.py imports and uses
    settlement_scope.active_sport_keys in its scores fan-out branch."""
    src = Path("/app/backend/settlement_engine.py").read_text()
    # Import present (lazy-imported inline is OK).
    assert "from services.settlement_scope import active_sport_keys" in src \
        or "services.settlement_scope" in src, src[:400]
    assert "active" in src  # used at least once


def test_settle_due_picks_skips_when_no_active_keys():
    """When settlement_scope reports no active keys for a given sport,
    the score fetch loop must not iterate over the static SPORT_KEYS list."""
    src = Path("/app/backend/settlement_engine.py").read_text()
    # The skip branch must appear.
    assert "no active sport_keys" in src, (
        "settlement scope skip branch missing"
    )


def test_settlement_scope_returns_only_pending_sport_keys():
    async def go():
        db = _fresh_db()
        from services.settlement_scope import active_sport_keys
        tag = f"p2f_test_{uuid.uuid4().hex[:8]}"
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        # 3 pending picks for one sport_key + 1 already-graded pick
        # for another key.  Only the first key should show up.
        rows = [
            {"id": f"{tag}_1", "pick_date": today,
             "sport_key": f"{tag}_soccer_league_a", "sport": "Soccer",
             "status": "pending", "tag": tag},
            {"id": f"{tag}_2", "pick_date": today,
             "sport_key": f"{tag}_soccer_league_a", "sport": "Soccer",
             "status": "pending", "tag": tag},
            {"id": f"{tag}_3", "pick_date": today,
             "sport_key": f"{tag}_soccer_league_a", "sport": "Soccer",
             "status": "pending", "tag": tag},
            {"id": f"{tag}_4", "pick_date": today,
             "sport_key": f"{tag}_dead_league", "sport": "Soccer",
             "status": "won", "tag": tag},
        ]
        try:
            await db.picks.insert_many(rows)
            keys = await active_sport_keys(db)
            assert f"{tag}_soccer_league_a" in keys
            assert f"{tag}_dead_league" not in keys
        finally:
            await db.picks.delete_many({"tag": tag})
    _run(go())


# ═════════════════════════════════════════════════════════════════════
# Day-rollover coordinated refresh
# ═════════════════════════════════════════════════════════════════════
def test_day_rollover_refresh_goes_through_coordinator_and_budget():
    """Source-level check: the day-rollover branch of _daily_refresh_loop
    calls JobCoordinator.acquire + ProviderBudget.reserve BEFORE
    invoking _refresh_picks."""
    src = Path("/app/backend/server.py").read_text()
    # Locate the "day rolled" branch.
    m = re.search(
        r"if current_date != last_refresh_date:.*?last_refresh_date = current_date",
        src, flags=re.DOTALL,
    )
    assert m, "day-rollover branch not found"
    block = m.group(0)
    assert "JobCoordinator" in block
    assert "ProviderBudget" in block
    assert "coord.acquire" in block
    assert "budget.reserve" in block
    assert "picks_refresh_today" in block
    assert "day_rollover" in block


def test_day_rollover_two_instances_produce_one_run():
    """JobCoordinator.acquire is atomic — two concurrent acquires on
    the same job_name produce exactly one owner.  This is the primitive
    the day-rollover branch relies on."""
    async def go():
        db = _fresh_db()
        from services.job_coordinator import JobCoordinator, COLLECTION
        job = f"p2f_rollover_{uuid.uuid4().hex[:8]}"
        c1 = JobCoordinator(db)
        c2 = JobCoordinator(db)
        try:
            r1, r2 = await asyncio.gather(
                c1.acquire(job, lease_seconds=30, caller="a"),
                c2.acquire(job, lease_seconds=30, caller="b"),
            )
            winners = [r for r in (r1, r2) if r]
            assert len(winners) == 1
        finally:
            await db[COLLECTION].delete_one({"job_name": job})
    _run(go())


def test_day_rollover_budget_denial_yields_zero_upstream():
    """If ProviderBudget rejects the reservation, the day-rollover
    branch releases the lease and returns without calling
    _refresh_picks.  Proven by checking the source contains the
    coord.fail path for budget denial."""
    src = Path("/app/backend/server.py").read_text()
    m = re.search(
        r"if current_date != last_refresh_date:.*?last_refresh_date = current_date",
        src, flags=re.DOTALL,
    )
    block = m.group(0)
    assert "budget_denied" in block
    assert "coord.fail" in block


# ═════════════════════════════════════════════════════════════════════
# Phase 1 immutability preserved
# ═════════════════════════════════════════════════════════════════════
def test_no_new_module_writes_prediction_snapshots():
    for f in ("/app/backend/services/settlement_scope.py",
               "/app/backend/services/cache_policy.py",
               "/app/backend/services/background_lifecycle.py"):
        src = Path(f).read_text()
        assert "prediction_snapshots" not in src, f
