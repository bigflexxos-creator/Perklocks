"""P0-D (2026-08-11) — Live soccer roster coverage.

Verifies:

  * The live ESPN roster ingest writes ``source="espn_live_roster"``
    identities to Mongo (both club and national-team streams).
  * A fresher live observation OVERRIDES an older
    ``soccer_player_form`` (P0-C fallback) entry for the same player.
  * A fresher live national-team observation OVERRIDES the curated
    P0-C bootstrap for the same player.
  * Older observations from any fallback source can NEVER overwrite
    a fresher live observation.
  * Canonical player identity, provider IDs, aliases, historical
    clubs and historical national teams survive the override.
  * P0-A / P0-B / P0-C behaviour remains intact.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, AsyncMock

from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

from motor.motor_asyncio import AsyncIOMotorClient


def _db():
    return AsyncIOMotorClient(os.environ["MONGO_URL"])[
        os.environ.get("DB_NAME", "lockscore_db")]


def _run(coro):
    return asyncio.run(coro)


_UID = lambda: uuid.uuid4().hex[:12]


# ── 1. Live source structure ─────────────────────────────────────
def test_live_league_slugs_cover_big5_plus_mls():
    from services.espn_live_soccer_rosters import LEAGUE_SLUGS
    canonical = set(LEAGUE_SLUGS.values())
    for lg in ("EPL", "La Liga", "Serie A", "Bundesliga",
               "Ligue 1", "MLS"):
        assert lg in canonical, f"missing live coverage for {lg}"


# ── 2. Fresh live observation overrides older soccer_player_form ─
def test_live_roster_overrides_older_soccer_player_form():
    from services.player_identity import (
        upsert_player, persist_identity, ensure_identity_indexes,
        reset_registry_for_tests, IDENTITY_COLLECTION,
        hydrate_registry_from_mongo, resolve_player,
    )

    async def go():
        db = _db()
        await ensure_identity_indexes(db)
        league = "EPL"                      # canonical, shared identity
        tag = _UID()
        name = f"Live Override Player {tag}"
        await db[IDENTITY_COLLECTION].delete_many(
            {"name_norm": name.lower()})
        reset_registry_for_tests()

        # ── Older soccer_player_form observation ──
        old_iso = (datetime.now(timezone.utc)
                    - timedelta(days=45)).isoformat()
        stale = upsert_player(
            name=name, sport="Soccer", league=league,
            provider="understat", provider_id=f"und_{tag}",
            current_team="Old Club FC",
            observed_at=old_iso, source="soccer_player_form",
            affiliation_type="club",
        )
        await persist_identity(db, stale.to_dict())

        # ── Fresher live ESPN observation ──
        # Simulate the production path: hydrate the registry from
        # Mongo so the same canonical_player_id is resolved for the
        # subsequent live observation (avoiding a duplicate mint).
        await hydrate_registry_from_mongo(db)
        new_iso = datetime.now(timezone.utc).isoformat()
        live = upsert_player(
            name=name, sport="Soccer", league=league,
            provider="espn", provider_id=f"esp_{tag}",
            current_team="New Live FC",
            observed_at=new_iso, source="espn_live_roster",
            affiliation_type="club",
        )
        await persist_identity(db, live.to_dict())

        stored = await db[IDENTITY_COLLECTION].find_one(
            {"name_norm": name.lower(),
             "canonical_player_id": live.canonical_player_id},
            {"_id": 0})
        assert stored is not None
        assert stored["current_team"] == "New Live FC"
        assert stored["source"] == "espn_live_roster"
        assert stored["observed_at"] == new_iso
        # Historical clubs preserved through override.
        hist_teams = [h["team"] for h in stored.get("historical_teams") or []]
        assert "Old Club FC" in hist_teams
        assert "New Live FC" in hist_teams
        # Both provider IDs preserved.
        pids = stored.get("provider_ids") or {}
        assert pids.get("understat") == f"und_{tag}"
        assert pids.get("espn") == f"esp_{tag}"
        # Canonical id survived.
        assert stored["canonical_player_id"] == live.canonical_player_id
        await db[IDENTITY_COLLECTION].delete_many(
            {"name_norm": name.lower()})
    _run(go())


# ── 3. Older fallback CANNOT overwrite fresher live ─────────────
def test_stale_soccer_player_form_cannot_overwrite_fresher_live():
    from services.player_identity import (
        upsert_player, persist_identity, ensure_identity_indexes,
        reset_registry_for_tests, IDENTITY_COLLECTION,
    )

    async def go():
        db = _db()
        await ensure_identity_indexes(db)
        league = "La Liga"
        tag = _UID()
        name = f"Stale Cannot Override {tag}"
        await db[IDENTITY_COLLECTION].delete_many(
            {"name_norm": name.lower()})
        reset_registry_for_tests()

        # Fresh live first.
        fresh_iso = datetime.now(timezone.utc).isoformat()
        live = upsert_player(
            name=name, sport="Soccer", league=league,
            provider="espn", provider_id=f"esp_{tag}",
            current_team="Live Club", observed_at=fresh_iso,
            source="espn_live_roster", affiliation_type="club",
        )
        await persist_identity(db, live.to_dict())

        # A LATER attempt by a STALE fallback source must not clobber.
        reset_registry_for_tests()
        stale_iso = (datetime.now(timezone.utc)
                      - timedelta(days=90)).isoformat()
        stale = upsert_player(
            name=name, sport="Soccer", league=league,
            provider="understat", provider_id=f"und_{tag}",
            current_team="Stale Wrong Club",
            observed_at=stale_iso,
            source="soccer_player_form",
            affiliation_type="club",
        )
        await persist_identity(db, stale.to_dict())

        stored = await db[IDENTITY_COLLECTION].find_one(
            {"name_norm": name.lower()}, {"_id": 0})
        assert stored["current_team"] == "Live Club"
        assert stored["observed_at"] == fresh_iso
        assert stored["source"] == "espn_live_roster"
        await db[IDENTITY_COLLECTION].delete_many(
            {"name_norm": name.lower()})
    _run(go())


# ── 4. Live NT observation overrides curated bootstrap ──────────
def test_live_national_team_overrides_curated_bootstrap():
    from services.player_identity import (
        upsert_player, persist_identity, ensure_identity_indexes,
        reset_registry_for_tests, IDENTITY_COLLECTION,
    )

    async def go():
        db = _db()
        await ensure_identity_indexes(db)
        # Use a unique canonical league so the test doesn't touch
        # any real bootstrap entries.
        league = "International"
        tag = _UID()
        name = f"NT Override Player {tag}"
        await db[IDENTITY_COLLECTION].delete_many(
            {"name_norm": name.lower()})
        reset_registry_for_tests()

        # Curated bootstrap (older).
        old_iso = (datetime.now(timezone.utc)
                    - timedelta(days=10)).isoformat()
        seed = upsert_player(
            name=name, sport="Soccer", league=league,
            current_team="Bootstrap Nation",
            observed_at=old_iso, source="curated_bootstrap_v1",
            affiliation_type="national_team",
        )
        await persist_identity(db, seed.to_dict())

        # Live ESPN roster observation with citizenship (fresher).
        # Simulate production path — hydrate before the second write.
        from services.player_identity import hydrate_registry_from_mongo
        await hydrate_registry_from_mongo(db)
        fresh_iso = datetime.now(timezone.utc).isoformat()
        live = upsert_player(
            name=name, sport="Soccer", league=league,
            provider="espn", provider_id=f"esp_{tag}",
            current_team="Live Nation X",
            observed_at=fresh_iso, source="espn_live_roster",
            affiliation_type="national_team",
        )
        await persist_identity(db, live.to_dict())

        stored = await db[IDENTITY_COLLECTION].find_one(
            {"name_norm": name.lower(),
             "canonical_player_id": live.canonical_player_id},
            {"_id": 0})
        assert stored["current_national_team"] == "Live Nation X"
        assert stored["national_team_source"] == "espn_live_roster"
        assert stored["national_team_observed_at"] == fresh_iso
        # Historical national teams preserved through override.
        hist_nt = [h["team"] for h in
                   stored.get("historical_national_teams") or []]
        assert "Bootstrap Nation" in hist_nt
        assert "Live Nation X" in hist_nt
        await db[IDENTITY_COLLECTION].delete_many(
            {"name_norm": name.lower()})
    _run(go())


def test_bootstrap_cannot_override_fresher_live_national_team():
    from services.player_identity import (
        upsert_player, persist_identity, ensure_identity_indexes,
        reset_registry_for_tests, IDENTITY_COLLECTION,
    )

    async def go():
        db = _db()
        await ensure_identity_indexes(db)
        league = "International"
        tag = _UID()
        name = f"Bootstrap Later Player {tag}"
        await db[IDENTITY_COLLECTION].delete_many(
            {"name_norm": name.lower()})
        reset_registry_for_tests()

        # Fresh live first.
        fresh_iso = datetime.now(timezone.utc).isoformat()
        live = upsert_player(
            name=name, sport="Soccer", league=league,
            provider="espn", provider_id=f"esp_{tag}",
            current_team="Live NT",
            observed_at=fresh_iso, source="espn_live_roster",
            affiliation_type="national_team",
        )
        await persist_identity(db, live.to_dict())

        # Bootstrap re-run later attempts to write with an older
        # timestamp (simulating a delayed replica).
        reset_registry_for_tests()
        old_iso = (datetime.now(timezone.utc)
                    - timedelta(days=5)).isoformat()
        seed = upsert_player(
            name=name, sport="Soccer", league=league,
            current_team="Bootstrap NT",
            observed_at=old_iso, source="curated_bootstrap_v1",
            affiliation_type="national_team",
        )
        await persist_identity(db, seed.to_dict())

        stored = await db[IDENTITY_COLLECTION].find_one(
            {"name_norm": name.lower()}, {"_id": 0})
        assert stored["current_national_team"] == "Live NT"
        assert stored["national_team_source"] == "espn_live_roster"
        assert stored["national_team_observed_at"] == fresh_iso
        await db[IDENTITY_COLLECTION].delete_many(
            {"name_norm": name.lower()})
    _run(go())


# ── 5. Full ingest wiring — mocked ESPN HTTP responses ──────────
def test_refresh_live_rosters_mocked_end_to_end():
    """Exercise the full `refresh_live_rosters` code path against
    fake ESPN responses.  Verifies club + national-team writes both
    land in Mongo through the P0-A layer."""
    from services.player_identity import (
        ensure_identity_indexes, IDENTITY_COLLECTION,
    )
    fake_teams = {
        "sports": [{"leagues": [{"teams": [
            {"team": {"id": "999", "displayName": "Fake Live FC"}},
        ]}]}]
    }
    fake_roster = {
        "athletes": [
            {"id": "8443001", "displayName": "Live Star Player One",
             "position": {"abbreviation": "F"}, "citizenship": "Wonderland"},
            {"id": "8443002", "displayName": "Live Star Player Two",
             "position": {"abbreviation": "M"}, "citizenship": "Neverland"},
        ]
    }

    async def _fake_get(cx, url):
        if "/teams/999/roster" in url:
            return fake_roster
        if url.endswith("/teams"):
            return fake_teams
        return None

    async def go():
        db = _db()
        await ensure_identity_indexes(db)
        await db[IDENTITY_COLLECTION].delete_many(
            {"source": "espn_live_roster",
             "name_norm": {"$in": ["live star player one",
                                     "live star player two"]}})
        from services import espn_live_soccer_rosters as mod
        with patch.object(mod, "_get_json", new=_fake_get):
            stats = await mod.refresh_live_rosters(
                db, league_slugs={"eng.1": "EPL"},
                max_concurrency=2, request_timeout=2.0,
            )
        assert stats["teams_scanned"] == 1
        assert stats["athletes_scanned"] == 2
        assert stats["club_writes"] >= 2
        assert stats["national_team_writes"] >= 2

        # Verify Mongo records.
        p1 = await db[IDENTITY_COLLECTION].find_one(
            {"name_norm": "live star player one"}, {"_id": 0})
        assert p1 is not None
        assert p1["current_team"] == "Fake Live FC"
        assert p1["current_national_team"] == "Wonderland"
        assert p1["source"] == "espn_live_roster"
        assert p1["national_team_source"] == "espn_live_roster"
        assert p1["provider_ids"].get("espn") == "8443001"

        await db[IDENTITY_COLLECTION].delete_many(
            {"source": "espn_live_roster",
             "name_norm": {"$in": ["live star player one",
                                     "live star player two"]}})
    _run(go())


# ── 6. Historical arrays are preserved through override ─────────
def test_transfer_history_preserved_when_live_overrides_history():
    """Player has: soccer_player_form club A (old) → live ESPN
    club B (new).  historical_teams must contain BOTH."""
    from services.player_identity import (
        upsert_player, persist_identity, ensure_identity_indexes,
        reset_registry_for_tests, IDENTITY_COLLECTION,
    )

    async def go():
        db = _db()
        await ensure_identity_indexes(db)
        league = "Serie A"
        tag = _UID()
        name = f"Transfer History {tag}"
        await db[IDENTITY_COLLECTION].delete_many(
            {"name_norm": name.lower()})
        reset_registry_for_tests()
        t0 = (datetime.now(timezone.utc)
              - timedelta(days=120)).isoformat()
        t2 = datetime.now(timezone.utc).isoformat()
        # Historical.
        seed = upsert_player(
            name=name, sport="Soccer", league=league,
            provider="understat", provider_id=f"und_{tag}",
            current_team="Old Club A", observed_at=t0,
            source="soccer_player_form", affiliation_type="club",
        )
        await persist_identity(db, seed.to_dict())
        # Live current — hydrate first (simulates startup path).
        from services.player_identity import hydrate_registry_from_mongo
        await hydrate_registry_from_mongo(db)
        live = upsert_player(
            name=name, sport="Soccer", league=league,
            provider="espn", provider_id=f"esp_{tag}",
            current_team="Live Club B", observed_at=t2,
            source="espn_live_roster", affiliation_type="club",
        )
        await persist_identity(db, live.to_dict())
        stored = await db[IDENTITY_COLLECTION].find_one(
            {"name_norm": name.lower(),
             "canonical_player_id": live.canonical_player_id},
            {"_id": 0})
        hist_teams = [h["team"] for h in
                       stored.get("historical_teams") or []]
        assert hist_teams == ["Old Club A", "Live Club B"]
        # The old row was closed by the transfer.
        assert stored["historical_teams"][0]["to"] == t2
        assert stored["historical_teams"][1]["to"] is None
        await db[IDENTITY_COLLECTION].delete_many(
            {"name_norm": name.lower()})
    _run(go())


# ── 7. Regression: P0-A/B/C still work ──────────────────────────
def test_p0abc_still_functional_after_p0d():
    from services.player_team_fixture_validator import (
        _extract_player_name, validate_player_fixture_pick,
        REASON_ROSTER_UNVERIFIED,
    )
    # P0-B parser.
    assert _extract_player_name(
        {"market": "Federico Vinas To Score or Assist"}) == "Federico Vinas"
    # P0-C: international fixture without NT data → roster_unverified.
    v = validate_player_fixture_pick(
        {"sport": "Soccer",
         "market": "Someone Anytime Goal Scorer",
         "event": "Argentina @ Brazil"},
        {}, national_team_lookup={})
    assert v["reason"] == REASON_ROSTER_UNVERIFIED


def test_locks_gate_still_strict_gt_85_after_p0d():
    from services.main_board_eligibility import is_main_board_eligible
    assert is_main_board_eligible({"lock_score": 85.0}) is False
    assert is_main_board_eligible({"lock_score": 85.001}) is True


# ── 8. Wiring markers in server.py ──────────────────────────────
def test_server_startup_wires_live_roster_ingest():
    src = open("/app/backend/server.py").read()
    assert "refresh_soccer_identity_registry" in src
    assert "P0-C/P0-D" in src
