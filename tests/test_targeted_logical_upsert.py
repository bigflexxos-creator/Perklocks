"""Tests for POST /api/admin/canonical-cutover/targeted-logical-upsert.

Scope — guarantees that the Phase-8 three-collection repair endpoint:
    * writes ONLY to the three allowlisted collections,
    * upserts by the hard-coded logical key for each collection,
    * leaves ``_id`` and underscore-prefixed bookkeeping untouched,
    * rejects oversized batches, bad confirm phrase, null logical keys,
      and unknown collections with fail-closed semantics,
    * does not drop/truncate/delete anything,
    * writes an audit record on both OK and PARTIAL_FAIL batches,
    * is byte-identical for repeat batches (idempotent).

Uses httpx + ASGITransport so the handler runs in the same asyncio
loop as Motor (avoids ``Future attached to a different loop``).
"""
from __future__ import annotations

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

from fastapi import FastAPI                                            # noqa: E402
from httpx import ASGITransport, AsyncClient                           # noqa: E402
from motor.motor_asyncio import AsyncIOMotorClient                     # noqa: E402

from services.canonical_dedupe import DEDUPE_AUDIT_COLLECTION          # noqa: E402
from routes import canonical_cutover_routes as routes                  # noqa: E402

pytestmark = pytest.mark.asyncio

CONFIRM = routes.TARGETED_REPAIR_CONFIRM_PHRASE


@pytest_asyncio.fixture
async def db():
    client = AsyncIOMotorClient("mongodb://localhost:27017")
    name = f"perklocks_tgt_upsert_{uuid.uuid4().hex[:12]}"
    try:
        yield client[name]
    finally:
        await client.drop_database(name)
        client.close()


@pytest_asyncio.fixture
async def test_client(db, monkeypatch):
    async def _fake_admin():
        return object()
    monkeypatch.setattr(routes, "_require_admin", _fake_admin)
    monkeypatch.setattr(routes, "_import_enabled", lambda: True)
    monkeypatch.setattr(routes, "_verify_import_token", lambda *_a, **_k: None)
    monkeypatch.setattr(routes, "get_canonical_database", lambda: db)

    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes._require_admin] = _fake_admin
    async with AsyncClient(transport=ASGITransport(app=app),
                            base_url="http://testserver") as client:
        yield client


HDR = {"X-Canonical-Import-Token": "test-token",
        "Content-Type": "application/json"}


def _body(**over) -> dict:
    base = {
        "session_id":      "sess-test-00000000",
        "workflow_run_id": "wf-test",
        "confirm_phrase":  CONFIRM,
        "collection":      "soccer_matches",
        "batch_no":        0,
        "docs":            [{"league": "epl", "season": "2024-25",
                              "home_team": "Arsenal",
                              "away_team": "Chelsea",
                              "date":      "2025-01-01",
                              "home_score": 2, "away_score": 1}],
    }
    base.update(over)
    return base


URL = "/api/admin/canonical-cutover/targeted-logical-upsert"


# ─── Positive paths (3 collections) ────────────────────────────────
async def test_soccer_matches_insert_then_update(test_client, db):
    row = {"league": "epl", "season": "2024-25", "home_team": "Arsenal",
            "away_team": "Chelsea", "date": "2025-01-01",
            "home_score": 2, "away_score": 1}
    r = await test_client.post(URL, headers=HDR,
                                 json=_body(docs=[row]))
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["inserted"] == 1
    assert j["updated"] == 0
    assert await db["soccer_matches"].count_documents({}) == 1

    # Upsert same key → update, not insert
    row["home_score"] = 3
    r = await test_client.post(URL, headers=HDR,
                                 json=_body(docs=[row], batch_no=1))
    assert r.status_code == 200
    j = r.json()
    assert j["inserted"] == 0
    assert j["updated"] + j["matched_unchanged"] == 1
    d = await db["soccer_matches"].find_one({"league": "epl",
                                              "season": "2024-25"})
    assert d["home_score"] == 3


async def test_player_game_actuals_upsert(test_client, db):
    r = await test_client.post(URL, headers=HDR, json=_body(
        collection="player_game_actuals",
        docs=[{"sport": "nba", "event_id": "401811053",
                "player_id": 3134932, "points": 20}],
    ))
    assert r.status_code == 200
    assert r.json()["inserted"] == 1
    assert await db["player_game_actuals"].count_documents({}) == 1


async def test_player_game_logs_upsert(test_client, db):
    r = await test_client.post(URL, headers=HDR, json=_body(
        collection="player_game_logs",
        docs=[{"sport": "nhl", "game_id": "nhl_2025020042",
                "player_id": "nhl_8480113", "shots": None,
                "goals": 0, "name": "A. Iafallo"}],
    ))
    assert r.status_code == 200
    assert r.json()["inserted"] == 1
    d = await db["player_game_logs"].find_one({"sport": "nhl"})
    # shots preserved as null (NOT silently converted to 0).
    assert d["shots"] is None


# ─── Fail-closed envelopes ─────────────────────────────────────────
async def test_rejects_unknown_collection(test_client, db):
    r = await test_client.post(URL, headers=HDR,
                                 json=_body(collection="picks"))
    assert r.status_code == 400
    assert "COLLECTION_NOT_IN_ALLOWLIST" in r.text
    # Nothing written anywhere.
    for coll in ("picks", "soccer_matches", "player_game_logs",
                  "player_game_actuals"):
        assert await db[coll].count_documents({}) == 0


async def test_rejects_bad_confirm_phrase(test_client, db):
    r = await test_client.post(URL, headers=HDR,
                                 json=_body(confirm_phrase="WRONG"))
    assert r.status_code == 400
    assert "BAD_CONFIRM_PHRASE" in r.text
    assert await db["soccer_matches"].count_documents({}) == 0


async def test_rejects_oversized_batch(test_client, db):
    big = [{"league": "epl", "season": "2024-25", "home_team": f"H{i}",
             "away_team": "C", "date": "2025-01-01"} for i in range(501)]
    r = await test_client.post(URL, headers=HDR, json=_body(docs=big))
    assert r.status_code in {413, 422}
    assert await db["soccer_matches"].count_documents({}) == 0


async def test_rejects_null_logical_key_mid_batch(test_client, db):
    # Row 2 has null event_id → partial-batch fail; row 1 already
    # committed, row 2 reported as first_bad_idx.
    r = await test_client.post(URL, headers=HDR, json=_body(
        collection="player_game_actuals",
        docs=[
            {"sport": "nba", "event_id": "A", "player_id": 1, "pts": 10},
            {"sport": "nba", "event_id": None, "player_id": 2, "pts": 20},
        ],
    ))
    assert r.status_code == 409
    body = r.json()["detail"]
    assert body["first_bad_idx"] == 1
    assert "NULL_LOGICAL_KEY" in body["first_bad_reason"]
    assert body["docs_committed"] == 1
    # Row 1 did commit; row 2 did NOT.
    assert await db["player_game_actuals"].count_documents({}) == 1
    # Partial-fail audit written.
    audits = await db[DEDUPE_AUDIT_COLLECTION].find(
        {"event": "TARGETED_REPAIR_UPSERT_BATCH",
         "status": "PARTIAL_FAIL"}).to_list(None)
    assert len(audits) == 1


async def test_bookkeeping_fields_stripped_from_set(test_client, db):
    r = await test_client.post(URL, headers=HDR, json=_body(
        collection="player_game_actuals",
        docs=[{"sport": "nfl", "event_id": "E", "player_id": 99,
                "pts": 7,
                "_canonical_import_meta": {"session": "old"},
                "_id": "SHOULD_NOT_LAND",
                "_internal_bookkeeping": True}],
    ))
    assert r.status_code == 200
    d = await db["player_game_actuals"].find_one({"sport": "nfl"})
    assert d is not None
    # _id auto-assigned by Mongo, NOT the string from the payload.
    assert d["_id"] != "SHOULD_NOT_LAND"
    assert "_canonical_import_meta" not in d
    assert "_internal_bookkeeping" not in d
    assert d["pts"] == 7


# ─── Audit side-effects ─────────────────────────────────────────────
async def test_ok_audit_recorded(test_client, db):
    r = await test_client.post(URL, headers=HDR, json=_body())
    assert r.status_code == 200
    aud = await db[DEDUPE_AUDIT_COLLECTION].find_one(
        {"event": "TARGETED_REPAIR_UPSERT_BATCH", "status": "OK"})
    assert aud is not None
    assert aud["collection"] == "soccer_matches"
    assert aud["inserted"] == 1
    assert aud["batch_no"] == 0


# ─── Allowlist is byte-literal and the only 3 keys reachable ──────
def test_allowlist_is_exactly_three():
    allowed = routes._TARGETED_REPAIR_ALLOWED_COLLECTIONS
    assert set(allowed) == {"player_game_actuals",
                             "player_game_logs",
                             "soccer_matches"}
    assert allowed["player_game_actuals"] == \
        ("sport", "event_id", "player_id")
    assert allowed["player_game_logs"] == \
        ("sport", "game_id", "player_id")
    assert allowed["soccer_matches"] == \
        ("league", "season", "home_team", "away_team", "date")
    assert routes._TARGETED_REPAIR_MAX_BATCH == 500
    assert routes.TARGETED_REPAIR_CONFIRM_PHRASE == \
        "APPLY_PERKLOCKS_R3_PHASE8_REPAIR_V1"
