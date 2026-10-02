"""Focused tests for the MongoDB-backed canonical worker lease.

Covers exactly the 15 scenarios demanded by the Perklocks
"PRODUCTION DISTRIBUTED WORKER LEASE" acceptance spec:

 1. Two eligible Production instances race → exactly one wins.
 2. Three or more eligible Production instances race → exactly one wins.
 3. Losing instance cannot execute protected canonical worker.
 4. Owner can renew lease.
 5. Non-owner cannot renew owner's lease.
 6. Owner loses authority immediately after failed renewal.
 7. Expired lease can be reclaimed by another eligible Production
    instance.
 8. Preview cannot acquire canonical worker lease.
 9. CANONICAL_WRITE_ENABLED=false cannot acquire lease.
10. BACKGROUND_WORKERS_ENABLED=false cannot acquire lease.
11. DATA_AUTHORITY != production cannot acquire lease.
12. Graceful release allows another eligible instance to acquire.
13. Crash simulation + TTL expiry allows failover (no restart).
14. Existing Preview background-worker suppression remains zero-running.
15. API/read operation remains available on a non-owner instance.

Run via:
    cd /app/backend && pytest tests/test_canonical_worker_lease.py -v
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import List

import pytest
from motor.motor_asyncio import AsyncIOMotorClient

from services import canonical_worker_lease as cwl
from services.canonical_worker_lease import (
    CanonicalWorkerLease,
    GLOBAL_LEASE_NAME,
    is_eligible_for_canonical_lease,
)


MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")


# ─── Env helpers ──────────────────────────────────────────────────────
def set_production_env(monkeypatch) -> None:
    monkeypatch.setenv("DATA_AUTHORITY", "production")
    monkeypatch.setenv("CANONICAL_WRITE_ENABLED", "true")
    monkeypatch.setenv("BACKGROUND_WORKERS_ENABLED", "true")


def set_preview_env(monkeypatch) -> None:
    monkeypatch.setenv("DATA_AUTHORITY", "preview")
    monkeypatch.setenv("CANONICAL_WRITE_ENABLED", "false")
    monkeypatch.setenv("BACKGROUND_WORKERS_ENABLED", "false")


# ─── Fixtures ─────────────────────────────────────────────────────────
@pytest.fixture
async def test_db():
    """Fresh test database per test — isolated from the real one."""
    db_name = f"lease_test_{uuid.uuid4().hex[:10]}"
    client = AsyncIOMotorClient(MONGO_URL)
    db = client[db_name]
    try:
        yield db
    finally:
        try:
            await client.drop_database(db_name)
        finally:
            client.close()


def make_instance(db, *, instance_id: str = None,
                  ttl_s: int = 90, heartbeat_s: int = 30,
                  collection_name: str = None) -> CanonicalWorkerLease:
    coll = collection_name or f"canonical_worker_leases_{uuid.uuid4().hex[:6]}"
    return CanonicalWorkerLease(
        db,
        lease_name=GLOBAL_LEASE_NAME,
        instance_id=instance_id or f"inst-{uuid.uuid4().hex[:8]}",
        ttl_s=ttl_s,
        heartbeat_s=heartbeat_s,
        collection_name=coll,
    )


def make_cluster(db, n: int, *, collection_name: str,
                 ttl_s: int = 90, heartbeat_s: int = 30
                 ) -> List[CanonicalWorkerLease]:
    """Build N leases that all target the same collection (same lease
    name) — simulating N replicas racing for the lease."""
    out = []
    for i in range(n):
        out.append(CanonicalWorkerLease(
            db,
            lease_name=GLOBAL_LEASE_NAME,
            instance_id=f"inst-{i}-{uuid.uuid4().hex[:6]}",
            ttl_s=ttl_s,
            heartbeat_s=heartbeat_s,
            collection_name=collection_name,
        ))
    return out


# ─── 1. Two Production instances race ────────────────────────────────
@pytest.mark.asyncio
async def test_01_two_instances_race_exactly_one_wins(test_db, monkeypatch):
    set_production_env(monkeypatch)
    coll = f"lease_t01_{uuid.uuid4().hex[:6]}"
    leases = make_cluster(test_db, 2, collection_name=coll)
    for L in leases:
        await L.ensure_index()

    results = await asyncio.gather(*(L.acquire() for L in leases))
    winners = sum(1 for r in results if r)
    assert winners == 1, f"expected exactly 1 winner, got {winners} (results={results})"


# ─── 2. Three+ instances race ────────────────────────────────────────
@pytest.mark.asyncio
async def test_02_three_plus_instances_race_exactly_one_wins(test_db, monkeypatch):
    set_production_env(monkeypatch)
    coll = f"lease_t02_{uuid.uuid4().hex[:6]}"
    leases = make_cluster(test_db, 5, collection_name=coll)
    for L in leases:
        await L.ensure_index()

    results = await asyncio.gather(*(L.acquire() for L in leases))
    winners = sum(1 for r in results if r)
    assert winners == 1, f"expected exactly 1 winner, got {winners}"
    # And exactly one row exists.
    rows = await test_db[coll].count_documents({"lease_name": GLOBAL_LEASE_NAME})
    assert rows == 1


# ─── 3. Losing instance cannot execute protected worker ───────────────
@pytest.mark.asyncio
async def test_03_losing_instance_cannot_execute_worker(test_db, monkeypatch):
    set_production_env(monkeypatch)
    coll = f"lease_t03_{uuid.uuid4().hex[:6]}"
    owner, loser = make_cluster(test_db, 2, collection_name=coll)
    await owner.ensure_index()
    assert await owner.acquire() is True
    assert await loser.acquire() is False

    # Simulate the server.py gate: non-owner should be rejected.
    executed = {"owner": False, "loser": False}

    async def protected_worker(lease, key):
        if lease.owns_local():
            executed[key] = True

    await asyncio.gather(
        protected_worker(owner, "owner"),
        protected_worker(loser, "loser"),
    )
    assert executed["owner"] is True
    assert executed["loser"] is False


# ─── 4. Owner can renew lease ────────────────────────────────────────
@pytest.mark.asyncio
async def test_04_owner_can_renew(test_db, monkeypatch):
    set_production_env(monkeypatch)
    L = make_instance(test_db, ttl_s=60, heartbeat_s=10)
    await L.ensure_index()
    assert await L.acquire() is True
    first_expires = L.last_expires_at()
    await asyncio.sleep(0.05)
    assert await L.renew() is True
    assert L.owns_local() is True
    assert L.last_expires_at() >= first_expires


# ─── 5. Non-owner cannot renew owner's lease ─────────────────────────
@pytest.mark.asyncio
async def test_05_non_owner_cannot_renew(test_db, monkeypatch):
    set_production_env(monkeypatch)
    coll = f"lease_t05_{uuid.uuid4().hex[:6]}"
    owner, ghost = make_cluster(test_db, 2, collection_name=coll)
    await owner.ensure_index()
    assert await owner.acquire() is True
    assert await ghost.acquire() is False
    assert await ghost.renew() is False, "non-owner must not be able to renew"
    assert ghost.owns_local() is False
    # Owner still owns.
    assert await owner.owns() is True


# ─── 6. Owner loses authority after failed renewal ───────────────────
@pytest.mark.asyncio
async def test_06_owner_loses_authority_after_failed_renewal(test_db, monkeypatch):
    set_production_env(monkeypatch)
    coll = f"lease_t06_{uuid.uuid4().hex[:6]}"
    owner, usurper = make_cluster(test_db, 2, collection_name=coll)
    await owner.ensure_index()
    assert await owner.acquire() is True

    # Forcibly expire the owner's lease and have another instance take it.
    now = datetime.utcnow()
    await test_db[coll].update_one(
        {"lease_name": GLOBAL_LEASE_NAME},
        {"$set": {"expires_at": now - timedelta(seconds=5)}},
    )
    assert await usurper.acquire() is True

    # Original owner tries to renew — must fail, local flag flips to False.
    ok = await owner.renew()
    assert ok is False
    assert owner.owns_local() is False


# ─── 7. Expired lease can be reclaimed ───────────────────────────────
@pytest.mark.asyncio
async def test_07_expired_lease_can_be_reclaimed(test_db, monkeypatch):
    set_production_env(monkeypatch)
    coll = f"lease_t07_{uuid.uuid4().hex[:6]}"
    first, second = make_cluster(test_db, 2, collection_name=coll)
    await first.ensure_index()
    assert await first.acquire() is True

    # Expire the row.
    now = datetime.utcnow()
    await test_db[coll].update_one(
        {"lease_name": GLOBAL_LEASE_NAME},
        {"$set": {"expires_at": now - timedelta(seconds=10)}},
    )
    # The second instance should be able to take over.
    assert await second.acquire() is True
    assert await second.owns() is True
    assert await first.owns() is False


# ─── 8. Preview cannot acquire lease ─────────────────────────────────
@pytest.mark.asyncio
async def test_08_preview_cannot_acquire(test_db, monkeypatch):
    set_preview_env(monkeypatch)
    L = make_instance(test_db)
    await L.ensure_index()
    assert await L.acquire() is False
    assert L.owns_local() is False
    # Row must not exist.
    rows = await test_db[L._coll.name].count_documents({})
    assert rows == 0


# ─── 9. CANONICAL_WRITE_ENABLED=false cannot acquire ─────────────────
@pytest.mark.asyncio
async def test_09_canonical_write_disabled_cannot_acquire(test_db, monkeypatch):
    monkeypatch.setenv("DATA_AUTHORITY", "production")
    monkeypatch.setenv("CANONICAL_WRITE_ENABLED", "false")
    monkeypatch.setenv("BACKGROUND_WORKERS_ENABLED", "true")
    L = make_instance(test_db)
    await L.ensure_index()
    assert is_eligible_for_canonical_lease() is False
    assert await L.acquire() is False


# ─── 10. BACKGROUND_WORKERS_ENABLED=false cannot acquire ─────────────
@pytest.mark.asyncio
async def test_10_background_workers_disabled_cannot_acquire(test_db, monkeypatch):
    monkeypatch.setenv("DATA_AUTHORITY", "production")
    monkeypatch.setenv("CANONICAL_WRITE_ENABLED", "true")
    monkeypatch.setenv("BACKGROUND_WORKERS_ENABLED", "false")
    L = make_instance(test_db)
    await L.ensure_index()
    assert is_eligible_for_canonical_lease() is False
    assert await L.acquire() is False


# ─── 11. DATA_AUTHORITY != production cannot acquire ─────────────────
@pytest.mark.asyncio
async def test_11_non_production_authority_cannot_acquire(test_db, monkeypatch):
    monkeypatch.setenv("DATA_AUTHORITY", "staging")
    monkeypatch.setenv("CANONICAL_WRITE_ENABLED", "true")
    monkeypatch.setenv("BACKGROUND_WORKERS_ENABLED", "true")
    L = make_instance(test_db)
    await L.ensure_index()
    assert is_eligible_for_canonical_lease() is False
    assert await L.acquire() is False


# ─── 12. Graceful release allows another instance to acquire ─────────
@pytest.mark.asyncio
async def test_12_graceful_release_allows_takeover(test_db, monkeypatch):
    set_production_env(monkeypatch)
    coll = f"lease_t12_{uuid.uuid4().hex[:6]}"
    first, second = make_cluster(test_db, 2, collection_name=coll)
    await first.ensure_index()
    assert await first.acquire() is True
    assert await second.acquire() is False

    await first.release()
    assert first.owns_local() is False

    assert await second.acquire() is True
    assert await second.owns() is True


# ─── 13. Crash + TTL expiry → failover without restart ───────────────
@pytest.mark.asyncio
async def test_13_crash_and_ttl_expiry_failover(test_db, monkeypatch):
    set_production_env(monkeypatch)
    coll = f"lease_t13_{uuid.uuid4().hex[:6]}"
    # Short TTL so the test runs fast; heartbeat left disabled so the
    # "dead" instance never renews, simulating a crash.
    first = CanonicalWorkerLease(
        test_db,
        lease_name=GLOBAL_LEASE_NAME,
        instance_id="crashed-inst",
        ttl_s=2,
        heartbeat_s=1,
        collection_name=coll,
    )
    second = CanonicalWorkerLease(
        test_db,
        lease_name=GLOBAL_LEASE_NAME,
        instance_id="rescue-inst",
        ttl_s=2,
        heartbeat_s=1,
        collection_name=coll,
    )
    await first.ensure_index()
    assert await first.acquire() is True
    # "Crash" — do NOT renew.  Just wait for TTL expiry.
    await asyncio.sleep(2.4)
    assert await second.acquire() is True
    assert await second.owns() is True


# ─── 14. Preview still runs zero background workers ──────────────────
@pytest.mark.asyncio
async def test_14_preview_background_worker_suppression_preserved(monkeypatch):
    """Deterministic verification of the Preview suppression rule.

    The lease is only one half of the gate — the data_authority
    module's :func:`require_background_workers` must still return
    False in Preview so the suppression log fires exactly as before.
    """
    set_preview_env(monkeypatch)
    from services.data_authority import (
        require_background_workers,
        background_workers_enabled,
    )
    assert background_workers_enabled() is False
    assert require_background_workers() is False


# ─── 15. API reads keep working on a non-owner instance ──────────────
@pytest.mark.asyncio
async def test_15_api_reads_available_on_non_owner(test_db, monkeypatch):
    """Non-owner instances must still serve read traffic.  The lease
    only gates mutation — reads must never be blocked.  We simulate a
    read by hitting the Mongo client the "non-owner" lease holds and
    confirming it works, and that the lease API itself does not raise
    for the loser.
    """
    set_production_env(monkeypatch)
    coll = f"lease_t15_{uuid.uuid4().hex[:6]}"
    owner, non_owner = make_cluster(test_db, 2, collection_name=coll)
    await owner.ensure_index()
    assert await owner.acquire() is True
    assert await non_owner.acquire() is False

    # Simulate an API read via the shared db handle.
    _ = await test_db.list_collection_names()
    # Non-owner's status snapshot must succeed and report ownership=False.
    snap = await non_owner.safe_snapshot()
    assert snap["this_instance_owns_lease"] is False
    assert snap["worker_lease_owner"] == owner.instance_id
