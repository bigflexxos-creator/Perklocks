"""Phase 1a — PredictionPublicationService contract + idempotency tests.

Coverage:
  A. Payload construction — every required field is stamped.
  B. Snapshot insert — atomic, unique per (prediction_id, version).
  C. Idempotency — re-publishing the same candidate returns existing.
  D. Concurrent publish — race safe under two parallel calls.
  E. Dual-write — picks doc receives published_* fields.
  F. Mismatch report — drift is logged, not silently absorbed.
  G. Missing id — rejected cleanly.
  H. Legacy-unknown tokens — used when version metadata is absent.
  I. Batch publish — errors captured per candidate, batch continues.
  J. Snapshot immutability — no service can bypass and overwrite v1.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

import pytest
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import DuplicateKeyError


def _run(c):
    return asyncio.run(c)


def _fresh_db():
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    return client[os.environ.get("DB_NAME", "lockscore_db")]


async def _wipe(db):
    from services.prediction_publication_service import (
        SNAPSHOT_COLLECTION, MISMATCH_COLLECTION,
    )
    # Only wipe the unit-test slice — anything with prediction_id starting
    # with "pub_test_" is fair game to remove.
    await db[SNAPSHOT_COLLECTION].delete_many(
        {"prediction_id": {"$regex": "^pub_test_"}})
    await db[MISMATCH_COLLECTION].delete_many(
        {"prediction_id": {"$regex": "^pub_test_"}})
    await db.picks.delete_many({"id": {"$regex": "^pub_test_"}})


def _candidate(pid: str, *, lock=88.0, prob=0.62, edge=3.5,
                grade="Strong Lock", confidence=88.0,
                line=1.5, odds=-140) -> dict:
    return {
        "id": pid,
        "sport": "MLB",
        "market": "Aaron Judge (NYY) Over 1.5 hits",
        "lock_score": lock,
        "win_probability": prob,
        "edge_percent": edge,
        "grade": grade,
        "confidence": confidence,
        "line": line,
        "book_odds": odds,
        "reasoning": {"summary": "12-game hitting streak vs LHP"},
        "model_version": "mlb_prop_v3.2",
        "fusion_version": "fusion_v4",
        "scoring_version": "lockscore_v2.1",
        "calibration_version": "cal_2026-08-01",
        "validator_version": "board_v2.0",
        "simulation_version": "mc_v1.5",
        "feature_snapshot_version": "feat_v2.0",
    }


# ────────────────────────────────────────────────────────────────
# A — Payload construction
# ────────────────────────────────────────────────────────────────
def test_A_payload_carries_every_required_field():
    async def run():
        db = _fresh_db()
        await _wipe(db)
        from services.prediction_publication_service import (
            PredictionPublicationService, PUBLISHED_FIELDS, VERSION_FIELDS,
        )
        pub = PredictionPublicationService(db)
        await pub.ensure_indices()
        cand = _candidate("pub_test_a1")
        r = await pub.publish(cand)
        snap = await pub.get_active_snapshot("pub_test_a1")
        assert snap is not None
        for f in PUBLISHED_FIELDS + VERSION_FIELDS:
            assert f in snap, f"snapshot missing {f}"
        assert snap["snapshot_version"] == 1
        assert snap["is_active"] is True
        assert snap["is_legacy"] is False
        assert snap["publication_source"] == "canonical_pipeline"
        assert snap["published_lock_score"] == 88.0
        assert snap["published_grade"] == "Strong Lock"
        assert snap["published_line"] == 1.5
        assert snap["published_odds"] == -140
        assert r.was_new is True
        await _wipe(db)
    _run(run())


# ────────────────────────────────────────────────────────────────
# B — Snapshot uniqueness
# ────────────────────────────────────────────────────────────────
def test_B_snapshot_version_is_unique_per_prediction():
    async def run():
        db = _fresh_db()
        await _wipe(db)
        from services.prediction_publication_service import (
            PredictionPublicationService, SNAPSHOT_COLLECTION,
        )
        pub = PredictionPublicationService(db)
        await pub.ensure_indices()
        # Try to bypass and manually insert a duplicate v1 — must fail.
        await pub.publish(_candidate("pub_test_b1"))
        dup_doc = {
            "prediction_id": "pub_test_b1",
            "pick_id": "pub_test_b1",
            "snapshot_version": 1,
            "board_version": "manual_dup",
            "published_probability": 0.5,
            "published_edge": 1.0,
            "published_lock_score": 50,
            "published_grade": "Playable Bet",
            "published_confidence": 50,
            "published_reasoning": "",
            "published_line": None, "published_odds": None,
            "model_version": "unknown", "fusion_version": "unknown",
            "scoring_version": "unknown", "calibration_version": "unknown",
            "validator_version": "unknown", "simulation_version": "unknown",
            "feature_snapshot_version": "unknown",
            "published_at": datetime.now(timezone.utc).isoformat(),
            "publication_source": "attacker",
            "is_legacy": False,
            "payload_hash": "dead", "idempotency_key": "beef",
            "is_active": True,
        }
        with pytest.raises(DuplicateKeyError):
            await db[SNAPSHOT_COLLECTION].insert_one(dup_doc)
        await _wipe(db)
    _run(run())


# ────────────────────────────────────────────────────────────────
# C — Idempotency
# ────────────────────────────────────────────────────────────────
def test_C_idempotent_republish_returns_existing():
    async def run():
        db = _fresh_db()
        await _wipe(db)
        from services.prediction_publication_service import (
            PredictionPublicationService, SNAPSHOT_COLLECTION,
        )
        pub = PredictionPublicationService(db)
        await pub.ensure_indices()
        c = _candidate("pub_test_c1")
        # Force a stable board_version so idempotency keys line up.
        pub._board_version = "fixed-board-c"
        r1 = await pub.publish(c)
        r2 = await pub.publish(c)
        r3 = await pub.publish(c)
        assert r1.was_new is True
        assert r2.was_new is False
        assert r3.was_new is False
        assert r1.idempotency_key == r2.idempotency_key == r3.idempotency_key
        n = await db[SNAPSHOT_COLLECTION].count_documents(
            {"prediction_id": "pub_test_c1"})
        assert n == 1, f"expected exactly 1 snapshot, got {n}"
        await _wipe(db)
    _run(run())


# ────────────────────────────────────────────────────────────────
# D — Concurrent publish
# ────────────────────────────────────────────────────────────────
def test_D_concurrent_publish_is_race_safe():
    async def run():
        db = _fresh_db()
        await _wipe(db)
        from services.prediction_publication_service import (
            PredictionPublicationService, SNAPSHOT_COLLECTION,
        )
        pub = PredictionPublicationService(db)
        pub._board_version = "fixed-board-d"
        await pub.ensure_indices()
        c = _candidate("pub_test_d1")
        # Fire 10 concurrent publishes for the same candidate.
        rs = await asyncio.gather(*[pub.publish(c) for _ in range(10)])
        # Exactly one should be "new"; the rest must all be existing.
        n_new = sum(1 for r in rs if r.was_new)
        assert n_new == 1, f"expected 1 new snapshot, got {n_new}"
        n = await db[SNAPSHOT_COLLECTION].count_documents(
            {"prediction_id": "pub_test_d1"})
        assert n == 1
        await _wipe(db)
    _run(run())


# ────────────────────────────────────────────────────────────────
# E — Dual-write applies published_* to picks
# ────────────────────────────────────────────────────────────────
def test_E_dual_write_updates_picks_document():
    async def run():
        db = _fresh_db()
        await _wipe(db)
        from services.prediction_publication_service import (
            PredictionPublicationService, PUBLISHED_FIELDS,
        )
        pub = PredictionPublicationService(db)
        await pub.ensure_indices()
        c = _candidate("pub_test_e1")
        # Publication needs an existing picks doc for dual-write.
        await db.picks.insert_one({**c, "pick_date": "2026-08-06"})
        r = await pub.publish(c)
        assert r.dual_write_applied is True
        row = await db.picks.find_one({"id": "pub_test_e1"})
        for f in PUBLISHED_FIELDS:
            assert f in row, f"picks doc missing {f} after dual-write"
        assert row["published_lock_score"] == 88.0
        assert row["published_grade"] == "Strong Lock"
        # Legacy field should also still exist untouched.
        assert row["lock_score"] == 88.0
        await _wipe(db)
    _run(run())


# ────────────────────────────────────────────────────────────────
# F — Mismatch report catches drift between legacy fields and snapshot
# ────────────────────────────────────────────────────────────────
def test_F_mismatch_report_records_drift():
    async def run():
        db = _fresh_db()
        await _wipe(db)
        from services.prediction_publication_service import (
            PredictionPublicationService, MISMATCH_COLLECTION,
        )
        pub = PredictionPublicationService(db)
        await pub.ensure_indices()
        c = _candidate("pub_test_f1")
        # Seed the picks doc with LEGACY fields that DISAGREE with the
        # candidate values (simulating a stale writer).
        await db.picks.insert_one({
            **c, "pick_date": "2026-08-06",
            "lock_score": 55.0,           # candidate says 88 → drift
            "win_probability": 0.30,      # candidate says 0.62 → drift
        })
        r = await pub.publish(c)
        assert r.mismatch_logged is True
        rows = await db[MISMATCH_COLLECTION].find(
            {"prediction_id": "pub_test_f1"}).to_list(10)
        assert len(rows) == 1
        drifts = {d["field"] for d in rows[0]["drifts"]}
        assert "lock_score" in drifts
        assert "win_probability" in drifts
        await _wipe(db)
    _run(run())


# ────────────────────────────────────────────────────────────────
# G — Missing id is rejected
# ────────────────────────────────────────────────────────────────
def test_G_missing_id_is_rejected():
    async def run():
        db = _fresh_db()
        from services.prediction_publication_service import (
            PredictionPublicationService,
        )
        pub = PredictionPublicationService(db)
        await pub.ensure_indices()
        with pytest.raises(ValueError):
            await pub.publish({"lock_score": 88})
    _run(run())


# ────────────────────────────────────────────────────────────────
# H — legacy_unknown tokens for missing version metadata
# ────────────────────────────────────────────────────────────────
def test_H_legacy_unknown_when_versions_missing():
    async def run():
        db = _fresh_db()
        await _wipe(db)
        from services.prediction_publication_service import (
            PredictionPublicationService, LEGACY_UNKNOWN,
        )
        pub = PredictionPublicationService(db)
        await pub.ensure_indices()
        c = _candidate("pub_test_h1")
        # Strip every version field.
        for k in ("model_version", "fusion_version", "scoring_version",
                  "calibration_version", "validator_version",
                  "simulation_version", "feature_snapshot_version"):
            c.pop(k, None)
        r = await pub.publish(c)
        snap = await pub.get_active_snapshot("pub_test_h1")
        for k in ("model_version", "fusion_version", "scoring_version",
                  "calibration_version", "validator_version",
                  "simulation_version", "feature_snapshot_version"):
            assert snap[k] == LEGACY_UNKNOWN, \
                f"expected {k}={LEGACY_UNKNOWN} on missing metadata"
        await _wipe(db)
    _run(run())


# ────────────────────────────────────────────────────────────────
# I — Batch publish captures errors without aborting the batch
# ────────────────────────────────────────────────────────────────
def test_I_batch_publish_error_isolation():
    async def run():
        db = _fresh_db()
        await _wipe(db)
        from services.prediction_publication_service import (
            PredictionPublicationService,
        )
        pub = PredictionPublicationService(db)
        await pub.ensure_indices()
        good1 = _candidate("pub_test_i1")
        bad   = {"sport": "MLB", "lock_score": 40}   # no id
        good2 = _candidate("pub_test_i2")
        summary = await pub.publish_batch(
            [good1, bad, good2], dual_write=False)
        assert summary["new_snapshots"] == 2
        assert summary["existing_snapshots"] == 0
        assert len(summary["errors"]) == 1
        await _wipe(db)
    _run(run())


# ────────────────────────────────────────────────────────────────
# J — Published snapshot is immutable — direct field update rejected
# by application code path.  (Mongo itself cannot enforce field-level
# immutability without a schema validator, but the publication service
# never issues an update to a published snapshot.)
# ────────────────────────────────────────────────────────────────
def test_J_service_never_updates_existing_snapshot():
    async def run():
        db = _fresh_db()
        await _wipe(db)
        from services.prediction_publication_service import (
            PredictionPublicationService, SNAPSHOT_COLLECTION,
        )
        pub = PredictionPublicationService(db)
        pub._board_version = "fixed-board-j"
        await pub.ensure_indices()
        c = _candidate("pub_test_j1")
        r1 = await pub.publish(c)
        # Now mutate the candidate lock_score and try to re-publish.
        c["lock_score"] = 45.0
        r2 = await pub.publish(c)
        # Because idempotency_key changes when values change but we
        # still write snapshot_version=1, the second insert should
        # collide on (prediction_id, snapshot_version=1) unique index
        # and NOT overwrite the existing snapshot.
        snap = await pub.get_active_snapshot("pub_test_j1")
        assert snap["published_lock_score"] == 88.0, \
            "snapshot was mutated post-publication — CONTRACT VIOLATION"
        # A drift warning should have been logged (idempotency key was
        # different but snapshot_version is the same).
        n = await db[SNAPSHOT_COLLECTION].count_documents(
            {"prediction_id": "pub_test_j1"})
        assert n == 1
        await _wipe(db)
    _run(run())
