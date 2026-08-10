"""P0-E (2026-08-11) — Soccer roster coverage + source-conflict safety.

Covers:

  * Expanded live club-roster coverage across leagues currently
    producing Perklocks picks (Saudi Pro, Eredivisie, Liga MX,
    Brasileirão, CSL, etc.).
  * Dedicated national-team roster ingester (ESPN confederations)
    replaces citizenship-as-NT auto-population.
  * Source-conflict protection: citizenship never causes hard
    ``team_mismatch``; conflicting authoritative NT vs weak
    citizenship signals resolve to ``roster_conflict`` or
    ``roster_unverified``.
  * The Endrick regression: club-side citizenship='Portugal' does
    NOT cause a rejection when the fixture is Brazil.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

from motor.motor_asyncio import AsyncIOMotorClient


def _db():
    return AsyncIOMotorClient(os.environ["MONGO_URL"])[
        os.environ.get("DB_NAME", "lockscore_db")]


def _run(coro):
    return asyncio.run(coro)


_UID = lambda: uuid.uuid4().hex[:12]


# ── 1. Expanded league coverage ──────────────────────────────────
def test_league_slugs_cover_currently_used_competitions():
    from services.espn_live_soccer_rosters import LEAGUE_SLUGS
    canonical = set(LEAGUE_SLUGS.values())
    # Every league below currently produces Perklocks picks per the
    # db.picks distribution (2026-08-11).
    required = {
        "EPL", "La Liga", "Serie A", "Bundesliga", "Ligue 1", "MLS",
        "Saudi Pro League", "Eredivisie", "Liga MX", "China Super League",
        "Campeonato Brasileiro Série A", "Brasileirão Série B",
        "Allsvenskan", "Eliteserien", "Primeira Liga",
    }
    missing = required - canonical
    assert not missing, f"live coverage missing for: {missing}"


# ── 2. Live club ingester writes citizenship→nationality only ────
def test_live_club_ingester_writes_only_nationality_from_citizenship():
    """P0-E requirement: the live club-roster ingester must NEVER
    populate ``current_national_team`` from ESPN's citizenship
    field.  Endrick's ESPN citizenship='Portugal' must never end up
    on his ``current_national_team``."""
    from services.player_identity import (
        ensure_identity_indexes, IDENTITY_COLLECTION,
    )
    import services.espn_live_soccer_rosters as mod

    fake_teams = {"sports":[{"leagues":[{"teams":[
        {"team": {"id": "9", "displayName": "Fake Endrick FC"}},
    ]}]}]}
    fake_roster = {"athletes": [
        {"id": "8443777", "displayName": "Endrick P0E Regression",
         "position": {"abbreviation": "F"},
         "citizenship": "Portugal"},   # wrong per ESPN
    ]}
    async def _fake_get(cx, url):
        if "/teams/9/roster" in url: return fake_roster
        if url.endswith("/teams"): return fake_teams
        return None

    async def go():
        db = _db()
        await ensure_identity_indexes(db)
        await db[IDENTITY_COLLECTION].delete_many(
            {"name_norm": "endrick p0e regression"})
        with patch.object(mod, "_get_json", new=_fake_get):
            await mod.refresh_live_rosters(
                db, league_slugs={"eng.1": "EPL"},
                max_concurrency=1, request_timeout=2.0,
            )
        doc = await db[IDENTITY_COLLECTION].find_one(
            {"name_norm": "endrick p0e regression"}, {"_id": 0})
        assert doc is not None
        # Nationality captured but NOT NT.
        assert doc.get("nationality") == "Portugal"
        assert doc.get("current_national_team") in (None, "", )
        assert doc.get("national_team_source") in (None, "unknown")
        await db[IDENTITY_COLLECTION].delete_many(
            {"name_norm": "endrick p0e regression"})
    _run(go())


# ── 3. Endrick regression — fixture Brazil vs Haiti ──────────────
def test_endrick_regression_no_rejection_from_wrong_citizenship():
    """Fixture: Haiti @ Brazil.  Even with only weak citizenship
    signal (wrongly='Portugal') and no authoritative NT record,
    the validator must NOT hard-reject as team_mismatch.

    After the FINAL CLEANUP (2026-08-11) the hand-curated
    ``_KNOWN_NT_CORRECTIONS`` map elevates Endrick's known correct
    NT (Brazil) — so this fixture now verifies outright.  Either
    outcome (verified or roster_unverified) is acceptable; the
    NON-ACCEPTABLE outcome is ``player_team_mismatch``."""
    from services.player_team_fixture_validator import (
        validate_player_fixture_pick, REASON_ROSTER_UNVERIFIED,
        REASON_ROSTER_CONFLICT, REASON_PLAYER_TEAM_MISMATCH,
    )
    # No authoritative NT; weak citizenship signal says Portugal.
    v = validate_player_fixture_pick(
        {"sport": "Soccer",
         "market": "Endrick To Score or Assist",
         "event": "Haiti @ Brazil",
         "league": "FIFA World Cup · Props"},
        {},
        national_team_lookup={},
        nationality_lookup={"endrick": "Portugal"})
    assert v["reason"] != REASON_PLAYER_TEAM_MISMATCH
    # After curated correction lands, verified=True is expected.
    # roster_unverified is the previous (still acceptable) outcome.
    assert v["verified"] is True or v["reason"] == REASON_ROSTER_UNVERIFIED


def test_endrick_regression_authoritative_brazil_wins_over_citizenship():
    """When we do have an authoritative NT record (from the dedicated
    NT ingester) saying Endrick=Brazil, a fixture Haiti @ Brazil
    must verify — the wrong citizenship 'Portugal' must not
    interfere."""
    from services.player_team_fixture_validator import (
        validate_player_fixture_pick,
    )
    v = validate_player_fixture_pick(
        {"sport": "Soccer",
         "market": "Endrick To Score or Assist",
         "event": "Haiti @ Brazil",
         "league": "FIFA World Cup · Props"},
        {},
        national_team_lookup={"endrick": "Brazil"},
        fresh_national_team_names={"endrick"},
        nationality_lookup={"endrick": "Portugal"})
    assert v["verified"] is True
    assert v["player_team"] == "Brazil"


# ── 4. roster_conflict classification ───────────────────────────
def test_conflicting_sources_return_roster_conflict_not_mismatch():
    """Authoritative NT record disagrees with citizenship, AND the
    NT record doesn't match the fixture — result must be
    ``roster_conflict``, never ``team_mismatch``."""
    from services.player_team_fixture_validator import (
        validate_player_fixture_pick, REASON_ROSTER_CONFLICT,
        REASON_PLAYER_TEAM_MISMATCH,
    )
    v = validate_player_fixture_pick(
        {"sport": "Soccer",
         "market": "Conflict Case Anytime Goal Scorer",
         "event": "France @ Spain",
         "league": "FIFA World Cup"},
        {},
        national_team_lookup={"conflict case": "Argentina"},   # authoritative
        fresh_national_team_names={"conflict case"},
        nationality_lookup={"conflict case": "Portugal"})       # disagrees
    assert v["reason"] == REASON_ROSTER_CONFLICT
    assert v["reason"] != REASON_PLAYER_TEAM_MISMATCH


def test_agreeing_sources_disagreeing_with_fixture_return_mismatch():
    """When authoritative NT and citizenship both agree — but the
    NT doesn't match the fixture — hard reject as
    ``team_mismatch``.  This is the confident-rejection path."""
    from services.player_team_fixture_validator import (
        validate_player_fixture_pick, REASON_PLAYER_TEAM_MISMATCH,
    )
    v = validate_player_fixture_pick(
        {"sport": "Soccer",
         "market": "Certain Foreigner Anytime Goal Scorer",
         "event": "France @ Spain",
         "league": "FIFA World Cup"},
        {},
        national_team_lookup={"certain foreigner": "Argentina"},
        fresh_national_team_names={"certain foreigner"},
        nationality_lookup={"certain foreigner": "Argentina"})
    assert v["reason"] == REASON_PLAYER_TEAM_MISMATCH


# ── 5. Citizenship-only weak evidence → verify when matches ────
def test_citizenship_alone_verifies_when_matches_fixture():
    """When no authoritative NT record exists but citizenship
    matches a fixture side, the pick verifies with an
    ``evidence="citizenship_weak"`` tag."""
    from services.player_team_fixture_validator import (
        validate_player_fixture_pick,
    )
    v = validate_player_fixture_pick(
        {"sport": "Soccer",
         "market": "Nation Match Anytime Goal Scorer",
         "event": "Argentina @ Brazil"},
        {},
        national_team_lookup={},
        nationality_lookup={"nation match": "Argentina"})
    assert v["verified"] is True
    assert v.get("evidence") == "citizenship_weak"


# ── 6. Dedicated NT roster module wiring ────────────────────────
def test_dedicated_nt_roster_module_exists_and_covers_confederations():
    from services.espn_national_team_rosters import (
        CONFEDERATION_SLUGS, refresh_national_team_rosters,
    )
    assert "fifa.worldq.conmebol" in CONFEDERATION_SLUGS
    assert "fifa.worldq.uefa" in CONFEDERATION_SLUGS
    assert "fifa.worldq.concacaf" in CONFEDERATION_SLUGS
    assert "fifa.worldq.afc" in CONFEDERATION_SLUGS
    assert "fifa.worldq.caf" in CONFEDERATION_SLUGS
    assert callable(refresh_national_team_rosters)


def test_dedicated_nt_roster_writes_authoritative_records():
    """Simulate the dedicated NT ingester ingesting a mocked
    CONMEBOL Argentina roster.  The resulting identity must have
    ``current_national_team = 'Argentina'`` and
    ``national_team_source = 'espn_national_team_roster'``."""
    from services.player_identity import (
        ensure_identity_indexes, IDENTITY_COLLECTION,
    )
    import services.espn_national_team_rosters as mod

    fake_teams = {"sports":[{"leagues":[{"teams":[
        {"team": {"id": "202", "displayName": "Argentina"}},
    ]}]}]}
    fake_roster = {"athletes": [
        {"id": "9990001", "displayName": "P0E Test Messi",
         "position": {"abbreviation": "F"},
         "citizenship": "Argentina"},
    ]}
    async def _fake_get(cx, url):
        if "/teams/202/roster" in url: return fake_roster
        if url.endswith("/teams"): return fake_teams
        return None

    async def go():
        db = _db()
        await ensure_identity_indexes(db)
        await db[IDENTITY_COLLECTION].delete_many(
            {"name_norm": "p0e test messi"})
        with patch.object(mod, "_get_json", new=_fake_get):
            stats = await mod.refresh_national_team_rosters(
                db, confederations=["fifa.worldq.conmebol"],
                max_concurrency=1, request_timeout=2.0,
            )
        assert stats["nations_covered"] >= 1
        assert stats["rosters_written"] >= 1
        doc = await db[IDENTITY_COLLECTION].find_one(
            {"name_norm": "p0e test messi"}, {"_id": 0})
        assert doc is not None
        assert doc["current_national_team"] == "Argentina"
        assert doc["national_team_source"] == "espn_national_team_roster"
        assert doc["league"] == "International"
        await db[IDENTITY_COLLECTION].delete_many(
            {"name_norm": "p0e test messi"})
    _run(go())


# ── 7. Dedicated NT roster is authoritative over bootstrap ─────
def test_dedicated_nt_roster_overrides_curated_bootstrap():
    from services.player_identity import (
        upsert_player, persist_identity, ensure_identity_indexes,
        reset_registry_for_tests, hydrate_registry_from_mongo,
        IDENTITY_COLLECTION,
    )

    async def go():
        db = _db()
        await ensure_identity_indexes(db)
        league = "International"
        tag = _UID()
        name = f"P0E NT Author Override {tag}"
        await db[IDENTITY_COLLECTION].delete_many(
            {"name_norm": name.lower()})
        reset_registry_for_tests()
        old_iso = (datetime.now(timezone.utc)
                   - timedelta(days=20)).isoformat()
        # Curated bootstrap first.
        seed = upsert_player(
            name=name, sport="Soccer", league=league,
            current_team="Bootstrap Nation",
            observed_at=old_iso, source="curated_bootstrap_v1",
            affiliation_type="national_team",
        )
        await persist_identity(db, seed.to_dict())
        # Later authoritative roster.
        await hydrate_registry_from_mongo(db)
        new_iso = datetime.now(timezone.utc).isoformat()
        live = upsert_player(
            name=name, sport="Soccer", league=league,
            provider="espn", provider_id=f"nt_{tag}",
            current_team="Roster Nation",
            observed_at=new_iso,
            source="espn_national_team_roster",
            affiliation_type="national_team",
        )
        await persist_identity(db, live.to_dict())
        doc = await db[IDENTITY_COLLECTION].find_one(
            {"name_norm": name.lower(),
             "canonical_player_id": live.canonical_player_id},
            {"_id": 0})
        assert doc["current_national_team"] == "Roster Nation"
        assert doc["national_team_source"] == "espn_national_team_roster"
        await db[IDENTITY_COLLECTION].delete_many(
            {"name_norm": name.lower()})
    _run(go())


# ── 8. Regression — earlier P0 fixes still work ────────────────
def test_p0abcd_regression_intact_after_p0e():
    from services.player_team_fixture_validator import (
        _extract_player_name, validate_player_fixture_pick,
        REASON_ROSTER_UNVERIFIED, REASON_PLAYER_TEAM_MISMATCH,
    )
    # P0-B parser.
    assert _extract_player_name(
        {"market": "Federico Vinas To Score or Assist"}) == "Federico Vinas"
    # P0-C: Messi with authoritative NT verifies on Argentina fixture.
    v = validate_player_fixture_pick(
        {"sport": "Soccer",
         "market": "Lionel Messi Anytime Goal Scorer",
         "event": "Algeria @ Argentina"},
        {"lionel messi": "Inter Miami CF"},
        fresh_roster_names={"lionel messi"},
        national_team_lookup={"lionel messi": "Argentina"},
        fresh_national_team_names={"lionel messi"})
    assert v["verified"] is True
    # P0-C: wrong national team is still a hard mismatch.
    v2 = validate_player_fixture_pick(
        {"sport": "Soccer",
         "market": "Lionel Messi Anytime Goal Scorer",
         "event": "France @ Spain"},
        {},
        national_team_lookup={"lionel messi": "Argentina"},
        fresh_national_team_names={"lionel messi"},
        nationality_lookup={"lionel messi": "Argentina"})
    assert v2["reason"] == REASON_PLAYER_TEAM_MISMATCH
    # P0-A/D wiring — index exists.
    from services.player_identity import ensure_identity_indexes

    async def go():
        db = _db()
        await ensure_identity_indexes(db)
        idx = await db["player_identities"].index_information()
        assert any(v.get("unique") for v in idx.values())
    _run(go())


def test_locks_gate_still_strict_gt_85_after_p0e():
    from services.main_board_eligibility import is_main_board_eligible
    assert is_main_board_eligible({"lock_score": 85.0}) is False
    assert is_main_board_eligible({"lock_score": 85.001}) is True


# ── 9. server startup wires it all ─────────────────────────────
def test_server_wires_dedicated_nt_ingester():
    """The startup path must import and run the new dedicated NT
    ingester alongside the club roster ingest."""
    src = open("/app/backend/services/soccer_identity_ingest.py").read()
    assert "refresh_national_team_rosters" in src
    assert "espn_national_team_rosters" in src
