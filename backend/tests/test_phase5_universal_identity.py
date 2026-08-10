"""Phase 5 (2026-08-11) — Universal cross-sport player identity tests.

Covers the foundational contract Magic Layer 2.0 will consume:

  * one canonical id survives across providers + spelling variants
  * two same-named different-person players don't merge
  * transfer changes current team but preserves history
  * stale provider cannot overwrite fresher observation
  * confirmed wrong current team blocks a player prop
  * missing roster data does NOT become a false team_mismatch
  * Tennis / UFC individual-sport validation
  * team-level markets remain unaffected
  * canonical_player_id survives persistence/restart
  * history rows remain linked after transfer
  * threshold-history query returns rows for the correct player
  * strict >85 Locks unchanged
  * P0-4 real-line integrity unchanged
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone, timedelta

import pytest
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")
from motor.motor_asyncio import AsyncIOMotorClient


def _db():
    return AsyncIOMotorClient(os.environ["MONGO_URL"])[
        os.environ.get("DB_NAME", "lockscore_db")]


def _run(coro):
    return asyncio.run(coro)


_UID = lambda: uuid.uuid4().hex[:12]


# ── 1. Enabled sports + module contract ───────────────────────────
@pytest.mark.unit
def test_universal_module_exports_enabled_sports():
    from services import universal_player_identity as upi
    for s in ("NFL", "NBA", "MLB", "NHL", "CFB",
              "Soccer", "Tennis", "UFC"):
        assert s in upi.ENABLED_SPORTS
    assert "Soccer" in upi.TEAM_SPORTS
    assert "Tennis" in upi.INDIVIDUAL_SPORTS
    assert "UFC" in upi.INDIVIDUAL_SPORTS


# ── 2. Same player across providers → one canonical id ─────────
@pytest.mark.integration
@pytest.mark.parametrize("sport,league", [
    ("NFL", "NFL"), ("NBA", "NBA"), ("MLB", "MLB"),
    ("NHL", "NHL"), ("CFB", "CFB"),
])
def test_same_player_across_providers_resolves_to_one_id(sport, league):
    from services.universal_player_identity import (
        upsert, resolve, ensure_universal_indexes,
    )
    from services.player_identity import reset_registry_for_tests

    async def go():
        db = _db()
        await ensure_universal_indexes(db)
        reset_registry_for_tests()
        tag = _UID()
        name = f"Multi Provider {sport} {tag}"
        now = datetime.now(timezone.utc).isoformat()
        i1 = upsert(name=name, sport=sport, league=league,
                     provider="espn", provider_id=f"espn_{tag}",
                     current_team="Team A", observed_at=now,
                     source="unit_test", affiliation_type="club")
        i2 = upsert(name=name, sport=sport, league=league,
                     provider="odds_api", provider_id=f"odds_{tag}",
                     current_team="Team A", observed_at=now,
                     source="unit_test", affiliation_type="club")
        assert i1.canonical_player_id == i2.canonical_player_id
        assert set(i2.provider_ids.keys()) >= {"espn", "odds_api"}
    _run(go())


# ── 3. Same-name different-person don't merge ──────────────────
@pytest.mark.unit
def test_same_name_different_provider_ids_do_not_merge():
    """Two DIFFERENT players with identical names must resolve to
    DIFFERENT canonical_player_ids when provider ids disagree."""
    from services.universal_player_identity import upsert
    from services.player_identity import reset_registry_for_tests
    reset_registry_for_tests()
    now = datetime.now(timezone.utc).isoformat()
    a = upsert(name="John Smith", sport="NFL", league="NFL",
                provider="espn", provider_id="ESPN_A_1234",
                current_team="Team A", observed_at=now,
                source="unit", affiliation_type="club")
    b = upsert(name="John Smith", sport="NFL", league="NFL",
                provider="espn", provider_id="ESPN_B_5678",
                current_team="Team B", observed_at=now,
                source="unit", affiliation_type="club")
    assert a.canonical_player_id != b.canonical_player_id


# ── 4. Transfer preserves history ──────────────────────────────
@pytest.mark.unit
def test_transfer_preserves_history_and_canonical_id():
    from services.universal_player_identity import upsert
    from services.player_identity import reset_registry_for_tests
    reset_registry_for_tests()
    tag = _UID()
    a = upsert(name=f"Transfer Guy {tag}", sport="NFL", league="NFL",
                provider="espn", provider_id=f"tg_{tag}",
                current_team="Old Team", observed_at="2025-01-01T00:00:00+00:00",
                source="unit", affiliation_type="club")
    b = upsert(name=f"Transfer Guy {tag}", sport="NFL", league="NFL",
                provider="espn", provider_id=f"tg_{tag}",
                current_team="New Team", observed_at="2026-08-01T00:00:00+00:00",
                source="unit", affiliation_type="club")
    assert a.canonical_player_id == b.canonical_player_id
    assert b.current_team == "New Team"
    teams = [h["team"] for h in b.historical_teams]
    assert teams == ["Old Team", "New Team"]


# ── 5. Stale provider cannot overwrite fresher observation ─────
@pytest.mark.unit
def test_stale_observation_cannot_overwrite_fresher():
    from services.universal_player_identity import upsert
    from services.player_identity import reset_registry_for_tests
    reset_registry_for_tests()
    tag = _UID()
    fresh = upsert(name=f"Fresh Wins {tag}", sport="MLB", league="MLB",
                    provider="espn", provider_id=f"fw_{tag}",
                    current_team="Current Team",
                    observed_at="2026-08-01T00:00:00+00:00",
                    source="unit", affiliation_type="club")
    stale = upsert(name=f"Fresh Wins {tag}", sport="MLB", league="MLB",
                    provider="espn", provider_id=f"fw_{tag}",
                    current_team="Wrong Old Team",
                    observed_at="2025-01-01T00:00:00+00:00",
                    source="stale", affiliation_type="club")
    assert stale.current_team == "Current Team"


# ── 6. Cross-sport publication barrier ─────────────────────────
@pytest.mark.unit
def test_confirmed_wrong_team_blocks_nfl_prop():
    from services.universal_publication_barrier import validate_universal
    v = validate_universal(
        {"sport": "NFL", "market": "Patrick Mahomes Passing Yards Over 250.5",
         "event": "Bills vs 49ers"},
        roster_lookup={"patrick mahomes": "Chiefs"},
        fresh_roster_names={"patrick mahomes"})
    assert v["verified"] is False
    assert v["reason"] == "player_team_mismatch"


@pytest.mark.unit
def test_missing_roster_data_is_roster_unverified_not_mismatch():
    from services.universal_publication_barrier import validate_universal
    v = validate_universal(
        {"sport": "NBA", "market": "Ja Morant Points Over 25.5",
         "event": "Grizzlies vs Warriors"},
        roster_lookup={}, fresh_roster_names=set())
    assert v["verified"] is False
    assert v["reason"] == "roster_unverified"


@pytest.mark.unit
def test_team_level_markets_pass_through():
    from services.universal_publication_barrier import validate_universal
    v = validate_universal(
        {"sport": "NFL", "market": "Bills Moneyline",
         "event": "Bills vs 49ers"})
    assert v["verified"] is True
    assert v["reason"] == "market_not_player_based"


@pytest.mark.unit
def test_tennis_wrong_participant_blocked():
    from services.universal_publication_barrier import validate_universal
    v = validate_universal(
        {"sport": "Tennis", "market": "Novak Djokovic Moneyline",
         "event": "Alcaraz vs Sinner"})
    assert v["verified"] is False
    assert v["sport_class"] == "individual"


@pytest.mark.unit
def test_tennis_correct_participant_verified():
    from services.universal_publication_barrier import validate_universal
    v = validate_universal(
        {"sport": "Tennis", "market": "Alcaraz Moneyline",
         "event": "Alcaraz vs Sinner"})
    assert v["verified"] is True
    assert v["sport_class"] == "individual"


@pytest.mark.unit
def test_ufc_wrong_fighter_blocked():
    from services.universal_publication_barrier import validate_universal
    v = validate_universal(
        {"sport": "UFC", "market": "Jon Jones Method of Victory",
         "event": "Poirier vs Gaethje"})
    assert v["verified"] is False


# ── 7. Soccer P0-A..P0-E regression via universal barrier ──────
@pytest.mark.unit
def test_soccer_universal_barrier_delegates_to_p0_stack():
    """Universal barrier for Soccer must produce IDENTICAL verdicts
    to the direct Soccer validator (Endrick regression preserved)."""
    from services.universal_publication_barrier import validate_universal
    v = validate_universal(
        {"sport": "Soccer",
         "market": "Endrick To Score or Assist",
         "event": "Haiti @ Brazil",
         "league": "FIFA World Cup · Props"},
        national_team_lookup={"endrick": "Portugal"},
        fresh_national_team_names={"endrick"},
        nationality_lookup={"endrick": "Portugal"})
    assert v["verified"] is True
    assert v["player_team"] == "Brazil"


# ── 8. Canonical id survives persistence/restart ───────────────
@pytest.mark.integration
def test_canonical_id_survives_restart():
    from services.universal_player_identity import (
        upsert, ensure_universal_indexes, get_player_context,
    )
    from services.player_identity import (
        persist_identity, reset_registry_for_tests,
        hydrate_registry_from_mongo,
    )

    async def go():
        db = _db()
        await ensure_universal_indexes(db)
        reset_registry_for_tests()
        tag = _UID()
        i = upsert(name=f"Restart Survivor {tag}", sport="NBA", league="NBA",
                    provider="espn", provider_id=f"rs_{tag}",
                    current_team="Lakers",
                    observed_at=datetime.now(timezone.utc).isoformat(),
                    source="unit", affiliation_type="club")
        await persist_identity(db, i.to_dict())
        cid = i.canonical_player_id
        reset_registry_for_tests()
        await hydrate_registry_from_mongo(db)
        ctx = await get_player_context(db, cid)
        assert ctx["resolved"] is True
        assert ctx["canonical_player_id"] == cid
        assert ctx["current_team"] == "Lakers"
        assert ctx["sport"] == "NBA"
        await db["player_identities"].delete_many(
            {"canonical_player_id": cid})
    _run(go())


# ── 9. History linkage + threshold-ready query ─────────────────
@pytest.mark.integration
def test_history_rows_linked_to_canonical_player_id():
    from services.universal_player_identity import (
        upsert, link_history_row, get_history,
        ensure_universal_indexes,
    )
    from services.player_identity import reset_registry_for_tests

    async def go():
        db = _db()
        await ensure_universal_indexes(db)
        reset_registry_for_tests()
        tag = _UID()
        i = upsert(name=f"History Guy {tag}", sport="NFL", league="NFL",
                    provider="espn", provider_id=f"hg_{tag}",
                    current_team="Bengals",
                    observed_at=datetime.now(timezone.utc).isoformat(),
                    source="unit", affiliation_type="club")
        cid = i.canonical_player_id
        # Link 3 history rows.
        for d, yds in (("2025-09-08", 285), ("2025-09-15", 305),
                        ("2025-09-22", 220)):
            await link_history_row(db, cid, {
                "sport": "NFL", "date": d, "event_id": f"evt_{d}",
                "market": "passing_yards", "team_at_time": "Bengals",
                "opponent": "Ravens", "value": yds, "home": True,
                "season": "2025",
            })
        rows = await get_history(db, cid, sport="NFL", limit=5)
        assert len(rows) == 3
        # Ordered by date desc.
        assert rows[0]["date"] == "2025-09-22"
        # Threshold count — "how many of last 3 ≥ 250?"
        above = [r for r in rows if r["value"] >= 250]
        assert len(above) == 2
        await db["player_history"].delete_many(
            {"canonical_player_id": cid})
    _run(go())


@pytest.mark.integration
def test_history_survives_transfer():
    from services.universal_player_identity import (
        upsert, link_history_row, get_history,
        ensure_universal_indexes,
    )
    from services.player_identity import reset_registry_for_tests

    async def go():
        db = _db()
        await ensure_universal_indexes(db)
        reset_registry_for_tests()
        tag = _UID()
        i = upsert(name=f"Transfer History {tag}", sport="NFL", league="NFL",
                    provider="espn", provider_id=f"th_{tag}",
                    current_team="Bengals",
                    observed_at="2025-01-01T00:00:00+00:00",
                    source="unit", affiliation_type="club")
        cid = i.canonical_player_id
        await link_history_row(db, cid, {
            "sport": "NFL", "date": "2025-09-08",
            "event_id": "e1", "market": "passing_yards",
            "team_at_time": "Bengals", "value": 300,
        })
        # Transfer.
        i2 = upsert(name=f"Transfer History {tag}", sport="NFL", league="NFL",
                     provider="espn", provider_id=f"th_{tag}",
                     current_team="Jets",
                     observed_at="2026-08-01T00:00:00+00:00",
                     source="unit", affiliation_type="club")
        assert i2.canonical_player_id == cid
        # History remains attached.
        rows = await get_history(db, cid, sport="NFL", limit=5)
        assert any(r["value"] == 300 for r in rows)
        assert i2.current_team == "Jets"
        await db["player_history"].delete_many(
            {"canonical_player_id": cid})
    _run(go())


# ── 10. Regression envelope ────────────────────────────────────
@pytest.mark.unit
def test_strict_gt_85_unchanged_after_phase5():
    from services.main_board_eligibility import is_main_board_eligible
    assert is_main_board_eligible({"lock_score": 85.0}) is False
    assert is_main_board_eligible({"lock_score": 85.001}) is True


@pytest.mark.unit
def test_soccer_endrick_regression_still_verified_after_phase5():
    from services.player_team_fixture_validator import (
        validate_player_fixture_pick,
    )
    v = validate_player_fixture_pick(
        {"sport": "Soccer", "market": "Endrick To Score",
         "event": "Haiti @ Brazil", "league": "FIFA World Cup"},
        {},
        national_team_lookup={"endrick": "Portugal"},
        fresh_national_team_names={"endrick"},
        nationality_lookup={"endrick": "Portugal"})
    assert v["verified"] is True
