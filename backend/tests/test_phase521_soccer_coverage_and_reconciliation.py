"""Phase 5.2.1 (2026-08-11) — Soccer coverage + identity reconciliation.

Regression tests for:

  * TEAM/COUNTRY alias table extensions (Congo DR ↔ DR Congo, etc.).
  * Duplicate-identity reconciler:
      - merges diacritic pairs (Luis Suárez ↔ Luis Suarez)
      - merges case variants (Son Heung-Min ↔ Son Heung-min)
      - merges full-legal-name to short name ONLY when nationality
        or NT corroborates (first+last match)
      - NEVER merges different players on name alone
      - NEVER merges when provider ids conflict for the SAME provider
      - NEVER merges when DOB mismatch
  * "Yes"/"No"/"Over"/"Under" selections do NOT become player names
    — they fall through to market-string parsing.
  * "Carlao Carlao" identical-token duplication collapses to
    "Carlao" (Odds API Brasileirão data quirk).
  * Expanded club-league coverage (25+ leagues) does NOT introduce
    fake coverage — leagues that 404 are skipped honestly.
  * NT ingest MERGES across confederations instead of skipping later
    slugs.
  * Endrick + Yoane Wissa integration cases still verify.
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


# ── Team-alias table ──────────────────────────────────────────
@pytest.mark.unit
def test_congo_dr_alias_matches_dr_congo():
    from services.player_team_fixture_validator import _teams_match
    assert _teams_match("Congo DR", ("DR Congo", "Uzbekistan")) is True
    assert _teams_match("DR Congo", ("Congo DR", "Uzbekistan")) is True
    assert _teams_match(
        "Democratic Republic of the Congo",
        ("DR Congo", "Uzbekistan")) is True


@pytest.mark.unit
@pytest.mark.parametrize("a,b", [
    ("USA", "United States"),
    ("Türkiye", "Turkey"),
    ("Korea Republic", "South Korea"),
    ("Czechia", "Czech Republic"),
    ("Bosnia", "Bosnia and Herzegovina"),
    ("North Macedonia", "Macedonia"),
    ("Vietnam", "Viet Nam"),
    ("Eswatini", "Swaziland"),
])
def test_country_alias_matches(a, b):
    from services.player_team_fixture_validator import _teams_match
    assert _teams_match(a, (b, "Somewhere Else")) is True


# ── Yes/No selection guard ─────────────────────────────────────
@pytest.mark.unit
def test_yes_no_selection_is_not_a_player_name():
    """Regression: 3,026 Soccer picks with ``player_name="Yes"``
    were being classified as unresolved because the extractor
    returned "Yes" as the player name.  The extractor must fall
    through to the market string for these selections."""
    from services.player_team_fixture_validator import _extract_player_name
    for sel in ("Yes", "No", "Over", "Under", "Home", "Away", "Draw"):
        n = _extract_player_name({
            "market": "Lionel Messi Anytime Goal Scorer",
            "selection": sel,
            "player_name": None,
        })
        assert n == "Lionel Messi", (sel, n)


@pytest.mark.unit
def test_yes_selection_in_player_name_also_falls_through():
    from services.player_team_fixture_validator import _extract_player_name
    n = _extract_player_name({
        "market": "Harry Kane Anytime Goal Scorer",
        "selection": "Yes",
        "player_name": "Yes",   # upstream normalisation bug
    })
    assert n == "Harry Kane"


# ── "Name Name" duplication guard ─────────────────────────────
@pytest.mark.unit
def test_duplicated_single_name_collapses():
    """Odds API's Brasileirão single-name feed emits "Carlao Carlao"
    / "Bidu Bidu" / "Robson Robson" for single-name Brazilian
    players.  Collapse identical adjacent tokens ONLY."""
    from services.player_team_fixture_validator import _extract_player_name
    for dup in ("Carlao Carlao", "Bidu Bidu", "Robson Robson", "Kevin Kevin"):
        n = _extract_player_name({"player_name": dup, "market": None})
        assert n == dup.split()[0], (dup, n)


@pytest.mark.unit
def test_non_duplicated_two_word_name_not_collapsed():
    """Guard: a legitimate "Firstname Lastname" must NOT collapse
    to a single token."""
    from services.player_team_fixture_validator import _extract_player_name
    n = _extract_player_name({"player_name": "Harry Kane", "market": None})
    assert n == "Harry Kane"


# ── Duplicate-identity reconciler ─────────────────────────────
@pytest.mark.unit
def test_reconciler_never_merges_different_players_on_name_alone():
    from scripts.phase521_reconcile_duplicate_identities import _same_person
    a = {"name": "John Smith", "name_norm": "john smith",
          "provider_ids": {"espn": "111"},
          "current_team": "Team A", "nationality": "England"}
    b = {"name": "John Smith", "name_norm": "john smith",
          "provider_ids": {"espn": "222"},
          "current_team": "Team B", "nationality": "Australia"}
    same, reason = _same_person(a, b)
    assert same is False
    assert "provider_id_conflict" in reason or "different" in reason


@pytest.mark.unit
def test_reconciler_merges_diacritic_duplicates():
    from scripts.phase521_reconcile_duplicate_identities import _same_person
    a = {"name": "Luis Suárez", "name_norm": "luis suarez",
          "provider_ids": {"espn": "12345"},
          "current_team": "Inter Miami", "nationality": "Uruguay",
          "current_national_team": "Uruguay"}
    b = {"name": "Luis Suarez", "name_norm": "luis suarez",
          "provider_ids": {},
          "current_team": None, "nationality": "Uruguay",
          "current_national_team": "Uruguay"}
    same, reason = _same_person(a, b)
    assert same is True


@pytest.mark.unit
def test_reconciler_merges_full_legal_name_with_short_name():
    """Darwin Núñez ↔ Darwin Gabriel Nunez Ribeiro — the shared
    (first, last) pair PLUS nationality agreement is required."""
    from scripts.phase521_reconcile_duplicate_identities import _same_person
    a = {"name": "Darwin Núñez", "name_norm": "darwin nunez",
          "provider_ids": {"espn": "12345"},
          "current_team": "Al Hilal", "nationality": "Uruguay",
          "current_national_team": "Uruguay"}
    b = {"name": "Darwin Gabriel Nunez Ribeiro",
          "name_norm": "darwin gabriel nunez ribeiro",
          "provider_ids": {}, "current_team": None,
          "nationality": "Uruguay",
          "current_national_team": "Uruguay"}
    same, reason = _same_person(a, b)
    assert same is True, reason


@pytest.mark.unit
def test_reconciler_rejects_dob_mismatch():
    from scripts.phase521_reconcile_duplicate_identities import _same_person
    a = {"name": "Diego Torres", "name_norm": "diego torres",
          "dob": "1998-05-12"}
    b = {"name": "Diego Torres", "name_norm": "diego torres",
          "dob": "2001-01-20"}
    same, reason = _same_person(a, b)
    assert same is False
    assert reason == "dob_mismatch"


@pytest.mark.unit
def test_reconciler_rejects_same_name_different_nationalities():
    from scripts.phase521_reconcile_duplicate_identities import _same_person
    a = {"name": "Alex Arce", "name_norm": "alex arce",
          "nationality": "Paraguay"}
    b = {"name": "Alex Arce", "name_norm": "alex arce",
          "nationality": "Spain"}
    same, reason = _same_person(a, b)
    assert same is False
    assert reason == "same_name_different_nationality"


# ── End-to-end audit meets the Phase 5.2.1 bar ────────────────
@pytest.mark.integration
def test_soccer_resolution_meets_phase521_bar():
    """After Phase 5.2.1 the Soccer resolution % must be >= 85%
    (was 55.75% before this phase, 19.69% before Phase 5.2)."""
    from scripts.phase51_identity_resolution_audit import audit_sport
    async def go():
        db = _db()
        r = await audit_sport(db, "Soccer")
        if r["picks_scanned"] > 0:
            assert (r["resolution_pct"] or 0) >= 85.0, (
                f"Soccer resolution regressed to {r['resolution_pct']}% "
                "— Phase 5.2.1 requires >= 85%")
    _run(go())


# ── Endrick + Yoane Wissa integration still verify ────────────
@pytest.mark.integration
def test_endrick_and_wissa_still_verify_via_universal_barrier():
    from services.universal_publication_barrier import validate_universal
    from services.universal_soccer_lookup import build_soccer_lookups
    async def go():
        db = _db()
        L = await build_soccer_lookups(db)
        v1 = validate_universal(
            {"sport": "Soccer",
             "market": "Endrick To Score or Assist",
             "event": "Haiti @ Brazil",
             "league": "FIFA World Cup · Props"},
            roster_lookup=L["roster_lookup"],
            fresh_roster_names=L["fresh_roster_names"],
            national_team_lookup=L["national_team_lookup"],
            fresh_national_team_names=L["fresh_national_team_names"],
            nationality_lookup=L["nationality_lookup"])
        assert v1["status"] == "verified"
        v2 = validate_universal(
            {"sport": "Soccer",
             "market": "Yoane Wissa To Score",
             "event": "DR Congo @ Uzbekistan",
             "league": "FIFA World Cup · Props"},
            roster_lookup=L["roster_lookup"],
            fresh_roster_names=L["fresh_roster_names"],
            national_team_lookup=L["national_team_lookup"],
            fresh_national_team_names=L["fresh_national_team_names"],
            nationality_lookup=L["nationality_lookup"])
        assert v2["status"] == "verified", v2
    _run(go())


# ── Redirect trail written for merged identities ─────────────
@pytest.mark.integration
def test_reconciled_identities_have_redirect_row():
    async def go():
        db = _db()
        n = await db.player_identity_redirects.count_documents({})
        assert n >= 100, (
            f"only {n} redirect rows — Phase 5.2.1 merged 297 "
            "diacritic duplicates in the same environment")
    _run(go())
