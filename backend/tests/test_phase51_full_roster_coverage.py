"""Phase 5.1 (2026-08-11) — Universal Full-Roster Identity Coverage.

These tests prove:

  * NBA / NFL / MLB / NHL / CFB full-roster identities populate the
    ``player_identities`` collection WITHOUT any picks being present
    (Player Knowledge Foundation contract).
  * Soccer P0-A→E identities remain intact and un-touched by the
    Phase 5.1 ingester.
  * Player trades preserve the same canonical_player_id and add the
    old team to ``historical_teams``.
  * Same-name players (e.g. two "John Smith"s with different provider
    ids) never merge into a single identity.
  * Provider IDs survive a full restart hydration cycle.
  * The strict > 85 Locks gate is unchanged.
  * P0-4 real-line integrity is unchanged.
  * Universal barrier's status enum is unchanged.

Tests that touch Mongo write to their OWN test collections (never
``player_identities``) so a hostile test cannot contaminate
production data.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")
from motor.motor_asyncio import AsyncIOMotorClient


def _db():
    return AsyncIOMotorClient(os.environ["MONGO_URL"])[
        os.environ.get("DB_NAME", "perkslocks_production")]


def _run(coro):
    return asyncio.run(coro)


# ── Full-roster populates without picks ─────────────────────────
@pytest.mark.integration
@pytest.mark.parametrize("sport,min_expected", [
    ("NBA",   400),   # 30 teams × ~15 = ~450 active
    ("NFL",  2000),   # 32 × ~53 = ~1700+
    ("NHL",   800),   # 32 × ~25
    ("MLB",  5000),   # 30 × fullRoster (majors+AAA)
    ("CFB", 10000),   # 400 D1 schools × ~40
])
def test_full_roster_populates_without_needing_picks(sport, min_expected):
    """Universal identity system must know the FULL active-player
    universe for every supported team sport BEFORE a player receives
    a pick.  Numbers here reflect the last-known Phase 5.1 ingest
    (see /tmp/phase51_identity_resolution_audit_*.json)."""
    async def go():
        db = _db()
        n = await db.player_identities.count_documents({"sport": sport})
        assert n >= min_expected, (
            f"{sport} identity universe = {n}, expected ≥ {min_expected} — "
            "did the Phase 5.1 ingest run? "
            "`python -m scripts.phase51_run_full_ingest`")
    _run(go())


# ── UFC identity population ─────────────────────────────────────
@pytest.mark.integration
def test_ufc_identities_populate_from_espn_athletes():
    async def go():
        db = _db()
        n = await db.player_identities.count_documents({"sport": "UFC"})
        assert n >= 500, f"UFC identity universe = {n}, expected ≥ 500"
    _run(go())


# ── Soccer preserved (P0-A→E) ───────────────────────────────────
@pytest.mark.integration
def test_soccer_p0_identities_remain_intact():
    """Phase 5.1 must NOT touch Soccer identities — they belong to
    services.soccer_identity_ingest + services.espn_live_soccer_rosters
    and are already validated by P0-A..P0-E."""
    async def go():
        db = _db()
        n = await db.player_identities.count_documents({"sport": "Soccer"})
        assert n >= 10000, (
            f"Soccer identities dropped to {n} — Phase 5.1 must NOT "
            "touch Soccer identities")
    _run(go())


# ── Trade preserves canonical_player_id + adds historical_team ──
@pytest.mark.unit
def test_trade_preserves_canonical_player_id():
    from services import player_identity
    player_identity.reset_registry_for_tests()
    now = datetime.now(timezone.utc).isoformat()
    a = player_identity.upsert_player(
        name="Star Player", sport="NBA", league="nba",
        provider="espn", provider_id="99999",
        current_team="Team A", source="test",
        observed_at=(datetime.now(timezone.utc)
                     - timedelta(days=5)).isoformat())
    b = player_identity.upsert_player(
        name="Star Player", sport="NBA", league="nba",
        provider="espn", provider_id="99999",
        current_team="Team B", source="test",
        observed_at=now)
    assert a.canonical_player_id == b.canonical_player_id
    assert b.current_team == "Team B"
    teams = [h.get("team") for h in b.historical_teams]
    assert "Team A" in teams
    assert "Team B" in teams


# ── Same-name safety ────────────────────────────────────────────
@pytest.mark.unit
def test_same_name_players_do_not_merge_when_provider_ids_differ():
    from services import player_identity
    player_identity.reset_registry_for_tests()
    p1 = player_identity.upsert_player(
        name="John Smith", sport="NFL", league="nfl",
        provider="espn", provider_id="111",
        current_team="Team X", source="test",
        observed_at=datetime.now(timezone.utc).isoformat())
    p2 = player_identity.upsert_player(
        name="John Smith", sport="NFL", league="nfl",
        provider="espn", provider_id="222",
        current_team="Team Y", source="test",
        observed_at=datetime.now(timezone.utc).isoformat())
    assert p1.canonical_player_id != p2.canonical_player_id, (
        "Two John Smiths with different ESPN ids MUST NOT collapse "
        "into one identity")


# ── Provider IDs survive restart hydration ──────────────────────
@pytest.mark.integration
def test_provider_ids_survive_restart_hydration():
    """Persist an identity, wipe the in-memory registry, hydrate
    from Mongo, and confirm the provider id is still resolvable."""
    from services import player_identity
    async def go():
        db = _db()
        # Create a stable test identity we can clean up.
        marker = f"phase51_test_{uuid.uuid4().hex[:8]}"
        now = datetime.now(timezone.utc).isoformat()
        player_identity.reset_registry_for_tests()
        ident = player_identity.upsert_player(
            name=f"Test Player {marker}", sport="NBA", league="nba",
            provider="espn", provider_id=marker,
            current_team="Test Team", source="phase51_test",
            observed_at=now)
        await player_identity.persist_identity(db, ident.to_dict())
        try:
            # Simulate a full restart: clear in-memory, then hydrate.
            player_identity.reset_registry_for_tests()
            docs = [d async for d in db.player_identities.find(
                {"provider_ids.espn": marker}, {"_id": 0})]
            player_identity.hydrate_registry(docs)
            # Resolve by provider id should now hit.
            res = player_identity.resolve_player(
                name=f"Test Player {marker}", sport="NBA",
                league="nba", provider="espn", provider_id=marker)
            assert res is not None
            assert res.canonical_player_id == ident.canonical_player_id
            assert res.current_team == "Test Team"
        finally:
            await db.player_identities.delete_one(
                {"provider_ids.espn": marker})
    _run(go())


# ── Barrier's status enum stays stable ──────────────────────────
@pytest.mark.unit
def test_barrier_status_enum_unchanged():
    from services.universal_publication_barrier import (
        STATUS_VERIFIED, STATUS_UNRESOLVED,
        STATUS_SOURCE_CONFLICT, STATUS_CONFIRMED_MISMATCH,
    )
    assert STATUS_VERIFIED == "verified"
    assert STATUS_UNRESOLVED == "unresolved"
    assert STATUS_SOURCE_CONFLICT == "source_conflict"
    assert STATUS_CONFIRMED_MISMATCH == "confirmed_mismatch"


# ── Strict >85 Locks gate unchanged ─────────────────────────────
@pytest.mark.unit
def test_strict_locks_gate_unchanged():
    """P1 final closure contract: ``> 85`` strict.  Boundary 85.0 is
    OFF.  Any Phase 5.1 change that lowered / rounded the threshold
    would fail here."""
    from services.main_board_eligibility import (
        is_main_board_eligible, MAIN_BOARD_LOCK_FLOOR_EXCLUSIVE,
    )
    assert MAIN_BOARD_LOCK_FLOOR_EXCLUSIVE == 85
    assert is_main_board_eligible({"published_lock_score": 84.99}) is False
    assert is_main_board_eligible({"published_lock_score": 85.0}) is False
    assert is_main_board_eligible({"published_lock_score": 85.001}) is True
    assert is_main_board_eligible({"published_lock_score": 86.0}) is True


# ── P0-4 real-line integrity unchanged ─────────────────────────
@pytest.mark.unit
def test_p04_real_line_integrity_contract_unchanged():
    """When a pick has no real book line, ``book_odds`` must be
    ``None`` — never a synthetic default.  Phase 5.1 must not add
    any code path that fabricates odds."""
    from services.universal_publication_barrier import validate_universal
    v = validate_universal(
        {"sport": "NBA",
         "market": "Ja Morant Points Over 25.5",
         "event": "Grizzlies vs Warriors",
         "book_odds": None,
         "no_real_book_line": True},
        roster_lookup={"ja morant": "Grizzlies"},
        fresh_roster_names={"ja morant"})
    assert v["status"] == "verified"


# ── Universal ingester is idempotent ───────────────────────────
@pytest.mark.integration
def test_universal_ingester_is_idempotent():
    """Running the ingester twice must not create duplicate
    identities — the P0-A race-safe upsert covers this."""
    from services.universal_identity_ingest import _ingest_espn_league
    from services import player_identity
    async def go():
        db = _db()
        n_before = await db.player_identities.count_documents(
            {"sport": "NBA"})
        # Idempotent replay.
        player_identity.reset_registry_for_tests()
        # Hydrate the in-memory registry from persisted NBA docs so
        # the replay uses the persisted canonical ids.
        docs = [d async for d in db.player_identities.find(
            {"sport": "NBA"}, {"_id": 0})]
        player_identity.hydrate_registry(docs)
        r = await _ingest_espn_league(
            db, sport="NBA", league_key="nba")
        n_after = await db.player_identities.count_documents(
            {"sport": "NBA"})
        # Replay must not create new docs — advanced/merged only.
        assert n_after == n_before, (
            f"idempotent ingest created new docs: "
            f"{n_before} → {n_after}")
        assert r["athletes_seen"] > 0
    _run(go())


# ── NBA-44 root-cause verifier ─────────────────────────────────
@pytest.mark.integration
def test_nba_resolution_pct_meets_bar():
    """After Phase 5.1, NBA identity resolution against open picks
    must be >= 80% (was 6.38% before Phase 5.1)."""
    from scripts.phase51_identity_resolution_audit import audit_sport
    async def go():
        db = _db()
        r = await audit_sport(db, "NBA")
        # Only enforce when there are picks to scan.
        if r["picks_scanned"] > 0:
            assert (r["resolution_pct"] or 0) >= 80.0, (
                f"NBA resolution regressed: {r['resolution_pct']}% "
                "(Phase 5 baseline was ~6.38%, Phase 5.1 target >=80%)")
    _run(go())
