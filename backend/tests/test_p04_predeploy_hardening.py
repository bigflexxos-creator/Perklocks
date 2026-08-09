"""P0-4 — Final pre-deploy hardening regression tests.

Two protections:

PART A — Canonical gate deployment safety
─────────────────────────────────────────
The `LOCKSCORE_REQUIRE_CANONICAL_PUBLICATION` env flag now defaults
to OFF.  Deploying code cannot unexpectedly activate the gate.
Production must explicitly opt in.

PART B — K-math reconciler runs BEFORE publish_batch
────────────────────────────────────────────────────
The Over/Under K-math reconciler used to mutate canonical fields on
already-published picks (P0-3 audit).  It now runs INSIDE the
refresh cycle BEFORE the publication step, and `safe_picks` is
re-hydrated from DB between the two so `publish_batch` snapshots
the K-math-corrected final state.
"""
from __future__ import annotations

import importlib
import pathlib


# --------------------------------------------------------------------------- #
# PART A — gate deployment safety
# --------------------------------------------------------------------------- #
def _reload_gate():
    import services.canonical_board_source as m
    importlib.reload(m)
    return m


class TestPartA_GateDefaultOff:
    def test_absent_env_var_leaves_gate_off(self, monkeypatch):
        monkeypatch.delenv("LOCKSCORE_REQUIRE_CANONICAL_PUBLICATION",
                            raising=False)
        m = _reload_gate()
        assert m.is_canonical_publication_required() is False
        assert m.canonical_publication_filter() == {}

    def test_module_constant_default_is_false(self, monkeypatch):
        monkeypatch.delenv("LOCKSCORE_REQUIRE_CANONICAL_PUBLICATION",
                            raising=False)
        m = _reload_gate()
        assert m._DEFAULT_ENABLED is False

    def test_explicit_false_keeps_gate_off(self, monkeypatch):
        monkeypatch.setenv("LOCKSCORE_REQUIRE_CANONICAL_PUBLICATION", "false")
        m = _reload_gate()
        assert m.is_canonical_publication_required() is False

    def test_explicit_true_enables_gate(self, monkeypatch):
        monkeypatch.setenv("LOCKSCORE_REQUIRE_CANONICAL_PUBLICATION", "true")
        m = _reload_gate()
        assert m.is_canonical_publication_required() is True
        assert m.canonical_publication_filter() == {
            "publication_source": {"$exists": True, "$ne": None}
        }

    def test_unknown_env_value_leaves_gate_off(self, monkeypatch):
        # A typo like "ture" or "enabled" must NOT accidentally
        # activate the gate — only the explicit ON set matches.
        for bad in ("ture", "enabled", "yesplz", "1.0", " ", "TRUEISH"):
            monkeypatch.setenv("LOCKSCORE_REQUIRE_CANONICAL_PUBLICATION", bad)
            m = _reload_gate()
            assert m.is_canonical_publication_required() is False, bad

    def test_safe_deploy_sequence_simulated(self, monkeypatch):
        """Step-by-step:  code deployed → env absent → gate OFF;
        after operator sets env=true → gate ON."""
        # 1. Fresh deploy — env absent.
        monkeypatch.delenv("LOCKSCORE_REQUIRE_CANONICAL_PUBLICATION",
                            raising=False)
        m = _reload_gate()
        assert m.is_canonical_publication_required() is False
        # 2. Operator turns it on after canonical verification.
        monkeypatch.setenv("LOCKSCORE_REQUIRE_CANONICAL_PUBLICATION", "true")
        m2 = _reload_gate()
        assert m2.is_canonical_publication_required() is True


# --------------------------------------------------------------------------- #
# PART B — K-math ordering (static-source guards)
# --------------------------------------------------------------------------- #
class TestPartB_KmathOrdering:
    def _orch_src(self) -> str:
        p = (pathlib.Path(__file__).resolve().parents[1]
             / "services" / "pick_refresh_orchestrator.py")
        return p.read_text()

    def test_reconciler_call_appears_before_publish_batch(self):
        src = self._orch_src()
        idx_recon = src.find(
            "_reconcile_player_prop_contradictions(safe_picks, date_str)")
        idx_pub = src.find("publisher.publish_batch(")
        assert idx_recon != -1, (
            "Reconciler call missing from orchestrator source"
        )
        assert idx_pub != -1, (
            "publish_batch call missing from orchestrator source"
        )
        assert idx_recon < idx_pub, (
            f"K-math reconciler must run BEFORE publish_batch — "
            f"recon@{idx_recon} publish@{idx_pub}"
        )

    def test_safe_picks_is_rehydrated_between_reconciler_and_publish(self):
        """After the reconciler mutates DB rows, `safe_picks` must be
        refreshed from DB before publish_batch so the snapshot
        reflects final K-math-corrected values."""
        src = self._orch_src()
        idx_recon = src.find(
            "_reconcile_player_prop_contradictions(safe_picks, date_str)")
        idx_pub = src.find("publisher.publish_batch(")
        window = src[idx_recon:idx_pub]
        # Look for the re-hydration query on db.picks.find keyed by
        # the safe_picks IDs.
        assert 'db.picks.find(' in window and '"$in": _sp_ids' in window, (
            "safe_picks re-hydration missing between reconciler and "
            "publish_batch"
        )

    def test_reconciler_only_called_once_per_refresh(self):
        """A duplicate call would create ambiguity and could re-mutate
        picks after publication."""
        src = self._orch_src()
        n = src.count(
            "_reconcile_player_prop_contradictions(safe_picks, date_str)")
        # Exactly one live call site inside `_refresh_picks_impl`.
        # The `async def _reconcile_player_prop_contradictions(` line
        # is the DEFINITION not a call, and does not match this exact
        # signature-with-arguments string.
        assert n == 1, f"expected exactly one reconciler call, found {n}"

    def test_reconciler_function_body_unchanged(self):
        """Guard against accidental formula edits — the K-math
        winner-selection block must still exist with its exact
        payload-copy fields.
        """
        src = self._orch_src()
        needle = 'update_payload["corrected_by"] = "reconciler_k_math"'
        assert needle in src, (
            "K-math correction payload has been altered — do NOT "
            "modify K-math formulas per P0-4 scope"
        )
        # The specific set of fields the reconciler copies must
        # still include all the canonical values (lock_score, edge,
        # etc.) — this is the SAME formula, just running earlier.
        for field in ("market", "selection", "side", "book_odds",
                      "edge_percent", "lock_score", "grade",
                      "confidence", "probability"):
            assert f'"{field}"' in src, (
                f"K-math payload field {field!r} was removed"
            )


# --------------------------------------------------------------------------- #
# PART B — K-math ordering (functional: in-refresh corrections snapshot)
# --------------------------------------------------------------------------- #
import asyncio
import os
import uuid
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

from motor.motor_asyncio import AsyncIOMotorClient


_TEST_ID_PREFIX = "p0test4_"


def _run(c):
    return asyncio.run(c)


def _fresh_db():
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    return client[os.environ.get("DB_NAME", "lockscore_db")]


async def _wipe(db):
    await db.picks.delete_many({"id": {"$regex": f"^{_TEST_ID_PREFIX}"}})
    await db.prediction_snapshots.delete_many(
        {"prediction_id": {"$regex": f"^{_TEST_ID_PREFIX}"}})


def test_functional_kmath_corrected_pick_snapshot_matches_final_state():
    """Simulate the P0-4 sequence:
      1. Insert a candidate with `lock_score=88` (pre-correction).
      2. Simulate the reconciler correcting `lock_score` to 92.
      3. Re-hydrate from DB.
      4. Publish.
      5. Assert the snapshot carries the CORRECTED value (92), not 88.
    """
    async def run():
        from services.publication_helpers import publish_upserted_picks

        db = _fresh_db()
        await _wipe(db)
        try:
            pid = _TEST_ID_PREFIX + uuid.uuid4().hex[:12]
            et = (datetime.now(timezone.utc)
                  + timedelta(hours=4)).isoformat()
            candidate = {
                "id": pid,
                "sport": "MLB",
                "market": "Player Over 6.5 K",
                "event": "Yankees @ Red Sox",
                "event_time": et,
                "pick_date": datetime.now(timezone.utc)
                                .strftime("%Y-%m-%d"),
                "selection": "Over",
                "lock_score": 88.0,          # pre-correction
                "lock_score_v2": 88.0,
                "win_probability": 60.0,
                "edge_percent": 3.0,
                "grade": "Strong Lock",
                "confidence": 88.0,
                "book_odds": -140,
                "line": 6.5,
                "reasoning": "pre-correction",
                "status": "pending",
                "no_bet": False,
                "off_board": False,
                "hide_from_main_board": False,
                "factors": {"form": 0.7},
                "source": "mlb_k_prop_engine",
                "model_version": "test.v1",
            }
            await db.picks.insert_one(candidate)

            # Simulate K-math reconciler: apply corrected values IN DB.
            await db.picks.update_one(
                {"id": pid},
                {"$set": {
                    "lock_score":     92.0,   # corrected
                    "win_probability": 66.0,
                    "edge_percent":    5.5,
                    "grade":           "Elite Lock",
                    "confidence":      92.0,
                    "reasoning":       "post-correction",
                    "corrected_by":    "reconciler_k_math",
                }},
            )

            # Re-hydrate safe_picks from DB (what the orchestrator now
            # does between reconciler and publish_batch).
            refreshed = await db.picks.find_one({"id": pid}, {"_id": 0})
            assert refreshed["lock_score"] == 92.0

            # Publish.
            summary = await publish_upserted_picks(
                db, [refreshed],
                publication_source="canonical_pipeline",
                caller_label="p0-4 kmath test",
            )
            assert summary.get("new_snapshots", 0) == 1

            snap = await db.prediction_snapshots.find_one(
                {"prediction_id": pid, "is_active": True}, {"_id": 0},
            )
            # Snapshot MUST reflect the corrected values, not the
            # pre-correction ones.
            assert snap["published_lock_score"] == 92.0
            assert abs(snap["published_probability"] - 0.66) < 1e-6
            assert snap["published_edge"] == 5.5
            assert snap["published_grade"] == "Elite Lock"
            # P0-1 (2026-08-11): confidence is a label string post-
            # publication.  A numeric fixture (92.0) stringifies to
            # "92.0" through the payload builder.
            assert snap["published_confidence"] == "92.0"

            # dual-write projection on picks also matches.
            after = await db.picks.find_one({"id": pid}, {"_id": 0})
            assert after["published_lock_score"] == 92.0
            assert after["publication_source"] == "canonical_pipeline"
        finally:
            await _wipe(db)
    _run(run())


def test_functional_kmath_deleted_loser_does_not_get_published():
    """When the reconciler DELETES a losing pick before publication,
    publish_batch running on the re-hydrated list must not emit a
    snapshot for the deleted pick."""
    async def run():
        from services.publication_helpers import publish_upserted_picks

        db = _fresh_db()
        await _wipe(db)
        try:
            winner_id = _TEST_ID_PREFIX + "win_" + uuid.uuid4().hex[:8]
            loser_id  = _TEST_ID_PREFIX + "los_" + uuid.uuid4().hex[:8]
            et = (datetime.now(timezone.utc)
                  + timedelta(hours=4)).isoformat()
            base = {
                "sport": "MLB",
                "event": "Y @ B",
                "event_time": et,
                "pick_date": datetime.now(timezone.utc)
                                .strftime("%Y-%m-%d"),
                "lock_score": 90.0, "win_probability": 65.0,
                "edge_percent": 4.0, "grade": "Strong Lock",
                "confidence": 90.0, "book_odds": -140, "line": 6.5,
                "status": "pending", "no_bet": False,
                "source": "mlb_k_prop_engine",
            }
            winner = {**base, "id": winner_id, "market": "P Over 6.5 K",
                      "selection": "Over"}
            loser  = {**base, "id": loser_id,  "market": "P Under 6.5 K",
                      "selection": "Under"}
            await db.picks.insert_many([winner, loser])
            # Simulate reconciler: delete loser.
            await db.picks.delete_one({"id": loser_id})
            # Re-hydrate.
            safe_picks_in = [winner, loser]   # in-memory pre-recon
            _sp_ids = [p["id"] for p in safe_picks_in]
            fresh = {}
            async for p in db.picks.find({"id": {"$in": _sp_ids}},
                                           {"_id": 0}):
                fresh[p["id"]] = p
            refreshed = [fresh[p["id"]] for p in safe_picks_in
                         if p["id"] in fresh]
            assert len(refreshed) == 1
            assert refreshed[0]["id"] == winner_id

            summary = await publish_upserted_picks(
                db, refreshed,
                publication_source="canonical_pipeline",
                caller_label="p0-4 loser test",
            )
            assert summary.get("new_snapshots", 0) == 1
            assert await db.prediction_snapshots.find_one(
                {"prediction_id": loser_id}) is None
            assert await db.prediction_snapshots.find_one(
                {"prediction_id": winner_id}) is not None
        finally:
            await _wipe(db)
    _run(run())
