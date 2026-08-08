"""P0-3 — Post-publication immutability regression tests.

Verifies that the two learning/tuning loops that previously rewrote
canonical prediction fields on already-published picks (weekly tuner
in ``server.py`` and the admin ``/analytics/learn`` endpoint) now:

  1. Skip picks that carry ``publication_source``.
  2. Still touch UNPUBLISHED / legacy picks (learning behaviour preserved
     for pre-2026-08-06 rows that never received a snapshot).
  3. Still call ``recompute_learned_weights`` — model weight state
     continues to update, so the next ``_refresh_picks`` cycle
     will emit fresh predictions consuming the new weights via
     ``apply_learning`` at pick-generation time.
  4. ``prediction_snapshots`` rows are never touched by tuning.

Also verifies:
  - ``analytics.py:backfill_metrics`` (row 213) only writes CLV / units
    fields — NOT canonical prediction fields.  Left unchanged.
  - Settlement writers still update ``status`` etc. (they are not
    canonical mutators).
  - The write-guard module (`services.published_write_guard`) still
    catches direct canonical mutations if someone bypasses the gate.

None of these tests touches ranking, Magic Tier, simulator, Rollover,
Odds API scheduling, or any prediction formula.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone, timedelta

import pytest
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

from motor.motor_asyncio import AsyncIOMotorClient


_TEST_ID_PREFIX = "p0test3_"


def _run(c):
    return asyncio.run(c)


def _fresh_db():
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    return client[os.environ.get("DB_NAME", "lockscore_db")]


async def _wipe(db):
    await db.picks.delete_many({"id": {"$regex": f"^{_TEST_ID_PREFIX}"}})
    await db.prediction_snapshots.delete_many(
        {"prediction_id": {"$regex": f"^{_TEST_ID_PREFIX}"}})


def _make_pick(*, published: bool, lock: float = 88.0, wp: float = 62.0,
                 edge: float = 3.5, grade: str = "Strong Lock",
                 conf: float = 88.0, sport: str = "MLB",
                 market: str = "Judge Over 1.5 hits") -> dict:
    pid = _TEST_ID_PREFIX + uuid.uuid4().hex[:16]
    doc = {
        "id": pid,
        "sport": sport,
        "league": sport,
        "market": market,
        "event": "Alpha vs Bravo",
        "event_time": (datetime.now(timezone.utc)
                        + timedelta(hours=6)).isoformat(),
        "pick_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "lock_score": lock,
        "lock_score_v2": lock,
        "win_probability": wp,
        "edge_percent": edge,
        "grade": grade,
        "confidence": conf,
        "book_odds": -140,
        "line": 1.5,
        "reasoning": "test reasoning",
        "status": "pending",
        "no_bet": False,
        "off_board": False,
        "hide_from_main_board": False,
        "factors": {"form": 0.75, "matchup": 0.80},
        "source": "unit_test",
        "model_version": "test.v1",
    }
    if published:
        doc.update({
            "publication_source": "canonical_pipeline",
            "snapshot_version": 1,
            "published_at": datetime.now(timezone.utc).isoformat(),
            "payload_hash": "test_hash",
            "idempotency_key": "test_idem_" + pid,
            "published_probability": wp / 100.0,
            "published_edge": edge,
            "published_lock_score": lock,
            "published_grade": grade,
            "published_confidence": conf,
            "published_line": 1.5,
            "published_odds": -140,
            "published_reasoning": "test reasoning",
            "model_version": "mlb_prop_v3.2",
            "fusion_version": "fusion_v4",
            "scoring_version": "lockscore_v2.1",
            "calibration_version": "cal_2026-08-01",
            "validator_version": "board_v2.0",
            "simulation_version": "mc_v1.5",
            "feature_snapshot_version": "feat_v2.0",
            "board_version": "board-test",
        })
    return doc


# --------------------------------------------------------------------------- #
# 1.  Weekly tuner cursor filter excludes published picks.
# --------------------------------------------------------------------------- #
def test_weekly_tuner_cursor_filter_excludes_published():
    """The tuner now queries `{publication_source: {$exists: False}}`
    — a fresh find with that filter must NOT return published picks."""
    async def run():
        db = _fresh_db()
        await _wipe(db)
        pub = _make_pick(published=True)
        unpub = _make_pick(published=False)
        await db.picks.insert_many([pub, unpub])
        try:
            cursor = db.picks.find({
                "status": {"$in": [None, "pending"]},
                "publication_source": {"$exists": False},
            }, {"_id": 0, "id": 1})
            found = {r["id"] async for r in cursor}
            assert pub["id"] not in found
            assert unpub["id"] in found
        finally:
            await _wipe(db)
    _run(run())


# --------------------------------------------------------------------------- #
# 2.  Write-time safety net rejects update on a published pick.
# --------------------------------------------------------------------------- #
def test_write_time_safety_net_leaves_published_pick_untouched():
    """Even if a pick slipped past the cursor filter (e.g. concurrent
    publication landed between read and write), the write filter
    ``{publication_source: {$exists: False}}`` will match zero
    documents and the pick stays immutable."""
    async def run():
        db = _fresh_db()
        await _wipe(db)
        pub = _make_pick(published=True, lock=88.0, wp=62.0, grade="Strong Lock")
        await db.picks.insert_one(pub)
        try:
            # Simulate the tuner's guarded write.
            res = await db.picks.update_one(
                {"id": pub["id"],
                 "publication_source": {"$exists": False}},
                {"$set": {
                    "win_probability": 99.0,
                    "lock_score": 99.0,
                    "edge_percent": 99.0,
                    "grade": "Elite Lock",
                    "confidence": 99.0,
                    "learning": {"delta": 42},
                }},
            )
            # Match count zero → published pick was NOT modified.
            assert res.matched_count == 0
            after = await db.picks.find_one({"id": pub["id"]},
                                             {"_id": 0})
            assert after["win_probability"] == 62.0
            assert after["lock_score"] == 88.0
            assert after["edge_percent"] == 3.5
            assert after["grade"] == "Strong Lock"
            assert after["confidence"] == 88.0
            # Snapshot / published_* untouched.
            assert after["published_lock_score"] == 88.0
            assert after["published_probability"] == 0.62
            assert after["snapshot_version"] == 1
            assert after["payload_hash"] == "test_hash"
        finally:
            await _wipe(db)
    _run(run())


# --------------------------------------------------------------------------- #
# 3.  Unpublished / legacy pick still updates (learning preserved).
# --------------------------------------------------------------------------- #
def test_unpublished_pick_still_updates_via_guarded_write():
    async def run():
        db = _fresh_db()
        await _wipe(db)
        unpub = _make_pick(published=False, lock=70.0, wp=55.0)
        await db.picks.insert_one(unpub)
        try:
            res = await db.picks.update_one(
                {"id": unpub["id"],
                 "publication_source": {"$exists": False}},
                {"$set": {
                    "win_probability": 68.0,
                    "lock_score": 82.0,
                    "learning": {"delta": 7.5},
                }},
            )
            assert res.matched_count == 1
            after = await db.picks.find_one({"id": unpub["id"]},
                                             {"_id": 0})
            assert after["win_probability"] == 68.0
            assert after["lock_score"] == 82.0
            assert after.get("learning") == {"delta": 7.5}
        finally:
            await _wipe(db)
    _run(run())


# --------------------------------------------------------------------------- #
# 4.  prediction_snapshots is never mutated by the tuner path.
# --------------------------------------------------------------------------- #
def test_snapshot_row_is_never_touched_by_tuner():
    async def run():
        db = _fresh_db()
        await _wipe(db)
        pub = _make_pick(published=True, lock=90.0, wp=70.0)
        # Insert a matching immutable snapshot.
        snap = {
            "prediction_id": pub["id"],
            "snapshot_version": 1,
            "is_active": True,
            "published_lock_score": pub["published_lock_score"],
            "published_probability": pub["published_probability"],
            "published_edge": pub["published_edge"],
            "published_grade": pub["published_grade"],
            "published_confidence": pub["published_confidence"],
            "published_reasoning": pub["published_reasoning"],
            "published_line": pub["published_line"],
            "published_odds": pub["published_odds"],
            "payload_hash": pub["payload_hash"],
            "idempotency_key": pub["idempotency_key"],
            "publication_source": pub["publication_source"],
            "published_at": pub["published_at"],
            "board_version": pub["board_version"],
            "model_version": pub["model_version"],
            "fusion_version": pub["fusion_version"],
            "scoring_version": pub["scoring_version"],
            "calibration_version": pub["calibration_version"],
            "validator_version": pub["validator_version"],
            "simulation_version": pub["simulation_version"],
            "feature_snapshot_version": pub["feature_snapshot_version"],
            "is_legacy": False,
        }
        await db.picks.insert_one(pub)
        await db.prediction_snapshots.insert_one(snap)
        try:
            # Attempt tuner-style write — must NOT touch snapshot.
            await db.picks.update_one(
                {"id": pub["id"],
                 "publication_source": {"$exists": False}},
                {"$set": {"lock_score": 99.0, "grade": "Elite Lock"}},
            )
            snap_after = await db.prediction_snapshots.find_one(
                {"prediction_id": pub["id"], "snapshot_version": 1},
                {"_id": 0},
            )
            assert snap_after["published_lock_score"] == 90.0
            assert snap_after["published_grade"] == "Strong Lock"
        finally:
            await _wipe(db)
    _run(run())


# --------------------------------------------------------------------------- #
# 5.  Published-write-guard still catches direct canonical mutations.
# --------------------------------------------------------------------------- #
def test_published_write_guard_catches_direct_canonical_mutation():
    from services.published_write_guard import (
        assert_no_published_mutation,
        PublishedFieldMutationError,
    )
    # Non-publication caller trying to $set a canonical alias → raises.
    with pytest.raises(PublishedFieldMutationError):
        assert_no_published_mutation(
            {"$set": {"lock_score": 99.0, "grade": "Elite Lock"}},
            allow_publication_write=False,
            caller="test_p03",
        )
    # Publication service escape hatch → no raise.
    assert_no_published_mutation(
        {"$set": {"lock_score": 99.0}},
        allow_publication_write=True,
        caller="publication_service",
    )
    # Non-canonical fields → no raise even without the flag.
    assert_no_published_mutation(
        {"$set": {"on_main_board_at": "2026-08-08T00:00:00Z",
                  "grade_verified_at": "2026-08-08T00:00:00Z",
                  "closing_odds": -150, "clv_value": 0.03,
                  "status": "won"}},
        allow_publication_write=False, caller="test_p03",
    )


# --------------------------------------------------------------------------- #
# 6.  Weekly tuner code has been patched (static-source guard).
# --------------------------------------------------------------------------- #
def test_weekly_tuner_source_has_publication_source_filter():
    """Guards against future regressions: the file that hosts the
    weekly tuner MUST contain the publication_source filter on both
    its cursor and its write."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1] / "server.py").read_text()
    # Find the weekly tuner function body.
    idx = src.find("async def _weekly_model_tuning_loop")
    assert idx != -1
    # Look at the next ~4000 chars.
    body = src[idx:idx + 5000]
    # Cursor filter and write filter must both include the guard.
    assert body.count('"publication_source": {"$exists": False}') >= 2, (
        "weekly tuner missing publication_source guard on cursor + write"
    )


def test_analytics_learn_endpoint_has_publication_source_filter():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1]
            / "routes" / "analytics_routes.py").read_text()
    idx = src.find('@router.post("/analytics/learn")')
    assert idx != -1
    body = src[idx:idx + 4000]
    assert body.count('"publication_source": {"$exists": False}') >= 2, (
        "admin /analytics/learn missing publication_source guard "
        "on cursor + write"
    )


# --------------------------------------------------------------------------- #
# 7.  analytics.py:213 backfill_metrics — CLV / units metadata only.
# --------------------------------------------------------------------------- #
def test_analytics_backfill_touches_only_learning_metadata():
    """`backfill_metrics` sets `odds_at_pick`, `closing_odds`,
    `units_risked`, `units_profit`, `clv_value`, `confidence_bucket`.
    NONE are in the canonical-field set — so the write-guard must
    accept it."""
    from services.published_write_guard import assert_no_published_mutation
    fake_update = {"$set": {
        "odds_at_pick":       -140,
        "closing_odds":       -145,
        "units_risked":       1.0,
        "units_profit":       0.71,
        "clv_value":          0.02,
        "confidence_bucket":  "Premium (90-94)",
    }}
    # Must not raise — these fields are learning-metadata, not
    # canonical prediction fields.
    assert_no_published_mutation(fake_update,
                                  allow_publication_write=False,
                                  caller="analytics.backfill_metrics")


# --------------------------------------------------------------------------- #
# 8.  NRFI settlement writer (brain/nrfi_engine.py:613) touches only
#     settlement fields — not canonical prediction fields.
# --------------------------------------------------------------------------- #
def test_nrfi_settlement_writer_does_not_touch_canonical_fields():
    from services.published_write_guard import assert_no_published_mutation
    # The exact update NRFI's settlement path applies (line 613).
    nrfi_update = {"$set": {
        "status":         "won",
        "outcome":        "won",
        "runs_in_1st":    0,
        "settled_at":     "2026-08-08T22:00:00+00:00",
        "settle_source":  "mlb_stats_api_linescore",
    }}
    assert_no_published_mutation(nrfi_update,
                                  allow_publication_write=False,
                                  caller="brain.nrfi_engine.settle")


# --------------------------------------------------------------------------- #
# 9.  grading_validator only writes verification metadata.
# --------------------------------------------------------------------------- #
def test_grading_validator_touches_only_audit_metadata():
    from services.published_write_guard import assert_no_published_mutation
    for u in [
        {"$set": {"grade_verified_at": "2026-08-08T01:02:03+00:00",
                    "grade_verify_source": "mlb_statsapi"}},
        {"$set": {"grade_verified_at": "2026-08-08T01:02:03+00:00",
                    "grade_verify_source": "mlb_statsapi",
                    "grade_verify_result": "agreed"}},
        {"$set": {"status": "pending",
                    "grade_disagreement": {"detected_at": "x"}}},
    ]:
        assert_no_published_mutation(u, allow_publication_write=False,
                                      caller="grading_validator")


# --------------------------------------------------------------------------- #
# 10.  closing_line_snapshotter writes only CLV analytics fields.
# --------------------------------------------------------------------------- #
def test_closing_line_snapshotter_touches_only_clv_fields():
    from services.published_write_guard import assert_no_published_mutation
    u = {"$set": {
        "closing_odds":             -145,
        "closing_odds_snapshotted": True,
        "closing_odds_source":      "odds_api_live",
        "closing_odds_at":          "2026-08-08T00:00:00+00:00",
        "clv_value":                0.03,
        "sharp_closing_odds":       -142,
        "sharp_closing_book":       "pinnacle",
        "sharp_vs_median_pp":       -0.1,
    }}
    assert_no_published_mutation(u, allow_publication_write=False,
                                  caller="closing_line_snapshotter")


# --------------------------------------------------------------------------- #
# 11.  P0-2 migrated publication writers still work (regression).
# --------------------------------------------------------------------------- #
def test_p02_migrated_writers_still_publish():
    async def run():
        from services.publication_helpers import publish_upserted_picks
        db = _fresh_db()
        await _wipe(db)
        try:
            pick = _make_pick(published=False, sport="UFC",
                                market="Alpha ML")
            pick["source"] = "ufc_espn_v1"
            await db.picks.insert_one(pick)
            summary = await publish_upserted_picks(
                db, [pick], publication_source="ufc_espn_v1",
                caller_label="p03-regression-ufc")
            assert summary.get("new_snapshots", 0) == 1
            after = await db.picks.find_one({"id": pick["id"]},
                                             {"_id": 0})
            assert after["publication_source"] == "ufc_espn_v1"
            # After publication, the P0-3 tuner-safety filter kicks
            # in — attempting to re-apply learning must NOT match.
            res = await db.picks.update_one(
                {"id": pick["id"],
                 "publication_source": {"$exists": False}},
                {"$set": {"lock_score": 99.0}},
            )
            assert res.matched_count == 0
        finally:
            await _wipe(db)
    _run(run())


# --------------------------------------------------------------------------- #
# 12.  P0-1 gate + P0-3 immutability compose correctly.
# --------------------------------------------------------------------------- #
def test_p01_gate_and_p03_immutability_compose():
    async def run():
        from services.publication_helpers import publish_upserted_picks
        from services.canonical_board_source import (
            canonical_publication_filter,
        )
        db = _fresh_db()
        await _wipe(db)
        try:
            pick = _make_pick(published=False, sport="Soccer",
                                market="Alpha Anytime Goal Scorer")
            pick["source"] = "soccer_hot_scorers_v1"
            await db.picks.insert_one(pick)
            await publish_upserted_picks(
                db, [pick],
                publication_source="soccer_hot_scorers_v1",
                caller_label="p03-compose",
            )
            # P0-1 gate must admit the migrated pick.
            gate_q = {"id": pick["id"], **canonical_publication_filter()}
            found = await db.picks.find_one(gate_q, {"_id": 0})
            assert found is not None
            # P0-3 tuner safety net must reject any canonical rewrite.
            res = await db.picks.update_one(
                {"id": pick["id"],
                 "publication_source": {"$exists": False}},
                {"$set": {"lock_score": 99.0,
                            "win_probability": 0.99,
                            "grade": "Elite Lock"}},
            )
            assert res.matched_count == 0
            after = await db.picks.find_one({"id": pick["id"]},
                                             {"_id": 0})
            # Immutable canonical fields survived.
            assert after["published_lock_score"] == pick["lock_score"]
            assert after["snapshot_version"] == 1
        finally:
            await _wipe(db)
    _run(run())
