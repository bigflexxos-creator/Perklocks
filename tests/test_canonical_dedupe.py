"""test_canonical_dedupe — R3 Phase-7 dedupe engine unit tests.

Uses the local MongoDB (same instance that serves preview) with a
throwaway test DB per test — no production state is touched.  All
destructive assertions are observed in the test DB only.

Covers:
  * canonical_doc_fingerprint excludes _id + bookkeeping fields.
  * classify_group — EXACT vs CONFLICTING by hash equality only.
  * scan_duplicates — correctly groups, classifies, bounds samples.
  * apply_dedupe_group — pinned-source authoritative survivor, EXACT
    keep-authoritative, CONFLICTING fail-closed when no match,
    pre- and post-delete guards, writes audit record, idempotent
    rerun.
  * APPLY scope enforcement — refuses out-of-scope collections.
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid

import pytest
import pytest_asyncio
pytestmark = pytest.mark.asyncio

sys.path.insert(0, "/app/backend")
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017/test")
os.environ.setdefault("DB_NAME", "perklocks_dedupe_tests")
os.environ.setdefault("JWT_SECRET",
    "test-only-not-a-secret-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")

from motor.motor_asyncio import AsyncIOMotorClient                   # noqa: E402

from services.canonical_dedupe import (                              # noqa: E402
    APPLY_DEDUPE_SCOPE,
    DEDUPE_AUDIT_COLLECTION,
    EXACT_DUPLICATE, CONFLICTING_DUPLICATE,
    canonical_doc_fingerprint,
    classify_group,
    scan_duplicates,
    apply_dedupe_group,
    REASON_MATCH_PINNED_SOURCE,
    REASON_EARLIEST_CANONICAL,
    REASON_LOWEST_ID,
)


@pytest_asyncio.fixture
async def db():
    """Fresh test DB per test — dropped at teardown."""
    client = AsyncIOMotorClient("mongodb://localhost:27017")
    name = f"perklocks_dedupe_tests_{uuid.uuid4().hex[:12]}"
    try:
        yield client[name]
    finally:
        await client.drop_database(name)
        client.close()


# ─── canonical_doc_fingerprint ─────────────────────────────────────
def test_fingerprint_excludes_mongo_id():
    a = {"_id": "abc", "settlement_id": "s1", "amount": 10}
    b = {"_id": "xyz", "settlement_id": "s1", "amount": 10}
    assert canonical_doc_fingerprint(a) == canonical_doc_fingerprint(b)


def test_fingerprint_excludes_underscore_prefixed_bookkeeping():
    a = {"_id": "abc", "settlement_id": "s1",
         "_canonical_import_meta": {"session": "r3"}}
    b = {"_id": "xyz", "settlement_id": "s1",
         "_canonical_import_meta": {"session": "r2"}}
    c = {"_id": "qqq", "settlement_id": "s1",
         "_resolution_source": "preview"}
    fp = canonical_doc_fingerprint(a)
    assert canonical_doc_fingerprint(b) == fp
    assert canonical_doc_fingerprint(c) == fp


def test_fingerprint_detects_business_field_diff():
    a = {"_id": "abc", "settlement_id": "s1", "amount": 10}
    b = {"_id": "xyz", "settlement_id": "s1", "amount": 11}  # business diff
    assert canonical_doc_fingerprint(a) != canonical_doc_fingerprint(b)


def test_fingerprint_is_deterministic_key_order_independent():
    a = {"settlement_id": "s1", "amount": 10, "sport": "nfl"}
    b = {"amount": 10, "sport": "nfl", "settlement_id": "s1"}
    assert canonical_doc_fingerprint(a) == canonical_doc_fingerprint(b)


def test_fingerprint_handles_datetime_and_objectid_scalars():
    from datetime import datetime, timezone
    from bson import ObjectId
    a = {"settlement_id": "s1", "at": datetime(2026, 1, 1, tzinfo=timezone.utc),
         "ref": ObjectId("5f5e5d5c5b5a595857565554")}
    b = dict(a)
    assert canonical_doc_fingerprint(a) == canonical_doc_fingerprint(b)


# ─── classify_group ────────────────────────────────────────────────
def test_classify_group_exact_when_all_same_fingerprint():
    assert classify_group(["a", "a", "a"]) == EXACT_DUPLICATE


def test_classify_group_conflicting_when_fingerprints_differ():
    assert classify_group(["a", "b"]) == CONFLICTING_DUPLICATE


# ─── scan_duplicates (paginated, read-only) ────────────────────────
async def test_scan_duplicates_finds_exact_and_conflicting_groups(db):
    coll = "settlement_events"
    # Group 1: 3-way EXACT (same business content, different _id)
    for i in range(3):
        await db[coll].insert_one({
            "_id": f"a{i}", "settlement_id": "s_exact",
            "sport": "nfl", "amount": 10,
        })
    # Group 2: 2-way CONFLICTING (same key, different amount)
    await db[coll].insert_one({
        "_id": "b0", "settlement_id": "s_conflict", "sport": "nfl", "amount": 20})
    await db[coll].insert_one({
        "_id": "b1", "settlement_id": "s_conflict", "sport": "nfl", "amount": 21})
    # Non-duplicate (unique logical key)
    await db[coll].insert_one({
        "_id": "c0", "settlement_id": "s_unique", "sport": "nfl", "amount": 30})

    report = await scan_duplicates(db, coll)
    assert report["total_docs"]              == 6
    assert report["logical_key_count"]       == 3
    assert report["duplicate_group_count"]   == 2
    assert report["page_exact_count"]        == 1
    assert report["page_conflicting_count"]  == 1
    assert report["page_group_count"]        == 2
    assert report["has_more"]                is False
    assert report["next_offset"]             is None

    by_key = {g["logical_key"]["settlement_id"]: g for g in report["groups"]}
    exact = by_key["s_exact"]
    conflict = by_key["s_conflict"]
    assert exact["classification"]              == EXACT_DUPLICATE
    assert exact["dup_count"]                   == 3
    assert exact["distinct_fingerprint_count"]  == 1
    assert len(exact["_ids"])                   == 3
    assert conflict["classification"]           == CONFLICTING_DUPLICATE
    assert conflict["dup_count"]                == 2
    assert conflict["distinct_fingerprint_count"] == 2


async def test_scan_duplicates_zero_duplicates_fast_path(db):
    coll = "settlement_events"
    for i in range(5):
        await db[coll].insert_one({"_id": f"u{i}", "settlement_id": f"k{i}"})
    r = await scan_duplicates(db, coll, offset=0, limit=100)
    assert r["total_docs"]            == 5
    assert r["logical_key_count"]     == 5
    assert r["duplicate_group_count"] == 0
    assert r["page_group_count"]      == 0
    assert r["has_more"]              is False
    assert r["next_offset"]           is None
    assert r["groups"]                == []


async def test_scan_duplicates_pagination_covers_all_groups(db):
    """Create 450 duplicate groups × 2 docs each.  Page through at
    limit=100 and assert the union of pages equals the full set."""
    coll = "settlement_events"
    for g in range(450):
        for d in range(2):
            await db[coll].insert_one({
                "_id":           f"g{g:04d}_d{d}",
                "settlement_id": f"key_{g:04d}",
                "amount":        g,
            })
    # Also add 100 non-duplicate docs
    for u in range(100):
        await db[coll].insert_one({
            "_id":           f"u{u:04d}",
            "settlement_id": f"unique_{u:04d}",
        })
    seen_keys = set()
    page_count = 0
    offset = 0
    while True:
        r = await scan_duplicates(db, coll, offset=offset, limit=100)
        assert r["duplicate_group_count"] == 450
        page_count += 1
        for g in r["groups"]:
            seen_keys.add(g["logical_key"]["settlement_id"])
        if not r["has_more"] or r["next_offset"] is None:
            break
        offset = r["next_offset"]
    assert len(seen_keys) == 450
    assert page_count     == 5    # 100+100+100+100+50
    # No overlap in paged keys (deterministic ordering)
    assert len(seen_keys) == 450


async def test_scan_duplicates_player_game_logs_style_large_many_groups(db):
    """Reproduce player_game_logs topology: thousands of 2-way
    CONFLICTING groups + non-duplicates.  Server must stay read-only
    and respond with a bounded page without blowing memory."""
    coll = "player_game_logs"
    for g in range(1200):
        await db[coll].insert_one({
            "_id":       f"log_a_{g:05d}", "sport": "nfl",
            "game_id":   f"game_{g:05d}",   "player_id": f"p_{g:05d}",
            "stat":      "pass_yds", "value": 100 + g,
        })
        await db[coll].insert_one({
            "_id":       f"log_b_{g:05d}", "sport": "nfl",
            "game_id":   f"game_{g:05d}",   "player_id": f"p_{g:05d}",
            "stat":      "pass_yds", "value": 999 + g,  # conflicting
        })
    for u in range(500):
        await db[coll].insert_one({
            "_id":       f"log_u_{u:05d}", "sport": "nfl",
            "game_id":   f"game_u_{u:05d}", "player_id": f"p_u_{u:05d}",
        })
    r = await scan_duplicates(db, coll, offset=0, limit=100)
    assert r["total_docs"]            == 1200 * 2 + 500
    assert r["duplicate_group_count"] == 1200
    assert r["page_group_count"]      == 100
    assert r["page_conflicting_count"] == 100
    assert r["page_exact_count"]      == 0
    assert r["has_more"]              is True
    assert r["next_offset"]           == 100
    # Each paged group has both docs with distinct fingerprints
    for g in r["groups"]:
        assert g["classification"]           == CONFLICTING_DUPLICATE
        assert g["dup_count"]                == 2
        assert len(g["_ids"])                == 2
        assert g["distinct_fingerprint_count"] == 2


async def test_scan_duplicates_massive_single_group_capped_at_sample(db):
    """A single logical key with 1 000 physical rows → one group of
    1 000.  The sampled fingerprints must be capped at
    ``max_docs_per_group`` and ``_ids_truncated`` must be True."""
    coll = "picks"
    for i in range(1000):
        await db[coll].insert_one({
            "_id":    f"pick_{i:05d}",
            "id":     "huge_key",
            "sport":  "nfl",
            "idx":    i,
        })
    r = await scan_duplicates(db, coll, offset=0, limit=10,
                               max_docs_per_group=25)
    assert r["duplicate_group_count"] == 1
    g = r["groups"][0]
    assert g["dup_count"]       == 1000
    assert len(g["_ids"])       == 25
    assert g["_ids_truncated"]  is True
    # Fingerprints differ (each doc has different `idx`) → CONFLICTING
    assert g["classification"] == CONFLICTING_DUPLICATE


async def test_scan_duplicates_offset_beyond_total_returns_empty(db):
    coll = "picks"
    for i in range(3):
        for d in range(2):
            await db[coll].insert_one({
                "_id": f"p{i}_{d}", "id": f"k{i}",
            })
    r = await scan_duplicates(db, coll, offset=10, limit=5)
    assert r["duplicate_group_count"] == 3
    assert r["page_group_count"]      == 0
    assert r["groups"]                == []
    assert r["has_more"]              is False
    assert r["next_offset"]           is None


async def test_scan_duplicates_is_read_only(db):
    """Call scan_duplicates multiple times with CRUD operations
    only inserting fixtures; assert no documents were deleted or
    modified by the scan itself."""
    coll = "settlement_events"
    for i in range(4):
        for d in range(2):
            await db[coll].insert_one({
                "_id": f"r{i}_{d}", "settlement_id": f"rk{i}", "val": i,
            })
    before_count = await db[coll].count_documents({})
    before_ids   = sorted([d["_id"] async for d in db[coll].find({})])
    for _ in range(3):
        await scan_duplicates(db, coll, offset=0, limit=100)
    after_count = await db[coll].count_documents({})
    after_ids   = sorted([d["_id"] async for d in db[coll].find({})])
    assert before_count == after_count
    assert before_ids   == after_ids
    # No audit records written by the scan (write happens only in APPLY).
    assert await db[DEDUPE_AUDIT_COLLECTION].count_documents({}) == 0


# ─── apply_dedupe_group — EXACT keeps authoritative survivor ──────
async def test_apply_dedupe_exact_keeps_authoritative_survivor(db):
    coll = "settlement_events"
    doc_tpl = {"settlement_id": "k1", "sport": "nfl", "amount": 10}
    await db[coll].insert_one({"_id": "d0", **doc_tpl})
    await db[coll].insert_one({"_id": "d1", **doc_tpl})
    await db[coll].insert_one({"_id": "d2", **doc_tpl})
    auth_fp = canonical_doc_fingerprint(doc_tpl)

    r = await apply_dedupe_group(
        db,
        collection=coll,
        logical_key_values={"settlement_id": "k1"},
        classification=EXACT_DUPLICATE,
        all_ids=["d0", "d1", "d2"],
        authoritative_fingerprint=auth_fp,
        workflow_run_id="wf-1",
        session_id="perklocks-cutover-20261003-r3",
    )
    assert r["deleted_count"] == 2
    # Survivor is the lowest _id (all equally authoritative)
    assert r["survivor_id"]        == "d0"
    assert r["survivor_reason"]    == REASON_LOWEST_ID
    assert r["pre_delete_guard"]   == "pass"
    assert r["post_delete_guard"]  == "pass"
    # Audit record was persisted
    audit_count = await db[DEDUPE_AUDIT_COLLECTION].count_documents({})
    assert audit_count == 1
    audit = await db[DEDUPE_AUDIT_COLLECTION].find_one({})
    assert audit["collection"]            == coll
    assert audit["survivor_id"]           == "d0"
    assert sorted(audit["deleted_ids"])   == ["d1", "d2"]
    assert audit["source_fingerprint"]    == auth_fp
    assert audit["workflow_run_id"]       == "wf-1"
    assert audit["session_id"]            == "perklocks-cutover-20261003-r3"
    # Only one doc remains for this logical key
    remaining = await db[coll].count_documents({"settlement_id": "k1"})
    assert remaining == 1


# ─── apply_dedupe_group — CONFLICTING with authoritative winner ───
async def test_apply_dedupe_conflicting_picks_authoritative_winner(db):
    coll = "settlement_events"
    good = {"settlement_id": "k2", "sport": "nfl", "amount": 20}  # authoritative
    bad  = {"settlement_id": "k2", "sport": "nfl", "amount": 99}  # divergent
    await db[coll].insert_one({"_id": "g0", **good})
    await db[coll].insert_one({"_id": "b0", **bad})
    auth_fp = canonical_doc_fingerprint(good)

    r = await apply_dedupe_group(
        db,
        collection=coll,
        logical_key_values={"settlement_id": "k2"},
        classification=CONFLICTING_DUPLICATE,
        all_ids=["g0", "b0"],
        authoritative_fingerprint=auth_fp,
        workflow_run_id="wf-2",
        session_id="r3",
    )
    assert r["survivor_id"]     == "g0"
    assert r["survivor_reason"] == REASON_MATCH_PINNED_SOURCE
    assert r["deleted_ids"]     == ["b0"]
    remaining = await db[coll].find_one({"settlement_id": "k2"})
    assert remaining["amount"]  == 20   # survivor is the authoritative one


# ─── apply_dedupe_group — CONFLICTING fails closed, no match ──────
async def test_apply_dedupe_conflicting_fails_closed_when_no_source_match(db):
    coll = "settlement_events"
    a = {"settlement_id": "k3", "amount": 1}
    b = {"settlement_id": "k3", "amount": 2}
    await db[coll].insert_one({"_id": "a0", **a})
    await db[coll].insert_one({"_id": "b0", **b})
    fake_source_fp = "deadbeef" * 8   # matches neither

    with pytest.raises(ValueError) as ei:
        await apply_dedupe_group(
            db,
            collection=coll,
            logical_key_values={"settlement_id": "k3"},
            classification=CONFLICTING_DUPLICATE,
            all_ids=["a0", "b0"],
            authoritative_fingerprint=fake_source_fp,
            workflow_run_id="wf-3", session_id="r3",
        )
    assert "NO_AUTHORITATIVE_WINNER" in str(ei.value)
    # Nothing was deleted
    assert await db[coll].count_documents({"settlement_id": "k3"}) == 2
    # No audit record on fail-closed
    assert await db[DEDUPE_AUDIT_COLLECTION].count_documents({}) == 0


# ─── apply_dedupe_group — EXACT but all diverge from source ───────
async def test_apply_dedupe_exact_group_diverging_from_source_fails_closed(db):
    coll = "settlement_events"
    doc = {"settlement_id": "k4", "amount": 5}
    await db[coll].insert_one({"_id": "e0", **doc})
    await db[coll].insert_one({"_id": "e1", **doc})
    fake_fp = "cafebabe" * 8   # matches neither

    with pytest.raises(ValueError) as ei:
        await apply_dedupe_group(
            db,
            collection=coll,
            logical_key_values={"settlement_id": "k4"},
            classification=EXACT_DUPLICATE,
            all_ids=["e0", "e1"],
            authoritative_fingerprint=fake_fp,
            workflow_run_id="wf-4", session_id="r3",
        )
    assert "EXACT_GROUP_DIVERGES_FROM_PINNED_SOURCE" in str(ei.value)
    assert await db[coll].count_documents({"settlement_id": "k4"}) == 2
    assert await db[DEDUPE_AUDIT_COLLECTION].count_documents({}) == 0


# ─── apply_dedupe_group — out-of-scope collection refused ─────────
async def test_apply_dedupe_refuses_out_of_scope_collection(db):
    with pytest.raises(ValueError) as ei:
        await apply_dedupe_group(
            db,
            collection="games",    # not in APPLY_DEDUPE_SCOPE
            logical_key_values={"sport": "nfl", "game_id": "g1"},
            classification=EXACT_DUPLICATE,
            all_ids=["x"],
            authoritative_fingerprint="x",
            workflow_run_id="w", session_id="r3",
        )
    assert "OUT_OF_SCOPE_COLLECTION" in str(ei.value)


# ─── apply_dedupe_group — classification claimed vs recomputed ────
async def test_apply_dedupe_rejects_classification_claim_mismatch(db):
    coll = "picks"
    await db[coll].insert_one({"_id": "p0", "id": "k5", "amount": 1})
    await db[coll].insert_one({"_id": "p1", "id": "k5", "amount": 2})  # conflict
    auth_fp = canonical_doc_fingerprint({"id": "k5", "amount": 1})
    with pytest.raises(ValueError) as ei:
        await apply_dedupe_group(
            db,
            collection=coll,
            logical_key_values={"id": "k5"},
            classification=EXACT_DUPLICATE,  # LIE — server recomputes CONFLICTING
            all_ids=["p0", "p1"],
            authoritative_fingerprint=auth_fp,
            workflow_run_id="w", session_id="r3",
        )
    assert "CLASSIFICATION_MISMATCH" in str(ei.value)


# ─── apply_dedupe_group — idempotent rerun on already-dedup'd group ─
async def test_apply_dedupe_idempotent_rerun_when_already_single(db):
    coll = "picks"
    await db[coll].insert_one({"_id": "s0", "id": "k6", "amount": 7})
    auth_fp = canonical_doc_fingerprint({"id": "k6", "amount": 7})
    # Pretend prior run already left one doc; call again with
    # all_ids=["s0"] — nothing to delete.
    r = await apply_dedupe_group(
        db,
        collection=coll,
        logical_key_values={"id": "k6"},
        classification=EXACT_DUPLICATE,
        all_ids=["s0"],
        authoritative_fingerprint=auth_fp,
        workflow_run_id="w", session_id="r3",
    )
    assert r["deleted_count"]      == 0
    assert r["idempotent_rerun"]   is True
    assert r["post_delete_guard"]  == "pass_noop"
    # No audit record for noop
    assert await db[DEDUPE_AUDIT_COLLECTION].count_documents({}) == 0


# ─── apply_dedupe_group — doc belongs to wrong logical key ─────────
async def test_apply_dedupe_rejects_wrong_logical_key_doc(db):
    coll = "picks"
    await db[coll].insert_one({"_id": "x0", "id": "k7", "amount": 1})
    await db[coll].insert_one({"_id": "y0", "id": "KX", "amount": 1})  # wrong key
    auth_fp = canonical_doc_fingerprint({"id": "k7", "amount": 1})
    with pytest.raises(ValueError) as ei:
        await apply_dedupe_group(
            db,
            collection=coll,
            logical_key_values={"id": "k7"},
            classification=EXACT_DUPLICATE,
            all_ids=["x0", "y0"],
            authoritative_fingerprint=auth_fp,
            workflow_run_id="w", session_id="r3",
        )
    assert "DOC_LOGICAL_KEY_MISMATCH" in str(ei.value)


# ─── Apply-scope allowlist contents ────────────────────────────────
def test_apply_scope_exactly_the_8_phase7_failures():
    assert APPLY_DEDUPE_SCOPE == frozenset({
        "picks", "player_game_actuals", "player_game_logs",
        "player_identities", "prediction_snapshots", "pregame_snapshots",
        "publication_events", "settlement_events",
    })


# ─── EXACT + authoritative-match: survivor matches source ──────────
async def test_apply_dedupe_exact_survivor_matches_source_takes_match_reason(db):
    coll = "settlement_events"
    good = {"settlement_id": "k8", "amount": 10}
    bad_but_same_biz = {"settlement_id": "k8", "amount": 10}
    # Both docs have identical business content → EXACT.  Source
    # fingerprint matches both.  Expected: pick lowest _id, reason
    # REASON_LOWEST_ID (both equally authoritative).
    await db[coll].insert_one({"_id": "zz_higher", **bad_but_same_biz})
    await db[coll].insert_one({"_id": "aa_lower",  **good})
    auth_fp = canonical_doc_fingerprint(good)
    r = await apply_dedupe_group(
        db,
        collection=coll,
        logical_key_values={"settlement_id": "k8"},
        classification=EXACT_DUPLICATE,
        all_ids=["zz_higher", "aa_lower"],
        authoritative_fingerprint=auth_fp,
        workflow_run_id="w", session_id="r3",
    )
    assert r["survivor_id"]     == "aa_lower"
    assert r["survivor_reason"] == REASON_LOWEST_ID   # tiebreak within matches
    assert r["deleted_ids"]     == ["zz_higher"]
