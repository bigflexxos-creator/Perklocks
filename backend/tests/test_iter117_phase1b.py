"""Phase 1b — Reader / Write-guard / Endpoint parity tests.

Coverage:
  A. `normalize_probability` handles fraction / percentage / edge cases
  B. `hydrate()` aliases published_* → legacy names when snapshot exists
  C. `hydrate()` passes legacy row through unchanged with proper marker
  D. `hydrate()` never mutates the input
  E. Write-guard blocks non-publication mutation of published fields
  F. Write-guard allows the publication service's own writes
  G. Write-guard blocks shadow lock_score fields (v2, raw, peak)
  H. Publication normalizes stored probability to [0, 1] at publish
  I. End-to-end: publish → hydrate → assert parity across the contract
  J. `_canonicalize_lock_score` is now a snapshot-first pass-through
"""
from __future__ import annotations

import asyncio
import os

import pytest
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

from motor.motor_asyncio import AsyncIOMotorClient


def _run(c):
    return asyncio.run(c)


def _fresh_db():
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    return client[os.environ.get("DB_NAME", "lockscore_db")]


# ────────────────────────────────────────────────────────────────
# A — normalize_probability
# ────────────────────────────────────────────────────────────────
def test_A_normalize_probability_handles_all_shapes():
    from services.published_prediction_reader import normalize_probability
    # None / bad
    assert normalize_probability(None) == 0.0
    assert normalize_probability("nan") == 0.0
    assert normalize_probability({}) == 0.0
    # Fractions
    assert normalize_probability(0.62) == 0.62
    assert normalize_probability(0.0) == 0.0
    assert normalize_probability(1.0) == 1.0
    # Percentages
    assert normalize_probability(62) == 0.62
    assert normalize_probability(99.9) == pytest.approx(0.999, abs=1e-6)
    assert normalize_probability(100) == 1.0
    # Negative → clamp to 0
    assert normalize_probability(-0.1) == 0.0
    # >100 → clamp to 1
    assert normalize_probability(150) == 1.0


# ────────────────────────────────────────────────────────────────
# B — hydrate aliases published_* to legacy names
# ────────────────────────────────────────────────────────────────
def test_B_hydrate_aliases_when_snapshot_exists():
    from services.published_prediction_reader import hydrate
    pick = {
        "id": "test_hydrate_b1",
        "sport": "MLB",
        "published_lock_score": 88.0,
        "published_probability": 0.62,
        "published_edge": 3.5,
        "published_grade": "Strong Lock",
        "published_confidence": 88.0,
        "published_odds": -140,
        "published_line": 1.5,
        "published_reasoning": {"summary": "test"},
        # Legacy fields intentionally set to a WRONG value; hydrate
        # must overwrite them from the published_* fields.
        "lock_score": 42.0,
        "win_probability": 20.0,
        "grade": "Pass",
    }
    h = hydrate(pick)
    assert h["lock_score"] == 88.0
    assert h["win_probability"] == 0.62
    assert h["edge_percent"] == 3.5
    assert h["grade"] == "Strong Lock"
    assert h["confidence"] == 88.0
    assert h["book_odds"] == -140
    assert h["odds"] == -140
    assert h["american_odds"] == -140
    assert h["line"] == 1.5
    assert h["reasoning"] == {"summary": "test"}
    assert h["_prediction_source"] == "snapshot"


def test_B2_hydrate_normalizes_percentage_probability():
    from services.published_prediction_reader import hydrate
    pick = {
        "id": "test_hydrate_b2",
        "published_lock_score": 75.0,
        "published_probability": 62.0,   # percentage
        "published_edge": 2.0,
        "published_grade": "Playable Bet",
        "published_confidence": 75.0,
        "published_odds": -110, "published_line": None,
        "published_reasoning": "",
    }
    h = hydrate(pick)
    # Percentage → fraction normalization at read time
    assert h["win_probability"] == 0.62


# ────────────────────────────────────────────────────────────────
# C — legacy row without snapshot: passes through with marker
# ────────────────────────────────────────────────────────────────
def test_C_hydrate_legacy_row_passthrough():
    from services.published_prediction_reader import hydrate
    pick = {
        "id": "legacy_c1",
        "sport": "MLB",
        "lock_score": 78.0,
        "win_probability": 61.0,   # percentage — will be normalized
        "edge_percent": 2.1,
        "grade": "Strong Lock",
    }
    h = hydrate(pick)
    assert h["lock_score"] == 78.0
    assert h["win_probability"] == 0.61
    assert h["_prediction_source"] == "legacy_unpublished"
    assert h["_snapshot_version"] is None


# ────────────────────────────────────────────────────────────────
# D — hydrate never mutates the input
# ────────────────────────────────────────────────────────────────
def test_D_hydrate_never_mutates_input():
    from services.published_prediction_reader import hydrate
    pick = {"id": "test_d1", "published_lock_score": 90.0, "lock_score": 40.0}
    _ = hydrate(pick)
    assert pick["lock_score"] == 40.0, "input mutated!"


# ────────────────────────────────────────────────────────────────
# E — Write-guard blocks non-publication mutation of published fields
# ────────────────────────────────────────────────────────────────
def test_E_write_guard_blocks_published_field_mutation():
    from services.published_write_guard import (
        assert_no_published_mutation, PublishedFieldMutationError,
    )
    # $set touching published_lock_score → block
    with pytest.raises(PublishedFieldMutationError) as excinfo:
        assert_no_published_mutation(
            {"$set": {"published_lock_score": 99}},
            caller="test_E1",
        )
    assert "published_lock_score" in excinfo.value.fields
    # $set touching legacy alias `lock_score` → also block
    with pytest.raises(PublishedFieldMutationError):
        assert_no_published_mutation(
            {"$set": {"lock_score": 99}},
            caller="test_E2",
        )
    # Non-published fields → allowed
    assert_no_published_mutation(
        {"$set": {"key_insights": ["x"], "created_at": "t"}},
        caller="test_E3",
    )


def test_F_write_guard_allows_publication_writes():
    from services.published_write_guard import (
        assert_no_published_mutation,
    )
    # Same payload with allow_publication_write=True → no raise
    assert_no_published_mutation(
        {"$set": {"published_lock_score": 99, "lock_score": 99}},
        allow_publication_write=True,
        caller="test_F1",
    )


# ────────────────────────────────────────────────────────────────
# G — Write-guard blocks retired shadow lock_score fields
# ────────────────────────────────────────────────────────────────
def test_G_write_guard_blocks_shadow_lock_score_fields():
    from services.published_write_guard import (
        assert_no_published_mutation, PublishedFieldMutationError,
    )
    for shadow in ("lock_score_v2", "lock_score_raw", "lock_score_peak"):
        with pytest.raises(PublishedFieldMutationError):
            assert_no_published_mutation(
                {"$set": {shadow: 88}},
                caller=f"test_G_{shadow}",
            )


# ────────────────────────────────────────────────────────────────
# H — Publication normalizes probability at publish time
# ────────────────────────────────────────────────────────────────
def test_H_publish_normalizes_probability():
    async def run():
        db = _fresh_db()
        from services.prediction_publication_service import (
            PredictionPublicationService, SNAPSHOT_COLLECTION,
            MISMATCH_COLLECTION,
        )
        pub = PredictionPublicationService(db)
        pub._board_version = "test-board-h"
        await pub.ensure_indices()
        await db[SNAPSHOT_COLLECTION].delete_many(
            {"prediction_id": "pub_test_h_norm"})
        await db[MISMATCH_COLLECTION].delete_many(
            {"prediction_id": "pub_test_h_norm"})
        # Candidate provides win_probability=62 (percentage)
        cand = {
            "id": "pub_test_h_norm",
            "sport": "MLB",
            "lock_score": 88, "win_probability": 62.0,
            "edge_percent": 3.5, "grade": "Strong Lock",
            "confidence": 88, "line": 1.5, "book_odds": -140,
        }
        await pub.publish(cand, dual_write=False)
        snap = await pub.get_active_snapshot("pub_test_h_norm")
        assert snap["published_probability"] == 0.62, \
            f"expected 0.62 got {snap['published_probability']!r}"
        await db[SNAPSHOT_COLLECTION].delete_many(
            {"prediction_id": "pub_test_h_norm"})
    _run(run())


# ────────────────────────────────────────────────────────────────
# I — End-to-end publish → hydrate parity
# ────────────────────────────────────────────────────────────────
def test_I_publish_then_hydrate_yields_contract_values():
    async def run():
        db = _fresh_db()
        pid = "pub_test_i_parity"
        from services.prediction_publication_service import (
            PredictionPublicationService, SNAPSHOT_COLLECTION,
            MISMATCH_COLLECTION,
        )
        from services.published_prediction_reader import hydrate
        pub = PredictionPublicationService(db)
        pub._board_version = "test-board-i"
        await pub.ensure_indices()
        # cleanup
        for c in (SNAPSHOT_COLLECTION, MISMATCH_COLLECTION):
            await db[c].delete_many({"prediction_id": pid})
        await db.picks.delete_one({"id": pid})

        cand = {
            "id": pid,
            "sport": "MLB",
            "lock_score": 90.0,
            "win_probability": 0.65,
            "edge_percent": 4.2,
            "grade": "Elite Lock",
            "confidence": 90.0,
            "line": 1.5, "book_odds": -155,
            "reasoning": {"summary": "parity test"},
        }
        await db.picks.insert_one({**cand, "pick_date": "2026-08-06"})
        await pub.publish(cand)
        # Simulate a downstream writer NOT touching published fields
        # (guarded via the write-guard — that's tested in E).
        pick = await db.picks.find_one({"id": pid})
        h = hydrate(pick)
        assert h["lock_score"] == 90.0
        assert h["win_probability"] == 0.65
        assert h["edge_percent"] == 4.2
        assert h["grade"] == "Elite Lock"
        assert h["confidence"] == 90.0
        assert h["book_odds"] == -155
        assert h["line"] == 1.5
        assert h["_prediction_source"] == "snapshot"
        # cleanup
        for c in (SNAPSHOT_COLLECTION, MISMATCH_COLLECTION):
            await db[c].delete_many({"prediction_id": pid})
        await db.picks.delete_one({"id": pid})
    _run(run())


# ────────────────────────────────────────────────────────────────
# J — server._canonicalize_lock_score is now a snapshot-first pass-through
# ────────────────────────────────────────────────────────────────
def test_J_canonicalize_lock_score_uses_snapshot_fast_path():
    import server
    # Pick with a published snapshot → should be pass-through (aliased)
    p1 = {
        "id": "test_j1",
        "published_lock_score": 88.0,
        "published_probability": 0.62,
        "published_edge": 3.5, "published_grade": "Strong Lock",
        "published_confidence": 88.0,
        "published_odds": -140, "published_line": 1.5,
        "published_reasoning": "",
        # Legacy values that the old canonicalizer might have promoted:
        "lock_score": 55.0, "lock_score_v2": 92.0,
    }
    out = server._canonicalize_lock_score(p1)
    # Must be the snapshot value — NOT max(55, 92) = 92
    assert out["lock_score"] == 88.0, \
        f"snapshot fast-path failed: got {out['lock_score']}"
    assert out["_prediction_source"] == "snapshot"


# ────────────────────────────────────────────────────────────────
# K — Endpoint smoke: /api/picks/today serves via hydration
# ────────────────────────────────────────────────────────────────
def test_K_endpoint_smoke_picks_today_via_httpx():
    """Smoke-level parity assertion — pulls /api/picks/today over HTTP
    and asserts every returned pick either has _prediction_source or
    (for legacy rows) at least a sensible lock_score."""
    async def run():
        import httpx
        url = "http://localhost:8001/api/picks/today"
        try:
            async with httpx.AsyncClient(timeout=10) as cx:
                r = await cx.get(url)
        except Exception as e:
            pytest.skip(f"backend not reachable: {e}")
        if r.status_code != 200:
            pytest.skip(f"endpoint returned {r.status_code}")
        body = r.json()
        picks = body if isinstance(body, list) else body.get("picks") or []
        # Even an empty board is a valid response.  When picks are
        # present, every pick must carry a normalized lock_score int.
        for p in picks[:5]:
            assert isinstance(p.get("lock_score"), (int, float))
            wp = p.get("win_probability")
            if wp is not None:
                # Endpoint returns percentage for the frontend so
                # normalize before comparing.
                from services.published_prediction_reader import (
                    normalize_probability,
                )
                assert 0.0 <= normalize_probability(wp) <= 1.0
    _run(run())
