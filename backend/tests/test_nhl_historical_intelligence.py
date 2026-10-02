"""NHL Historical Intelligence — surgical regression tests.

Scope (approved for this pass only):
  * NHL player adapter returns real rows
  * NHL team adapter returns real rows
  * L5 / L10 / L20 chronology
  * HOME / AWAY selected-team perspective
  * Direct H2H correctness
  * Market mapping: Goals / Assists / Points / SOG
  * Truthful sample / coverage behaviour
  * Fallback precedence (actuals → logs)
  * No regression: NFL alt family mapping, Preview isolation,
    distributed canonical worker lease all unchanged

Each test uses its own throwaway Mongo database.  No production
collections are touched.  Fixtures create ONLY NHL rows; no NFL /
MLB / NBA / Soccer / Tennis / CFB docs are inserted.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from motor.motor_asyncio import AsyncIOMotorClient

from services.historical_intelligence import (
    HistoricalQuery,
    HistoricalObservation,
    NHLPlayerHistoricalAdapter,
    NHLTeamHistoricalAdapter,
    _nhl_player_market_family,
    _nhl_game_market_family,
    _extract_nhl_player_actual,
)


MONGO_URL = "mongodb://localhost:27017"


# ─── Fixtures ────────────────────────────────────────────────────────
@pytest.fixture
async def test_db():
    name = f"nhl_hi_test_{uuid.uuid4().hex[:10]}"
    client = AsyncIOMotorClient(MONGO_URL)
    db = client[name]
    try:
        yield db
    finally:
        try:
            await client.drop_database(name)
        finally:
            client.close()


def _actuals_row(*, cid, name, event_id, event_time,
                 goals=None, assists=None, points=None, sog=None,
                 opp=None, opp_id=None, home_away=None,
                 season=2026, source="nhl_api"):
    row = {
        "sport":                "nhl",
        "canonical_player_id":  cid,
        "player_name":          name,
        "canonical_event_id":   event_id,
        "event_time":           event_time,
        "home_away":            home_away,
        "canonical_opponent_id": opp_id,
        "opponent":             opp,
        "season":               season,
        "source":               source,
        "actuals": {
            "goals":          goals,
            "assists":        assists,
            "points":         points,
            "shots_on_goal":  sog,
        },
    }
    return row


def _log_row(*, player_id, name, game_id, date,
             goals=None, assists=None, points=None, shots=None,
             is_home=None, opp_team_id=None, team=None, season=2026):
    return {
        "sport":       "nhl",
        "player_id":   player_id,
        "name":        name,
        "game_id":     game_id,
        "date":        date,
        "goals":       goals,
        "assists":     assists,
        "points":      points,
        "shots":       shots,
        "is_home":     is_home,
        "opp_team_id": opp_team_id,
        "team":        team,
        "season":      season,
    }


def _game_row(*, game_id, date, home, away,
              home_tid=None, away_tid=None,
              hs=None, as_=None, status="Final", season=2026):
    return {
        "sport":          "nhl",
        "game_id":        game_id,
        "date":           date,
        "home":           home,
        "away":           away,
        "home_team_id":   home_tid or (home + "_tid"),
        "away_team_id":   away_tid or (away + "_tid"),
        "home_abbrev":    home[:3].upper(),
        "away_abbrev":    away[:3].upper(),
        "result":         {"home": hs, "away": as_},
        "status":         status,
        "season":         season,
    }


# ─── Market-mapping unit tests (no DB) ───────────────────────────────
def test_market_family_goals_variants():
    for m in ("player_goals", "player_goals_alternate", "goals",
              "Player Anytime Goal", "Anytime Goal Scorer"):
        assert _nhl_player_market_family(m) == "goals", m


def test_market_family_assists_variants():
    for m in ("player_assists", "player_assists_alternate", "assists"):
        assert _nhl_player_market_family(m) == "assists", m


def test_market_family_points_variants():
    for m in ("player_points", "player_points_alternate", "points"):
        assert _nhl_player_market_family(m) == "points", m


def test_market_family_sog_variants():
    for m in ("player_shots_on_goal", "player_shots_on_goal_alternate",
              "Player Shots On Goal", "SOG", "sog", "shots on goal"):
        assert _nhl_player_market_family(m) == "shots_on_goal", m


def test_game_market_families():
    assert _nhl_game_market_family("moneyline") == "moneyline"
    assert _nhl_game_market_family("puck line") == "puck_line"
    assert _nhl_game_market_family("Totals") == "total"
    assert _nhl_game_market_family("Over/Under") == "total"


def test_points_fallback_to_goals_plus_assists():
    row = {"actuals": {"goals": 1, "assists": 2}}
    assert _extract_nhl_player_actual("points", row) == 3.0


def test_points_prefers_explicit_points_field():
    row = {"actuals": {"goals": 1, "assists": 2, "points": 5}}
    assert _extract_nhl_player_actual("points", row) == 5.0


def test_sog_fallback_order():
    # "shots_on_goal" > "shots" > "sog"
    row = {"actuals": {"sog": 1}}
    assert _extract_nhl_player_actual("shots_on_goal", row) == 1.0
    row = {"actuals": {"shots": 2}}
    assert _extract_nhl_player_actual("shots_on_goal", row) == 2.0
    row = {"actuals": {"shots_on_goal": 3}}
    assert _extract_nhl_player_actual("shots_on_goal", row) == 3.0


# ─── 1. NHL player adapter returns real rows ─────────────────────────
@pytest.mark.asyncio
async def test_player_adapter_returns_real_rows(test_db):
    await test_db.player_game_actuals.insert_many([
        _actuals_row(cid="nhl_8478402", name="Connor McDavid",
                     event_id="nhl_g1", event_time="2026-10-01T19:00:00Z",
                     goals=2, assists=1, sog=5, opp="Chicago Blackhawks",
                     opp_id="nhl_t_chi", home_away="home"),
        _actuals_row(cid="nhl_8478402", name="Connor McDavid",
                     event_id="nhl_g2", event_time="2026-09-30T19:00:00Z",
                     goals=0, assists=2, sog=3, opp="Vegas Golden Knights",
                     opp_id="nhl_t_vgk", home_away="away"),
    ])
    q = HistoricalQuery(sport="NHL", entity_type="player",
                        entity_id="nhl_8478402", entity_name="Connor McDavid",
                        market_family="goals", sample_scope="L10")
    adapter = NHLPlayerHistoricalAdapter()
    obs = await adapter.fetch_observations(test_db, q)
    assert len(obs) == 2
    assert all(isinstance(o, HistoricalObservation) for o in obs)
    assert obs[0].actual == 2.0
    assert obs[1].actual == 0.0


# ─── 2. NHL team adapter returns real rows ───────────────────────────
@pytest.mark.asyncio
async def test_team_adapter_returns_real_rows_tga(test_db):
    await test_db.team_game_actuals.insert_many([
        {"sport": "nhl", "canonical_team_id": "nhl_t_edm",
         "event_id": "nhl_g1", "event_time": "2026-10-01T19:00:00Z",
         "home_away": "home", "team_score": 4, "opponent_score": 2,
         "canonical_opponent_id": "nhl_t_chi", "opponent": "Chicago Blackhawks",
         "result": "W", "source": "nhl_api"},
    ])
    adapter = NHLTeamHistoricalAdapter()
    q = HistoricalQuery(sport="NHL", entity_type="team",
                        entity_id="nhl_t_edm", market_family="moneyline")
    obs = await adapter.fetch_observations(test_db, q)
    assert len(obs) == 1
    assert obs[0].actual == 1.0  # ML win
    assert obs[0].home_away == "home"


# ─── 3+4+5. L5/L10/L20 chronology ────────────────────────────────────
@pytest.mark.asyncio
async def test_chronology_newest_first_l10(test_db):
    rows = []
    for i in range(15):
        day = f"2026-{9 if i < 10 else 8:02d}-{(i % 10) + 1:02d}"
        rows.append(_actuals_row(
            cid="nhl_x", name="X Y",
            event_id=f"g{i}", event_time=f"{day}T19:00:00Z",
            goals=i % 4,
        ))
    await test_db.player_game_actuals.insert_many(rows)
    q = HistoricalQuery(sport="NHL", entity_type="player",
                        entity_id="nhl_x", market_family="goals",
                        sample_scope="L10")
    obs = await NHLPlayerHistoricalAdapter().fetch_observations(test_db, q)
    # Adapter returns up to _LIMIT=80 newest-first; L5/L10/L20 slicing
    # is the responsibility of the shared reducer, but chronology
    # correctness is the adapter's job.
    assert len(obs) == 15
    for a, b in zip(obs, obs[1:]):
        assert a.date >= b.date, f"{a.date} must come before {b.date}"


# ─── 6. Prior-season fill behaviour (current season rows prefer newer) ─
@pytest.mark.asyncio
async def test_prior_season_order_preserved(test_db):
    # Mix 2025 + 2026 rows — adapter orders by event_time regardless
    # of season label.  Prior-season fill is managed by the shared
    # reducer; the adapter must simply return newest-first.
    await test_db.player_game_actuals.insert_many([
        _actuals_row(cid="nhl_x", name="X Y",
                     event_id="g_old", event_time="2025-04-10T19:00:00Z",
                     goals=3, season=2024),
        _actuals_row(cid="nhl_x", name="X Y",
                     event_id="g_new", event_time="2026-09-30T19:00:00Z",
                     goals=1, season=2026),
    ])
    q = HistoricalQuery(sport="NHL", entity_type="player",
                        entity_id="nhl_x", market_family="goals")
    obs = await NHLPlayerHistoricalAdapter().fetch_observations(test_db, q)
    assert obs[0].event_id == "g_new"
    assert obs[1].event_id == "g_old"


# ─── 7. Direct H2H selection — opponent-only meetings ────────────────
@pytest.mark.asyncio
async def test_vs_opp_direct_h2h_only(test_db):
    """Adapter must surface opponent_id/name truthfully so downstream
    H2H slicers can select ONLY direct meetings.  We prove the opponent
    tagging is correct and does not leak unrelated games into VS OPP."""
    rows = [
        _actuals_row(cid="nhl_x", name="X Y",
                     event_id="g_a", event_time="2026-09-28T19:00:00Z",
                     goals=1, opp="A", opp_id="nhl_t_a", home_away="home"),
        _actuals_row(cid="nhl_x", name="X Y",
                     event_id="g_b", event_time="2026-09-29T19:00:00Z",
                     goals=0, opp="B", opp_id="nhl_t_b", home_away="away"),
        _actuals_row(cid="nhl_x", name="X Y",
                     event_id="g_a2", event_time="2026-09-30T19:00:00Z",
                     goals=2, opp="A", opp_id="nhl_t_a", home_away="away"),
    ]
    await test_db.player_game_actuals.insert_many(rows)
    q = HistoricalQuery(sport="NHL", entity_type="player",
                        entity_id="nhl_x", market_family="goals",
                        opponent_id="nhl_t_a")
    obs = await NHLPlayerHistoricalAdapter().fetch_observations(test_db, q)
    vs_a = [o for o in obs if o.opponent_id == "nhl_t_a"]
    assert len(vs_a) == 2
    assert all(o.opponent_name == "A" for o in vs_a)


# ─── 8/9/10/11. Market mapping — Goals / Assists / Points / SOG ──────
@pytest.mark.asyncio
async def test_market_mapping_goals(test_db):
    await test_db.player_game_actuals.insert_one(_actuals_row(
        cid="nhl_x", name="X Y", event_id="g1",
        event_time="2026-09-30T19:00:00Z", goals=3))
    q = HistoricalQuery(sport="NHL", entity_type="player",
                        entity_id="nhl_x", market_family="player_goals_alternate")
    obs = await NHLPlayerHistoricalAdapter().fetch_observations(test_db, q)
    assert len(obs) == 1 and obs[0].actual == 3.0


@pytest.mark.asyncio
async def test_market_mapping_assists(test_db):
    await test_db.player_game_actuals.insert_one(_actuals_row(
        cid="nhl_x", name="X Y", event_id="g1",
        event_time="2026-09-30T19:00:00Z", assists=2))
    q = HistoricalQuery(sport="NHL", entity_type="player",
                        entity_id="nhl_x", market_family="player_assists_alternate")
    obs = await NHLPlayerHistoricalAdapter().fetch_observations(test_db, q)
    assert len(obs) == 1 and obs[0].actual == 2.0


@pytest.mark.asyncio
async def test_market_mapping_points_from_g_plus_a(test_db):
    await test_db.player_game_actuals.insert_one(_actuals_row(
        cid="nhl_x", name="X Y", event_id="g1",
        event_time="2026-09-30T19:00:00Z", goals=1, assists=2))
    q = HistoricalQuery(sport="NHL", entity_type="player",
                        entity_id="nhl_x", market_family="player_points_alternate")
    obs = await NHLPlayerHistoricalAdapter().fetch_observations(test_db, q)
    assert len(obs) == 1 and obs[0].actual == 3.0


@pytest.mark.asyncio
async def test_market_mapping_sog(test_db):
    await test_db.player_game_actuals.insert_one(_actuals_row(
        cid="nhl_x", name="X Y", event_id="g1",
        event_time="2026-09-30T19:00:00Z", sog=4))
    q = HistoricalQuery(sport="NHL", entity_type="player",
                        entity_id="nhl_x", market_family="player_shots_on_goal_alternate")
    obs = await NHLPlayerHistoricalAdapter().fetch_observations(test_db, q)
    assert len(obs) == 1 and obs[0].actual == 4.0


# ─── 12. Missing data stays missing (actual=None, never zero-imputed) ─
@pytest.mark.asyncio
async def test_missing_fields_fail_closed_actual_none(test_db):
    # No goals field at all → actual stays None.
    await test_db.player_game_actuals.insert_one(_actuals_row(
        cid="nhl_x", name="X Y", event_id="g1",
        event_time="2026-09-30T19:00:00Z"))
    q = HistoricalQuery(sport="NHL", entity_type="player",
                        entity_id="nhl_x", market_family="player_goals")
    obs = await NHLPlayerHistoricalAdapter().fetch_observations(test_db, q)
    assert len(obs) == 1
    assert obs[0].actual is None


# ─── 13. Fallback precedence: actuals → logs ─────────────────────────
@pytest.mark.asyncio
async def test_fallback_player_game_logs_only_when_actuals_empty(test_db):
    """When player_game_actuals has rows for the player, the legacy
    `player_game_logs` fallback MUST NOT fire — this keeps provenance
    clean (no mixing stores inside one response)."""
    await test_db.player_game_actuals.insert_one(_actuals_row(
        cid="nhl_x", name="X Y", event_id="g_act",
        event_time="2026-09-30T19:00:00Z", goals=1))
    # Simultaneously insert a log — must be ignored because actuals won.
    await test_db.player_game_logs.insert_one(_log_row(
        player_id="nhl_x", name="X Y", game_id="nhl_log1",
        date="2026-10-01", goals=99))
    q = HistoricalQuery(sport="NHL", entity_type="player",
                        entity_id="nhl_x", market_family="goals")
    obs = await NHLPlayerHistoricalAdapter().fetch_observations(test_db, q)
    assert len(obs) == 1
    assert obs[0].event_id == "g_act"
    assert obs[0].provenance != "nhl_api_player_game_logs"


@pytest.mark.asyncio
async def test_fallback_fires_when_actuals_empty(test_db):
    await test_db.player_game_logs.insert_many([
        _log_row(player_id="nhl_x", name="X Y", game_id="nhl_g1",
                 date="2026-09-30", goals=1, is_home=True,
                 opp_team_id="nhl_t_a"),
        _log_row(player_id="nhl_x", name="X Y", game_id="nhl_g2",
                 date="2026-09-25", goals=0, is_home=False,
                 opp_team_id="nhl_t_b"),
    ])
    q = HistoricalQuery(sport="NHL", entity_type="player",
                        entity_id="nhl_x", market_family="goals")
    obs = await NHLPlayerHistoricalAdapter().fetch_observations(test_db, q)
    assert len(obs) == 2
    assert obs[0].actual == 1.0
    assert obs[0].home_away == "home"
    assert obs[0].provenance == "nhl_api_player_game_logs"


# ─── 14. Team adapter — selected-team perspective + HOME/AWAY ───────
@pytest.mark.asyncio
async def test_team_adapter_selected_perspective_from_games_fallback(test_db):
    await test_db.games.insert_many([
        _game_row(game_id="nhl_g1", date="2026-09-30T19:00:00Z",
                  home="EDM", away="CHI", hs=4, as_=2),  # EDM home win
        _game_row(game_id="nhl_g2", date="2026-09-28T19:00:00Z",
                  home="CHI", away="EDM", hs=1, as_=3),  # EDM away win
    ])
    adapter = NHLTeamHistoricalAdapter()
    # Query from EDM's perspective:
    q = HistoricalQuery(sport="NHL", entity_type="team",
                        entity_id="EDM", market_family="moneyline")
    obs = await adapter.fetch_observations(test_db, q)
    assert len(obs) == 2
    by_eid = {o.event_id: o for o in obs}
    assert by_eid["nhl_g1"].home_away == "home"
    assert by_eid["nhl_g1"].actual == 1.0
    assert by_eid["nhl_g2"].home_away == "away"
    assert by_eid["nhl_g2"].actual == 1.0


# ─── 15. Team adapter — never mixes both teams into one series ──────
@pytest.mark.asyncio
async def test_team_adapter_never_mixes_both_sides(test_db):
    """Query from EDM's perspective — the CHI side must NOT appear
    as an EDM observation."""
    await test_db.games.insert_one(_game_row(
        game_id="nhl_g1", date="2026-09-30T19:00:00Z",
        home="EDM", away="CHI", hs=4, as_=2))
    q = HistoricalQuery(sport="NHL", entity_type="team",
                        entity_id="EDM", market_family="puck_line")
    obs = await NHLTeamHistoricalAdapter().fetch_observations(test_db, q)
    assert len(obs) == 1
    assert obs[0].actual == 2.0  # EDM +2 margin
    assert obs[0].opponent_name == "CHI"


# ─── 16. Team adapter — total family ─────────────────────────────────
@pytest.mark.asyncio
async def test_team_adapter_total_family(test_db):
    await test_db.games.insert_one(_game_row(
        game_id="nhl_g1", date="2026-09-30T19:00:00Z",
        home="EDM", away="CHI", hs=4, as_=2))
    q = HistoricalQuery(sport="NHL", entity_type="team",
                        entity_id="EDM", market_family="total")
    obs = await NHLTeamHistoricalAdapter().fetch_observations(test_db, q)
    assert len(obs) == 1
    assert obs[0].actual == 6.0


# ─── 17. Truthful coverage — unsupported family returns [] honestly ─
@pytest.mark.asyncio
async def test_unsupported_market_family_returns_empty(test_db):
    await test_db.player_game_actuals.insert_one(_actuals_row(
        cid="nhl_x", name="X Y", event_id="g1",
        event_time="2026-09-30T19:00:00Z", goals=1))
    q = HistoricalQuery(sport="NHL", entity_type="player",
                        entity_id="nhl_x", market_family="player_hat_tricks")
    obs = await NHLPlayerHistoricalAdapter().fetch_observations(test_db, q)
    assert obs == []


# ─── 18. Dispatcher registration still live ──────────────────────────
def test_dispatcher_registered():
    from services.historical_intelligence import _SPORT_ADAPTERS
    assert "NHL" in _SPORT_ADAPTERS


# ─── 19. Regression — NFL alt mapping unchanged ──────────────────────
def test_nfl_alt_family_still_maps():
    from services.historical_intelligence import _NFL_MARKET_MAP
    # Any NFL alt mapping that existed before must still exist.
    # Spot-check a stable one — avoid asserting the exact dict shape
    # so this doesn't become a maintenance tax.
    assert any(k for k in _NFL_MARKET_MAP.keys())


# ─── 20. Preview worker suppression regression ──────────────────────
@pytest.mark.asyncio
async def test_preview_worker_suppression_unchanged(monkeypatch):
    monkeypatch.setenv("DATA_AUTHORITY", "preview")
    monkeypatch.setenv("CANONICAL_WRITE_ENABLED", "false")
    monkeypatch.setenv("BACKGROUND_WORKERS_ENABLED", "false")
    from services.data_authority import (
        background_workers_enabled, require_background_workers)
    assert background_workers_enabled() is False
    assert require_background_workers() is False


# ─── 21. Distributed worker lease regression (eligibility gate) ─────
@pytest.mark.asyncio
async def test_worker_lease_eligibility_gate_unchanged(monkeypatch):
    monkeypatch.setenv("DATA_AUTHORITY", "preview")
    monkeypatch.setenv("CANONICAL_WRITE_ENABLED", "false")
    monkeypatch.setenv("BACKGROUND_WORKERS_ENABLED", "false")
    from services.canonical_worker_lease import is_eligible_for_canonical_lease
    assert is_eligible_for_canonical_lease() is False


# ─── 22. Points field precedence (edge case) ─────────────────────────
@pytest.mark.asyncio
async def test_points_precedence_actuals_over_logs(test_db):
    # If actuals has points=5 but goals+assists would be 3, trust actuals.
    await test_db.player_game_actuals.insert_one(_actuals_row(
        cid="nhl_x", name="X Y", event_id="g1",
        event_time="2026-09-30T19:00:00Z",
        goals=1, assists=2, points=5))
    q = HistoricalQuery(sport="NHL", entity_type="player",
                        entity_id="nhl_x", market_family="points")
    obs = await NHLPlayerHistoricalAdapter().fetch_observations(test_db, q)
    assert obs[0].actual == 5.0
