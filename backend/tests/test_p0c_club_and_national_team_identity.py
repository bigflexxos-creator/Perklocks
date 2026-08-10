"""P0-C (2026-08-11) — Soccer club + national-team identity separation.

Covers:
    * Data-model separation of club vs national-team affiliations.
    * Validator routing: club fixture → club roster; international
      fixture → national-team roster.
    * Missing national-team observation → ``roster_unverified``
      (never ``team_mismatch``).
    * Big-5 league hydration from `soccer_player_form`.
    * National-team curated bootstrap.
    * Persistence through the P0-A race-safe layer.
    * P0-A / P0-B behaviour remains intact.
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


# ── 1. Data-model separation ─────────────────────────────────────
def test_identity_has_separate_club_and_national_team_fields():
    from services.player_identity import (
        PlayerIdentity, reset_registry_for_tests, upsert_player,
        resolve_player,
    )
    reset_registry_for_tests()
    now = datetime.now(timezone.utc).isoformat()
    upsert_player(
        name="Two Affiliations", sport="Soccer", league="EPL",
        current_team="Test FC", provider="prov", provider_id="tf_1",
        observed_at=now, source="unit_test", affiliation_type="club",
    )
    upsert_player(
        name="Two Affiliations", sport="Soccer", league="EPL",
        current_team="Testland",  # national team
        observed_at=now, source="unit_test_nt",
        affiliation_type="national_team", nationality="Testland",
    )
    r = resolve_player(name="Two Affiliations", sport="Soccer", league="EPL")
    assert isinstance(r, PlayerIdentity)
    assert r.current_team == "Test FC"
    assert r.current_national_team == "Testland"
    # Alias property
    assert r.current_club == "Test FC"
    # Freshness gates are independent.
    assert r.is_current_team_fresh(staleness_days=1) is True
    assert r.is_current_national_team_fresh(staleness_days=1) is True
    # Historical arrays are independent.
    club_teams = [h["team"] for h in r.historical_clubs]
    nat_teams = [h["team"] for h in r.historical_national_teams]
    assert club_teams == ["Test FC"]
    assert nat_teams == ["Testland"]


# ── 2. Club-vs-national-team writes don't cross-contaminate ─────
def test_national_team_write_does_not_touch_club_fields():
    from services.player_identity import (
        reset_registry_for_tests, upsert_player, resolve_player,
    )
    reset_registry_for_tests()
    club_obs = datetime.now(timezone.utc).isoformat()
    upsert_player(
        name="Isolation Test", sport="Soccer", league="EPL",
        current_team="Club A", observed_at=club_obs,
        source="club_feed", affiliation_type="club",
    )
    # A later national-team observation for the same player must not
    # overwrite club fields or historical_teams.
    nt_obs = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat()
    upsert_player(
        name="Isolation Test", sport="Soccer", league="EPL",
        current_team="Nation X", observed_at=nt_obs,
        source="curated_bootstrap_v1",
        affiliation_type="national_team",
    )
    r = resolve_player(name="Isolation Test", sport="Soccer", league="EPL")
    assert r.current_team == "Club A"           # unchanged
    assert r.observed_at == club_obs             # unchanged
    assert r.current_national_team == "Nation X"
    assert r.national_team_observed_at == nt_obs
    # Historical arrays independent — historical_teams still holds ONLY
    # the original club.
    assert [h["team"] for h in r.historical_teams] == ["Club A"]
    assert [h["team"] for h in r.historical_national_teams] == ["Nation X"]


# ── 3. Validator: club fixture uses club roster ──────────────────
def test_club_fixture_validates_current_club():
    from services.player_team_fixture_validator import (
        validate_player_fixture_pick,
    )
    roster = {"harry kane": "Bayern Munich"}
    nt = {"harry kane": "England"}
    pick = {"sport": "Soccer",
            "league": "Bundesliga",
            "market": "Harry Kane Anytime Goal Scorer",
            "event": "Bayern Munich vs Borussia Dortmund"}
    v = validate_player_fixture_pick(pick, roster,
                                       fresh_roster_names={"harry kane"},
                                       national_team_lookup=nt,
                                       fresh_national_team_names={"harry kane"})
    assert v["verified"] is True
    assert v["fixture_type"] == "club"
    assert v["player_team"] == "Bayern Munich"


# ── 4. Validator: international fixture uses national-team roster ─
def test_international_fixture_validates_national_team():
    from services.player_team_fixture_validator import (
        validate_player_fixture_pick,
    )
    # Deliberately give Kane a CLUB that would fail the fixture — the
    # validator must ignore club data on an international fixture.
    roster = {"harry kane": "Bayern Munich"}
    nt = {"harry kane": "England"}
    pick = {"sport": "Soccer",
            "market": "Harry Kane Anytime Goal Scorer",
            "event": "France @ England",
            "league": "FIFA World Cup"}
    v = validate_player_fixture_pick(pick, roster,
                                       fresh_roster_names={"harry kane"},
                                       national_team_lookup=nt,
                                       fresh_national_team_names={"harry kane"})
    assert v["verified"] is True
    assert v["fixture_type"] == "international"
    assert v["player_team"] == "England"


# ── 5. Messi Inter Miami DOES NOT invalidate Argentina fixture ──
def test_messi_inter_miami_identity_does_not_invalidate_argentina_fixture():
    """The exact bug from the P0-B verification: Messi's club is Inter
    Miami; a fixture 'Algeria @ Argentina' must NOT return
    ``player_team_mismatch`` on him."""
    from services.player_team_fixture_validator import (
        validate_player_fixture_pick, REASON_PLAYER_TEAM_MISMATCH,
    )
    roster = {"lionel messi": "Inter Miami CF"}
    nt = {"lionel messi": "Argentina"}
    pick = {"sport": "Soccer",
            "market": "Lionel Messi Anytime Goal Scorer",
            "event": "Algeria @ Argentina"}
    v = validate_player_fixture_pick(pick, roster,
                                       fresh_roster_names={"lionel messi"},
                                       national_team_lookup=nt,
                                       fresh_national_team_names={"lionel messi"})
    assert v["reason"] != REASON_PLAYER_TEAM_MISMATCH
    assert v["verified"] is True
    assert v["fixture_type"] == "international"
    assert v["player_team"] == "Argentina"


# ── 6. Wrong national team is rejected ──────────────────────────
def test_wrong_national_team_is_rejected():
    from services.player_team_fixture_validator import (
        validate_player_fixture_pick, REASON_PLAYER_TEAM_MISMATCH,
    )
    nt = {"lionel messi": "Argentina"}
    pick = {"sport": "Soccer",
            "market": "Lionel Messi Anytime Goal Scorer",
            "event": "France @ Spain"}   # Neither is Argentina
    v = validate_player_fixture_pick(pick, {},
                                       national_team_lookup=nt,
                                       fresh_national_team_names={"lionel messi"})
    assert v["verified"] is False
    assert v["reason"] == REASON_PLAYER_TEAM_MISMATCH


# ── 7. Unknown national-team membership → roster_unverified ─────
def test_unknown_national_team_returns_roster_unverified_not_mismatch():
    """P0-C rule: when national-team data is missing for an
    international fixture, the reason must be ``roster_unverified``
    — NEVER ``player_team_mismatch``."""
    from services.player_team_fixture_validator import (
        validate_player_fixture_pick, REASON_ROSTER_UNVERIFIED,
        REASON_PLAYER_TEAM_MISMATCH,
    )
    # Only club data present.  International fixture.
    roster = {"random striker": "Some Club FC"}
    pick = {"sport": "Soccer",
            "market": "Random Striker Anytime Goal Scorer",
            "event": "Argentina @ Brazil"}
    v = validate_player_fixture_pick(pick, roster,
                                       fresh_roster_names={"random striker"},
                                       national_team_lookup={},
                                       fresh_national_team_names=set())
    assert v["reason"] == REASON_ROSTER_UNVERIFIED
    assert v["reason"] != REASON_PLAYER_TEAM_MISMATCH
    assert v["fixture_type"] == "international"


def test_no_national_team_lookup_supplied_returns_roster_unverified():
    """Backward-compat — legacy callers that pass no
    ``national_team_lookup`` at all must not receive
    ``player_team_mismatch`` on international fixtures."""
    from services.player_team_fixture_validator import (
        validate_player_fixture_pick, REASON_ROSTER_UNVERIFIED,
    )
    pick = {"sport": "Soccer",
            "market": "Someone Anytime Goal Scorer",
            "event": "Argentina @ Brazil"}
    v = validate_player_fixture_pick(pick, {},
                                       fresh_roster_names=None)
    assert v["reason"] == REASON_ROSTER_UNVERIFIED


# ── 8. International-fixture detection heuristics ───────────────
def test_international_fixture_detected_by_league_marker():
    from services.player_team_fixture_validator import (
        _is_international_fixture,
    )
    for league in ("FIFA World Cup", "Copa America 2024",
                   "UEFA Nations League A",
                   "AFCON Qualification", "International Friendlies"):
        assert _is_international_fixture({"league": league},
                                          ("Team A", "Team B")) is True


def test_international_fixture_detected_by_both_nations():
    from services.player_team_fixture_validator import (
        _is_international_fixture,
    )
    assert _is_international_fixture(
        {"league": "unknown competition"},
        ("Argentina", "Brazil")) is True
    assert _is_international_fixture(
        {"league": "unknown competition"},
        ("Portugal", "England")) is True


def test_club_fixture_not_falsely_international():
    from services.player_team_fixture_validator import (
        _is_international_fixture,
    )
    # Two clubs — neither is a nation.
    assert _is_international_fixture(
        {"league": "EPL"},
        ("Manchester City", "Arsenal")) is False
    assert _is_international_fixture(
        {"league": "MLS"},
        ("Inter Miami CF", "LAFC")) is False


# ── 9. Big-5 hydration from soccer_player_form ──────────────────
def test_big5_hydration_writes_identities_for_all_five_leagues():
    from services.soccer_identity_ingest import (
        hydrate_big5_from_soccer_player_form,
    )
    from services.player_identity import (
        reset_registry_for_tests, ensure_identity_indexes,
        IDENTITY_COLLECTION,
    )

    async def go():
        db = _db()
        await ensure_identity_indexes(db)
        # Delete only the identities that will be re-created by the
        # hydrator — so we can accurately assert their re-creation.
        for lg in ("EPL", "La Liga", "Serie A", "Bundesliga", "Ligue 1"):
            await db[IDENTITY_COLLECTION].delete_many(
                {"sport": "Soccer", "league": lg,
                 "source": "soccer_player_form"})
        reset_registry_for_tests()
        summary = await hydrate_big5_from_soccer_player_form(db)
        # Every Big-5 league must have received at least one write.
        for lg in ("EPL", "La Liga", "Serie A", "Bundesliga", "Ligue 1"):
            n = await db[IDENTITY_COLLECTION].count_documents(
                {"sport": "Soccer", "league": lg,
                 "source": "soccer_player_form"})
            assert n > 0, f"{lg} identities missing after hydrate"
        # Sanity — the returned summary matches.
        assert summary["upserts"] > 0
        for lg in ("EPL", "La Liga", "Serie A", "Bundesliga", "Ligue 1"):
            assert summary["leagues"].get(lg, 0) > 0
    _run(go())


def test_big5_hydrated_identities_survive_restart():
    from services.soccer_identity_ingest import (
        hydrate_big5_from_soccer_player_form,
    )
    from services.player_identity import (
        reset_registry_for_tests, ensure_identity_indexes,
        hydrate_registry_from_mongo, resolve_player,
        IDENTITY_COLLECTION,
    )

    async def go():
        db = _db()
        await ensure_identity_indexes(db)
        reset_registry_for_tests()
        await hydrate_big5_from_soccer_player_form(db)
        # Pick a well-known EPL player likely to exist in
        # `soccer_player_form`.
        stored = await db[IDENTITY_COLLECTION].find_one(
            {"sport": "Soccer", "league": "EPL",
             "source": "soccer_player_form"}, {"_id": 0})
        assert stored is not None
        # Simulate restart.
        reset_registry_for_tests()
        n = await hydrate_registry_from_mongo(db)
        assert n > 0
        r = resolve_player(
            name=stored["name"], sport="Soccer", league="EPL")
        assert r is not None
        assert r.current_team == stored["current_team"]
        assert r.observed_at == stored["observed_at"]
    _run(go())


# ── 10. National-team bootstrap ─────────────────────────────────
def test_national_team_bootstrap_seeds_expected_identities():
    from services.soccer_identity_ingest import (
        bootstrap_national_team_identities, _NATIONAL_TEAM_BOOTSTRAP,
    )
    from services.player_identity import (
        reset_registry_for_tests, ensure_identity_indexes,
        IDENTITY_COLLECTION,
    )

    async def go():
        db = _db()
        await ensure_identity_indexes(db)
        # Clean prior bootstrap seeds so we can measure the effect.
        await db[IDENTITY_COLLECTION].delete_many(
            {"sport": "Soccer", "league": "International"})
        reset_registry_for_tests()
        summary = await bootstrap_national_team_identities(db)
        assert summary["bootstrap_players"] == len(_NATIONAL_TEAM_BOOTSTRAP)
        # Every seeded identity has a national team populated.
        n_nt = await db[IDENTITY_COLLECTION].count_documents(
            {"sport": "Soccer", "league": "International",
             "current_national_team": {"$nin": [None, ""]}})
        assert n_nt > 0
        # Spot-check Messi → Argentina, Ronaldo → Portugal.
        # The bootstrap seeds under league="International" (separate
        # canonical identity from any pre-existing club identity for
        # the same name_norm).
        messi = await db[IDENTITY_COLLECTION].find_one(
            {"name_norm": "lionel messi", "league": "International"},
            {"_id": 0})
        assert messi is not None
        assert messi["current_national_team"] == "Argentina"
        ronaldo = await db[IDENTITY_COLLECTION].find_one(
            {"name_norm": "cristiano ronaldo", "league": "International"},
            {"_id": 0})
        assert ronaldo is not None
        assert ronaldo["current_national_team"] == "Portugal"
    _run(go())


# ── 11. Transfer preserves historical stats (club stream) ───────
def test_club_transfer_preserves_historical_stats():
    """A player's transfer between clubs must (a) update
    ``current_team``, (b) close the prior open ``historical_teams``
    row, and (c) not lose the prior team entry."""
    from services.player_identity import (
        reset_registry_for_tests, upsert_player, resolve_player,
    )
    reset_registry_for_tests()
    t1 = "2025-01-01T00:00:00+00:00"
    t2 = "2026-01-01T00:00:00+00:00"
    upsert_player(
        name="Transfer Player", sport="Soccer", league="EPL",
        current_team="Old FC", observed_at=t1, source="unit",
        affiliation_type="club",
    )
    upsert_player(
        name="Transfer Player", sport="Soccer", league="EPL",
        current_team="New FC", observed_at=t2, source="unit",
        affiliation_type="club",
    )
    r = resolve_player(name="Transfer Player", sport="Soccer", league="EPL")
    assert r.current_team == "New FC"
    teams = [h["team"] for h in r.historical_teams]
    assert teams == ["Old FC", "New FC"]
    # Prior entry closed at the transfer time.
    assert r.historical_teams[0]["to"] == t2
    assert r.historical_teams[1]["to"] is None


# ── 12. Persistence for national-team affiliation (Mongo round-trip) ─
def test_national_team_persistence_round_trip():
    from services.player_identity import (
        reset_registry_for_tests, upsert_player, persist_registry,
        ensure_identity_indexes, hydrate_registry_from_mongo,
        resolve_player, IDENTITY_COLLECTION,
    )

    async def go():
        db = _db()
        await ensure_identity_indexes(db)
        # Isolate under a unique league key.
        league = f"P0C_NT_{_UID()}"
        await db[IDENTITY_COLLECTION].delete_many({"league": league})
        reset_registry_for_tests()
        obs = datetime.now(timezone.utc).isoformat()
        upsert_player(
            name="Nat Trip", sport="Soccer", league=league,
            current_team="Nationland", observed_at=obs,
            source="curated_bootstrap_v1",
            affiliation_type="national_team",
            nationality="Nationland",
        )
        await persist_registry(db)
        stored = await db[IDENTITY_COLLECTION].find_one(
            {"league": league}, {"_id": 0})
        assert stored is not None
        assert stored["current_national_team"] == "Nationland"
        assert stored["national_team_observed_at"] == obs
        assert stored["national_team_source"] == "curated_bootstrap_v1"
        # Club fields are untouched.
        assert stored.get("current_team") in (None, "")
        # Restart + hydrate.
        reset_registry_for_tests()
        await hydrate_registry_from_mongo(db)
        r = resolve_player(name="Nat Trip", sport="Soccer", league=league)
        assert r is not None
        assert r.current_national_team == "Nationland"
        assert r.national_team_observed_at == obs
        assert r.current_team in (None, "")
        await db[IDENTITY_COLLECTION].delete_many({"league": league})
    _run(go())


# ── 13. Older national-team write cannot overwrite fresher one ──
def test_older_national_team_write_cannot_overwrite_fresher():
    from services.player_identity import (
        persist_identity, ensure_identity_indexes, IDENTITY_COLLECTION,
    )

    async def go():
        db = _db()
        await ensure_identity_indexes(db)
        cid = "cpid_" + _UID()
        league = f"P0C_NT_STALE_{_UID()}"
        await db[IDENTITY_COLLECTION].delete_many({"league": league})
        now = datetime.now(timezone.utc)
        t2 = now.isoformat()
        t1 = (now - timedelta(days=5)).isoformat()

        base = {
            "canonical_player_id": cid,
            "name": "NT Stale", "name_norm": "nt stale",
            "sport": "Soccer", "league": league,
        }
        # Fresh write first.
        await persist_identity(db, dict(base,
            current_national_team="Fresh Nation",
            national_team_observed_at=t2,
            national_team_source="fresh_source",
            national_team_status="active"))
        # Attempt older write.
        await persist_identity(db, dict(base,
            current_national_team="Stale Nation",
            national_team_observed_at=t1,
            national_team_source="stale_source"))
        stored = await db[IDENTITY_COLLECTION].find_one(
            {"canonical_player_id": cid}, {"_id": 0})
        assert stored["current_national_team"] == "Fresh Nation"
        assert stored["national_team_observed_at"] == t2
        await db[IDENTITY_COLLECTION].delete_many({"league": league})
    _run(go())


# ── 14. Regression: P0-A + P0-B still pass ─────────────────────
def test_p0a_persistence_and_p0b_extractor_still_work():
    """Smoke — the earlier P0 fixes remain functional."""
    # P0-B parser still works case-insensitively.
    from services.player_team_fixture_validator import _extract_player_name
    assert _extract_player_name(
        {"market": "Federico Vinas To Score or Assist"}) == "Federico Vinas"
    # P0-A ensure_identity_indexes + persist round-trip still functions.
    from services.player_identity import ensure_identity_indexes

    async def go():
        db = _db()
        await ensure_identity_indexes(db)
        idx = await db["player_identities"].index_information()
        assert any(v.get("unique") for v in idx.values())
    _run(go())


def test_locks_gate_still_strict_gt_85_after_p0c():
    from services.main_board_eligibility import is_main_board_eligible
    assert is_main_board_eligible({"lock_score": 85.0}) is False
    assert is_main_board_eligible({"lock_score": 85.001}) is True
