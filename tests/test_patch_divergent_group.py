"""Tests for POST /api/admin/canonical-cutover/patch-divergent-group.

This endpoint is intentionally narrow (hard-coded allowlist of the
two Phase-7 NHL ``player_game_logs`` divergence groups, single field
``shots``, single transition ``null→0``). These tests exercise the
underlying route handler directly against a local test Mongo database
so we can prove:

    * Both whitelisted groups patch successfully, write two audit
      records (DIVERGENCE_PATCH + UPDATE_RESULT), and both docs now
      independently match the pinned authoritative fingerprint.
    * Fails closed on every single precondition miss with ZERO
      intentional mutation (DB unchanged, no success audit written).
    * Collection, logical-key, field, old-value, new-value,
      authoritative-fingerprint, _id set, duplicate count, and current
      value are each independently validated.
    * Only the two whitelisted logical keys are reachable.

We stub ``_require_admin``, ``_import_enabled``, ``_verify_import_token``,
and ``get_canonical_database`` so the test fixtures do not have to
exercise the real auth or env stack.
"""
from __future__ import annotations

import copy
import os
import sys
import uuid

import pytest
import pytest_asyncio

sys.path.insert(0, "/app/backend")
os.environ.setdefault("MONGO_URL",  "mongodb://localhost:27017/test")
os.environ.setdefault("DB_NAME",    "perklocks_dedupe_tests")
os.environ.setdefault("JWT_SECRET",
    "test-only-not-a-secret-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
os.environ.setdefault("CANONICAL_IMPORT_ENABLED", "true")
os.environ.setdefault("CANONICAL_IMPORT_TOKEN",   "test-token")

from bson import ObjectId                                              # noqa: E402
from fastapi import FastAPI                                            # noqa: E402
from httpx import ASGITransport, AsyncClient                           # noqa: E402
from motor.motor_asyncio import AsyncIOMotorClient                     # noqa: E402

from services.canonical_dedupe import (                                # noqa: E402
    DEDUPE_AUDIT_COLLECTION, canonical_doc_fingerprint,
)
from routes import canonical_cutover_routes as routes                  # noqa: E402

pytestmark = pytest.mark.asyncio


# ─── Hard-coded allowlist (mirrors the backend route) ───────────────
GROUP_1_KEY = {"sport": "nhl", "game_id": "nhl_2025020042", "player_id": "nhl_8480113"}
GROUP_1_IDS = ("6ac449987944462a0d2a4278", "6ac449987944462a0d2a4279")
GROUP_1_AFTER_FP = "14492740b54d0a86d682b4dc624ba08d6e23efa17dc057a9a9ff44e9c55def98"

GROUP_2_KEY = {"sport": "nhl", "game_id": "nhl_2025020055", "player_id": "nhl_8480798"}
GROUP_2_IDS = ("6ac449bf7944462a0d2a5328", "6ac449bf7944462a0d2a532d")
GROUP_2_AFTER_FP = "641515526acaf694a4e788260e1de4da5cac52d7d7e8629ecb2874c9f9318d92"


def _seed_group_1(db) -> None:
    """Insert two Production-shaped divergent docs for group 1.
    ``shots`` is intentionally absent (missing) in one and null in the
    other to prove the endpoint accepts both representations.
    """
    base = {**GROUP_1_KEY, "goals": 1, "assists": 0, "toi_seconds": 1200,
            "penalties": 0, "period_log": [1, 2, 3]}
    db["player_game_logs"].insert_many([
        {"_id": ObjectId(GROUP_1_IDS[0]), **base},                 # shots missing
        {"_id": ObjectId(GROUP_1_IDS[1]), **base, "shots": None},  # shots null
    ])


def _seed_group_2(db) -> None:
    base = {**GROUP_2_KEY, "goals": 0, "assists": 1, "toi_seconds": 900,
            "penalties": 1, "period_log": [1, 2, 3]}
    db["player_game_logs"].insert_many([
        {"_id": ObjectId(GROUP_2_IDS[0]), **base, "shots": None},
        {"_id": ObjectId(GROUP_2_IDS[1]), **base, "shots": None},
    ])


# ─── Pinned fingerprint derivation per seeded doc shape ─────────────
def _compute_pinned_fingerprint(seed_doc_wo_shots: dict) -> str:
    """Compute what the pinned authoritative fingerprint WOULD be for a
    doc of this shape with ``shots: 0``. We use this to compute the
    authoritative fingerprint the handler expects (so tests do not
    rely on the hard-coded hex constants matching the test seed
    shape).
    """
    doc = {**seed_doc_wo_shots, "shots": 0}
    return canonical_doc_fingerprint(doc)


# ─── Fixtures ────────────────────────────────────────────────────────
@pytest_asyncio.fixture
async def db():
    """Fresh test DB per test — dropped at teardown."""
    client = AsyncIOMotorClient("mongodb://localhost:27017")
    name = f"perklocks_patch_divergent_{uuid.uuid4().hex[:12]}"
    try:
        yield client[name]
    finally:
        await client.drop_database(name)
        client.close()


@pytest_asyncio.fixture
async def test_client(db, monkeypatch):
    """Async httpx client wired to the test DB with auth stubbed.

    Uses httpx.AsyncClient + ASGITransport (not fastapi.testclient) so
    the ASGI handler runs in the same asyncio loop as Motor — avoids
    the ``Future attached to a different loop`` error.

    The hard-coded allowlist in the route carries the Production
    before/after fingerprint hashes — synthetic test documents will
    not produce those exact hashes. The positive-path tests use the
    ``_patch_allowlist_for_test`` helper below to swap the constants.
    Negative-path tests leave the allowlist as-is so they also
    exercise the before/authoritative fingerprint check against
    deterministic hashes.
    """
    async def _fake_admin():
        return object()
    monkeypatch.setattr(routes, "_require_admin", _fake_admin)
    monkeypatch.setattr(routes, "_import_enabled", lambda: True)
    monkeypatch.setattr(routes, "_verify_import_token", lambda *_a, **_k: None)
    monkeypatch.setattr(routes, "get_canonical_database", lambda: db)

    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes._require_admin] = _fake_admin

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


def _patch_allowlist_for_test(monkeypatch, group_key: dict,
                              before_fp: str, after_fp: str,
                              doc_ids: tuple[str, str]) -> None:
    """Swap the hard-coded allowlist constants used by the route so
    positive tests can exercise the end-to-end mutate path with
    fingerprints derived from synthetic seed data.
    """
    original = routes._PATCH_DIVERGENT_ALLOWED_GROUPS
    replacement = tuple(
        {**g,
         "expected_before_fingerprint":        before_fp,
         "expected_authoritative_fingerprint": after_fp,
         "expected_doc_ids":                   doc_ids}
        if g["logical_key_values"] == group_key else g
        for g in original
    )
    monkeypatch.setattr(routes, "_PATCH_DIVERGENT_ALLOWED_GROUPS", replacement)


HDR = {"X-Canonical-Import-Token": "test-token", "Content-Type": "application/json"}


def _body(**overrides) -> dict:
    base = {
        "session_id":                "sess-test-00000000",
        "workflow_run_id":           "wf-test",
        "confirm_phrase":            routes.PATCH_DIVERGENT_CONFIRM_PHRASE,
        "collection":                "player_game_logs",
        "logical_key_values":        GROUP_1_KEY,
        "field":                     "shots",
        "old_value_sentinel":        None,
        "new_value":                 0,
        "authoritative_fingerprint": GROUP_1_AFTER_FP,
    }
    base.update(overrides)
    return base


# ─── Positive cases ─────────────────────────────────────────────────
async def test_patch_group1_succeeds_and_writes_two_audits(
    test_client, db, monkeypatch,
):
    base_wo_shots = {**GROUP_1_KEY, "goals": 1, "assists": 0,
                     "toi_seconds": 1200, "penalties": 0,
                     "period_log": [1, 2, 3]}
    # Both docs have ``shots`` explicitly null — the same shape as
    # Production (census confirmed EXACT_DUPLICATE, so both docs
    # must share one fingerprint).
    await db["player_game_logs"].insert_many([
        {"_id": ObjectId(GROUP_1_IDS[0]), **base_wo_shots, "shots": None},
        {"_id": ObjectId(GROUP_1_IDS[1]), **base_wo_shots, "shots": None},
    ])
    before_fp = canonical_doc_fingerprint({**base_wo_shots, "shots": None})
    expected_fp = _compute_pinned_fingerprint(base_wo_shots)
    _patch_allowlist_for_test(monkeypatch, GROUP_1_KEY,
                              before_fp, expected_fp, GROUP_1_IDS)

    r = await test_client.post(
        "/api/admin/canonical-cutover/patch-divergent-group",
        headers=HDR,
        json=_body(authoritative_fingerprint=expected_fp,
                   logical_key_values=GROUP_1_KEY),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["matched_count"] == 2
    assert body["modified_count"] == 2
    assert body["after_fingerprint"] == expected_fp

    # DB post-state: both docs now have shots=0 and the fingerprints
    # match the pinned authoritative.
    docs = []
    async for d in db["player_game_logs"].find({}):
        docs.append(d)
    assert len(docs) == 2
    assert all(d["shots"] == 0 for d in docs)
    assert all(canonical_doc_fingerprint(d) == expected_fp for d in docs)

    # Audit — exactly one DIVERGENCE_PATCH + one UPDATE_RESULT with
    # post_verification=OK.
    pre_audits  = []
    post_audits = []
    async for a in db[DEDUPE_AUDIT_COLLECTION].find({}):
        if a.get("event") == "DIVERGENCE_PATCH":
            pre_audits.append(a)
        elif a.get("event") == "UPDATE_RESULT":
            post_audits.append(a)
    assert len(pre_audits)  == 1
    assert len(post_audits) == 1
    assert post_audits[0]["post_verification"] == "OK"
    assert post_audits[0]["matched_count"]  == 2
    assert post_audits[0]["modified_count"] == 2


async def test_patch_group2_also_whitelisted(test_client, db, monkeypatch):
    base_wo_shots = {**GROUP_2_KEY, "goals": 0, "assists": 1,
                     "toi_seconds": 900, "penalties": 1,
                     "period_log": [1, 2, 3]}
    await db["player_game_logs"].insert_many([
        {"_id": ObjectId(GROUP_2_IDS[0]), **base_wo_shots, "shots": None},
        {"_id": ObjectId(GROUP_2_IDS[1]), **base_wo_shots, "shots": None},
    ])
    before_fp = canonical_doc_fingerprint({**base_wo_shots, "shots": None})
    expected_fp = _compute_pinned_fingerprint(base_wo_shots)
    _patch_allowlist_for_test(monkeypatch, GROUP_2_KEY,
                              before_fp, expected_fp, GROUP_2_IDS)

    r = await test_client.post(
        "/api/admin/canonical-cutover/patch-divergent-group",
        headers=HDR,
        json=_body(logical_key_values=GROUP_2_KEY,
                   authoritative_fingerprint=expected_fp),
    )
    assert r.status_code == 200, r.text
    assert r.json()["modified_count"] == 2


# ─── Allowlist + envelope: fail closed with ZERO mutation ──────────
@pytest.mark.parametrize(
    "override, expect_status, expect_substr",
    [
        # Not in allowlist
        ({"logical_key_values": {"sport": "nhl", "game_id": "nhl_XXX",
                                 "player_id": "nhl_1"}},  400, "GROUP_NOT_IN_ALLOWLIST"),
        ({"collection": "picks"},                         400, "GROUP_NOT_IN_ALLOWLIST"),
        # Wrong field
        ({"field": "goals"},                              400, "FIELD_NOT_ALLOWED"),
        # Wrong old value
        ({"old_value_sentinel": 0},                       400, "OLD_VALUE_NOT_ALLOWED"),
        # Wrong new value
        ({"new_value": 1},                                400, "NEW_VALUE_NOT_ALLOWED"),
        # Wrong authoritative fingerprint (constant mismatch)
        ({"authoritative_fingerprint": "0" * 64},         400, "AUTHORITATIVE_FINGERPRINT_MISMATCH"),
        # Wrong confirm phrase
        ({"confirm_phrase": "WRONG"},                     400, "BAD_CONFIRM_PHRASE"),
    ],
)
async def test_envelope_and_allowlist_fail_closed(
    test_client, db, override, expect_status, expect_substr,
):
    # Seed docs so a mutation would be visible if accidentally performed.
    base_wo_shots = {**GROUP_1_KEY, "goals": 1, "assists": 0,
                     "toi_seconds": 1200, "penalties": 0}
    await db["player_game_logs"].insert_many([
        {"_id": ObjectId(GROUP_1_IDS[0]), **base_wo_shots},
        {"_id": ObjectId(GROUP_1_IDS[1]), **base_wo_shots},
    ])
    before_shots = [d.get("shots", "MISSING")
                     for d in await db["player_game_logs"].find({}).to_list(None)]

    r = await test_client.post(
        "/api/admin/canonical-cutover/patch-divergent-group",
        headers=HDR,
        json=_body(**override),
    )
    assert r.status_code == expect_status, r.text
    assert expect_substr in r.text

    # DB unchanged.
    after_shots = [d.get("shots", "MISSING")
                    for d in await db["player_game_logs"].find({}).to_list(None)]
    assert before_shots == after_shots
    # No audits written.
    assert await db[DEDUPE_AUDIT_COLLECTION].count_documents({}) == 0


# ─── Preconditions tied to live DB state ───────────────────────────
async def test_fails_closed_when_logical_key_count_not_two(
    test_client, db, monkeypatch,
):
    # Only one doc for the whitelisted key — count mismatch.
    base_wo_shots = {**GROUP_1_KEY, "goals": 1, "assists": 0}
    await db["player_game_logs"].insert_one(
        {"_id": ObjectId(GROUP_1_IDS[0]), **base_wo_shots})

    # Make the body's authoritative_fingerprint pass the allowlist
    # check so the handler proceeds to the count precondition.
    _patch_allowlist_for_test(
        monkeypatch, GROUP_1_KEY,
        before_fp=canonical_doc_fingerprint(base_wo_shots),
        after_fp="a" * 64, doc_ids=GROUP_1_IDS,
    )
    r = await test_client.post(
        "/api/admin/canonical-cutover/patch-divergent-group",
        headers=HDR,
        json=_body(authoritative_fingerprint="a" * 64),
    )
    assert r.status_code == 409
    assert "LOGICAL_KEY_COUNT_UNEXPECTED" in r.text
    # DB unchanged; no audit.
    assert await db[DEDUPE_AUDIT_COLLECTION].count_documents({}) == 0
    d = await db["player_game_logs"].find_one({"_id": ObjectId(GROUP_1_IDS[0])})
    assert "shots" not in d


async def test_fails_closed_when_doc_ids_do_not_match_whitelist(
    test_client, db, monkeypatch,
):
    # Two docs on the whitelisted logical key but with WRONG _ids.
    base_wo_shots = {**GROUP_1_KEY, "goals": 1, "assists": 0}
    foreign_ids = [ObjectId(), ObjectId()]
    await db["player_game_logs"].insert_many([
        {"_id": foreign_ids[0], **base_wo_shots},
        {"_id": foreign_ids[1], **base_wo_shots},
    ])
    _patch_allowlist_for_test(
        monkeypatch, GROUP_1_KEY,
        before_fp=canonical_doc_fingerprint(base_wo_shots),
        after_fp="a" * 64, doc_ids=GROUP_1_IDS,
    )
    r = await test_client.post(
        "/api/admin/canonical-cutover/patch-divergent-group",
        headers=HDR,
        json=_body(authoritative_fingerprint="a" * 64),
    )
    assert r.status_code == 409
    assert "DOC_IDS_UNEXPECTED" in r.text
    assert await db[DEDUPE_AUDIT_COLLECTION].count_documents({}) == 0


async def test_fails_closed_when_current_value_is_not_null(
    test_client, db, monkeypatch,
):
    base_wo_shots = {**GROUP_1_KEY, "goals": 1, "assists": 0}
    await db["player_game_logs"].insert_many([
        {"_id": ObjectId(GROUP_1_IDS[0]), **base_wo_shots, "shots": 3},
        {"_id": ObjectId(GROUP_1_IDS[1]), **base_wo_shots, "shots": None},
    ])
    _patch_allowlist_for_test(
        monkeypatch, GROUP_1_KEY,
        before_fp="b" * 64, after_fp="a" * 64, doc_ids=GROUP_1_IDS,
    )
    r = await test_client.post(
        "/api/admin/canonical-cutover/patch-divergent-group",
        headers=HDR,
        json=_body(authoritative_fingerprint="a" * 64),
    )
    assert r.status_code == 409
    assert "CURRENT_VALUE_NOT_NULL" in r.text
    d = await db["player_game_logs"].find_one({"_id": ObjectId(GROUP_1_IDS[0])})
    assert d["shots"] == 3
    assert await db[DEDUPE_AUDIT_COLLECTION].count_documents({}) == 0


async def test_fails_closed_when_before_fingerprint_mismatches(
    test_client, db, monkeypatch,
):
    # Seed the two whitelisted _ids but with "wrong-shape" business
    # content so the actual before-fingerprint diverges from the
    # whitelist constant.
    base_wo_shots = {**GROUP_1_KEY, "goals": 999}
    await db["player_game_logs"].insert_many([
        {"_id": ObjectId(GROUP_1_IDS[0]), **base_wo_shots},
        {"_id": ObjectId(GROUP_1_IDS[1]), **base_wo_shots},
    ])
    # Allowlist carries a before_fp constant that does NOT match the
    # test seed shape's actual fingerprint — the BEFORE_FINGERPRINT
    # check must fail.
    _patch_allowlist_for_test(
        monkeypatch, GROUP_1_KEY,
        before_fp="d" * 64, after_fp="a" * 64, doc_ids=GROUP_1_IDS,
    )
    r = await test_client.post(
        "/api/admin/canonical-cutover/patch-divergent-group",
        headers=HDR,
        json=_body(authoritative_fingerprint="a" * 64),
    )
    assert r.status_code == 409
    assert "BEFORE_FINGERPRINT_MISMATCH" in r.text
    docs = await db["player_game_logs"].find({}).to_list(None)
    assert all("shots" not in d for d in docs)
    assert await db[DEDUPE_AUDIT_COLLECTION].count_documents({}) == 0


# ─── Allowlist completeness: only exactly two groups reachable ─────
def test_allowlist_contains_exactly_two_groups():
    assert len(routes._PATCH_DIVERGENT_ALLOWED_GROUPS) == 2
    assert routes._PATCH_DIVERGENT_FIELD == "shots"
    assert routes._PATCH_DIVERGENT_OLD_ALLOWED == (None,)
    assert routes._PATCH_DIVERGENT_NEW_VALUE == 0
    # And the two expected keys are exactly the two NHL groups.
    seen_keys = {tuple(sorted(g["logical_key_values"].items()))
                 for g in routes._PATCH_DIVERGENT_ALLOWED_GROUPS}
    expected = {
        tuple(sorted({"sport": "nhl", "game_id": "nhl_2025020042",
                      "player_id": "nhl_8480113"}.items())),
        tuple(sorted({"sport": "nhl", "game_id": "nhl_2025020055",
                      "player_id": "nhl_8480798"}.items())),
    }
    assert seen_keys == expected
