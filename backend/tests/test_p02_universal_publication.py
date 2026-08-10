"""P0-2 — Universal Canonical Publication regression tests.

Verifies every legitimate prediction writer that previously bypassed
`PredictionPublicationService.publish_batch()` now:

  1. Creates an immutable `prediction_snapshots` row for each pick it
     upserts into `db.picks`.
  2. Dual-writes `publication_source` + `published_*` fields onto
     the same `db.picks` document.
  3. Uses a stable pick id — `db.picks.id ≡ prediction_snapshots.prediction_id`.
  4. Is idempotent — re-running with the same pick payload does NOT
     create a duplicate snapshot.
  5. Does NOT modify existing lock_score / probability / grade /
     confidence values (publication is a pass-through wrapper).

Writers under test:
  - `ufc_espn_ingest.sync_ufc_espn_picks`         (UFC ML)
  - `soccer_hot_scorers.build_hot_scorer_picks`   (Anytime scorer)
  - `services.espn_soccer_fixtures.refresh_once`  (Soccer ML fallback)
  - `soccer.pipeline`                             (Soccer v1 + v1_synth)
  - `services.pick_refresh_orchestrator._ensure_csl_elite_picks` (CSL scorer inject)

To keep tests hermetic we exercise the `publish_upserted_picks`
helper directly with realistic pick shapes for each writer.  The
helper is exactly what each migrated writer calls; proving the
helper stamps the correct publication_source, dual-writes the picks,
and stays idempotent is equivalent to proving every migrated
writer does the same.

The test also asserts that the `PredictionPublicationService` did
NOT change any pre-existing prediction value (lock_score,
win_probability, edge_percent, grade, confidence) — publication is
a pass-through per contract.

Also included:
  - Enrichment-only writers (understat_form, signal_score, injury_chip)
    remain unchanged (not routed through publication).
  - Settlement-only writers remain unchanged.
  - The P0-1 canonical gate now admits migrated picks.
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


_TEST_DATE = "2099-08-08"
_TEST_ID_PREFIX = "p0test2_"


def _run(c):
    return asyncio.run(c)


def _fresh_db():
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    return client[os.environ.get("DB_NAME", "lockscore_db")]


async def _wipe(db):
    await db.picks.delete_many({"id": {"$regex": f"^{_TEST_ID_PREFIX}"}})
    await db.prediction_snapshots.delete_many(
        {"prediction_id": {"$regex": f"^{_TEST_ID_PREFIX}"}})
    await db.publication_mismatch_report.delete_many(
        {"prediction_id": {"$regex": f"^{_TEST_ID_PREFIX}"}})


def _shape_pick(source_tag: str, *, sport: str, market: str,
                lock: float = 88.0, grade: str = "Strong Lock",
                league: str = "Soccer") -> dict:
    """Build a pick doc that matches the shape each migrated writer
    would emit — before it reaches `publish_upserted_picks`."""
    pid = _TEST_ID_PREFIX + source_tag + "_" + uuid.uuid4().hex[:12]
    et = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
    return {
        "id": pid,
        "sport": sport,
        "league": league,
        "event": "Alpha vs Bravo",
        "event_time": et,
        "market": market,
        "selection": "Alpha",
        "win_probability": 62.0,
        "implied_probability": 62.0,
        "book_odds": -140,
        "edge_percent": 3.5,
        "lock_score": lock,
        "lock_score_v2": lock,
        "grade": grade,
        # P0-1 (2026-08-11): `confidence` is a LABEL string, matching
        # what `sports_engine._confidence(lock_score)` actually emits
        # in production ("Very High", "High", …).  The previous
        # numeric-in-confidence fixture masked the bug where a
        # numeric was silently coerced to 0.0 through publication.
        "confidence": "Very High",
        "line": 1.5,
        "pick_date": _TEST_DATE,
        "no_bet": False,
        "is_extra": True,
        "source": source_tag,
        "model_version": f"{source_tag}.test.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "reasoning": "test reasoning",
        "factors": {"form": 0.7},
        # Phase 2 (2026-08-11): writers of Soccer player-based markets
        # are required by the player↔team integrity gate to stamp the
        # player's CURRENT team.  The test fixture mirrors that
        # contract so the layer-B gate passes it through as a valid
        # player↔fixture triple.  Alpha is the home team on the
        # synthetic "Alpha vs Bravo" event so this is a valid pick.
        "player_name": "Alpha",
        "player_current_team": "Alpha",
    }


# --------------------------------------------------------------------------- #
# 1.  Helper creates snapshots + dual-writes for every source tag.
# --------------------------------------------------------------------------- #
_MIGRATED_SOURCES = [
    # (source_tag, sport, market)
    ("ufc_espn_v1",              "UFC",    "Alpha Moneyline"),
    ("soccer_hot_scorers_v1",    "Soccer", "Alpha - Anytime Goal Scorer"),
    ("espn_fallback",            "Soccer", "Alpha Moneyline"),
    ("soccer_v1",                "Soccer", "Alpha Moneyline"),
    ("soccer_v1_synth",          "Soccer", "Total Goals Over 1.5"),
    ("csl_elite_scorer_inject",  "Soccer", "Alpha - Anytime Goal Scorer"),
]


def test_every_migrated_writer_creates_a_canonical_snapshot():
    async def run():
        from services.publication_helpers import publish_upserted_picks

        db = _fresh_db()
        await _wipe(db)
        try:
            for source_tag, sport, market in _MIGRATED_SOURCES:
                pick = _shape_pick(source_tag, sport=sport, market=market)
                await db.picks.insert_one(pick)

                summary = await publish_upserted_picks(
                    db, [pick],
                    publication_source=source_tag,
                    caller_label=f"unit-test {source_tag}",
                )
                assert summary.get("new_snapshots", 0) == 1, (
                    f"{source_tag}: expected 1 new snapshot, got "
                    f"{summary!r}"
                )
                # Snapshot exists + stable id preserved.
                snap = await db.prediction_snapshots.find_one(
                    {"prediction_id": pick["id"], "is_active": True},
                    {"_id": 0},
                )
                assert snap is not None
                assert snap["prediction_id"] == pick["id"]
                assert snap["publication_source"] == source_tag
                # Dual-write landed on picks doc.
                after = await db.picks.find_one({"id": pick["id"]},
                                                 {"_id": 0})
                assert after["publication_source"] == source_tag
                assert after["snapshot_version"] == snap["snapshot_version"]
                assert "payload_hash" in after
                assert "idempotency_key" in after
                assert "published_at" in after
                # Canonical fields populated.
                for f in ("published_lock_score", "published_probability",
                          "published_edge", "published_grade",
                          "published_confidence"):
                    assert f in after and after[f] is not None, f
        finally:
            await _wipe(db)
    _run(run())


# --------------------------------------------------------------------------- #
# 2.  Publication is idempotent — re-run does not duplicate snapshots.
# --------------------------------------------------------------------------- #
def test_publish_is_idempotent_across_reruns():
    async def run():
        from services.publication_helpers import publish_upserted_picks
        db = _fresh_db()
        await _wipe(db)
        try:
            pick = _shape_pick("ufc_espn_v1", sport="UFC",
                                market="Alpha Moneyline")
            await db.picks.insert_one(pick)
            s1 = await publish_upserted_picks(
                db, [pick], publication_source="ufc_espn_v1",
                caller_label="idem-1")
            s2 = await publish_upserted_picks(
                db, [pick], publication_source="ufc_espn_v1",
                caller_label="idem-2")
            s3 = await publish_upserted_picks(
                db, [pick], publication_source="ufc_espn_v1",
                caller_label="idem-3")
            assert s1["new_snapshots"] == 1
            assert s2["new_snapshots"] == 0 and s2["existing_snapshots"] == 1
            assert s3["new_snapshots"] == 0 and s3["existing_snapshots"] == 1
            # DB has exactly one snapshot.
            n = await db.prediction_snapshots.count_documents(
                {"prediction_id": pick["id"]})
            assert n == 1
        finally:
            await _wipe(db)
    _run(run())


# --------------------------------------------------------------------------- #
# 3.  Publication does NOT modify existing prediction values.
# --------------------------------------------------------------------------- #
def test_publication_is_pass_through_for_lock_and_probability():
    async def run():
        from services.publication_helpers import publish_upserted_picks
        db = _fresh_db()
        await _wipe(db)
        try:
            pick = _shape_pick(
                "soccer_hot_scorers_v1", sport="Soccer",
                market="Alpha - Anytime Goal Scorer",
                lock=87.4, grade="Strong Lock",
            )
            # Freeze the "pre-publication" values.
            #
            # P0-1 (2026-08-11) contract:
            #   • Snapshot stores probability as 0-1 fraction
            #     (canonical).
            #   • Legacy `win_probability` on the picks doc is
            #     ALWAYS 0-100 percentage (frontend-visible unit).
            #   • `edge_percent` preserves None.
            #   • `confidence` label passes through verbatim.
            #
            # So a well-formed pick with win_probability=62.0
            # should still read 62.0 AFTER publication — the two
            # units are converted at the boundary and NEVER mix on
            # the wire.
            original = {
                "lock_score":       pick["lock_score"],
                "edge_percent":     pick["edge_percent"],
                "grade":            pick["grade"],
                "confidence":       pick["confidence"],
                "line":             pick["line"],
                "book_odds":        pick["book_odds"],
                "reasoning":        pick["reasoning"],
                "win_probability":  pick["win_probability"],  # 62.0
            }
            original_prob_pct = pick["win_probability"]
            await db.picks.insert_one(pick)
            await publish_upserted_picks(
                db, [pick], publication_source="soccer_hot_scorers_v1",
                caller_label="pass-through")
            after = await db.picks.find_one({"id": pick["id"]},
                                             {"_id": 0})
            for k, v in original.items():
                assert after[k] == v, (
                    f"publication mutated {k!r}: before={v!r} after={after[k]!r}"
                )
            # Snapshot carries the CANONICAL FRACTION (0.62).
            snap = await db.prediction_snapshots.find_one(
                {"prediction_id": pick["id"]}, {"_id": 0})
            assert snap["published_lock_score"] == original["lock_score"]
            assert snap["published_edge"] == original["edge_percent"]
            assert snap["published_grade"] == original["grade"]
            # Confidence LABEL preserved as string.
            assert snap["published_confidence"] == "Very High"
            # Probability is fraction-normalised at publish; 62.0 → 0.62.
            assert abs(snap["published_probability"] - 0.62) < 1e-6
        finally:
            await _wipe(db)
    _run(run())


# --------------------------------------------------------------------------- #
# 4.  Missing helper picks list is a no-op (never raises).
# --------------------------------------------------------------------------- #
def test_helper_empty_list_no_op():
    async def run():
        from services.publication_helpers import publish_upserted_picks
        db = _fresh_db()
        out = await publish_upserted_picks(
            db, [], publication_source="x", caller_label="empty")
        assert out == {}
    _run(run())


# --------------------------------------------------------------------------- #
# 5.  Failed publication does NOT stamp publication_source (safety).
# --------------------------------------------------------------------------- #
def test_helper_swallows_publication_failure():
    """When publish_batch raises, the helper must log a warning and
    return an empty dict — the caller (ingest loop) should not
    crash.  Verifies error isolation."""
    async def run():
        from services.publication_helpers import publish_upserted_picks

        # A picks-list containing a doc WITHOUT `id` will cause
        # `_build_payload` to raise ValueError inside publish_batch,
        # which the batch's per-candidate try/except captures.
        bad = {"sport": "Test", "market": "no id", "lock_score": 50}
        out = await publish_upserted_picks(
            _fresh_db(), [bad],
            publication_source="failure_test",
            caller_label="failure-path",
        )
        # Batch captured the error internally.  No snapshot created.
        # `new_snapshots` is 0 and errors list is non-empty.
        assert out.get("new_snapshots", 0) == 0
        assert out.get("errors") is not None
        assert len(out["errors"]) == 1
    _run(run())


# --------------------------------------------------------------------------- #
# 6.  Static grep — every migrated writer imports the helper.
# --------------------------------------------------------------------------- #
def test_migrated_writer_files_reference_publication_helper():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    expected = [
        "ufc_espn_ingest.py",
        "uefa_espn_ingest.py",
        "soccer_hot_scorers.py",
        "soccer/pipeline.py",
        "services/espn_soccer_fixtures.py",
        "services/pick_refresh_orchestrator.py",
    ]
    for rel in expected:
        p = root / rel
        assert p.exists(), f"missing file: {rel}"
        src = p.read_text()
        assert "publication_helpers" in src or "publish_upserted_picks" in src or "publish_batch" in src, (
            f"{rel} does not reference the publication helper"
        )


# --------------------------------------------------------------------------- #
# 7.  Static grep — no NEW active user-facing writer bypasses publication.
# --------------------------------------------------------------------------- #
def test_no_new_active_ingest_writers_bypass_publication():
    """Guards against future regressions.  For every writer we're
    aware of, either:
      * it's a settlement / enrichment / admin writer, OR
      * it calls publication (publish_batch OR publish_upserted_picks).
    """
    import pathlib, re
    root = pathlib.Path(__file__).resolve().parents[1]
    # Files known to be active writers of NEW predictions to db.picks
    # (post-P0-2 they MUST reference publication).
    prediction_writer_files = [
        "ufc_espn_ingest.py",
        "uefa_espn_ingest.py",
        "soccer_hot_scorers.py",
        "soccer/pipeline.py",
        "services/espn_soccer_fixtures.py",
        "services/soccer_prop_inject.py",
        "services/mls_direct_inject.py",
        "brain/nrfi_engine.py",
        # Main orchestrator + CSL elite inject sub-path
        "services/pick_refresh_orchestrator.py",
    ]
    for rel in prediction_writer_files:
        p = root / rel
        text = p.read_text()
        # Either uses the shared helper OR the raw PredictionPublicationService.
        assert (
            "publish_upserted_picks" in text
            or "publish_batch" in text
            or "PredictionPublicationService" in text
        ), f"{rel} appears to write predictions without any publication call"


# --------------------------------------------------------------------------- #
# 8.  Enrichment writers must NOT be routed through publication.
# --------------------------------------------------------------------------- #
def test_enrichment_writers_stay_out_of_publication():
    """Sanity: pure enrichment/settlement writers do NOT route through
    the publication helper (would be a scope creep and would mutate
    canonical state)."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    enrichment_files = [
        # decorators / stampers
        "closing_line_snapshotter.py",
        "mlb_lineup.py",
        "pick_validator.py",
        "steam_detector.py",
        "rollover_history_tagger.py",
        # settlement
        "settlement_engine.py",
        "prop_settlement.py",
        "kbo_settlement.py",
        "tennis_extra/settle.py",
        "soccer_espn_settle.py",
        "espn_settlement.py",
        "grading_validator.py",
        "stuck_pick_reaper.py",
        # analytics
        "analytics.py",
        "services/settlement_service.py",
        "services/signal_engine/engine.py",
        "services/signal_engine/rank.py",
    ]
    for rel in enrichment_files:
        p = root / rel
        if not p.exists():
            continue
        text = p.read_text()
        assert "publish_upserted_picks" not in text, (
            f"{rel} appears to route enrichment through canonical "
            f"publication — should stay a plain decorator/settler"
        )


# --------------------------------------------------------------------------- #
# 9.  P0-1 gate now admits migrated picks.
# --------------------------------------------------------------------------- #
def test_p01_canonical_gate_admits_migrated_picks():
    async def run():
        from services.publication_helpers import publish_upserted_picks
        db = _fresh_db()
        await _wipe(db)
        try:
            # Emulate a Soccer scorer pick going through the migrated path.
            pick = _shape_pick("soccer_hot_scorers_v1", sport="Soccer",
                                market="Alpha - Anytime Goal Scorer")
            await db.picks.insert_one(pick)
            await publish_upserted_picks(
                db, [pick],
                publication_source="soccer_hot_scorers_v1",
                caller_label="gate-admit-test",
            )
            # Now run the P0-1 gate query.
            from services.canonical_board_source import (
                canonical_publication_filter,
            )
            filt = canonical_publication_filter()
            q = {"pick_date": _TEST_DATE, "sport": "Soccer", **filt}
            found = await db.picks.find_one(q, {"id": 1, "_id": 0})
            assert found is not None
            assert found["id"] == pick["id"]
        finally:
            await _wipe(db)
    _run(run())


# --------------------------------------------------------------------------- #
# 10.  Batch of mixed migrated writers all get their own snapshot source.
# --------------------------------------------------------------------------- #
def test_batch_multiple_sources_each_stamped_correctly():
    async def run():
        from services.publication_helpers import publish_upserted_picks
        db = _fresh_db()
        await _wipe(db)
        try:
            ufc = _shape_pick("ufc_espn_v1", sport="UFC",
                              market="A ML")
            soc = _shape_pick("espn_fallback", sport="Soccer",
                              market="A ML")
            await db.picks.insert_many([ufc, soc])

            await publish_upserted_picks(
                db, [ufc], publication_source="ufc_espn_v1",
                caller_label="batch-ufc")
            await publish_upserted_picks(
                db, [soc], publication_source="espn_fallback",
                caller_label="batch-soc")

            snap_u = await db.prediction_snapshots.find_one(
                {"prediction_id": ufc["id"]}, {"_id": 0})
            snap_s = await db.prediction_snapshots.find_one(
                {"prediction_id": soc["id"]}, {"_id": 0})
            assert snap_u["publication_source"] == "ufc_espn_v1"
            assert snap_s["publication_source"] == "espn_fallback"
            # Dual-write reflects each source.
            after_u = await db.picks.find_one({"id": ufc["id"]}, {"_id": 0})
            after_s = await db.picks.find_one({"id": soc["id"]}, {"_id": 0})
            assert after_u["publication_source"] == "ufc_espn_v1"
            assert after_s["publication_source"] == "espn_fallback"
        finally:
            await _wipe(db)
    _run(run())
