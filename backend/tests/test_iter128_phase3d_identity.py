"""Phase 3D — Identity contract & resolver tests (dry-run first)."""
from __future__ import annotations

import asyncio
import os

import pytest
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

from services.identity_contracts import (
    EventIdentity, TeamIdentity, PlayerIdentity, MarketContractIdentity,
    PredictionIdentity, BetLegIdentity,
)
from services.identity_resolver import (
    normalize_name,
    resolve_event, resolve_team, resolve_player,
    resolve_market_contract, resolve_prediction, resolve_bet_leg,
    dry_run_scan_collection,
)


# ── 1. Deterministic canonical IDs from provider IDs ─────────────────
def test_provider_ids_produce_deterministic_canonical_ids():
    a = resolve_player(provider="odds_api", provider_player_id="12345",
                       display_name="A", sport="MLB")
    b = resolve_player(provider="odds_api", provider_player_id="12345",
                       display_name="A", sport="MLB")
    assert a.canonical_player_id == b.canonical_player_id
    assert a.identity_quality == "provider"


# ── 2. Same display name / different provider IDs → distinct ─────────
def test_same_display_name_different_provider_ids_stay_distinct():
    a = resolve_player(provider="odds_api", provider_player_id="1",
                       display_name="Aaron Judge", sport="MLB")
    b = resolve_player(provider="odds_api", provider_player_id="2",
                       display_name="Aaron Judge", sport="MLB")
    assert a.canonical_player_id != b.canonical_player_id


# ── 3. Same player name on different teams → distinct ────────────────
def test_same_player_name_different_teams_stay_distinct_in_fallback():
    a = resolve_player(display_name="Michael Smith", sport="Soccer",
                       team_id="team_x")
    b = resolve_player(display_name="Michael Smith", sport="Soccer",
                       team_id="team_y")
    assert a.canonical_player_id != b.canonical_player_id
    assert a.identity_quality == "fallback"


# ── 4. Same event text / different providers → distinct until mapped ─
def test_same_event_text_different_providers_stay_distinct():
    a = resolve_event(provider="odds_api", provider_event_id="e1",
                      sport_key="baseball_mlb")
    b = resolve_event(provider="football_data", provider_event_id="e1",
                      sport_key="baseball_mlb")
    assert a.canonical_event_id != b.canonical_event_id


# ── 5. Different lines → distinct market contracts ───────────────────
def test_market_contracts_with_different_lines_stay_distinct():
    a = resolve_market_contract(canonical_event_id="e", market_key="mlb_hrr",
                                side="over", line=0.5, bookmaker="dk")
    b = resolve_market_contract(canonical_event_id="e", market_key="mlb_hrr",
                                side="over", line=1.5, bookmaker="dk")
    c = resolve_market_contract(canonical_event_id="e", market_key="mlb_hrr",
                                side="over", line=2.5, bookmaker="dk")
    assert len({a.canonical_market_contract_id,
                b.canonical_market_contract_id,
                c.canonical_market_contract_id}) == 3


# ── 6. Different bookmakers → distinct market contracts ──────────────
def test_market_contracts_with_different_sportsbooks_stay_distinct():
    a = resolve_market_contract(canonical_event_id="e", market_key="mlb_hrr",
                                side="over", line=1.5, bookmaker="dk")
    b = resolve_market_contract(canonical_event_id="e", market_key="mlb_hrr",
                                side="over", line=1.5, bookmaker="fd")
    assert a.canonical_market_contract_id != b.canonical_market_contract_id


# ── 7. Provider ID beats alias matching ──────────────────────────────
def test_provider_id_beats_alias_matching():
    a = resolve_player(provider="odds_api", provider_player_id="99",
                       display_name="Different Name", sport="MLB",
                       team_id="t")
    b = resolve_player(display_name="Different Name", sport="MLB",
                       team_id="t")
    assert a.identity_quality == "provider"
    assert b.identity_quality == "fallback"
    assert a.canonical_player_id != b.canonical_player_id


# ── 8. Ambiguous aliases do not auto-resolve ─────────────────────────
def test_ambiguous_alias_missing_context_becomes_unresolved():
    # Missing team_id → unresolved, not silently merged.
    r = resolve_player(display_name="Aaron Judge", sport="MLB")
    assert r.identity_quality == "unresolved"


# ── 9. First-token name matching is NEVER used ───────────────────────
def test_first_token_matching_is_not_used():
    # Two players with same first name but different surnames must
    # produce different canonical ids.
    a = resolve_player(display_name="Aaron Judge", sport="MLB",
                       team_id="t")
    b = resolve_player(display_name="Aaron Rodgers", sport="MLB",
                       team_id="t")
    assert a.canonical_player_id != b.canonical_player_id


# ── 10. Fallback identities are clearly marked ───────────────────────
def test_fallback_identity_is_marked():
    r = resolve_player(display_name="X", sport="MLB", team_id="t")
    assert r.identity_quality == "fallback"
    assert r.canonical_player_id.startswith("fallback:")


# ── 11. Dry-run does not mutate production records ───────────────────
def test_dry_run_scan_does_not_mutate():
    async def run():
        from motor.motor_asyncio import AsyncIOMotorClient
        c = AsyncIOMotorClient(os.environ["MONGO_URL"])
        db = c[os.environ["DB_NAME"]]
        # Snapshot counts BEFORE
        before = await db.picks.count_documents({})
        await dry_run_scan_collection(db, "picks", sample_size=50)
        after = await db.picks.count_documents({})
        assert before == after
        c.close()
    asyncio.run(run())


# ── 12. Public schemas unchanged (regression covered elsewhere) ──────
def test_contracts_do_not_export_secrets():
    e = resolve_event(provider="odds_api", provider_event_id="x")
    r = repr(e)
    assert "password" not in r.lower()


# ── 13. Prediction identity from ID stays canonical ──────────────────
def test_prediction_identity():
    p = resolve_prediction(prediction_id="p1", snapshot_id="s1")
    assert p.identity_quality == "canonical"


# ── 14. Bet leg identity requires user + leg ids ─────────────────────
def test_bet_leg_identity_requires_ids():
    ok  = resolve_bet_leg(user_bet_id="u", leg_id="l")
    bad = resolve_bet_leg(user_bet_id="", leg_id="l")
    assert ok.identity_quality == "canonical"
    assert bad.identity_quality == "unresolved"


# ── 15. Normalisation never yields first token only ──────────────────
def test_normalize_name_preserves_full_name():
    assert normalize_name("Aaron Judge") == "aaron_judge"
    assert normalize_name("Aaron Judge") != normalize_name("Aaron Rodgers")


# ── 16. Missing line degrades market contract quality ────────────────
def test_missing_line_or_book_degrades_quality():
    r = resolve_market_contract(canonical_event_id="e", market_key="mlb_hrr",
                                side="over", line=None, bookmaker="dk")
    assert r.identity_quality == "fallback"
    r2 = resolve_market_contract(canonical_event_id="e", market_key="mlb_hrr",
                                 side="over", line=1.5, bookmaker=None)
    assert r2.identity_quality == "fallback"
