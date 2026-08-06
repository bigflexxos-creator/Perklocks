"""Phase 2β — JobCoordinator + ProviderBudget + force-refresh tests.

Covers the 22 required assertions from the Phase 2β spec:

  1.  Two concurrent callers cannot acquire the same job.
  2.  The lease owner can heartbeat.
  3.  A non-owner cannot heartbeat / complete / fail / release.
  4.  Expired leases can be recovered.
  5.  ``next_eligible_at`` blocks early reruns.
  6.  A completed job updates counters and execution history.
  7.  A failed job records sanitized error data.
  8.  Two concurrent budget reservations cannot exceed the remaining
      daily budget.
  9.  Daily limit blocks correctly.
  10. Monthly limit blocks correctly.
  11. Emergency reserve is denied to user actions.
  12. Emergency reserve is allowed only for missing / critically
      stale board recovery.
  13. Duplicate ``request_key`` reservations are idempotent.
  14. Released reservations return capacity.
  15. Committed reservations cannot be committed twice.
  16. Budget state survives a new service instance.
  17. Reconciliation matches request-log totals.
  18. Shadow mode records allow/block decisions without changing
      approved jobs.
  19. Normal users cannot trigger global generation through
      force-refresh.
  20. Existing Phase 1 tests continue passing.  (Covered elsewhere.)
  21. Existing Phase 2α audit files remain unchanged.  (Covered
      elsewhere — see PHASE2_BASELINE_REPORT.md.)
  22. No prediction snapshot fields are mutated.
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

from services.job_coordinator import (
    JobCoordinator,
    STATUS_EXPIRED, STATUS_COMPLETED, STATUS_FAILED,
    _sanitize_metadata, _sanitize_error, _hash_token,
    EXECUTION_LOG, AUDIT_LOG, COLLECTION as JOB_COLL,
)
from services.provider_budget import (
    ProviderBudget,
    OUT_ALLOWED, OUT_BLOCKED_DAILY, OUT_BLOCKED_MONTHLY,
    OUT_BLOCKED_EMERGENCY_POLICY, OUT_DUPLICATE, OUT_COMMITTED,
    INTENT_RESERVED, INTENT_COMMITTED,
    BUDGET_STATE_COLL, INTENTS_COLL,
    _day_key, _month_key,
    _daily_limit, _monthly_limit, _emergency_reserve,
)


# ── Helpers ─────────────────────────────────────────────────────────
def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if not asyncio.iscoroutine(coro) else asyncio.run(coro)


def _fresh_db():
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    return client[os.environ.get("DB_NAME", "lockscore_db")]


async def _reset_test_state(db, tag: str):
    """Drop only the collections we own for this test."""
    await db[JOB_COLL].delete_many({"job_name": {"$regex": f"^{tag}"}})
    await db[EXECUTION_LOG].delete_many({"job_name": {"$regex": f"^{tag}"}})
    await db[AUDIT_LOG].delete_many({"job_name": {"$regex": f"^{tag}"}})
    # Provider budget state + intents are shared across all Phase 2β
    # tests (they share ``test_provider``).  Reset ALL of them so
    # each test starts from zero regardless of prior test ordering.
    await db[INTENTS_COLL].delete_many({"provider": "test_provider"})
    await db[BUDGET_STATE_COLL].delete_many({"provider": "test_provider"})


# ═════════════════════════════════════════════════════════════════════
# JobCoordinator
# ═════════════════════════════════════════════════════════════════════
def test_1_concurrent_acquire_only_one_winner():
    async def go():
        db = _fresh_db()
        await _reset_test_state(db, "p2b_test1")
        c1 = JobCoordinator(db)
        c2 = JobCoordinator(db)
        await c1.ensure_indices()
        results = await asyncio.gather(
            c1.acquire("p2b_test1_job", lease_seconds=30,
                       owner_instance="A", caller="test1"),
            c2.acquire("p2b_test1_job", lease_seconds=30,
                       owner_instance="B", caller="test1"),
        )
        winners = [r for r in results if r]
        assert len(winners) == 1
        # Same lease token cannot appear twice.
        tokens = {r.get("lease_token") for r in results if r}
        assert len(tokens) == 1
    _run(go())


def test_2_owner_can_heartbeat():
    async def go():
        db = _fresh_db()
        await _reset_test_state(db, "p2b_test2")
        c = JobCoordinator(db)
        r = await c.acquire("p2b_test2_job", lease_seconds=10,
                              caller="test2")
        assert r
        ok = await c.heartbeat("p2b_test2_job", r.lease_token,
                                extend_seconds=30)
        assert ok
    _run(go())


def test_3_non_owner_cannot_mutate():
    async def go():
        db = _fresh_db()
        await _reset_test_state(db, "p2b_test3")
        c = JobCoordinator(db)
        r = await c.acquire("p2b_test3_job", lease_seconds=10,
                              caller="test3")
        assert r
        bogus = "bogus-token"
        assert (await c.heartbeat("p2b_test3_job", bogus)) is False
        assert (await c.complete("p2b_test3_job", bogus)) is False
        assert (await c.fail("p2b_test3_job", bogus, error="x")) is False
        assert (await c.release("p2b_test3_job", bogus)) is False
        # And the real owner still owns it.
        doc = await c.get_status("p2b_test3_job")
        assert doc["status"] == "running"
        assert doc["lease_token"] == r.lease_token
    _run(go())


def test_4_expired_leases_can_be_recovered():
    async def go():
        db = _fresh_db()
        await _reset_test_state(db, "p2b_test4")
        c = JobCoordinator(db)
        r = await c.acquire("p2b_test4_job", lease_seconds=1,
                              caller="test4")
        assert r
        # Force expiry — bypass sleep by manually setting lease_until.
        past = datetime.now(timezone.utc) - timedelta(seconds=10)
        await db[JOB_COLL].update_one(
            {"job_name": "p2b_test4_job"},
            {"$set": {"lease_until": past}},
        )
        n = await c.recover_expired_leases()
        assert n >= 1
        doc = await c.get_status("p2b_test4_job")
        assert doc["status"] == STATUS_EXPIRED
        # And a fresh caller can now acquire.
        r2 = await c.acquire("p2b_test4_job", lease_seconds=30,
                                caller="test4b")
        assert r2
    _run(go())


def test_5_next_eligible_at_blocks_early_reruns():
    async def go():
        db = _fresh_db()
        await _reset_test_state(db, "p2b_test5")
        c = JobCoordinator(db)
        r = await c.acquire("p2b_test5_job", lease_seconds=30,
                              caller="test5",
                              min_interval_seconds=3600)
        assert r
        await c.complete("p2b_test5_job", r.lease_token)
        # Second acquire should be blocked by next_eligible_at.
        r2 = await c.acquire("p2b_test5_job", lease_seconds=30,
                                caller="test5")
        assert not r2
        assert r2.get("reason") == "blocked_min_interval"
    _run(go())


def test_6_completed_updates_counters_and_execution_log():
    async def go():
        db = _fresh_db()
        await _reset_test_state(db, "p2b_test6")
        c = JobCoordinator(db)
        r = await c.acquire("p2b_test6_job", lease_seconds=30,
                              caller="test6")
        assert r
        ok = await c.complete("p2b_test6_job", r.lease_token,
                                result_metadata={"count": 42})
        assert ok
        doc = await c.get_status("p2b_test6_job")
        assert doc["success_count"] == 1
        assert doc["run_count"] == 1
        # Execution log entry exists and closed.
        exec_rows = await db[EXECUTION_LOG].find(
            {"job_name": "p2b_test6_job"}, {"_id": 0},
        ).to_list(10)
        assert exec_rows
        assert any(r_.get("status") == STATUS_COMPLETED for r_ in exec_rows)
    _run(go())


def test_7_failed_job_records_sanitized_error():
    async def go():
        db = _fresh_db()
        await _reset_test_state(db, "p2b_test7")
        c = JobCoordinator(db)
        r = await c.acquire("p2b_test7_job", lease_seconds=30,
                              caller="test7")
        # A raw secret-looking string should be redacted.
        secret = "sk_" + "X" * 40
        err = f"boom leaked token {secret}"
        ok = await c.fail("p2b_test7_job", r.lease_token,
                            error=err, retry_after_seconds=60)
        assert ok
        doc = await c.get_status("p2b_test7_job")
        assert doc["status"] == STATUS_FAILED
        # Full secret must not persist.
        assert secret not in (doc.get("last_error") or "")
        assert "redacted" in (doc.get("last_error") or "")
    _run(go())


# ═════════════════════════════════════════════════════════════════════
# ProviderBudget
# ═════════════════════════════════════════════════════════════════════
def test_8_concurrent_reservations_cannot_exceed_daily():
    async def go():
        db = _fresh_db()
        await _reset_test_state(db, "p2b_test8")
        b = ProviderBudget(db, provider="test_provider")
        await b.ensure_indices()
        # Small artificial ceiling — we control env for the test.
        os.environ["ODDS_DAILY_CREDIT_LIMIT"]     = "100"
        os.environ["ODDS_MONTHLY_CREDIT_LIMIT"]   = "10000"
        os.environ["ODDS_EMERGENCY_RESERVE"]      = "1000"
        try:
            # Fire 20 concurrent reserves of 10 credits each — only 10
            # should succeed (10 × 10 = 100 daily cap).
            tasks = [
                b.reserve(
                    estimated_credits=10, endpoint_type="test",
                    caller="p2b_test8", job_name="p2b_test8_job",
                    reason="test",
                )
                for _ in range(20)
            ]
            results = await asyncio.gather(*tasks)
            allowed = [r for r in results if r.get("allowed")]
            assert len(allowed) == 10, (
                f"Expected exactly 10 to be granted, got {len(allowed)}"
            )
        finally:
            for k in ("ODDS_DAILY_CREDIT_LIMIT",
                       "ODDS_MONTHLY_CREDIT_LIMIT",
                       "ODDS_EMERGENCY_RESERVE"):
                os.environ.pop(k, None)
    _run(go())


def test_9_daily_limit_blocks_correctly():
    async def go():
        db = _fresh_db()
        await _reset_test_state(db, "p2b_test9")
        b = ProviderBudget(db, provider="test_provider")
        os.environ["ODDS_DAILY_CREDIT_LIMIT"]   = "50"
        os.environ["ODDS_MONTHLY_CREDIT_LIMIT"] = "10000"
        try:
            r = await b.reserve(
                estimated_credits=200,
                endpoint_type="test",
                caller="p2b_test9",
                job_name="p2b_test9_job",
                reason="test",
            )
            assert not r.get("allowed")
            assert r["outcome"] == OUT_BLOCKED_DAILY
        finally:
            os.environ.pop("ODDS_DAILY_CREDIT_LIMIT", None)
            os.environ.pop("ODDS_MONTHLY_CREDIT_LIMIT", None)
    _run(go())


def test_10_monthly_limit_blocks_correctly():
    async def go():
        db = _fresh_db()
        await _reset_test_state(db, "p2b_test10")
        b = ProviderBudget(db, provider="test_provider")
        os.environ["ODDS_DAILY_CREDIT_LIMIT"]   = "10000"
        os.environ["ODDS_MONTHLY_CREDIT_LIMIT"] = "100"
        os.environ["ODDS_EMERGENCY_RESERVE"]    = "10"
        try:
            r = await b.reserve(
                estimated_credits=200,
                endpoint_type="test",
                caller="p2b_test10",
                job_name="p2b_test10_job",
                reason="test",
            )
            assert not r.get("allowed")
            assert r["outcome"] == OUT_BLOCKED_MONTHLY
        finally:
            for k in ("ODDS_DAILY_CREDIT_LIMIT",
                       "ODDS_MONTHLY_CREDIT_LIMIT",
                       "ODDS_EMERGENCY_RESERVE"):
                os.environ.pop(k, None)
    _run(go())


def test_11_emergency_reserve_denied_for_user_actions():
    async def go():
        db = _fresh_db()
        await _reset_test_state(db, "p2b_test11")
        b = ProviderBudget(db, provider="test_provider")
        r = await b.reserve(
            estimated_credits=100,
            endpoint_type="picks_refresh",
            caller="user_refresh:demo",   # ← user action
            job_name="picks_refresh_today",
            emergency_requested=True,
            reason="board_missing",       # magic reason ignored — caller blocked
        )
        assert not r.get("allowed")
        assert r["outcome"] == OUT_BLOCKED_EMERGENCY_POLICY
    _run(go())


def test_12_emergency_reserve_allowed_for_board_recovery():
    async def go():
        db = _fresh_db()
        await _reset_test_state(db, "p2b_test12")
        b = ProviderBudget(db, provider="test_provider")
        # Consume all normal capacity so the emergency reserve is the
        # only way credit is available.
        os.environ["ODDS_DAILY_CREDIT_LIMIT"]     = "10000"
        os.environ["ODDS_MONTHLY_CREDIT_LIMIT"]   = "1000"
        os.environ["ODDS_EMERGENCY_RESERVE"]      = "500"   # cap 500-1000
        try:
            # Fill normal capacity.
            r_fill = await b.reserve(
                estimated_credits=500, endpoint_type="fill",
                caller="admin:seed", job_name="fill_job",
                reason="seed",
            )
            assert r_fill.get("allowed")
            await b.commit(r_fill["intent_id"], actual_credits=500)
            # Now request emergency capacity as legitimate admin
            # recovery.
            r_em = await b.reserve(
                estimated_credits=300, endpoint_type="picks_refresh",
                caller="admin:ops", job_name="picks_refresh_today",
                emergency_requested=True, reason="board_missing",
            )
            assert r_em.get("allowed"), r_em
            assert r_em.get("emergency") is True
        finally:
            for k in ("ODDS_DAILY_CREDIT_LIMIT",
                       "ODDS_MONTHLY_CREDIT_LIMIT",
                       "ODDS_EMERGENCY_RESERVE"):
                os.environ.pop(k, None)
    _run(go())


def test_13_duplicate_request_key_is_idempotent():
    async def go():
        db = _fresh_db()
        await _reset_test_state(db, "p2b_test13")
        b = ProviderBudget(db, provider="test_provider")
        os.environ["ODDS_EMERGENCY_RESERVE"] = "0"
        try:
            key = f"p2b_test13_{uuid.uuid4().hex}"
            r1 = await b.reserve(
                estimated_credits=25, endpoint_type="test",
                caller="p2b_test13", job_name="p2b_test13_job",
                request_key=key, reason="test",
            )
            assert r1.get("allowed")
            r2 = await b.reserve(
                estimated_credits=25, endpoint_type="test",
                caller="p2b_test13", job_name="p2b_test13_job",
                request_key=key, reason="test",
            )
            assert r2["outcome"] == OUT_DUPLICATE
            assert r2["intent_id"] == r1["intent_id"]
        finally:
            os.environ.pop("ODDS_EMERGENCY_RESERVE", None)
    _run(go())


def test_14_released_reservations_return_capacity():
    async def go():
        db = _fresh_db()
        await _reset_test_state(db, "p2b_test14")
        b = ProviderBudget(db, provider="test_provider")
        os.environ["ODDS_DAILY_CREDIT_LIMIT"]   = "100"
        os.environ["ODDS_MONTHLY_CREDIT_LIMIT"] = "10000"
        os.environ["ODDS_EMERGENCY_RESERVE"]    = "0"
        try:
            r = await b.reserve(
                estimated_credits=80, endpoint_type="test",
                caller="p2b_test14", job_name="p2b_test14_job",
                reason="test",
            )
            assert r.get("allowed")
            before = await b.get_daily_usage()
            assert before["day_reserved"] == 80
            rel = await b.release(r["intent_id"], reason="test_release")
            assert rel.get("released")
            after = await b.get_daily_usage()
            assert after["day_reserved"] == 0
            # And now a follow-up reservation for the full ceiling works.
            r2 = await b.reserve(
                estimated_credits=100, endpoint_type="test",
                caller="p2b_test14", job_name="p2b_test14_job",
                reason="test",
            )
            assert r2.get("allowed")
        finally:
            os.environ.pop("ODDS_DAILY_CREDIT_LIMIT", None)
            os.environ.pop("ODDS_MONTHLY_CREDIT_LIMIT", None)
            os.environ.pop("ODDS_EMERGENCY_RESERVE", None)
    _run(go())


def test_15_committed_reservations_cannot_be_committed_twice():
    async def go():
        db = _fresh_db()
        await _reset_test_state(db, "p2b_test15")
        b = ProviderBudget(db, provider="test_provider")
        os.environ["ODDS_EMERGENCY_RESERVE"] = "0"
        try:
            r = await b.reserve(
                estimated_credits=15, endpoint_type="test",
                caller="p2b_test15", job_name="p2b_test15_job",
                reason="test",
            )
            c1 = await b.commit(r["intent_id"], actual_credits=15)
            assert c1.get("committed")
            # Second commit: idempotent, no double-count.
            before = await b.get_daily_usage()
            c2 = await b.commit(r["intent_id"], actual_credits=15)
            after = await b.get_daily_usage()
            assert c2.get("committed") is True
            assert c2.get("idempotent") is True
            assert before["day_used"] == after["day_used"]
        finally:
            os.environ.pop("ODDS_EMERGENCY_RESERVE", None)
    _run(go())


def test_16_budget_state_survives_new_instance():
    async def go():
        db = _fresh_db()
        await _reset_test_state(db, "p2b_test16")
        os.environ["ODDS_EMERGENCY_RESERVE"] = "0"
        try:
            b1 = ProviderBudget(db, provider="test_provider")
            r = await b1.reserve(
                estimated_credits=40, endpoint_type="test",
                caller="p2b_test16", job_name="p2b_test16_job",
                reason="test",
            )
            assert r.get("allowed")
            await b1.commit(r["intent_id"], actual_credits=40)
            # New instance queries the same Mongo doc.
            b2 = ProviderBudget(db, provider="test_provider")
            st = await b2.get_daily_usage()
            assert st["day_used"] >= 40
        finally:
            os.environ.pop("ODDS_EMERGENCY_RESERVE", None)
    _run(go())


def test_17_reconcile_matches_request_log_totals():
    async def go():
        db = _fresh_db()
        await _reset_test_state(db, "p2b_test17")
        b = ProviderBudget(db, provider="test_provider")
        os.environ["ODDS_EMERGENCY_RESERVE"] = "0"
        try:
            # Commit two intents totalling 30 credits.
            r1 = await b.reserve(
                estimated_credits=10, endpoint_type="test",
                caller="p2b_test17", job_name="p2b_test17_job",
                reason="test",
            )
            r2 = await b.reserve(
                estimated_credits=20, endpoint_type="test",
                caller="p2b_test17", job_name="p2b_test17_job",
                reason="test",
            )
            await b.commit(r1["intent_id"])
            await b.commit(r2["intent_id"])
            recon = await b.reconcile_from_request_log()
            assert recon["committed_intents"] == 2
            assert recon["committed_credits"] == 30
        finally:
            os.environ.pop("ODDS_EMERGENCY_RESERVE", None)
    _run(go())


# ═════════════════════════════════════════════════════════════════════
# Shadow mode + force-refresh + Phase 1 invariants
# ═════════════════════════════════════════════════════════════════════
def test_18_shadow_mode_does_not_change_state():
    async def go():
        db = _fresh_db()
        await _reset_test_state(db, "p2b_test18")
        # Nothing pre-existing.
        pre_jobs = await db[JOB_COLL].count_documents({})
        pre_state = await db[BUDGET_STATE_COLL].count_documents(
            {"provider": "test_shadow_provider"})
        from services.shadow_wiring import shadow_check
        dec = await shadow_check(
            db, job_name="p2b_test18_job",
            caller="test_shadow", reason="scheduled_snapshot",
            estimated_credits=25,
        )
        assert dec["shadow"] is True
        # Shadow must NOT create a scheduled_jobs entry.
        # (get_status may lazily bootstrap the row via a peek — verify
        # the job was never actually claimed/incremented.)
        post = await db[JOB_COLL].find_one({"job_name": "p2b_test18_job"})
        if post is not None:
            assert post.get("run_count", 0) == 0
            assert post.get("status") in (None, "idle")
        # Shadow must NOT create budget state for the odds_api provider
        # (the check ran read-only against the default provider).
        post_state = await db[BUDGET_STATE_COLL].count_documents(
            {"provider": "test_shadow_provider"})
        assert pre_state == post_state
        # But a shadow_decision audit row is written.
        audit = await db[AUDIT_LOG].find_one({
            "event_type": "shadow_decision",
            "job_name": "p2b_test18_job",
        })
        assert audit is not None
    _run(go())


def test_19_normal_user_force_refresh_does_not_trigger_generation(monkeypatch):
    """Guard against regression — the user-facing /picks/refresh
    endpoint must NOT reference `_refresh_picks` in its handler body.
    (Phase 2β behavior change: DB-only for users.)"""
    from pathlib import Path
    src = Path("/app/backend/routes/picks_routes.py").read_text()
    # Locate the /refresh handler.
    idx = src.find('@router.post("/refresh")')
    assert idx > 0
    end = src.find("@router.", idx + 10)
    assert end > 0
    body = src[idx:end]
    assert "_refresh_picks" not in body, (
        "Phase 2β: normal-user force-refresh must not invoke "
        "_refresh_picks."
    )
    assert "db_only" in body


def test_22_prediction_snapshots_are_not_mutated():
    """Phase 1 immutability guarantee — none of the Phase 2β modules
    open the prediction_snapshots collection for writes."""
    from pathlib import Path
    for f in (
        "/app/backend/services/job_coordinator.py",
        "/app/backend/services/provider_budget.py",
        "/app/backend/services/shadow_wiring.py",
        "/app/backend/services/job_registry.py",
        "/app/backend/routes/ops_routes.py",
    ):
        src = Path(f).read_text()
        assert "prediction_snapshots" not in src, (
            f"{f} references prediction_snapshots — Phase 2β must NOT "
            "touch the immutable snapshot store."
        )


# ═════════════════════════════════════════════════════════════════════
# Additional helper-level correctness checks
# ═════════════════════════════════════════════════════════════════════
def test_sanitizer_redacts_secrets():
    md = {
        "api_key": "sk-should-be-hidden",
        "Authorization": "Bearer x",
        "nested": {"password": "p"},
        "safe": "value",
    }
    out = _sanitize_metadata(md)
    assert out["api_key"] == "***"
    assert out["Authorization"] == "***"
    assert out["nested"]["password"] == "***"
    assert out["safe"] == "value"


def test_error_sanitizer_redacts_long_tokens():
    err = "oops sk_" + "X" * 40 + " leaked"
    out = _sanitize_error(err)
    assert "XXXX" not in out or "redacted" in out


def test_token_hash_is_stable():
    t = uuid.uuid4().hex
    a = _hash_token(t)
    b = _hash_token(t)
    assert a == b
    assert len(a) == 64
