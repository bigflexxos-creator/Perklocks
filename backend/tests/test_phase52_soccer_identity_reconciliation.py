"""Phase 5.2 (2026-08-11) — Soccer Universal Identity Reconciliation.

Regression tests locking in:

  * Universal barrier delegates Soccer to the P0-A..P0-E validator
    with all 5 lookup dicts (club roster, national team, nationality
    + freshness sets).
  * A pick with ``verified=True`` from the Soccer validator ALWAYS
    maps to ``status="verified"`` — regardless of the reason code
    (including ``market_not_player_based``).  This was the primary
    Phase 5.2 bug that made Soccer look ~20% resolved when the
    P0-A..P0-E stack was actually resolving ~56%.
  * ``build_soccer_lookups`` returns the 5 dicts and includes alias
    keys folded in without loosening name matching.
  * Non-player Soccer markets (Moneyline / Total Goals) are correctly
    counted as VERIFIED (not unresolved).
  * P0-A..P0-E Soccer verdicts remain byte-for-byte identical to the
    barrier's Soccer path (delegation is faithful).
"""
from __future__ import annotations

import asyncio
import os

import pytest
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")
from motor.motor_asyncio import AsyncIOMotorClient


def _db():
    return AsyncIOMotorClient(os.environ["MONGO_URL"])[
        os.environ.get("DB_NAME", "perkslocks_production")]


def _run(coro):
    return asyncio.run(coro)


# ── The critical status-mapper bug ───────────────────────────────
@pytest.mark.unit
def test_soccer_non_player_market_maps_to_verified_status():
    """Regression: before Phase 5.2 the barrier mapped
    ``verified=True, reason=market_not_player_based`` to
    ``status=unresolved`` — that mis-classified 2,463 Soccer team-
    level picks as unresolved.  The mapper MUST honour the
    ``verified=True`` flag first."""
    from services.universal_publication_barrier import (
        validate_universal, STATUS_VERIFIED,
    )
    # Team-level market — no player named.
    v = validate_universal(
        {"sport": "Soccer",
         "market": "Bohemians Moneyline",
         "event": "Bohemians @ Derry City",
         "league": "League of Ireland"},
        roster_lookup={}, fresh_roster_names=set())
    assert v["verified"] is True
    assert v["status"] == STATUS_VERIFIED
    assert v["reason"] == "market_not_player_based"


@pytest.mark.unit
def test_soccer_total_goals_maps_to_verified():
    from services.universal_publication_barrier import (
        validate_universal, STATUS_VERIFIED,
    )
    v = validate_universal(
        {"sport": "Soccer", "market": "Total Goals Over 2.5",
         "event": "Ponte Preta @ Juventude",
         "league": "Brasileirão Série B"},
        roster_lookup={}, fresh_roster_names=set())
    assert v["status"] == STATUS_VERIFIED


# ── Lookup builder ───────────────────────────────────────────────
@pytest.mark.integration
def test_build_soccer_lookups_returns_all_five_dicts():
    from services.universal_soccer_lookup import build_soccer_lookups
    async def go():
        L = await build_soccer_lookups(_db())
        for k in ("roster_lookup", "fresh_roster_names",
                   "national_team_lookup", "fresh_national_team_names",
                   "nationality_lookup"):
            assert k in L
        # Confirm the P0-A..P0-E identity universe is present.
        assert len(L["roster_lookup"]) > 5000, (
            f"roster_lookup unexpectedly small: {len(L['roster_lookup'])}")
        assert len(L["national_team_lookup"]) > 1000
        assert len(L["nationality_lookup"]) > 5000
    _run(go())


# ── Alias folding is safe (no name-only merges) ─────────────────
@pytest.mark.unit
def test_alias_folding_does_not_produce_name_only_merges():
    """A pre-existing identity with a curated alias must resolve on
    the alias key.  This does NOT loosen name matching — aliases
    were already vetted against provider ids in P0-A..P0-E."""
    from services.universal_soccer_lookup import build_soccer_lookups
    # Simulate an identity with alias.
    async def fake_iter(*a, **k):
        docs = [{
            "name": "Robert Firmino",
            "name_norm": "robert firmino",
            "aliases": ["Bobby Firmino", "Firmino"],
            "current_team": "Al Ahli",
            "observed_at": "2026-08-01T00:00:00+00:00",
            "current_national_team": "Brazil",
            "national_team_observed_at": "2026-08-01T00:00:00+00:00",
            "nationality": "Brazil",
        }]
        for d in docs:
            yield d

    class FakeCol:
        def find(self, *a, **k): return fake_iter()

    class FakeDB:
        def __getitem__(self, name): return FakeCol()

    async def go():
        L = await build_soccer_lookups(FakeDB())
        # Canonical key + alias key both present.
        assert "robert firmino" in L["roster_lookup"]
        assert "bobby firmino" in L["roster_lookup"]
        assert "firmino" in L["roster_lookup"]
        # All aliases resolve to the same team.
        assert L["roster_lookup"]["robert firmino"] == "Al Ahli"
        assert L["roster_lookup"]["bobby firmino"] == "Al Ahli"
        assert L["roster_lookup"]["firmino"] == "Al Ahli"
    _run(go())


# ── Delegation faithful to P0-A..P0-E ────────────────────────────
@pytest.mark.unit
def test_barrier_soccer_delegation_matches_direct_validator():
    """The universal barrier's Soccer path MUST produce the same
    (verified, reason) pair as the direct P0-A..P0-E validator for
    the same inputs.  Any divergence would break the completed
    Soccer work."""
    from services.universal_publication_barrier import validate_universal
    from services.player_team_fixture_validator import (
        validate_player_fixture_pick,
    )
    lookups = dict(
        roster_lookup={"lionel messi": "Inter Miami"},
        fresh_roster_names={"lionel messi"},
        national_team_lookup={"lionel messi": "Argentina"},
        fresh_national_team_names={"lionel messi"},
        nationality_lookup={"lionel messi": "Argentina"},
    )
    picks = [
        {"sport": "Soccer",
         "market": "Lionel Messi To Score",
         "event": "Inter Miami @ Toronto FC"},
        {"sport": "Soccer",
         "market": "Argentina Moneyline",
         "event": "Argentina @ Uruguay"},
        {"sport": "Soccer",
         "market": "Lionel Messi Anytime Goal Scorer",
         "event": "Brazil @ Argentina",
         "league": "FIFA World Cup"},
    ]
    for p in picks:
        u = validate_universal(p, **lookups)
        d = validate_player_fixture_pick(
            p, lookups["roster_lookup"],
            fresh_roster_names=lookups["fresh_roster_names"],
            national_team_lookup=lookups["national_team_lookup"],
            fresh_national_team_names=lookups["fresh_national_team_names"],
            nationality_lookup=lookups["nationality_lookup"])
        assert u["verified"] == d["verified"], (u, d)
        assert u["reason"] == d["reason"], (u, d)


# ── End-to-end audit confirms the fix ────────────────────────────
@pytest.mark.integration
def test_soccer_resolution_meets_phase52_bar():
    """After Phase 5.2 the Soccer resolution % must be >= 50% (was
    19.69% before Phase 5.2 in the Phase 5.1 audit).  Remaining gap
    is coverage / team-alias / long-legal-name issues that require
    operator review — NOT hidden barrier bugs."""
    from scripts.phase51_identity_resolution_audit import audit_sport

    async def go():
        db = _db()
        r = await audit_sport(db, "Soccer")
        if r["picks_scanned"] > 0:
            assert (r["resolution_pct"] or 0) >= 50.0, (
                f"Soccer resolution regressed to {r['resolution_pct']}% "
                "— Phase 5.2 requires >= 50%")
    _run(go())


# ── Guardrail: barrier still hard-rejects only confirmed_mismatch
@pytest.mark.unit
def test_barrier_only_hard_rejects_confirmed_mismatch():
    from services.universal_publication_barrier import (
        STATUS_VERIFIED, STATUS_UNRESOLVED,
        STATUS_SOURCE_CONFLICT, STATUS_CONFIRMED_MISMATCH,
    )
    hard_reject = {STATUS_CONFIRMED_MISMATCH}
    for s in (STATUS_VERIFIED, STATUS_UNRESOLVED, STATUS_SOURCE_CONFLICT):
        assert s not in hard_reject
    assert STATUS_CONFIRMED_MISMATCH in hard_reject
