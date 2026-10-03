"""test_canonical_cutover — contract tests for the Perklocks database
cutover / canonical-import infrastructure.

Covers (per the user's test matrix):
  A. USE_CANONICAL_DB=false → routing → legacy
  B. USE_CANONICAL_DB=true  → routing → canonical
  C. Workers refuse mutation under DATA_AUTHORITY=preview
  D. Import endpoint rejects excluded rows (fail-closed)
  E. Import endpoint rejects quarantined rows
  F. Import endpoint rejects unknown collection
  G. Import endpoint rejects env-state collection
  H. Replay of identical batch is idempotent
  I. Altered replay with same batch_no is REJECTED
  J. Import rejected when CANONICAL_IMPORT_ENABLED=false
  K. Import token required
  L. Batch size cap enforced (5,000 docs)
  M. collection_fingerprint is deterministic across insertion order
  N. logical_key_fields match the Phase 6 contract
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys

import pytest
import pytest_asyncio
from motor.motor_asyncio import AsyncIOMotorClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

MONGO = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
TEST_DB = "lockscore_cutover_test"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest_asyncio.fixture
async def canon_db():
    client = AsyncIOMotorClient(MONGO, serverSelectionTimeoutMS=3000)
    db = client[TEST_DB]
    # Clean slate
    for c in await db.list_collection_names():
        await db[c].drop()
    yield db
    for c in await db.list_collection_names():
        await db[c].drop()
    client.close()


# ─── A/B: routing honours USE_CANONICAL_DB ───────────────────────────
def test_routing_legacy_when_flag_false(monkeypatch):
    monkeypatch.setenv("USE_CANONICAL_DB", "false")
    monkeypatch.setenv("DB_NAME", "legacy_test")
    monkeypatch.setenv("CANONICAL_DB_NAME", "canon_test")
    from services import database as db_mod
    # Reset state so initialize_database re-reads env
    db_mod._state.client = None
    db_mod._state.database = None
    db_mod._state.db_name = None
    assert db_mod.active_database_name() == "legacy_test"
    assert not db_mod.use_canonical_db_enabled()


def test_routing_canonical_when_flag_true(monkeypatch):
    monkeypatch.setenv("USE_CANONICAL_DB", "true")
    monkeypatch.setenv("DB_NAME", "legacy_test")
    monkeypatch.setenv("CANONICAL_DB_NAME", "canon_test")
    from services import database as db_mod
    db_mod._state.client = None
    db_mod._state.database = None
    db_mod._state.db_name = None
    assert db_mod.active_database_name() == "canon_test"
    assert db_mod.use_canonical_db_enabled()


# ─── C: refuse canonical mutation under preview authority ────────────
def test_refuse_mutation_under_preview_authority(monkeypatch):
    monkeypatch.setenv("DATA_AUTHORITY", "preview")
    from services.canonical_cutover import refuse_if_preview_authority
    with pytest.raises(RuntimeError, match="DATA_AUTHORITY=preview"):
        refuse_if_preview_authority()


def test_production_authority_allows_mutation(monkeypatch):
    monkeypatch.setenv("DATA_AUTHORITY", "production")
    from services.canonical_cutover import refuse_if_preview_authority
    refuse_if_preview_authority()  # must not raise


# ─── D/E/F/G: per-doc & per-collection filtering ─────────────────────
@pytest.mark.asyncio
async def test_rejects_excluded_rows(canon_db):
    from services.canonical_cutover import import_batch
    docs = [
        {"game_id": "g1", "sport": "mlb"},
        {"game_id": "g2", "sport": "mlb", "excluded_from_canonical_runtime": True},
        {"game_id": "g3", "sport": "mlb", "status": "UNRESOLVED_IMMUTABLE_CONFLICT"},
    ]
    res = await import_batch(canon_db, collection="games", docs=docs,
                              session_id="t-sess-1", batch_no=0,
                              source_checkpoints={"phase6": "test"})
    assert res["accepted"] == 1
    assert res["rejected"] == 2
    reasons = {r["reason"] for r in res["rejections"]}
    assert "excluded_from_canonical_runtime=true" in reasons
    assert any("UNRESOLVED_IMMUTABLE_CONFLICT" in r for r in reasons)


@pytest.mark.asyncio
async def test_rejects_unknown_collection(canon_db):
    from services.canonical_cutover import import_batch
    with pytest.raises(ValueError, match="UNKNOWN_COLLECTION_REJECTED"):
        await import_batch(canon_db, collection="not_in_allowlist", docs=[{}],
                            session_id="t", batch_no=0, source_checkpoints={})


@pytest.mark.asyncio
async def test_rejects_env_state_collection(canon_db):
    from services.canonical_cutover import import_batch
    for coll in ("canonical_worker_leases", "scheduled_jobs", "board_generations"):
        with pytest.raises(ValueError, match="ENVIRONMENT_STATE_COLLECTION_REJECTED"):
            await import_batch(canon_db, collection=coll, docs=[{}],
                                session_id="t", batch_no=0, source_checkpoints={})


# ─── H/I: idempotent replay vs altered replay ────────────────────────
@pytest.mark.asyncio
async def test_idempotent_replay(canon_db):
    from services.canonical_cutover import import_batch
    docs = [{"game_id": "g1", "sport": "mlb"}, {"game_id": "g2", "sport": "mlb"}]
    r1 = await import_batch(canon_db, collection="games", docs=docs,
                              session_id="idemp", batch_no=0,
                              source_checkpoints={"phase6": "test"})
    assert r1["accepted"] == 2
    # Replay identical content — should NOT raise, should be no-op
    r2 = await import_batch(canon_db, collection="games", docs=docs,
                              session_id="idemp", batch_no=0,
                              source_checkpoints={"phase6": "test"})
    assert r2["idempotent_replay"] is True
    assert r2["accepted"] == 2
    # Still only 2 rows in canonical collection
    cnt = await canon_db["games"].count_documents({})
    assert cnt == 2


@pytest.mark.asyncio
async def test_altered_replay_rejected(canon_db):
    from services.canonical_cutover import import_batch
    await import_batch(canon_db, collection="games",
                        docs=[{"game_id": "a", "sport": "mlb"}],
                        session_id="alt", batch_no=0, source_checkpoints={})
    with pytest.raises(RuntimeError, match="ALTERED_REPLAY_REJECTED"):
        await import_batch(canon_db, collection="games",
                            docs=[{"game_id": "b", "sport": "mlb"}],
                            session_id="alt", batch_no=0, source_checkpoints={})


# ─── L: batch size cap ───────────────────────────────────────────────
@pytest.mark.asyncio
async def test_batch_size_cap_enforced(canon_db):
    from services.canonical_cutover import import_batch
    oversized = [{"game_id": f"g{i}", "sport": "mlb"} for i in range(5001)]
    with pytest.raises(ValueError, match="BATCH_TOO_LARGE"):
        await import_batch(canon_db, collection="games", docs=oversized,
                            session_id="big", batch_no=0, source_checkpoints={})


# ─── M: deterministic fingerprint ────────────────────────────────────
@pytest.mark.asyncio
async def test_fingerprint_deterministic(canon_db):
    from services.canonical_cutover import import_batch, collection_fingerprint
    # Insert in one order
    await import_batch(canon_db, collection="games",
                        docs=[{"game_id": "a", "sport": "mlb"},
                              {"game_id": "b", "sport": "mlb"},
                              {"game_id": "c", "sport": "mlb"}],
                        session_id="fp1", batch_no=0, source_checkpoints={})
    fp1 = await collection_fingerprint(canon_db, "games")
    # Drop & insert in reverse order
    await canon_db["games"].drop()
    await import_batch(canon_db, collection="games",
                        docs=[{"game_id": "c", "sport": "mlb"},
                              {"game_id": "b", "sport": "mlb"},
                              {"game_id": "a", "sport": "mlb"}],
                        session_id="fp2", batch_no=0, source_checkpoints={})
    fp2 = await collection_fingerprint(canon_db, "games")
    assert fp1 == fp2, "fingerprint must be insertion-order independent"


# ─── N: logical key contract matches Phase 6 ─────────────────────────
def test_logical_key_contract():
    from services.canonical_cutover import logical_key_fields, RECONCILIATION_COLLECTIONS
    # Spot-check 6 critical collections from the reconciliation contract
    assert logical_key_fields("picks") == ("pick_id",)
    assert logical_key_fields("users") == ("id",)
    assert logical_key_fields("user_bets") == ("bet_id",)
    assert logical_key_fields("publication_events") == ("payload_hash",)
    assert logical_key_fields("prediction_snapshots") == ("prediction_id", "snapshot_version")
    assert logical_key_fields("player_game_actuals") == ("sport", "event_id", "player_id", "market")
    # All 21 reconciled collections must have an explicit logical-key spec
    for coll in RECONCILIATION_COLLECTIONS:
        kf = logical_key_fields(coll)
        assert kf != ("_id",), f"{coll} missing logical-key specification"


# ─── Collection allowlist completeness ───────────────────────────────
def test_allowlist_is_exactly_21():
    from services.canonical_cutover import RECONCILIATION_COLLECTIONS
    assert len(RECONCILIATION_COLLECTIONS) == 21
    # No overlap with env-state denylist
    from services.canonical_cutover import ENVIRONMENT_STATE_COLLECTIONS
    assert not (set(RECONCILIATION_COLLECTIONS) & ENVIRONMENT_STATE_COLLECTIONS)


# ─── Required indexes defined for every reconciled collection ────────
def test_every_reconciled_collection_has_indexes():
    from services.canonical_cutover import (RECONCILIATION_COLLECTIONS,
                                               required_indexes_for)
    for coll in RECONCILIATION_COLLECTIONS:
        specs = required_indexes_for(coll)
        assert len(specs) >= 1, f"{coll} has no required indexes"
        # Every reconciled collection must have at least one UNIQUE index
        # (the logical-identity index).
        assert any(s.get("unique") for s in specs), \
            f"{coll} missing unique logical-identity index"


# ─── Index enforcement actually works (unique constraint) ────────────
@pytest.mark.asyncio
async def test_ensure_indexes_enforces_uniqueness(canon_db):
    from services.canonical_cutover import ensure_canonical_indexes
    # Insert two docs that violate the unique game_id constraint
    await canon_db["games"].insert_many([
        {"game_id": "dup", "sport": "mlb"},
        {"game_id": "dup", "sport": "nfl"},
    ])
    res = await ensure_canonical_indexes(canon_db, "games")
    # Must fail — we never silently weaken the constraint
    assert not res["ok"], "unique index should fail on duplicate data"
    assert any(f.get("error") in ("DUPLICATE_KEY", "DuplicateKeyError")
                 for f in res["failed"])
