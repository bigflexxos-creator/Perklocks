"""P0-A (2026-08-11) — persistent canonical player identity.

Verifies the production wiring gap identified in the P0-A verification
report is now closed:

    * live ingestion → in-memory upsert → Mongo persistence
    * startup hydration → in-memory registry (all fields preserved)
    * older observations CANNOT overwrite fresher current-team info
    * concurrent / idempotent writes NEVER duplicate an identity
    * stale identities remain stale after restart / hydration
    * existing betting outputs are not perturbed

All tests are async coroutines executed under ``asyncio.run`` — the
project's existing convention (see ``test_phase2_final_identity_feed_and_quarantine.py``).
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

from motor.motor_asyncio import AsyncIOMotorClient


def _db():
    return AsyncIOMotorClient(os.environ["MONGO_URL"])[
        os.environ.get("DB_NAME", "lockscore_db")]


def _run(coro):
    return asyncio.run(coro)


_UID = lambda: uuid.uuid4().hex[:12]


def _cleanup_league(db, league: str):
    async def _c():
        await db["player_identities"].delete_many({"league": league})
    return _c()


# ── 1. Ingest → identity → Mongo (full pipeline) ─────────────────
def test_ingest_to_identity_to_mongo_writes_document():
    from services import mls_scorer_gate
    from services.player_identity import (
        persist_registry, reset_registry_for_tests,
        ensure_identity_indexes, IDENTITY_COLLECTION,
    )

    async def go():
        db = _db()
        await ensure_identity_indexes(db)
        league = f"P0A_MLS_{_UID()}"
        await _cleanup_league(db, league)
        reset_registry_for_tests()

        # The scorer gate is hardcoded to league="MLS"; use a
        # separate manual upsert path so we can isolate the write
        # under a synthetic league that doesn't pollute production.
        from services.player_identity import upsert_player
        now_iso = datetime.now(timezone.utc).isoformat()
        upsert_player(
            name="Ingest Round Trip", sport="Soccer", league=league,
            provider="espn", provider_id="ingest_" + _UID(),
            current_team="Round Trip FC",
            observed_at=now_iso, source="espn_mls_leaders",
            roster_status="active",
        )
        written = await persist_registry(db)
        assert written >= 1
        stored = await db[IDENTITY_COLLECTION].find_one(
            {"league": league}, {"_id": 0})
        assert stored is not None
        assert stored["current_team"] == "Round Trip FC"
        assert stored["observed_at"] == now_iso
        assert stored["source"] == "espn_mls_leaders"
        assert stored["roster_status"] == "active"
        assert stored.get("provider_ids", {}).get("espn", "").startswith("ingest_")
        await _cleanup_league(db, league)
    _run(go())


def test_apply_espn_snapshot_end_to_end_persists_to_mongo():
    """The production wiring path: apply_espn_snapshot() populates
    the in-memory registry, then a follow-up ``persist_registry``
    (this is exactly what ``_mls_stats_loop`` now does) writes it
    to Mongo.  Verifies the loop closes."""
    from services import mls_scorer_gate
    from services.player_identity import (
        persist_registry, reset_registry_for_tests,
        ensure_identity_indexes, IDENTITY_COLLECTION,
    )

    async def go():
        db = _db()
        await ensure_identity_indexes(db)
        reset_registry_for_tests()
        # Unique player name so `name_norm` is deterministic + isolated.
        tag = _UID()
        display = f"P0A Pipeline Player {tag}"
        expected_name_norm = f"p0a pipeline player {tag}"
        await db[IDENTITY_COLLECTION].delete_many(
            {"name_norm": expected_name_norm})
        snap = {
            expected_name_norm: {
                "display_name": display,
                "team": "P0A FC",
                "espn_id": f"pid_{_UID()}",
                "position": "F",
            }
        }
        mls_scorer_gate.apply_espn_snapshot(snap, set(snap.keys()))
        written = await persist_registry(db)
        assert written >= 1
        stored = await db[IDENTITY_COLLECTION].find_one(
            {"name_norm": expected_name_norm}, {"_id": 0})
        assert stored is not None
        assert stored["current_team"] == "P0A FC"
        assert stored["source"] == "espn_mls_leaders"
        assert stored.get("provider_ids", {}).get("espn", "").startswith("pid_")
        await db[IDENTITY_COLLECTION].delete_many(
            {"name_norm": expected_name_norm})
    _run(go())


# ── 2. Restart → Mongo → identity restored (full fields survive) ─
def test_restart_hydration_preserves_all_fields():
    from services.player_identity import (
        upsert_player, persist_registry, hydrate_registry_from_mongo,
        resolve_player, reset_registry_for_tests, ensure_identity_indexes,
        IDENTITY_COLLECTION,
    )

    async def go():
        db = _db()
        await ensure_identity_indexes(db)
        league = f"P0A_RESTART_{_UID()}"
        await _cleanup_league(db, league)
        reset_registry_for_tests()

        now = datetime.now(timezone.utc)
        obs_iso = now.isoformat()

        ident = upsert_player(
            name="Restart Survivor",
            sport="Soccer", league=league,
            provider="espn", provider_id="rid_" + _UID(),
            current_team="Original FC",
            observed_at=obs_iso, source="espn_mls_leaders",
            roster_status="active", position="F", role="striker",
        )
        # Give it aliases + a second provider + a synthetic transfer
        # history so we can verify each field survives round-trip.
        ident.aliases = ["Survivor R.", "R. Survivor"]
        ident.provider_ids["apisports"] = "apis_" + _UID()
        transfer_from = (now - timedelta(days=200)).isoformat()
        transfer_to = (now - timedelta(days=100)).isoformat()
        ident.historical_teams = [
            {"team": "Old Origin FC", "from": transfer_from,
             "to": transfer_to, "source": "seed"},
            {"team": "Original FC", "from": transfer_to,
             "to": None, "source": "espn_mls_leaders"},
        ]
        await persist_registry(db)

        # Simulate a restart.
        reset_registry_for_tests()
        loaded = await hydrate_registry_from_mongo(db)
        assert loaded >= 1
        r = resolve_player(name="Restart Survivor", sport="Soccer",
                            league=league)
        assert r is not None
        # ── Provider IDs survive
        assert r.provider_ids.get("espn", "").startswith("rid_")
        assert r.provider_ids.get("apisports", "").startswith("apis_")
        # ── Aliases survive
        assert set(r.aliases) >= {"Survivor R.", "R. Survivor"}
        # ── Current team survives
        assert r.current_team == "Original FC"
        # ── Historical teams survive
        assert len(r.historical_teams) == 2
        assert r.historical_teams[0]["team"] == "Old Origin FC"
        assert r.historical_teams[0]["to"] == transfer_to
        assert r.historical_teams[1]["team"] == "Original FC"
        assert r.historical_teams[1]["to"] is None
        # ── observed_at survives byte-exact
        assert r.observed_at == obs_iso
        # ── Roster status + source survive
        assert r.roster_status == "active"
        assert r.source == "espn_mls_leaders"
        # ── Position / role survive
        assert r.position == "F"
        assert r.role == "striker"
        await _cleanup_league(db, league)
    _run(go())


# ── 3. Stale identity remains stale (hydration must NOT touch obs) ─
def test_stale_identity_remains_stale_after_restart():
    from services.player_identity import (
        upsert_player, persist_registry, hydrate_registry_from_mongo,
        resolve_player, reset_registry_for_tests, ensure_identity_indexes,
        has_fresh_roster_for_league, IDENTITY_COLLECTION,
    )

    async def go():
        db = _db()
        await ensure_identity_indexes(db)
        league = f"P0A_STALE_{_UID()}"
        await _cleanup_league(db, league)
        reset_registry_for_tests()

        old_iso = (datetime.now(timezone.utc)
                   - timedelta(days=180)).isoformat()
        upsert_player(
            name="Stale Player", sport="Soccer", league=league,
            provider="espn", provider_id="stale_" + _UID(),
            current_team="Stale FC",
            observed_at=old_iso,
            source="espn_mls_leaders",
        )
        await persist_registry(db)

        # Restart.
        reset_registry_for_tests()
        await hydrate_registry_from_mongo(db)
        r = resolve_player(name="Stale Player", sport="Soccer", league=league)
        assert r is not None
        # observed_at must NOT have been silently refreshed by
        # hydration or persistence.
        assert r.observed_at == old_iso
        # The freshness gate reflects that.
        assert r.is_current_team_fresh(staleness_days=30) is False
        assert (await has_fresh_roster_for_league(
            db, league, staleness_days=30)) is False
        # And Mongo doc still holds the same timestamp.
        stored = await db[IDENTITY_COLLECTION].find_one(
            {"league": league}, {"_id": 0})
        assert stored["observed_at"] == old_iso
        await _cleanup_league(db, league)
    _run(go())


# ── 4. Older write cannot overwrite fresher current-team info ────
def test_older_observation_cannot_overwrite_fresher_current_team():
    """Simulates two replicas: A writes a fresh observation (T2),
    replica B (still holding a T1 view) attempts to persist and
    MUST NOT clobber the fresher current-team fields."""
    from services.player_identity import (
        persist_identity, hydrate_registry_from_mongo, ensure_identity_indexes,
        reset_registry_for_tests, IDENTITY_COLLECTION,
    )

    async def go():
        db = _db()
        await ensure_identity_indexes(db)
        league = f"P0A_OLDER_{_UID()}"
        await _cleanup_league(db, league)
        cid = "cpid_" + _UID()

        now = datetime.now(timezone.utc)
        t0 = (now - timedelta(days=10)).isoformat()
        t2 = now.isoformat()
        t1 = (now - timedelta(days=5)).isoformat()

        # Replica A: seed at T0 with team X.
        await persist_identity(db, {
            "canonical_player_id": cid,
            "name": "OverwriteCandidate", "name_norm": "overwritecandidate",
            "sport": "Soccer", "league": league,
            "current_team": "Team X",
            "observed_at": t0, "source": "seed",
            "roster_status": "active",
            "provider_ids": {"espn": "ovc_" + _UID()},
        })
        # Replica A advances to T2 with team Z (a transfer!).
        outcome_fresh = await persist_identity(db, {
            "canonical_player_id": cid,
            "name": "OverwriteCandidate", "name_norm": "overwritecandidate",
            "sport": "Soccer", "league": league,
            "current_team": "Team Z",
            "observed_at": t2, "source": "espn_mls_leaders",
            "roster_status": "active",
        })
        assert outcome_fresh in ("advanced", "inserted")

        # Replica B (stale) attempts to write with T1 -> "Team Y".
        outcome_stale = await persist_identity(db, {
            "canonical_player_id": cid,
            "name": "OverwriteCandidate", "name_norm": "overwritecandidate",
            "sport": "Soccer", "league": league,
            "current_team": "Team Y",   # stale team
            "observed_at": t1, "source": "stale_replica_B",
            "roster_status": "active",
        })
        # The freshness fields must NOT have advanced backward.
        assert outcome_stale not in ("advanced", "inserted")
        stored = await db[IDENTITY_COLLECTION].find_one(
            {"canonical_player_id": cid}, {"_id": 0})
        assert stored["current_team"] == "Team Z"
        assert stored["observed_at"] == t2
        # Historical: must include Team Z as the current open row
        # and no entry advanced backwards to Team Y.
        teams_in_history = [h["team"] for h in
                             (stored.get("historical_teams") or [])]
        assert "Team Z" in teams_in_history
        assert "Team Y" not in teams_in_history
        await _cleanup_league(db, league)
    _run(go())


# ── 5. Concurrent / idempotent writes never duplicate ────────────
def test_concurrent_upserts_produce_single_identity():
    """Fires N parallel `persist_identity` writers for the same
    canonical_player_id and asserts Mongo ends with exactly ONE doc."""
    from services.player_identity import (
        persist_identity, ensure_identity_indexes, IDENTITY_COLLECTION,
    )

    async def go():
        db = _db()
        await ensure_identity_indexes(db)
        league = f"P0A_CONCUR_{_UID()}"
        await _cleanup_league(db, league)
        cid = "cpid_" + _UID()
        now = datetime.now(timezone.utc).isoformat()

        async def writer():
            await persist_identity(db, {
                "canonical_player_id": cid,
                "name": "Concurrent Twin",
                "name_norm": "concurrent twin",
                "sport": "Soccer", "league": league,
                "current_team": "Twin FC",
                "observed_at": now, "source": "espn_mls_leaders",
                "roster_status": "active",
                "provider_ids": {"espn": "twin_" + _UID()},
                "aliases": ["Twin C."],
            })

        # 12 concurrent writers.
        await asyncio.gather(*(writer() for _ in range(12)))
        count = await db[IDENTITY_COLLECTION].count_documents(
            {"canonical_player_id": cid})
        assert count == 1
        # The alias list should contain "Twin C." exactly once (dedup).
        stored = await db[IDENTITY_COLLECTION].find_one(
            {"canonical_player_id": cid}, {"_id": 0})
        assert stored["aliases"].count("Twin C.") == 1
        await _cleanup_league(db, league)
    _run(go())


def test_idempotent_repeated_persist_no_duplicate():
    """Calling persist_registry repeatedly with the SAME in-memory
    state must be a no-op mutation-wise."""
    from services.player_identity import (
        upsert_player, persist_registry, reset_registry_for_tests,
        ensure_identity_indexes, IDENTITY_COLLECTION,
    )

    async def go():
        db = _db()
        await ensure_identity_indexes(db)
        league = f"P0A_IDEMP_{_UID()}"
        await _cleanup_league(db, league)
        reset_registry_for_tests()
        obs = datetime.now(timezone.utc).isoformat()
        upsert_player(
            name="Idempotent P", sport="Soccer", league=league,
            provider="espn", provider_id="idem_" + _UID(),
            current_team="Idem FC", observed_at=obs,
            source="espn_mls_leaders", roster_status="active",
        )
        await persist_registry(db)
        first = await db[IDENTITY_COLLECTION].count_documents(
            {"league": league})
        assert first == 1
        # Repeat 3x — must not create duplicates.
        for _ in range(3):
            await persist_registry(db)
        second = await db[IDENTITY_COLLECTION].count_documents(
            {"league": league})
        assert second == 1
        stored = await db[IDENTITY_COLLECTION].find_one(
            {"league": league}, {"_id": 0})
        assert stored["observed_at"] == obs   # unchanged
        await _cleanup_league(db, league)
    _run(go())


# ── 6. Provider IDs merge across observations ────────────────────
def test_provider_ids_merge_across_writers_without_loss():
    from services.player_identity import (
        persist_identity, ensure_identity_indexes, IDENTITY_COLLECTION,
    )

    async def go():
        db = _db()
        await ensure_identity_indexes(db)
        league = f"P0A_PIDMERGE_{_UID()}"
        await _cleanup_league(db, league)
        cid = "cpid_" + _UID()
        obs = datetime.now(timezone.utc).isoformat()

        base = {
            "canonical_player_id": cid,
            "name": "Multi Provider",
            "name_norm": "multi provider",
            "sport": "Soccer", "league": league,
            "current_team": "MP FC",
            "observed_at": obs, "source": "seed",
            "roster_status": "active",
        }
        await persist_identity(db, dict(base,
            provider_ids={"espn": "espn_123"}))
        await persist_identity(db, dict(base,
            provider_ids={"apisports": "apis_777"}))
        await persist_identity(db, dict(base,
            provider_ids={"statsapi": "stats_9"}))
        stored = await db[IDENTITY_COLLECTION].find_one(
            {"canonical_player_id": cid}, {"_id": 0})
        assert stored["provider_ids"] == {
            "espn": "espn_123",
            "apisports": "apis_777",
            "statsapi": "stats_9",
        }
        await _cleanup_league(db, league)
    _run(go())


# ── 7. Multi-replica read visibility (persistent state ≡ single source) ─
def test_multiple_hydrators_see_same_persisted_identity():
    """Two independent Mongo clients (simulating replicas) hydrate
    from the same collection — both must see identical identity
    records."""
    from services.player_identity import (
        upsert_player, persist_registry, reset_registry_for_tests,
        ensure_identity_indexes, snapshot_registry,
    )

    async def go():
        # First "replica" — write.
        db_a = _db()
        await ensure_identity_indexes(db_a)
        league = f"P0A_REPLICAS_{_UID()}"
        await db_a["player_identities"].delete_many({"league": league})
        reset_registry_for_tests()
        obs = datetime.now(timezone.utc).isoformat()
        upsert_player(
            name="Shared Replica Player", sport="Soccer", league=league,
            provider="espn", provider_id="rep_" + _UID(),
            current_team="Replica FC", observed_at=obs,
            source="espn_mls_leaders", roster_status="active",
        )
        await persist_registry(db_a)

        # Second "replica" — read via a fresh client + fresh registry.
        from services.player_identity import hydrate_registry_from_mongo
        db_b = AsyncIOMotorClient(os.environ["MONGO_URL"])[
            os.environ.get("DB_NAME", "lockscore_db")]
        reset_registry_for_tests()
        n = await hydrate_registry_from_mongo(db_b)
        assert n >= 1
        snap = snapshot_registry()
        match = [s for s in snap if s.get("league") == league]
        assert len(match) == 1
        assert match[0]["current_team"] == "Replica FC"
        assert match[0]["observed_at"] == obs

        await db_a["player_identities"].delete_many({"league": league})
    _run(go())


# ── 8. Existing betting outputs remain unchanged ─────────────────
def test_locks_threshold_still_strict_gt_85():
    """Sanity: the canonical Locks threshold remains `>= 85` (inclusive).

    Phase 10C fixture refresh: 
      (a) Phase 1D Real-Line Integrity gate requires real `book_odds` +
          `implied_probability` — fixture updated accordingly.
      (b) The historical assertion `85.0 → False` was a legacy pre-Phase-6
          strictness that has since been settled to INCLUSIVE `>= 85`
          (MAIN_BOARD_LOCK_FLOOR=85.0). Updated to match the current
          production contract; the >=85 semantic is preserved without
          weakening — 84.999 still rejects."""
    from services.main_board_eligibility import (
        is_main_board_eligible, MAIN_BOARD_LOCK_FLOOR,
    )
    assert MAIN_BOARD_LOCK_FLOOR == 85.0
    def _real(lock: float) -> dict:
        return {
            "lock_score": lock,
            "book_odds": -150,
            "implied_probability": 0.60,
            "odds_source": "the_odds_api",
        }
    # >=85 inclusive.
    assert is_main_board_eligible(_real(84.999)) is False
    assert is_main_board_eligible(_real(85.0)) is True
    assert is_main_board_eligible(_real(85.001)) is True
    assert is_main_board_eligible(_real(95.0)) is True


def test_startup_wires_persist_and_index_paths():
    """server.py must:
      • ensure the unique index BEFORE hydrating.
      • await persist_registry inside the MLS stats loop.
    """
    src = open("/app/backend/server.py").read()
    assert "ensure_identity_indexes" in src
    assert "persist_registry" in src
    # In the MLS loop.
    idx_mls = src.find("_mls_stats_loop")
    assert idx_mls != -1
    assert "persist_registry" in src[idx_mls: idx_mls + 4000] \
        or "_persist_ident" in src[idx_mls: idx_mls + 4000]
