"""PERKLOCKS PHASE 1B — per-sport authoritative runtime wiring tests.

Production-path proofs:
  * NFL regular + preseason game markets (ML/Spread/Total) run through
    the Platinum game simulator with deterministic seeds.
  * NBA / CFB / UFC / NHL game markets REACH evaluation and record
    MODEL_UNAVAILABLE funnel telemetry — never sportsbook-follow picks.
  * UFC totals are no longer silently suppressed (legacy _ufc_ml_only
    retired) — they reach the model stage.
  * NHL is wired into SPORT_KEYS / generate_all_picks (R2a).
  * Tennis fallback operates as a controlled gap-filler (R4).
  * Soccer legacy pipeline pick emission is retired (T1) and synthetic
    scorer picks are research-only.
  * MLB hitter-prop markets remain reachable (registry ↔ runtime parity).

Run: EXPO_PUBLIC_BACKEND_URL=http://localhost:8001 python -m pytest -q \
     tests/test_phase1b_runtime_wiring.py
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

import sports_engine as se  # noqa: E402
from services import funnel_telemetry as funnel  # noqa: E402
from services.platinum_nfl.game_runtime import (  # noqa: E402
    platinum_game_side_probability,
)


# ── Fixtures ─────────────────────────────────────────────────────────

def _game(sport_key: str, home: str = "Kansas City Chiefs",
          away: str = "Denver Broncos", *, total_line: float = 45.5,
          spread: float = -3.5) -> dict:
    """Fabricated Odds-API game payload with REAL market structure
    (h2h + spreads + totals from two books)."""
    from datetime import datetime, timedelta, timezone
    commence = (datetime.now(timezone.utc)
                + timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
    def _book(key):
        return {
            "key": key,
            "markets": [
                {"key": "h2h", "outcomes": [
                    {"name": home, "price": -150},
                    {"name": away, "price": 130},
                ]},
                {"key": "totals", "outcomes": [
                    {"name": "Over", "point": total_line, "price": -110},
                    {"name": "Under", "point": total_line, "price": -110},
                ]},
                {"key": "spreads", "outcomes": [
                    {"name": home, "point": spread, "price": -110},
                    {"name": away, "point": -spread, "price": -110},
                ]},
            ],
        }
    return {
        "id": f"evt-{sport_key}-test-001",
        "sport_key": sport_key,
        "home_team": home,
        "away_team": away,
        "commence_time": commence,
        "bookmakers": [_book("draftkings"), _book("fanduel")],
    }


NFL_CTX_OK = {
    "nfl_model_available": True,
    "expected_margin_home": 9.0,
    "expected_total": 51.0,
}
NFL_CTX_MISSING = {
    "nfl_model_available": False,
    "nfl_model_reason": "TEAM_RATINGS_MISSING(home,away)",
}
TODAY = "2026-06-15"


@pytest.fixture(autouse=True)
def _clean_funnel():
    funnel.drain()
    yield
    funnel.drain()


# ── §3 NFL — Platinum game-market wiring (R1) ────────────────────────

class TestNFLPlatinumGameWiring:
    def test_ml_pick_uses_platinum_sim(self):
        g = _game("americanfootball_nfl")
        g["_ctx"] = dict(NFL_CTX_OK)
        picks = se._picks_from_game("NFL", "NFL", g, TODAY)
        ml = [p for p in picks if "Moneyline" in (p.get("market") or "")]
        assert ml, "expected an NFL ML pick from the Platinum sim path"
        p = ml[0]
        assert p.get("model_source") == "platinum_nfl_game_sim"
        assert p.get("platinum_game_sim", {}).get("sim_probability") is not None
        assert p.get("season_type") == "REGULAR_SEASON"

    def test_totals_and_spread_use_platinum_sim(self):
        g = _game("americanfootball_nfl")
        g["_ctx"] = dict(NFL_CTX_OK)
        picks = se._picks_from_game("NFL", "NFL", g, TODAY)
        provenanced = [p for p in picks
                       if p.get("model_source") == "platinum_nfl_game_sim"]
        markets = {p["market"] for p in provenanced}
        # Every emitted NFL game pick must carry Platinum provenance —
        # zero sportsbook-follow emissions.
        assert len(provenanced) == len(picks), (
            f"non-Platinum NFL picks leaked: "
            f"{[p['market'] for p in picks if p not in provenanced]}")
        assert any("Moneyline" in m for m in markets)

    def test_preseason_classification(self):
        g = _game("americanfootball_nfl_preseason")
        g["_ctx"] = dict(NFL_CTX_OK)
        picks = se._picks_from_game("NFL", "NFL Preseason", g, TODAY)
        assert picks, "preseason game markets must reach Platinum sim"
        for p in picks:
            assert p.get("season_type") == "PRESEASON"
            assert p.get("model_source") == "platinum_nfl_game_sim"

    def test_ratings_missing_records_model_unavailable_no_pick(self):
        g = _game("americanfootball_nfl")
        g["_ctx"] = dict(NFL_CTX_MISSING)
        picks = se._picks_from_game("NFL", "NFL", g, TODAY)
        assert picks == [], "no NFL picks may emit without the model"
        reasons = {r["reason"] for r in funnel.peek(sport="NFL")}
        assert any("TEAM_RATINGS_MISSING" in r or r == "MODEL_UNAVAILABLE"
                   for r in reasons), reasons

    def test_same_input_determinism(self):
        g = _game("americanfootball_nfl")
        res1 = platinum_game_side_probability(
            game=g, ctx=NFL_CTX_OK, market="Total", side="Over",
            line=45.5, book_total_line=45.5)
        res2 = platinum_game_side_probability(
            game=g, ctx=NFL_CTX_OK, market="Total", side="Over",
            line=45.5, book_total_line=45.5)
        assert res1["available"] and res2["available"]
        assert res1["prob"] == res2["prob"]
        sp1 = platinum_game_side_probability(
            game=g, ctx=NFL_CTX_OK, market="Spread",
            side=g["home_team"], line=-6.5, is_home_side=True,
            book_total_line=45.5)
        sp2 = platinum_game_side_probability(
            game=g, ctx=NFL_CTX_OK, market="Spread",
            side=g["home_team"], line=-6.5, is_home_side=True,
            book_total_line=45.5)
        assert sp1["available"] and sp1["prob"] == sp2["prob"]

    def test_both_total_sides_evaluated(self):
        g = _game("americanfootball_nfl")
        over = platinum_game_side_probability(
            game=g, ctx=NFL_CTX_OK, market="Total", side="Over",
            line=45.5, book_total_line=45.5)
        under = platinum_game_side_probability(
            game=g, ctx=NFL_CTX_OK, market="Total", side="Under",
            line=45.5, book_total_line=45.5)
        assert over["available"] and under["available"]
        # complementary (± push mass)
        assert abs((over["prob"] + under["prob"]) - 1.0) < 0.02


# ── §4-§9 model-less sports — MODEL_UNAVAILABLE, never book-follow ───

class TestModelUnavailableSports:
    @pytest.mark.parametrize("sport,sport_key", [
        ("NBA", "basketball_nba"),
        ("CFB", "americanfootball_ncaaf"),
        ("NHL", "icehockey_nhl"),
        ("UFC", "mma_mixed_martial_arts"),
    ])
    def test_no_bookfollow_picks_and_funnel_reason(self, sport, sport_key):
        g = _game(sport_key, home="Team Alpha", away="Team Beta")
        picks = se._picks_from_game(sport, sport, g, TODAY)
        assert picks == [], (
            f"{sport} emitted sportsbook-follow picks: "
            f"{[p.get('market') for p in picks]}")
        recs = funnel.peek(sport=sport)
        assert any(r["reason"] == "MODEL_UNAVAILABLE" for r in recs), recs
        markets_recorded = {r["market"] for r in recs}
        assert "moneyline" in markets_recorded

    def test_ufc_totals_reach_evaluation_not_suppressed(self):
        """T3b — the legacy _ufc_ml_only suppression is retired: UFC
        totals must REACH the model stage (funnel record proves it)."""
        g = _game("mma_mixed_martial_arts", home="Fighter A",
                  away="Fighter B", total_line=2.5)
        se._picks_from_game("UFC", "UFC", g, TODAY)
        recs = funnel.peek(sport="UFC")
        assert any(r["market"] == "total" for r in recs), (
            "UFC totals never reached the model stage — "
            f"records: {recs}")

    def test_nhl_spread_reaches_evaluation(self):
        g = _game("icehockey_nhl", home="Colorado Avalanche",
                  away="Dallas Stars", total_line=6.5, spread=-1.5)
        se._picks_from_game("NHL", "NHL", g, TODAY)
        recs = funnel.peek(sport="NHL")
        assert {"moneyline", "total", "spread"} <= {r["market"] for r in recs}


# ── NHL wiring (R2a) ─────────────────────────────────────────────────

class TestNHLWiring:
    def test_sport_keys_and_labels(self):
        assert se.SPORT_KEYS.get("NHL") == ["icehockey_nhl"]
        assert se.LEAGUE_LABELS.get("icehockey_nhl") == "NHL"
        assert se._unit("NHL") == "Goals"

    def test_fetcher_exists_and_generation_includes_nhl(self):
        assert callable(getattr(se, "fetch_nhl_picks", None))
        src = inspect.getsource(se.generate_all_picks)
        assert '_want("NHL")' in src and "fetch_nhl_picks" in src

    def test_registry_runtime_parity(self):
        from services.sport_capability_registry import enabled_sports
        for sport in enabled_sports():
            assert sport in se.SPORT_KEYS, (
                f"registry enables {sport} but runtime has no SPORT_KEYS "
                f"entry — capability/runtime contradiction")


# ── Tennis gap-filler (R4) ───────────────────────────────────────────

class TestTennisGapFill:
    def _run(self, primary, extra):
        from services.pick_refresh_orchestrator import _tennis_gap_fill_filter
        return _tennis_gap_fill_filter(primary, extra)

    def test_covered_event_rejected(self):
        primary = [{"sport": "Tennis", "id": "p1",
                    "event": "Carlos Alcaraz @ Jannik Sinner"}]
        extra = [{"sport": "Tennis", "id": "x1",
                  "event": "Jannik Sinner vs Carlos Alcaraz",
                  "book_odds": -150}]
        kept, stats = self._run(primary, extra)
        assert kept == [] and stats["rejected_covered"] == 1
        assert funnel.peek(reason=funnel.GAP_FILL_EVENT_COVERED_BY_PRIMARY)

    def test_no_real_line_rejected(self):
        extra = [{"sport": "Tennis", "id": "x2",
                  "event": "Player One vs Player Two",
                  "book_odds": None, "no_real_book_line": True}]
        kept, stats = self._run([], extra)
        assert kept == [] and stats["rejected_no_real_line"] == 1
        assert funnel.peek(reason=funnel.GAP_FILL_NO_REAL_BOOK_LINE)

    def test_genuine_gap_with_real_line_kept(self):
        primary = [{"sport": "Tennis", "id": "p1",
                    "event": "Carlos Alcaraz @ Jannik Sinner"}]
        extra = [{"sport": "Tennis", "id": "x3",
                  "event": "Iga Swiatek vs Aryna Sabalenka",
                  "book_odds": -180}]
        kept, stats = self._run(primary, extra)
        assert len(kept) == 1 and stats["kept"] == 1


# ── Soccer consolidation (T1 + synthetic scorer contract) ────────────

class TestSoccerConsolidation:
    def test_legacy_pipeline_emission_retired_by_default(self):
        from soccer import pipeline as sp
        assert sp.LEGACY_PICK_EMIT_ENABLED is False

    def test_pipeline_guards_pick_writes(self):
        from soccer import pipeline as sp
        src = inspect.getsource(sp.run_prediction_pipeline)
        assert "LEGACY_PICK_EMIT_ENABLED" in src
        assert "LEGACY_PIPELINE_RETIRED" in src

    def test_synthetic_scorer_is_research_only(self):
        src = inspect.getsource(se._fetch_player_props_for_sport)
        assert "SYNTHETIC_SCORER_RESEARCH_ONLY" in src
        assert "all_picks.extend(synth_picks)" not in src
        assert "model_research_evidence" in src


# ── MLB reachability (registry ↔ runtime parity) ─────────────────────

class TestMLBHitterPropReachability:
    HITTER_MARKETS = {
        "batter_hits", "batter_hits_alternate",
        "batter_hits_runs_rbis", "batter_hits_runs_rbis_alternate",
        "batter_home_runs", "batter_home_runs_alternate",
        "batter_rbis", "batter_rbis_alternate",
        "batter_total_bases", "batter_total_bases_alternate",
    }

    def test_hitter_markets_in_fetch_list(self):
        assert self.HITTER_MARKETS <= set(se.PLAYER_PROP_MARKETS["MLB"])

    def test_registry_matches_runtime_fetch_list(self):
        from services.sport_capability_registry import prop_markets_for
        assert set(prop_markets_for("MLB")) == set(se.PLAYER_PROP_MARKETS["MLB"])

    def test_mlb_in_prop_fetch_loop(self):
        src = inspect.getsource(se.generate_all_picks)
        assert '"MLB", "NBA", "NFL", "Soccer"' in src

    def test_hitter_drop_paths_are_telemetried(self):
        src = inspect.getsource(se._props_picks_from_event)
        assert "MISSING_FEATURE_DATA" in src, (
            "MLB hitter-prop feature-gate drops must be funnel-recorded")


# ── Production-path integration (provider mocked at the boundary) ────

class TestProductionPathIntegration:
    """Run the REAL ``generate_all_picks`` production entrypoint with
    the odds provider mocked at the HTTP boundary — proves the full
    fetch → context → engine → builder path without burning quota."""

    def _run_generation(self, monkeypatch, sport: str, sport_key: str,
                        game: dict, ctx: dict) -> list[dict]:
        import asyncio

        async def _fake_fetch_odds_for(key, regions="us", sport=None,
                                       *a, **kw):
            return [dict(game)] if key == sport_key else []

        async def _fake_props(*a, **kw):
            return []

        async def _fake_ctx(g):
            return dict(ctx)

        async def _noop_load_active():
            return None

        monkeypatch.setattr(se, "_fetch_odds_for", _fake_fetch_odds_for)
        monkeypatch.setattr(se, "_fetch_player_props_for_sport", _fake_props)
        monkeypatch.setattr(se, "_load_active_sports", _noop_load_active)
        monkeypatch.setattr(se, "_ACTIVE_KEYS", {sport_key})
        import services.platinum_nfl.game_runtime as gr
        monkeypatch.setattr(gr, "build_nfl_game_model_context", _fake_ctx)
        return asyncio.run(
            se.generate_all_picks(TODAY, sport_filter=sport))

    def test_nfl_end_to_end_platinum(self, monkeypatch):
        g = _game("americanfootball_nfl")
        picks = self._run_generation(
            monkeypatch, "NFL", "americanfootball_nfl", g, NFL_CTX_OK)
        assert picks, "production path emitted no NFL picks"
        for p in picks:
            assert p.get("model_source") == "platinum_nfl_game_sim", (
                f"non-authoritative NFL pick leaked: {p.get('market')}")

    def test_nhl_end_to_end_model_unavailable(self, monkeypatch):
        g = _game("icehockey_nhl", home="Colorado Avalanche",
                  away="Dallas Stars", total_line=6.5, spread=-1.5)
        picks = self._run_generation(
            monkeypatch, "NHL", "icehockey_nhl", g, {})
        assert picks == [], "NHL must not emit picks without a model"
        recs = funnel.peek(sport="NHL", reason="MODEL_UNAVAILABLE")
        assert {"moneyline", "total", "spread"} <= {r["market"] for r in recs}


# ── Funnel telemetry mechanics ───────────────────────────────────────

class TestFunnelTelemetry:
    def test_record_and_drain(self):
        funnel.record(sport="NHL", market="total", stage="model",
                      reason=funnel.MODEL_UNAVAILABLE, event="A @ B")
        assert funnel.buffered_count() == 1
        recs = funnel.drain()
        assert recs[0]["reason"] == "MODEL_UNAVAILABLE"
        assert funnel.buffered_count() == 0

    def test_orchestrator_flushes(self):
        src = inspect.getsource(
            __import__("services.pick_refresh_orchestrator",
                       fromlist=["PickRefreshOrchestrator"])
            .PickRefreshOrchestrator.refresh)
        assert "funnel_telemetry" in src and "flush" in src
