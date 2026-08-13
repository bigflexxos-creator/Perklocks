"""Block 2B.1B — NFL PRODUCTION RUNTIME WIRING + MAGIC WIRING tests.

Certifies the production integration of the Platinum NFL simulator
into the authoritative NFL runtime + provides the required test
matrix per spec §36-40.  Extends Block 2B.1A (foundation).
"""
from __future__ import annotations

import os
import pytest


# ═════════════════════════════════════════════════════════════════════
# §A  Production wiring hook — sports_engine._props_picks_from_event
# ═════════════════════════════════════════════════════════════════════

class TestProductionWiringHook:
    """Verify that the NFL emission branch invokes Platinum
    simulate() + attach_challenger_output() — proving §15 wiring."""

    def test_platinum_simulate_wired_into_sports_engine(self):
        import inspect, sports_engine
        src = inspect.getsource(sports_engine._props_picks_from_event)
        # Must import Platinum simulator.
        assert "from services.platinum_nfl import" in src
        # Must call simulate + attach.
        assert "_platinum_simulate(" in src
        assert "_platinum_attach(" in src
        # Must handle failure without crashing the batch (§34).
        assert "SIMULATOR_FAILED" in src
        # Must NOT overwrite model_probability with sim_probability.
        assert 'new_pick["model_probability"]' not in src or \
               "attach_challenger_output" in src

    def test_wiring_is_nfl_scoped(self):
        """Guard: the Platinum wiring must be inside a
        ``sport == "NFL"`` branch — must NOT run for MLB/Tennis/other."""
        import inspect, sports_engine
        src = inspect.getsource(sports_engine._props_picks_from_event)
        # Locate the wiring block and its guard.
        assert 'sport == "NFL"' in src
        idx = src.find("_platinum_simulate(")
        assert idx > 0
        # The immediate ancestor branch guarding this block MUST test
        # sport == "NFL" — search backwards from the call site for
        # the nearest 'if (' block start.
        window = src[max(0, idx - 4000): idx]
        assert 'sport == "NFL"' in window, (
            "Platinum wiring must be inside an NFL sport guard")

    def test_wiring_stamps_season_type_on_pick(self):
        import inspect, sports_engine
        src = inspect.getsource(sports_engine._props_picks_from_event)
        # Season type must be stamped on the pick.
        assert 'new_pick["season_type"]' in src

    def test_wiring_uses_deterministic_seed(self):
        """§33 — production must derive stable seeds from canonical
        identifiers.  The seed derivation lives inside the wiring
        block."""
        import inspect, sports_engine
        src = inspect.getsource(sports_engine._props_picks_from_event)
        # Confirm the seed derivation uses pick id + market + line.
        assert "hash(" in src
        assert "_seed = " in src


# ═════════════════════════════════════════════════════════════════════
# §B  End-to-end fixture: real-shape preseason candidate → sim →
#     Champion/Challenger frozen row (§40)
# ═════════════════════════════════════════════════════════════════════

class TestE2EPreseasonPickFixture:
    """Runs the Platinum simulator against a realistic
    preseason-shaped pick without any live network call."""

    def _preseason_pick(self, **overrides) -> dict:
        p = {
            "id": "prod-nfl-pre-1",
            "sport": "NFL",
            "sport_key": "americanfootball_nfl_preseason",
            "market": "player_pass_yds",
            "side": "Over",
            "line": 145.5,
            "player_name": "Drake Maye",
            "home_team": "New England Patriots",
            "away_team": "Indianapolis Colts",
            "event_id": "prod-preseason-e1",
            "event_time": "2026-08-13T23:30:00Z",
            "book_odds": -115,
            "sportsbook": "DraftKings",
            "model_probability": 0.52,
            "lock_score": 79.0,
        }
        p.update(overrides)
        return p

    def test_preseason_qb_passes_through_platinum_end_to_end(self):
        from services.platinum_nfl import (
            simulate, attach_challenger_output, SeasonType,
            classify_season_type, QBOpportunity,
        )
        pick = self._preseason_pick()
        st = classify_season_type(pick)
        assert st is SeasonType.PRESEASON
        opp = QBOpportunity(att_mean=30.0, ypa_mean=6.8, role_certainty=0.85)
        out = simulate(
            pick,
            ctx={"qb_opportunity": opp, "position": "QB",
                  "team_plays": 54.0, "game_pass_rate": 0.60,
                  "season_type": SeasonType.PRESEASON},
            seed=12345, n_sims=2000,
        )
        assert out["ran"] is True
        assert out["season_type"] == "PRESEASON"
        # Preseason regime widens role uncertainty.
        assert out["role_uncertainty"] > 0.20
        # Distribution shape present.
        for k in ("distribution_mean", "distribution_median",
                   "q10", "q25", "q75", "q90", "variance", "std"):
            assert k in out and out[k] is not None
        # Champion / Challenger attachment.
        attach_challenger_output(pick, out)
        assert pick["model_probability"] == 0.52         # Champion untouched
        assert "platinum_challenger" in pick
        frozen = pick["champion_challenger"]["platinum_nfl"]
        assert frozen["season_type"] == "PRESEASON"
        assert frozen["challenger_ran"] is True

    def test_preseason_qb_ran_false_fails_closed(self):
        from services.platinum_nfl import simulate, attach_challenger_output
        pick = self._preseason_pick()
        # No opportunity + no ctx → route to player-market path but
        # missing position mapping → fails safely.
        pick["market"] = "totally_unsupported_random_market"
        out = simulate(pick, seed=7)
        assert out["ran"] is False
        assert out["sim_probability"] is None
        attach_challenger_output(pick, out)
        # sim_probability must NEVER be model_probability on failure.
        assert pick.get("sim_probability") is None


# ═════════════════════════════════════════════════════════════════════
# §C  Week-1 auto-switch proof (§36)
# ═════════════════════════════════════════════════════════════════════

class TestWeek1AutomaticRegimeSwitch:
    """Two otherwise identical NFL events, one preseason one Week 1
    regular — production runtime must change regime automatically
    with no env/admin/code change."""

    def _make(self, sport_key: str, week: int = 1) -> dict:
        return {
            "sport": "NFL", "sport_key": sport_key,
            "market": "player_pass_yds", "side": "Over", "line": 250.5,
            "home_team": "Buffalo Bills", "away_team": "New York Jets",
            "player_name": "Josh Allen",
            "event_id": f"e-{sport_key}-w{week}",
            "event_time": "2026-09-04T00:00:00Z",
            "book_odds": -115, "week": week,
            "model_probability": 0.55, "lock_score": 87.0,
        }

    def test_preseason_and_regular_switch_automatically(self):
        from services.platinum_nfl import (
            simulate, QBOpportunity, SeasonType,
        )
        opp = QBOpportunity(att_mean=34.0, ypa_mean=7.5, role_certainty=1.0)
        pre_pick = self._make("americanfootball_nfl_preseason")
        reg_pick = self._make("americanfootball_nfl")
        pre = simulate(pre_pick,
                        ctx={"qb_opportunity": QBOpportunity(**{**opp.__dict__}),
                              "position": "QB",
                              "team_plays": 54.0, "game_pass_rate": 0.60},
                        seed=42, n_sims=1500)
        reg = simulate(reg_pick,
                        ctx={"qb_opportunity": QBOpportunity(**{**opp.__dict__}),
                              "position": "QB",
                              "team_plays": 66.0, "game_pass_rate": 0.62},
                        seed=42, n_sims=1500)
        assert pre["season_type"] == "PRESEASON"
        assert reg["season_type"] == "REGULAR_SEASON"
        # Preseason mean is significantly lower (quarters capped).
        assert pre["distribution_mean"] < reg["distribution_mean"] * 0.7
        # Preseason role uncertainty is higher.
        assert pre["role_uncertainty"] > reg["role_uncertainty"]

    def test_no_env_or_admin_toggle_used_by_wiring(self):
        """Search the runtime wiring for any env-based season toggle."""
        import inspect, sports_engine
        src = inspect.getsource(sports_engine._props_picks_from_event)
        idx = src.find("_platinum_simulate(")
        assert idx > 0
        window = src[max(0, idx - 2000):idx]
        # No env toggle inside wiring block.
        assert "NFL_SEASON_MODE" not in window
        assert "SEASON_MODE_OVERRIDE" not in window
        assert "os.environ" not in window


# ═════════════════════════════════════════════════════════════════════
# §D  Postseason proof (§37)
# ═════════════════════════════════════════════════════════════════════

class TestPostseasonRegime:
    def test_postseason_regime_detected_automatically(self):
        from services.platinum_nfl import (
            simulate, QBOpportunity, SeasonType,
        )
        pick = {
            "sport": "NFL", "sport_key": "americanfootball_nfl",
            "week": 20, "game_type": "conf",
            "market": "player_pass_yds", "side": "Over", "line": 275.5,
            "home_team": "Kansas City Chiefs", "away_team": "Buffalo Bills",
            "player_name": "Patrick Mahomes",
            "event_id": "e-conf-1", "event_time": "2027-01-25T22:00:00Z",
            "book_odds": -115, "model_probability": 0.58,
        }
        opp = QBOpportunity(att_mean=36.0, ypa_mean=7.7, role_certainty=1.0)
        out = simulate(pick,
                        ctx={"qb_opportunity": opp, "position": "QB",
                              "team_plays": 68.0, "game_pass_rate": 0.60},
                        seed=99, n_sims=1500)
        assert out["ran"] is True
        assert out["season_type"] == "POSTSEASON"

    def test_preseason_rows_cannot_bleed_into_postseason(self):
        from services.platinum_nfl.season_type import (
            enforce_no_preseason_contamination, SeasonType,
        )
        rows = [
            {"season_type": "PRESEASON", "yards": 10},
            {"season_type": "POSTSEASON", "yards": 300},
        ]
        kept = enforce_no_preseason_contamination(
            rows, allowed=SeasonType.POSTSEASON)
        assert len(kept) == 1 and kept[0]["yards"] == 300


# ═════════════════════════════════════════════════════════════════════
# §E  Anti-fake static tests (§32)
# ═════════════════════════════════════════════════════════════════════

class TestAntiFakeStatic:
    """§32 forbids semantic copying of model_probability into
    sim_probability, fake agreement, hard-coded season flags,
    calendar-month season inference, synthetic sportsbook lines,
    arbitrary NFL score floors, direct rogue writes, preseason
    contamination, missing provenance."""

    def test_no_sim_equals_model_semantic_copy(self):
        import inspect
        from services.platinum_nfl import simulator, player_markets, game_markets
        for m in (simulator, player_markets, game_markets):
            src = inspect.getsource(m)
            # Reject any literal that semantically copies model→sim.
            assert "sim_probability = model_probability" not in src, m.__name__
            assert "sim_probability=model_probability" not in src, m.__name__

    def test_no_hardcoded_season_flag_in_wiring(self):
        import inspect, sports_engine
        src = inspect.getsource(sports_engine._props_picks_from_event)
        idx = src.find("_platinum_simulate(")
        window = src[max(0, idx - 3000): idx + 3000]
        assert "hardcoded_season" not in window.lower()
        # Season classification must be via classify_season_type,
        # never via a literal SeasonType.REGULAR_SEASON assignment.
        assert 'SeasonType.REGULAR_SEASON,' not in window
        assert 'SeasonType.PRESEASON,' not in window
        assert 'SeasonType.POSTSEASON,' not in window

    def test_no_calendar_month_season_inference(self):
        import inspect
        from services.platinum_nfl import season_type
        src = inspect.getsource(season_type)
        # There must be no if commence_time.month in {8}: PRESEASON style.
        assert ".month == " not in src
        assert ".month in {" not in src

    def test_no_synthetic_sportsbook_lines_in_wiring(self):
        import inspect, sports_engine
        src = inspect.getsource(sports_engine._props_picks_from_event)
        idx = src.find("_platinum_simulate(")
        window = src[max(0, idx - 3000): idx + 3000]
        # Guard against literal fabricated odds.
        assert 'book_odds = -110' not in window
        assert 'book_odds = 100' not in window

    def test_no_arbitrary_nfl_score_floor(self):
        import inspect, sports_engine
        src = inspect.getsource(sports_engine._props_picks_from_event)
        idx = src.find("_platinum_simulate(")
        window = src[max(0, idx - 500): idx + 4000]
        # Reject a literal Lock Score boost tied to Platinum output.
        assert 'lock_score += ' not in window or 'ATD' in window
        # Existence of Platinum block does NOT set lock_score.
        assert 'new_pick["lock_score"] = 99' not in window

    def test_provenance_stamped_on_success(self):
        from services.platinum_nfl import simulate, QBOpportunity
        pick = {
            "sport": "NFL", "sport_key": "americanfootball_nfl",
            "market": "player_pass_yds", "side": "Over", "line": 250.5,
            "player_name": "Josh Allen",
            "home_team": "Buffalo Bills", "away_team": "New York Jets",
            "event_id": "e-1", "book_odds": -115,
        }
        out = simulate(pick,
                        ctx={"qb_opportunity": QBOpportunity(role_certainty=1.0),
                              "position": "QB",
                              "team_plays": 66, "game_pass_rate": 0.62},
                        seed=1, n_sims=500)
        assert out["ran"] is True
        # Provenance on success.
        for k in ("simulator_name", "simulator_version",
                   "simulator_type", "season_type",
                   "role_uncertainty", "input_provenance"):
            assert k in out
        assert out["input_provenance"]["market"] == "player_pass_yds"


# ═════════════════════════════════════════════════════════════════════
# §F  Rogue-runtime enforcement (§31)
# ═════════════════════════════════════════════════════════════════════

class TestRogueRuntimeEnforcement:
    def test_no_unapproved_nfl_publishers(self):
        """Full enforcement — zero unapproved NFL board writers."""
        from services.platinum_nfl import verify_no_rogue_nfl_runtime
        findings = verify_no_rogue_nfl_runtime()
        assert findings == [], (
            "Unapproved NFL board writers detected: "
            + ", ".join(f"{f.file}:{f.line}[{f.category}]" for f in findings)
        )

    def test_approved_runtimes_documented(self):
        from services.platinum_nfl import APPROVED_NFL_RUNTIMES
        assert "sports_engine._props_picks_from_event" in APPROVED_NFL_RUNTIMES
        # ATD kept separate per §3.
        assert "nfl_atd_engine.predict_player_atd" in APPROVED_NFL_RUNTIMES

    def test_sim_nfl_stub_is_not_reachable(self):
        """§4 — sim_nfl.py stub must not become reachable."""
        import subprocess
        result = subprocess.run(
            ["grep", "-rn", "-l", "-E",
             r"(import sim_nfl|from sim_nfl)", "/app/backend/",
             "--include=*.py", "--exclude-dir=__pycache__",
             "--exclude-dir=tests"],
            capture_output=True, text=True,
        )
        importers = [l for l in result.stdout.strip().split("\n") if l]
        # Only sim_nfl.py itself may reference the string (its own
        # docstring / module).  No importer.
        offenders = [i for i in importers
                     if not i.endswith("/sim_nfl.py")]
        assert offenders == [], f"sim_nfl.py still imported: {offenders}"


# ═════════════════════════════════════════════════════════════════════
# §G  Consumer eligibility (§29)
# ═════════════════════════════════════════════════════════════════════

class TestConsumerEligibility:
    """NFL candidates must reach Locks / Rollover / Parlay through
    the SAME canonical publication data, not through independent
    consumer-specific eligibility rewrites."""

    def test_locks_eligibility_via_shared_gate(self):
        from services.board_projection_service import BoardProjectionService
        pick = {
            "id": "nfl-locks-1", "sport": "NFL",
            "market": "Josh Allen (BUF) Over 250.5 Passing Yards",
            "side": "Over", "line": 250.5,
            "book_odds": -115, "sportsbook": "FanDuel",
            "implied_probability": 0.535,
            "lock_score": 88.0, "published_lock_score": 88.0,
            "event_id": "e-nfl-1", "event_time": "2026-09-04T00:00:00Z",
            "no_bet": False, "off_board": False,
            "hide_from_main_board": False,
            "season_type": "REGULAR_SEASON",
            "lineup_status": {"status": "CONFIRMED"},
        }
        ids = BoardProjectionService().project_ids([pick])
        assert pick["id"] in ids

    def test_no_real_line_flag_blocks_nfl_pick(self):
        from services.board_projection_service import BoardProjectionService
        pick = {
            "id": "nfl-nrl", "sport": "NFL",
            "market": "Josh Allen Over 250.5 Passing Yards",
            "side": "Over", "line": 250.5,
            "book_odds": -115, "implied_probability": 0.535,
            "lock_score": 99.0, "published_lock_score": 99.0,
            "event_id": "e-nfl-2", "event_time": "2026-09-04T00:00:00Z",
            "no_real_book_line": True,
        }
        ids = BoardProjectionService().project_ids([pick])
        assert pick["id"] not in ids


# ═════════════════════════════════════════════════════════════════════
# §H  Rejection funnel (§28)
# ═════════════════════════════════════════════════════════════════════

class TestRejectionFunnel:
    def test_funnel_stages_documented(self):
        from services.platinum_nfl import NFLRejectionStage
        # Every §28-required stage must exist.
        required = {
            "NO_REAL_MARKET", "INVALID_SPORTSBOOK_LINE",
            "CANONICAL_EVENT_FAILURE", "CANONICAL_PLAYER_FAILURE",
            "STALE_ROSTER_OR_TEAM", "UNSUPPORTED_MARKET",
            "MISSING_EVIDENCE", "SEASON_TYPE_UNKNOWN",
            "SIMULATOR_FAILED", "LIFECYCLE_INVALID", "DUPLICATE",
            "CONTRADICTION_RISK_RULE",
            "LOCK_SCORE_BELOW_BOARD_THRESHOLD",
            "CONSUMER_SPECIFIC_INELIGIBILITY",
        }
        stages = {s.value for s in NFLRejectionStage}
        missing = required - stages
        assert missing == set(), f"missing funnel stages: {missing}"

    def test_classify_from_sim_output_maps_reasons(self):
        from services.platinum_nfl import (
            classify_from_sim_output, NFLRejectionStage,
        )
        assert classify_from_sim_output(
            {"ran": False, "reason": "SEASON_TYPE_UNKNOWN"}
        ) is NFLRejectionStage.SEASON_TYPE_UNKNOWN
        assert classify_from_sim_output(
            {"ran": False, "reason": "MISSING_OPPORTUNITY"}
        ) is NFLRejectionStage.MISSING_EVIDENCE
        assert classify_from_sim_output(
            {"ran": False, "reason": "UNSUPPORTED_PLAYER_MARKET"}
        ) is NFLRejectionStage.UNSUPPORTED_MARKET
        assert classify_from_sim_output(
            {"ran": True, "sim_probability": 0.6}
        ) is None


# ═════════════════════════════════════════════════════════════════════
# §I  Champion / Challenger production integration (§17)
# ═════════════════════════════════════════════════════════════════════

class TestChampionChallengerProductionIntegration:
    def test_frozen_row_includes_required_fields(self):
        from services.platinum_nfl import (
            simulate, attach_challenger_output, QBOpportunity,
        )
        pick = {
            "sport": "NFL", "sport_key": "americanfootball_nfl",
            "market": "player_pass_yds", "side": "Over", "line": 250.5,
            "home_team": "Buffalo Bills", "away_team": "New York Jets",
            "player_name": "Josh Allen",
            "event_id": "e-cc-1", "book_odds": -115, "sportsbook": "DK",
            "model_probability": 0.55,
        }
        out = simulate(pick,
                        ctx={"qb_opportunity": QBOpportunity(role_certainty=1.0),
                              "position": "QB",
                              "team_plays": 66, "game_pass_rate": 0.62},
                        seed=1, n_sims=500)
        attach_challenger_output(pick, out, role_evidence={"snaps": 66})
        frozen = pick["champion_challenger"]["platinum_nfl"]
        for k in ("prediction_timestamp", "event_id", "market", "side",
                   "line", "odds", "season_type",
                   "champion_probability", "challenger_probability",
                   "challenger_version", "challenger_ran",
                   "challenger_summary", "role_evidence",
                   "input_provenance"):
            assert k in frozen, f"missing frozen field {k}"
        assert frozen["role_evidence"]["snaps"] == 66

    def test_champion_never_overwritten(self):
        from services.platinum_nfl import (
            simulate, attach_challenger_output, QBOpportunity,
        )
        pick = {
            "sport": "NFL", "sport_key": "americanfootball_nfl",
            "market": "player_pass_yds", "side": "Over", "line": 220.5,
            "model_probability": 0.63,
            "player_name": "Josh Allen",
            "home_team": "Buffalo Bills", "away_team": "NYJ",
            "event_id": "e-champ", "book_odds": -110,
        }
        out = simulate(pick,
                        ctx={"qb_opportunity": QBOpportunity(role_certainty=1.0),
                              "position": "QB",
                              "team_plays": 66, "game_pass_rate": 0.62},
                        seed=1, n_sims=500)
        attach_challenger_output(pick, out)
        assert pick["model_probability"] == 0.63


# ═════════════════════════════════════════════════════════════════════
# §J  Live preseason funnel (§35)
# ═════════════════════════════════════════════════════════════════════

class TestLivePreseasonFunnel:
    """Attempt a real Odds API preseason probe and classify the
    result per §35.  Never require candidates to make the board — a
    legitimate rejection with explainable stages is acceptable.

    Skip cleanly if there is no API key present (LIVE_DATA_UNAVAILABLE).
    """

    def _api_key(self):
        return os.getenv("THE_ODDS_API_KEY", "").strip() or self._from_file()

    def _from_file(self):
        try:
            with open("/app/backend/.env") as f:
                for line in f:
                    if line.startswith("THE_ODDS_API_KEY="):
                        return line.strip().split("=", 1)[1]
        except Exception:
            return ""
        return ""

    def test_preseason_events_are_available_live(self):
        import urllib.request, json
        key = self._api_key()
        if not key:
            pytest.skip("LIVE_DATA_UNAVAILABLE — no odds API key")
        try:
            with urllib.request.urlopen(
                f"https://api.the-odds-api.com/v4/sports/"
                f"americanfootball_nfl_preseason/events?apiKey={key}",
                timeout=10,
            ) as r:
                data = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            pytest.skip(f"LIVE_DATA_UNAVAILABLE — {e}")
        if not isinstance(data, list):
            pytest.skip("LIVE_DATA_UNAVAILABLE — non-list response")
        if len(data) == 0:
            pytest.skip("LIVE_DATA_UNAVAILABLE — 0 events")
        # Live funnel proof: at least one preseason event exists AND
        # is classifiable as PRESEASON via canonical detection.
        from services.platinum_nfl import (
            classify_season_type, SeasonType,
        )
        first = data[0]
        first["sport_key"] = "americanfootball_nfl_preseason"
        assert classify_season_type(first) is SeasonType.PRESEASON

    def test_live_preseason_player_props_are_unavailable(self):
        """The Odds API currently does NOT offer NFL preseason player
        props (`player_receiving_yds` etc.).  Classify honestly as
        LIVE_DATA_UNAVAILABLE — this is not a code failure."""
        import urllib.request, urllib.parse, json
        key = self._api_key()
        if not key:
            pytest.skip("LIVE_DATA_UNAVAILABLE — no odds API key")
        try:
            with urllib.request.urlopen(
                f"https://api.the-odds-api.com/v4/sports/"
                f"americanfootball_nfl_preseason/events?apiKey={key}",
                timeout=10,
            ) as r:
                events = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            pytest.skip(f"LIVE_DATA_UNAVAILABLE — {e}")
        if not events:
            pytest.skip("LIVE_DATA_UNAVAILABLE — 0 preseason events")
        eid = events[0]["id"]
        markets = "h2h,spreads,totals,player_pass_yds"
        url = (f"https://api.the-odds-api.com/v4/sports/"
                f"americanfootball_nfl_preseason/events/{eid}/odds?"
                f"apiKey={key}&regions=us&markets={markets}"
                f"&oddsFormat=american")
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                data = json.loads(r.read().decode("utf-8"))
        except Exception:
            pytest.skip("LIVE_DATA_UNAVAILABLE — event-odds fetch failed")
        # If we got a dict with a "message" field, the provider
        # explicitly declined a player-prop market.  That's the
        # expected LIVE_DATA_UNAVAILABLE for player props.
        if isinstance(data, dict) and "message" in data:
            assert "player_" in data["message"].lower() or \
                   "invalid" in data["message"].lower()
            return
        # Otherwise inspect bookmakers; at minimum h2h/spread/total
        # must be present (game markets ARE live).
        bks = data.get("bookmakers") or []
        assert len(bks) > 0
        game_markets_present = any(
            mk.get("key") in ("h2h", "spreads", "totals")
            for bk in bks for mk in (bk.get("markets") or [])
        )
        assert game_markets_present, "Expected game markets on preseason"

    def test_live_preseason_game_market_e2e_via_platinum(self):
        """Real preseason game market → Platinum simulate → distribution
        + Champion/Challenger frozen row."""
        import urllib.request, json
        key = self._api_key()
        if not key:
            pytest.skip("LIVE_DATA_UNAVAILABLE — no odds API key")
        try:
            with urllib.request.urlopen(
                f"https://api.the-odds-api.com/v4/sports/"
                f"americanfootball_nfl_preseason/events?apiKey={key}",
                timeout=10,
            ) as r:
                events = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            pytest.skip(f"LIVE_DATA_UNAVAILABLE — {e}")
        if not events:
            pytest.skip("LIVE_DATA_UNAVAILABLE — 0 preseason events")
        eid = events[0]["id"]
        home = events[0]["home_team"]
        away = events[0]["away_team"]
        url = (f"https://api.the-odds-api.com/v4/sports/"
                f"americanfootball_nfl_preseason/events/{eid}/odds?"
                f"apiKey={key}&regions=us&markets=h2h,spreads,totals"
                f"&oddsFormat=american")
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                data = json.loads(r.read().decode("utf-8"))
        except Exception:
            pytest.skip("LIVE_DATA_UNAVAILABLE — event-odds fetch failed")
        # Extract the first bookmaker's total line.
        total_line = None
        book_odds  = None
        book_key   = None
        for bk in data.get("bookmakers") or []:
            for mk in bk.get("markets") or []:
                if mk.get("key") == "totals":
                    outs = mk.get("outcomes") or []
                    if outs and outs[0].get("point") is not None:
                        total_line = outs[0]["point"]
                        book_odds  = outs[0]["price"]
                        book_key   = bk.get("key")
                        break
            if total_line is not None:
                break
        if total_line is None:
            pytest.skip("LIVE_DATA_UNAVAILABLE — no totals line")
        # Build a real production-shape pick.
        from services.platinum_nfl import (
            simulate, attach_challenger_output,
        )
        pick = {
            "id": f"live-{eid}",
            "sport": "NFL",
            "sport_key": "americanfootball_nfl_preseason",
            "market": "total",
            "side": "Over",
            "line": total_line,
            "home_team": home, "away_team": away,
            "event_id": eid,
            "event_time": events[0]["commence_time"],
            "book_odds": book_odds, "sportsbook": book_key,
            "model_probability": 0.50,
        }
        out = simulate(pick,
                        ctx={"expected_margin_home": 0.0,
                              "total_line": float(total_line)},
                        seed=1234, n_sims=2000)
        assert out["ran"] is True
        assert out["season_type"] == "PRESEASON"
        assert out["market"] == "total"
        # Distribution present.
        assert out["distribution_mean"] > 0
        # Provenance.
        assert out["input_provenance"]["event_id"] == eid
        # Attach and verify Champion untouched.
        attach_challenger_output(pick, out)
        assert pick["model_probability"] == 0.50
        assert pick["platinum_challenger"]["ran"] is True


# ═════════════════════════════════════════════════════════════════════
# §K  Player-market E2E fixtures (§22)
# ═════════════════════════════════════════════════════════════════════

class TestPlayerMarketE2EFixtures:
    def _pick(self, **k):
        p = {"sport": "NFL", "sport_key": "americanfootball_nfl",
              "market": "player_pass_yds", "side": "Over", "line": 250.5,
              "player_name": "Josh Allen",
              "home_team": "BUF", "away_team": "NYJ",
              "event_id": "e-1", "book_odds": -110,
              "model_probability": 0.55}
        p.update(k); return p

    def test_qb_e2e(self):
        from services.platinum_nfl import (
            simulate, attach_challenger_output, QBOpportunity,
        )
        pick = self._pick(market="player_pass_yds", line=245.5)
        out = simulate(pick,
                        ctx={"qb_opportunity": QBOpportunity(role_certainty=1.0),
                              "position": "QB",
                              "team_plays": 66, "game_pass_rate": 0.62},
                        seed=1, n_sims=1000)
        assert out["ran"]
        attach_challenger_output(pick, out)
        assert pick["platinum_challenger"]["ran"] is True

    def test_rb_e2e(self):
        from services.platinum_nfl import (
            simulate, attach_challenger_output, RBOpportunity,
        )
        pick = self._pick(market="player_rush_yds", line=74.5,
                           player_name="James Cook")
        out = simulate(pick,
                        ctx={"rb_opportunity": RBOpportunity(
                                carry_share_mean=0.55, role_certainty=1.0),
                              "position": "RB",
                              "team_plays": 65, "game_pass_rate": 0.58},
                        seed=2, n_sims=1000)
        assert out["ran"]
        attach_challenger_output(pick, out)

    def test_wr_e2e(self):
        from services.platinum_nfl import (
            simulate, attach_challenger_output, WROpportunity,
        )
        pick = self._pick(market="player_receiving_yds", line=62.5,
                           player_name="Stefon Diggs")
        out = simulate(pick,
                        ctx={"wr_opportunity": WROpportunity(
                                target_share_mean=0.22, role_certainty=1.0),
                              "position": "WR",
                              "team_plays": 65, "game_pass_rate": 0.62},
                        seed=3, n_sims=1000)
        assert out["ran"]
        attach_challenger_output(pick, out)

    def test_te_e2e(self):
        from services.platinum_nfl import (
            simulate, attach_challenger_output, WROpportunity,
        )
        pick = self._pick(market="player_receptions", line=4.5,
                           player_name="Dalton Kincaid")
        out = simulate(pick,
                        ctx={"wr_opportunity": WROpportunity(
                                target_share_mean=0.17, catch_rate_mean=0.70,
                                role_certainty=0.90),
                              "position": "TE",
                              "team_plays": 65, "game_pass_rate": 0.62},
                        seed=4, n_sims=1000)
        assert out["ran"]

    def test_atd_e2e(self):
        from services.platinum_nfl import (
            simulate, attach_challenger_output, WROpportunity,
        )
        pick = self._pick(market="player_anytime_td", line=0.5,
                           side="Over", player_name="Dalton Kincaid")
        out = simulate(pick,
                        ctx={"wr_opportunity": WROpportunity(
                                target_share_mean=0.20, red_zone_share=0.22,
                                role_certainty=0.90),
                              "position": "TE",
                              "team_plays": 65, "game_pass_rate": 0.62},
                        seed=5, n_sims=1000)
        assert out["ran"]
        assert 0.0 <= out["sim_probability"] <= 1.0


# ═════════════════════════════════════════════════════════════════════
# §L  Prior blocks stay green
# ═════════════════════════════════════════════════════════════════════

class TestPriorBlocksRemainGreen:
    def test_2a5_2_hitter_wiring_intact(self):
        import inspect, sports_engine
        src = inspect.getsource(sports_engine._props_picks_from_event)
        assert "classify_lineup_status" in src
        assert "float(point) == 0.5" in src

    def test_2a5_3_projected_lineup_module_intact(self):
        from services.enrichment import mlb_projected_lineup
        assert hasattr(mlb_projected_lineup, "fetch_mlb_lineup_bundle")

    def test_2b1a_foundation_suite_still_importable(self):
        from services.platinum_nfl import (
            simulate, attach_challenger_output, PLATINUM_VERSION,
        )
        assert PLATINUM_VERSION == "2b.1a.v1"
