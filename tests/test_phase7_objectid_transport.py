"""R3 Phase-7 ObjectId transport serialisation tests.

Scope: proves the Phase-7 Support-confirmed fix in
``backend/services/canonical_dedupe.py``:

    * Census ``_ids`` emerging from ``scan_duplicates`` are
      JSON-serialisable (ObjectId → 24-char hex string).
    * The APPLY path (``apply_dedupe_group``) accepts those stringified
      ObjectId ids and re-hydrates them to native ``bson.ObjectId``
      before any Mongo find/delete op, so the stored documents are
      actually located and reconciled.
    * The APPLY path remains fully functional for collections whose
      ``_id`` values are already strings (UUIDs) — ids are preserved
      byte-identical.
    * APPLY still fails closed on wrong / non-existent ids.
    * No change to survivor-fingerprint authority, dedupe rules,
      migration, session, indexes, Phase-8, scoring, or any
      application logic.
"""
from __future__ import annotations

import json
import os
import sys
import uuid

import pytest
import pytest_asyncio
pytestmark = pytest.mark.asyncio

sys.path.insert(0, "/app/backend")
os.environ.setdefault("MONGO_URL",  "mongodb://localhost:27017/test")
os.environ.setdefault("DB_NAME",    "perklocks_dedupe_tests")
os.environ.setdefault("JWT_SECRET",
    "test-only-not-a-secret-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")

from bson import ObjectId                                             # noqa: E402
from motor.motor_asyncio import AsyncIOMotorClient                    # noqa: E402

from services.canonical_dedupe import (                               # noqa: E402
    DEDUPE_AUDIT_COLLECTION,
    EXACT_DUPLICATE,
    canonical_doc_fingerprint,
    scan_duplicates,
    apply_dedupe_group,
    REASON_MATCH_PINNED_SOURCE,
    _jsonable_id,
    _normalize_mongo_id,
    _is_valid_objectid_str,
)


@pytest_asyncio.fixture
async def db():
    """Fresh test DB per test — dropped at teardown."""
    client = AsyncIOMotorClient("mongodb://localhost:27017")
    name = f"perklocks_dedupe_obj_tests_{uuid.uuid4().hex[:12]}"
    try:
        yield client[name]
    finally:
        await client.drop_database(name)
        client.close()


# ──────────────────────────────────────────────────────────────────────
# Pure helpers — no DB
# ──────────────────────────────────────────────────────────────────────

def test_is_valid_objectid_str_discriminates_correctly():
    # 24-char hex ObjectId → True.
    assert _is_valid_objectid_str(str(ObjectId())) is True
    # UUID (36 chars) → False.
    assert _is_valid_objectid_str(str(uuid.uuid4())) is False
    # Plain string → False.
    assert _is_valid_objectid_str("not-an-object-id") is False
    # Non-string → False.
    assert _is_valid_objectid_str(12345) is False
    assert _is_valid_objectid_str(None) is False
    # 24-char but non-hex → False.
    assert _is_valid_objectid_str("z" * 24) is False


def test_jsonable_id_roundtrip():
    oid = ObjectId()
    hex_id = str(oid)
    # ObjectId → hex string.
    assert _jsonable_id(oid) == hex_id
    # String stays string.
    assert _jsonable_id(hex_id) == hex_id
    # Non-ObjectId types preserved.
    uid = str(uuid.uuid4())
    assert _jsonable_id(uid) == uid
    assert _jsonable_id(42) == 42
    assert _jsonable_id(None) is None


def test_normalize_mongo_id_roundtrip():
    oid = ObjectId()
    hex_id = str(oid)
    # 24-hex string → ObjectId with equal value.
    normalized = _normalize_mongo_id(hex_id)
    assert isinstance(normalized, ObjectId)
    assert normalized == oid
    # UUID preserved as-is.
    uid = str(uuid.uuid4())
    assert _normalize_mongo_id(uid) == uid
    # Already-ObjectId preserved.
    assert _normalize_mongo_id(oid) == oid
    # Non-string preserved.
    assert _normalize_mongo_id(42) == 42


# ──────────────────────────────────────────────────────────────────────
# Case A: Census group with ObjectId _ids returns JSON-safe response
# ──────────────────────────────────────────────────────────────────────
async def test_census_with_objectid_ids_is_json_safe(db):
    coll = "settlement_events"
    oid1, oid2, oid3 = ObjectId(), ObjectId(), ObjectId()

    # Two docs share the same logical key (settlement_id) → EXACT group.
    await db[coll].insert_many([
        {"_id": oid1, "settlement_id": "SETL-1", "amount": 100},
        {"_id": oid2, "settlement_id": "SETL-1", "amount": 100},
        {"_id": oid3, "settlement_id": "SETL-2", "amount": 50},  # not dup
    ])

    summary = await scan_duplicates(db, coll, offset=0, limit=100,
                                    max_docs_per_group=50)
    assert summary["duplicate_group_count"] == 1
    assert len(summary["groups"]) == 1
    g = summary["groups"][0]

    # Case B: every _id in the response is a string, not an ObjectId.
    for i in g["_ids"]:
        assert isinstance(i, str), f"census _id leaked raw type: {type(i)}"
    assert set(g["_ids"]) == {str(oid1), str(oid2)}

    # Case A: the full summary round-trips through JSON without error.
    serialised = json.dumps(summary)
    assert "SETL-1" in serialised
    # And none of the raw hex ids got mangled.
    assert str(oid1) in serialised
    assert str(oid2) in serialised


# ──────────────────────────────────────────────────────────────────────
# Case C: APPLY accepts the stringified ObjectId ids and finds the docs
# ──────────────────────────────────────────────────────────────────────
async def test_apply_accepts_stringified_objectid_ids(db):
    coll = "settlement_events"
    oid1, oid2 = ObjectId(), ObjectId()
    doc1 = {"_id": oid1, "settlement_id": "SETL-7", "amount": 100}
    doc2 = {"_id": oid2, "settlement_id": "SETL-7", "amount": 100}  # EXACT dup
    await db[coll].insert_many([doc1, doc2])

    # Pinned-source fingerprint = business-content fingerprint of the
    # (identical) docs.
    fp = canonical_doc_fingerprint(doc1)
    assert fp == canonical_doc_fingerprint(doc2)

    # Simulate the full round-trip the workflow uses: census → JSON → APPLY.
    summary = await scan_duplicates(db, coll, offset=0, limit=100,
                                    max_docs_per_group=50)
    group = summary["groups"][0]
    assert all(isinstance(i, str) for i in group["_ids"])

    result = await apply_dedupe_group(
        db,
        collection=coll,
        logical_key_values={"settlement_id": "SETL-7"},
        classification=EXACT_DUPLICATE,
        all_ids=group["_ids"],                       # ← stringified ids
        authoritative_fingerprint=fp,
        workflow_run_id="wf-objectid-test",
        session_id="sess-objectid-test",
    )

    # One doc was deleted and one survived.
    assert result["deleted_count"] == 1
    # Survivor-reason authority rules are unchanged by the serialisation
    # fix. EXACT with two pinned-source matches → earliest/lowest-id
    # tiebreak (REASON_LOWEST_ID here since no hint provided).
    assert result["survivor_reason"] in {
        REASON_MATCH_PINNED_SOURCE,
        "lowest_id_fallback",
    }
    assert isinstance(result["survivor_id"],   str)
    assert isinstance(result["deleted_ids"][0], str)
    # The survivor id corresponds to one of the two original ObjectIds.
    assert result["survivor_id"] in {str(oid1), str(oid2)}

    # DB post-state: exactly one remaining doc for the logical key.
    remaining = await db[coll].count_documents({"settlement_id": "SETL-7"})
    assert remaining == 1

    # Audit record is JSON-safe (no ObjectId values).
    audit = await db[DEDUPE_AUDIT_COLLECTION].find_one(
        {"collection": coll, "logical_key_values.settlement_id": "SETL-7"})
    assert audit is not None
    for i in audit["all_ids"]:
        assert isinstance(i, str)
    assert isinstance(audit["survivor_id"], str)
    for i in audit["deleted_ids"]:
        assert isinstance(i, str)


# ──────────────────────────────────────────────────────────────────────
# Case D: APPLY still works for collections with string-_id docs
# ──────────────────────────────────────────────────────────────────────
async def test_apply_works_with_string_ids(db):
    coll = "picks"
    sid1, sid2 = f"pick-{uuid.uuid4()}", f"pick-{uuid.uuid4()}"
    doc1 = {"_id": sid1, "id": "PICK-SHARED", "amount": 7}
    doc2 = {"_id": sid2, "id": "PICK-SHARED", "amount": 7}  # EXACT dup
    await db[coll].insert_many([doc1, doc2])

    fp = canonical_doc_fingerprint(doc1)
    summary = await scan_duplicates(db, coll, offset=0, limit=100,
                                    max_docs_per_group=50)
    group = summary["groups"][0]
    # UUIDs preserved byte-identical through serialisation.
    assert set(group["_ids"]) == {sid1, sid2}
    for i in group["_ids"]:
        assert isinstance(i, str)
        assert len(i) != 24  # explicitly NOT ObjectId-shaped

    result = await apply_dedupe_group(
        db,
        collection=coll,
        logical_key_values={"id": "PICK-SHARED"},
        classification=EXACT_DUPLICATE,
        all_ids=group["_ids"],
        authoritative_fingerprint=fp,
        workflow_run_id="wf-string-test",
        session_id="sess-string-test",
    )
    assert result["deleted_count"] == 1
    assert result["survivor_id"] in {sid1, sid2}
    # String ids preserved unchanged — not normalised to ObjectId.
    assert isinstance(result["survivor_id"], str)
    assert len(result["survivor_id"]) == len(sid1)

    remaining = await db[coll].count_documents({"id": "PICK-SHARED"})
    assert remaining == 1


# ──────────────────────────────────────────────────────────────────────
# Case E: APPLY still fails closed on wrong / non-existent ids
# ──────────────────────────────────────────────────────────────────────
async def test_apply_fails_closed_on_nonexistent_ids(db):
    coll = "settlement_events"
    real = ObjectId()
    await db[coll].insert_one(
        {"_id": real, "settlement_id": "SETL-X", "amount": 1})

    # Pass one real id (stringified) and one fabricated hex id.
    fake = str(ObjectId())
    fp   = canonical_doc_fingerprint({"settlement_id": "SETL-X", "amount": 1})

    with pytest.raises(ValueError, match="GROUP_IDS_MISSING_FROM_DB"):
        await apply_dedupe_group(
            db,
            collection=coll,
            logical_key_values={"settlement_id": "SETL-X"},
            classification=EXACT_DUPLICATE,
            all_ids=[str(real), fake],              # ← fake id triggers guard
            authoritative_fingerprint=fp,
            workflow_run_id="wf-fail-closed",
            session_id="sess-fail-closed",
        )

    # DB untouched.
    assert await db[coll].count_documents({}) == 1
    # No audit written for a failed guard.
    assert await db[DEDUPE_AUDIT_COLLECTION].count_documents({}) == 0


# ──────────────────────────────────────────────────────────────────────
# Case F — regression spot-check: the dedupe rule set is unchanged.
# This is a focused assertion that fingerprint/classification/
# survivor-authority logic was not inadvertently touched by the
# serialisation fix. CONFLICTING without pinned-source match MUST
# still fail closed with NO_AUTHORITATIVE_WINNER.
# ──────────────────────────────────────────────────────────────────────
async def test_conflicting_without_source_match_still_fails_closed(db):
    coll = "settlement_events"
    oid1, oid2 = ObjectId(), ObjectId()
    await db[coll].insert_many([
        {"_id": oid1, "settlement_id": "SETL-C", "amount": 100},
        {"_id": oid2, "settlement_id": "SETL-C", "amount": 999},   # conflict
    ])
    # Pinned source has a THIRD distinct fingerprint — no winner.
    unrelated_fp = canonical_doc_fingerprint(
        {"settlement_id": "SETL-C", "amount": 7777})

    with pytest.raises(ValueError, match="NO_AUTHORITATIVE_WINNER"):
        await apply_dedupe_group(
            db,
            collection=coll,
            logical_key_values={"settlement_id": "SETL-C"},
            classification="CONFLICTING_DUPLICATE",
            all_ids=[str(oid1), str(oid2)],
            authoritative_fingerprint=unrelated_fp,
            workflow_run_id="wf-rule-regression",
            session_id="sess-rule-regression",
        )
    # DB untouched.
    assert await db[coll].count_documents({"settlement_id": "SETL-C"}) == 2
    assert await db[DEDUPE_AUDIT_COLLECTION].count_documents({}) == 0
