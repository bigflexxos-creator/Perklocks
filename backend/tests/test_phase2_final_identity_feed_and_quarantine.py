"""Phase 2 Final (2026-08-11) — live identity feed + old-pick quarantine.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import uuid
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

from motor.motor_asyncio import AsyncIOMotorClient


_BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _db():
    return AsyncIOMotorClient(os.environ["MONGO_URL"])[
        os.environ.get("DB_NAME", "lockscore_db")]


def _run(coro):
    return asyncio.run(coro)


# ── 1. mls_scorer_gate propagates observations to player_identity ──
def test_apply_espn_snapshot_populates_identity_registry():
    from services import mls_scorer_gate
    from services.player_identity import (
        resolve_player, reset_registry_for_tests,
    )
    reset_registry_for_tests()
    snap = {
        "lionel messi": {
            "display_name": "Lionel Messi",
            "team": "Inter Miami CF",
            "espn_id": "8443",
            "position": "F",
        },
        "denis bouanga": {
            "display_name": "Denis Bouanga",
            "team": "LAFC",
            "espn_id": "12345",
        },
    }
    names = set(snap.keys())
    mls_scorer_gate.apply_espn_snapshot(snap, names)
    messi = resolve_player(name="Lionel Messi", sport="Soccer", league="MLS")
    assert messi is not None
    assert messi.current_team == "Inter Miami CF"
    assert messi.source == "espn_mls_leaders"
    assert messi.provider_ids.get("espn") == "8443"
    assert messi.is_current_team_fresh(staleness_days=1) is True


def test_espn_by_name_alias_populated_after_snapshot():
    """Callers read `_espn_by_name` directly — must be populated after
    every snapshot."""
    from services import mls_scorer_gate
    snap = {"messi": {"display_name": "Messi", "team": "Inter Miami"}}
    mls_scorer_gate.apply_espn_snapshot(snap, set(snap.keys()))
    got = getattr(mls_scorer_gate, "_espn_by_name", None)
    assert got is not None
    assert "messi" in got


# ── 2. Persist + hydrate round-trip ─────────────────────────────────
def test_persist_and_hydrate_round_trip_from_mongo():
    from services.player_identity import (
        upsert_player, persist_registry, hydrate_registry_from_mongo,
        resolve_player, reset_registry_for_tests, IDENTITY_COLLECTION,
    )

    async def go():
        db = _db()
        # Isolate this test with a stable canonical player.
        await db[IDENTITY_COLLECTION].delete_many(
            {"canonical_player_id": {"$regex": "^cpid_"}, "sport": "Soccer",
             "league": "TestLeague"})
        reset_registry_for_tests()
        upsert_player(
            name="Test Persistent Player", sport="Soccer",
            league="TestLeague", provider="espn", provider_id="TEST-42",
            current_team="Persistent FC",
            observed_at=datetime.now(timezone.utc).isoformat(),
            source="unit_test",
        )
        n = await persist_registry(db)
        assert n >= 1
        # Simulate a restart.
        reset_registry_for_tests()
        loaded = await hydrate_registry_from_mongo(db)
        assert loaded >= 1
        r = resolve_player(name="Test Persistent Player", sport="Soccer",
                           league="TestLeague")
        assert r is not None
        assert r.current_team == "Persistent FC"
        # Cleanup
        await db[IDENTITY_COLLECTION].delete_many(
            {"canonical_player_id": r.canonical_player_id})
    _run(go())


def test_freshness_survives_persistence():
    """observed_at is stored as ISO string on the Mongo doc so it
    survives serialization + hydration."""
    from services.player_identity import (
        upsert_player, persist_registry, hydrate_registry_from_mongo,
        resolve_player, reset_registry_for_tests, IDENTITY_COLLECTION,
    )
    now_iso = datetime.now(timezone.utc).isoformat()

    async def go():
        db = _db()
        reset_registry_for_tests()
        upsert_player(
            name="Fresh Player", sport="Soccer", league="TestFreshLeague",
            provider="espn", provider_id="FRESH-1",
            current_team="Fresh FC",
            observed_at=now_iso,
        )
        await persist_registry(db)
        reset_registry_for_tests()
        await hydrate_registry_from_mongo(db)
        r = resolve_player(name="Fresh Player", sport="Soccer",
                           league="TestFreshLeague")
        assert r is not None
        assert r.observed_at == now_iso
        # Fresh within 30 days.
        assert r.is_current_team_fresh(staleness_days=30) is True
        await db[IDENTITY_COLLECTION].delete_many(
            {"canonical_player_id": r.canonical_player_id})
    _run(go())


def test_has_fresh_roster_for_league_flag():
    """When the identity registry has no fresh entry for a league,
    `has_fresh_roster_for_league` returns False — callers can gate
    publication on this."""
    from services.player_identity import (
        upsert_player, persist_registry, reset_registry_for_tests,
        has_fresh_roster_for_league, IDENTITY_COLLECTION,
    )

    async def go():
        db = _db()
        reset_registry_for_tests()
        assert (await has_fresh_roster_for_league(db, "PhantomLeague")) is False
        upsert_player(
            name="X", sport="Soccer", league="PhantomLeague",
            provider="p", provider_id="1", current_team="X FC",
            observed_at=datetime.now(timezone.utc).isoformat(),
        )
        await persist_registry(db)
        assert (await has_fresh_roster_for_league(db, "PhantomLeague")) is True
        # Old observation → stale.
        old = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
        upsert_player(
            name="Y", sport="Soccer", league="AncientLeague",
            provider="p", provider_id="2", current_team="Y FC",
            observed_at=old,
        )
        await persist_registry(db)
        assert (await has_fresh_roster_for_league(
            db, "AncientLeague", staleness_days=30)) is False
        await db[IDENTITY_COLLECTION].delete_many(
            {"league": {"$in": ["PhantomLeague", "AncientLeague"]}})
    _run(go())


# ── 3. Old already-published invalid pick — quarantine flow ─────────
def test_quarantine_marks_offboard_without_deleting_snapshot():
    from scripts.quarantine_invalid_soccer_player_props import run

    async def go():
        db = _db()
        pid = "phase2fin_" + uuid.uuid4().hex[:12]
        pick = {
            "id": pid, "sport": "Soccer", "league": "TestQuarantineLeague",
            "event": "HJK vs Inter Turku",
            "market": "Victor Lind - Anytime Goal Scorer",
            "player_name": "Victor Lind",
            "player_current_team": "Ilves",   # NOT on the fixture
            "event_time": (datetime.now(timezone.utc)
                            + timedelta(hours=6)).isoformat(),
            "win_probability": 62.0, "lock_score": 92.0,
            "published_lock_score": 92.0,
            "publication_source": "canonical_pipeline",
            "grade": "Elite Lock", "confidence": "Very High",
            "book_odds": None, "edge_percent": None,
            "pick_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "settled": False,
        }
        snap = {"prediction_id": pid, "sport": "Soccer",
                "published_lock_score": 92.0, "is_active": True}
        await db.picks.delete_many({"id": pid})
        await db.prediction_snapshots.delete_many({"prediction_id": pid})
        await db.picks.insert_one(pick)
        await db.prediction_snapshots.insert_one(snap)
        try:
            stats = await run(db, apply=True)
            assert stats["scanned"] >= 1
            # Snapshot must still exist (audit trail preserved).
            surviving = await db.prediction_snapshots.find_one(
                {"prediction_id": pid}, {"_id": 0})
            assert surviving is not None
            # Pick doc off_board=True + tagged.
            after = await db.picks.find_one({"id": pid}, {"_id": 0})
            assert after["off_board"] is True
            assert after["player_team_invalid"] is True
            assert "quarantine_applied_at" in after
        finally:
            await db.picks.delete_many({"id": pid})
            await db.prediction_snapshots.delete_many({"prediction_id": pid})
    _run(go())


def test_quarantine_never_touches_settled_history():
    from scripts.quarantine_invalid_soccer_player_props import run

    async def go():
        db = _db()
        pid = "phase2fin_settled_" + uuid.uuid4().hex[:12]
        pick = {
            "id": pid, "sport": "Soccer", "league": "TestSettled",
            "event": "HJK vs Inter Turku",
            "market": "Historical Player - Anytime Goal Scorer",
            "player_name": "Historical Player",
            "player_current_team": "Some Old Club",
            "event_time": (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat(),
            "win_probability": 62.0, "lock_score": 92.0,
            "settled": True,        # settled = untouchable
            "no_bet": False,
            "pick_date": (datetime.now(timezone.utc)
                           - timedelta(days=7)).strftime("%Y-%m-%d"),
        }
        await db.picks.delete_many({"id": pid})
        await db.picks.insert_one(pick)
        try:
            before_off_board = pick.get("off_board")
            await run(db, apply=True)
            after = await db.picks.find_one({"id": pid}, {"_id": 0})
            # Settled — off_board flag must not have been set by the script.
            assert after.get("off_board") == before_off_board
            assert after.get("player_team_invalid") is None
        finally:
            await db.picks.delete_many({"id": pid})
    _run(go())


def test_quarantine_dry_run_writes_nothing():
    from scripts.quarantine_invalid_soccer_player_props import run

    async def go():
        db = _db()
        pid = "phase2fin_dry_" + uuid.uuid4().hex[:12]
        pick = {
            "id": pid, "sport": "Soccer", "league": "TestDry",
            "event": "HJK vs Inter Turku",
            "market": "Victor Lind - Anytime Goal Scorer",
            "player_name": "Victor Lind",
            "player_current_team": "Ilves",
            "event_time": (datetime.now(timezone.utc)
                            + timedelta(hours=6)).isoformat(),
            "win_probability": 62.0, "lock_score": 92.0,
            "publication_source": "canonical_pipeline",
            "settled": False, "no_bet": False,
            "pick_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }
        await db.picks.delete_many({"id": pid})
        await db.picks.insert_one(pick)
        try:
            stats = await run(db, apply=False)
            assert stats["quarantined_writes"] == 0
            after = await db.picks.find_one({"id": pid}, {"_id": 0})
            assert after.get("off_board") is not True
        finally:
            await db.picks.delete_many({"id": pid})
    _run(go())


# ── 4. Regression — >85 gate + P0-4 intact ─────────────────────────
def test_locks_gate_still_strict_gt_85():
    from services.main_board_eligibility import is_main_board_eligible
    assert is_main_board_eligible({"lock_score": 85.0}) is False
    assert is_main_board_eligible({"lock_score": 85.001}) is True


def test_startup_hydrates_identity_from_mongo_source_marker():
    """Server startup must call `hydrate_registry_from_mongo` so a
    restart / replica gets the freshest observations."""
    src = (_BACKEND_ROOT / "server.py").read_text()
    assert "hydrate_registry_from_mongo" in src
    assert "Phase 2 Final" in src
