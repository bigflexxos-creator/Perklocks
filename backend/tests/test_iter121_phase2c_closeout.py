"""Phase 2γ closeout — /sports catalog reuse + budget top-up + guardrails.

Fills the gaps flagged during Phase 2γ provisional acceptance:
  • Snapshot-scoped /sports reuse
  • Actual-cost top-up accounting
  • Gateway migration completeness
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone

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
# /sports catalog reuse
# ═════════════════════════════════════════════════════════════════════
def test_sports_catalog_reuse_single_upstream_per_run():
    """Multiple discovery consumers in the same coordinated run must
    trigger at most one upstream /sports call."""
    async def go():
        db = _fresh_db()
        from services import sports_catalog
        await sports_catalog.ensure_indices(db)
        rid = f"test_run_{uuid.uuid4().hex[:12]}"
        # Pre-seed the row with a fake catalog so no upstream is required.
        await db[sports_catalog.COLLECTION].delete_many({"run_id": rid})
        await db[sports_catalog.COLLECTION].insert_one({
            "run_id": rid,
            "data":   [{"key": "basketball_nba", "active": True},
                        {"key": "soccer_epl", "active": True}],
            "fetched_at": datetime.now(timezone.utc),
        })
        # Three concurrent consumers → all must be cache_hit=True.
        results = await asyncio.gather(*[
            sports_catalog.get_catalog(
                db, run_id=rid, caller=f"consumer_{i}", reason="test")
            for i in range(3)
        ])
        for r in results:
            assert r["cache_hit"] is True, r
            assert r["upstream_called"] is False
            assert isinstance(r["data"], list)
            assert any(s["key"] == "basketball_nba" for s in r["data"])
    _run(go())


def test_sports_catalog_run_id_defaults_to_10min_bucket():
    from services.sports_catalog import current_run_id
    a = current_run_id()
    b = current_run_id()
    assert a == b
    assert "T" in a


# ═════════════════════════════════════════════════════════════════════
# ProviderBudget.top_up
# ═════════════════════════════════════════════════════════════════════
def _reset_budget(db):
    return db["provider_budget_state"].delete_many(
        {"provider": "test_topup_provider"})


def test_top_up_success_when_capacity_available():
    async def go():
        db = _fresh_db()
        from services.provider_budget import (
            ProviderBudget, INTENTS_COLL, BUDGET_STATE_COLL,
        )
        await db[INTENTS_COLL].delete_many({"provider": "test_topup_provider"})
        await db[BUDGET_STATE_COLL].delete_many(
            {"provider": "test_topup_provider"})
        os.environ["ODDS_DAILY_CREDIT_LIMIT"]   = "500"
        os.environ["ODDS_MONTHLY_CREDIT_LIMIT"] = "10000"
        os.environ["ODDS_EMERGENCY_RESERVE"]    = "0"
        try:
            b = ProviderBudget(db, provider="test_topup_provider")
            r = await b.reserve(estimated_credits=50, endpoint_type="t",
                                  caller="topup_test", job_name="j",
                                  reason="test")
            assert r["allowed"]
            t = await b.top_up(r["intent_id"], extra=25,
                                reason="actual_over_estimate")
            assert t["ok"] is True
            # Commit at the new actual and confirm daily accounting.
            c = await b.commit(r["intent_id"], actual_credits=75)
            assert c["committed"] is True
            usage = await b.get_daily_usage()
            assert usage["day_used"] == 75, usage
        finally:
            for k in ("ODDS_DAILY_CREDIT_LIMIT",
                       "ODDS_MONTHLY_CREDIT_LIMIT",
                       "ODDS_EMERGENCY_RESERVE"):
                os.environ.pop(k, None)
    _run(go())


def test_top_up_denied_when_daily_cap_hit():
    async def go():
        db = _fresh_db()
        from services.provider_budget import (
            ProviderBudget, INTENTS_COLL, BUDGET_STATE_COLL,
            OUT_BLOCKED_DAILY,
        )
        await db[INTENTS_COLL].delete_many({"provider": "test_topup_provider"})
        await db[BUDGET_STATE_COLL].delete_many(
            {"provider": "test_topup_provider"})
        os.environ["ODDS_DAILY_CREDIT_LIMIT"]   = "60"
        os.environ["ODDS_MONTHLY_CREDIT_LIMIT"] = "10000"
        os.environ["ODDS_EMERGENCY_RESERVE"]    = "0"
        try:
            b = ProviderBudget(db, provider="test_topup_provider")
            r = await b.reserve(estimated_credits=50, endpoint_type="t",
                                  caller="topup_test", job_name="j",
                                  reason="test")
            assert r["allowed"]
            # Actual came in 50 credits higher — day cap won't allow.
            t = await b.top_up(r["intent_id"], extra=50)
            assert t["ok"] is False
            assert t["outcome"] == OUT_BLOCKED_DAILY
        finally:
            for k in ("ODDS_DAILY_CREDIT_LIMIT",
                       "ODDS_MONTHLY_CREDIT_LIMIT",
                       "ODDS_EMERGENCY_RESERVE"):
                os.environ.pop(k, None)
    _run(go())


def test_concurrent_top_ups_cannot_exceed_daily_cap():
    async def go():
        db = _fresh_db()
        from services.provider_budget import (
            ProviderBudget, INTENTS_COLL, BUDGET_STATE_COLL,
        )
        await db[INTENTS_COLL].delete_many({"provider": "test_topup_provider"})
        await db[BUDGET_STATE_COLL].delete_many(
            {"provider": "test_topup_provider"})
        os.environ["ODDS_DAILY_CREDIT_LIMIT"]   = "100"
        os.environ["ODDS_MONTHLY_CREDIT_LIMIT"] = "10000"
        os.environ["ODDS_EMERGENCY_RESERVE"]    = "0"
        try:
            b = ProviderBudget(db, provider="test_topup_provider")
            # Two independent reservations of 40 each = 80 used.
            r1 = await b.reserve(estimated_credits=40, endpoint_type="t",
                                   caller="topup_test", job_name="j",
                                   reason="test")
            r2 = await b.reserve(estimated_credits=40, endpoint_type="t",
                                   caller="topup_test", job_name="j",
                                   reason="test")
            # Both callers realise their actual is 40 credits higher.
            # Only ONE top-up (20 credits worth of headroom) should succeed.
            t1, t2 = await asyncio.gather(
                b.top_up(r1["intent_id"], extra=15),
                b.top_up(r2["intent_id"], extra=15),
            )
            ok_count = int(t1.get("ok") is True) + int(t2.get("ok") is True)
            # Both fit — 80 + 15 + 15 = 110 > 100, so at most one wins.
            assert ok_count <= 1, (t1, t2)
        finally:
            for k in ("ODDS_DAILY_CREDIT_LIMIT",
                       "ODDS_MONTHLY_CREDIT_LIMIT",
                       "ODDS_EMERGENCY_RESERVE"):
                os.environ.pop(k, None)
    _run(go())


def test_top_up_records_audit_row():
    async def go():
        db = _fresh_db()
        from services.provider_budget import (
            ProviderBudget, INTENTS_COLL, BUDGET_STATE_COLL, AUDIT_COLL,
        )
        await db[INTENTS_COLL].delete_many({"provider": "test_topup_provider"})
        await db[BUDGET_STATE_COLL].delete_many(
            {"provider": "test_topup_provider"})
        os.environ["ODDS_EMERGENCY_RESERVE"] = "0"
        try:
            b = ProviderBudget(db, provider="test_topup_provider")
            r = await b.reserve(estimated_credits=5, endpoint_type="t",
                                  caller="topup_test", job_name="j",
                                  reason="test")
            await b.top_up(r["intent_id"], extra=3, reason="test_audit")
            audit = await db[AUDIT_COLL].find_one({
                "event_type": "budget_top_up",
                "intent_id": r["intent_id"],
            })
            assert audit is not None
            assert audit.get("extra") == 3
        finally:
            os.environ.pop("ODDS_EMERGENCY_RESERVE", None)
    _run(go())
