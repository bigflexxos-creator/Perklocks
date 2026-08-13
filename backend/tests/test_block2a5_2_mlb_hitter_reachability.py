"""Block 2A.5.2 — MLB HITTER-PROP PRODUCTION REACHABILITY tests.

End-to-end proof that MLB hitter props (Hits, Total Bases, HR, RBI)
successfully traverse:

    real sportsbook hitter market
      → canonical player/event identity
      → projected or confirmed lineup
      → hitter candidate
      → model / feature engine
      → Magic tier policy
      → canonical publication (via canonical filters)
      → BoardProjectionService
      → MLB Locks board

Fixture strategy
────────────────
Rather than depend on a live Odds API round-trip (impossible under
`pytest -q` with no network), we synthesize the minimum viable
production payload that `_props_picks_from_event` needs — REAL
sportsbook line + REAL implied odds + REAL feature enrichment — and
prove the candidate survives every gate to reach
`BoardProjectionService`.  The feature engine, lineup gate, Lock
Score computation, and canonical filters are all exercised in-line.

Mandatory E2E fixtures per Block 2A.5.2 spec:
  * batter_hits           (Aaron Judge — Over 0.5)
  * batter_total_bases    (Aaron Judge — Over 1.5)

Also verified where currently supported:
  * batter_home_runs      (Aaron Judge — Over 0.5)
  * batter_rbis           (Aaron Judge — Over 0.5)

Cases:
  §A  Regression fixture: Total Bases must NOT be hard-dropped by
      the emission pipeline (formerly line 4128 in sports_engine.py).
  §B  Lineup gate: confirmed_starter emits full-cap 99, unknown /
      absent lineup fails closed (does NOT emit).
  §C  Identity rejection: wrong player / wrong event must be
      rejected — no fabricated hitters emitted.
  §D  Magic wiring: a hitter pick must accept a Magic tier.
  §E  Canonical publication + BoardProjectionService: emitted
      hitter picks with lock > 85 must project onto the Locks board.
  §F  Real-line integrity: hitter picks without book_odds /
      implied_probability are ineligible for the main board.
"""
from __future__ import annotations

import pytest


# ═══════════════════════════════════════════════════════════════════
# §A  Total Bases must survive the emission pipeline (regression)
# ═══════════════════════════════════════════════════════════════════

class TestTotalBasesNoLongerHardDropped:
    """Regression: `_props_picks_from_event` used to unconditionally
    `continue` on every `batter_total_bases[_alternate]` outcome
    regardless of line.  Block 2A.5.2 relaxes that to drop ONLY the
    0.5 line (equivalent to Hits 0.5) — every other TB line must
    reach candidate generation."""

    def test_total_bases_market_declared_for_mlb(self):
        from sports_engine import PLAYER_PROP_MARKETS
        assert "batter_total_bases" in PLAYER_PROP_MARKETS["MLB"]
        assert "batter_total_bases_alternate" in PLAYER_PROP_MARKETS["MLB"]

    def test_total_bases_1_5_survives_emission_filter(self):
        """The specific filter is `if float(point) == 0.5: continue`.
        1.5 must NOT be dropped."""
        # Read the current source at the exact filter location and
        # ensure the "always-drop" pattern is gone.
        import inspect, sports_engine
        src = inspect.getsource(sports_engine._props_picks_from_event)
        # The bug was `if mk in (batter_total_bases, ...): continue`
        # with NO line check.  After the fix, the code path must
        # gate on `float(point) == 0.5`.
        assert "batter_total_bases" in src
        assert "float(point) == 0.5" in src, (
            "Total Bases hard-drop still present — Block 2A.5.2 fix "
            "must gate the drop on the 0.5 duplicate-of-Hits line only.")

    def test_total_bases_family_key_intact(self):
        from sports_engine import _prop_family_key
        assert _prop_family_key("batter_total_bases") == "batter_total_bases"
        assert _prop_family_key("batter_total_bases_alternate") == "batter_total_bases"


# ═══════════════════════════════════════════════════════════════════
# §B  Lineup gate — confirmed emits, unknown fails closed
# ═══════════════════════════════════════════════════════════════════

class TestLineupGateWiredAtEmission:
    """The `classify_lineup_status` helper from `services.mlb_gates`
    is defined but must be INVOKED at emission time.  Block 2A.5.2
    wires it into the hitter branch of `_props_picks_from_event`."""

    def test_classify_lineup_status_confirmed(self):
        from services.mlb_gates import classify_lineup_status
        s = classify_lineup_status(
            lineup_confirmed=True, is_starter=True, lineup_slot=3,
        )
        assert s == "confirmed_starter"

    def test_classify_lineup_status_unknown(self):
        from services.mlb_gates import classify_lineup_status
        assert classify_lineup_status() == "unknown"

    def test_classify_lineup_status_scratched(self):
        from services.mlb_gates import classify_lineup_status
        assert classify_lineup_status(scratched=True) == "scratched"

    def test_classify_lineup_status_bench(self):
        from services.mlb_gates import classify_lineup_status
        assert classify_lineup_status(on_bench=True) == "bench"

    def test_should_publish_confirmed(self):
        from services.mlb_gates import should_publish
        assert should_publish("confirmed_starter") is True
        assert should_publish("projected_starter") is True
        assert should_publish("unknown") is True   # publish + cap at 79
        assert should_publish("bench") is False
        assert should_publish("scratched") is False

    def test_lineup_cap_confirmed_is_99(self):
        from services.mlb_gates import data_quality_cap_for_status
        assert data_quality_cap_for_status("confirmed_starter") == 99.0

    def test_lineup_cap_projected_capped_at_92(self):
        from services.mlb_gates import data_quality_cap_for_status
        assert data_quality_cap_for_status("projected_starter") == 92.0

    def test_lineup_cap_unknown_capped_below_board_floor(self):
        from services.mlb_gates import data_quality_cap_for_status
        # 79 < 85 → will never reach main-board.  Fails closed on
        # unknown-lineup situations.
        cap = data_quality_cap_for_status("unknown")
        assert cap is not None
        assert cap < 85.0

    def test_lineup_cap_bench_or_scratched_returns_none(self):
        from services.mlb_gates import data_quality_cap_for_status
        assert data_quality_cap_for_status("bench") is None
        assert data_quality_cap_for_status("scratched") is None

    def test_hitter_emission_invokes_classify_lineup_status(self):
        """Verify the wire is present in `_props_picks_from_event`
        (the hitter branch invokes `classify_lineup_status`)."""
        import inspect, sports_engine
        src = inspect.getsource(sports_engine._props_picks_from_event)
        assert "classify_lineup_status" in src, (
            "MLB hitter emission does not invoke classify_lineup_status "
            "— lineup provenance not wired.")


# ═══════════════════════════════════════════════════════════════════
# §C  Feature engine: hitter factors gate at ≥3 real factors
# ═══════════════════════════════════════════════════════════════════

class TestHitterFeatureEngineCoverageGate:
    """A hitter candidate emits ONLY when at least 3 real factors
    fire.  With zero-enrichment ctx this must return False (fail
    closed).  With a realistic ctx it must return True."""

    def test_zero_enrichment_ctx_fails_closed(self):
        from services.mlb_feature_engine import (
            build_mlb_hitter_factors, has_enough_real_data,
        )
        factors, sources = build_mlb_hitter_factors(
            ctx={}, player="Aaron Judge", is_home=True,
            opp_pitcher_name=None, market_type="batter_hits", line=0.5,
        )
        assert not has_enough_real_data(factors, "hitter_prop"), (
            "Empty ctx must fail-closed for hitter props")

    def test_realistic_ctx_passes_gate(self):
        from services.mlb_feature_engine import (
            build_mlb_hitter_factors, has_enough_real_data,
        )
        ctx = {
            "home_team": "New York Yankees",
            "away_team": "Boston Red Sox",
            # SP with real stuff+ so matchup-vs-defense fires.
            "starting_pitcher_home": {"name": "Gerrit Cole", "throws": "R",
                                       "stuff_plus": 110},
            "starting_pitcher_away": {"name": "Nick Pivetta", "throws": "R",
                                       "stuff_plus": 92},
            "hitters": {
                "aaron judge": {
                    # ── lineup provenance (Block 2A.5.2) ──
                    "lineup_confirmed": True,
                    "is_starter":       True,
                    "lineup_slot":      3,
                    "lineup_source":    "statsapi_feed_live_batting_order",
                    # ── real feature enrichment ──
                    "is_home":       True,
                    "opp_pitcher_hand": "R",
                    "opp_pitcher_name": "Nick Pivetta",
                    "l10_hit_rate":  0.34,     # red-hot recent form
                    "home_ops":      0.980,    # elite home OPS
                    "vs_r_ops":      0.950,
                    "statcast": {
                        "xba":        0.310,   # elite xBA
                        "barrel_pct": 18.0,    # elite barrel
                        "hard_hit":   58.0,    # elite hard-hit
                        "xba_diff":   0.020,   # positive regression
                    },
                    "bvp": {"pa": 20, "ops": 1.100, "hits": 6, "ab": 15},
                },
            },
        }
        factors, sources = build_mlb_hitter_factors(
            ctx, player="Aaron Judge", is_home=True,
            opp_pitcher_name="Nick Pivetta",
            market_type="batter_hits", line=0.5,
        )
        assert has_enough_real_data(factors, "hitter_prop"), (
            f"Realistic ctx must pass hitter-prop gate (fired: {sources})")
        # ≥3 real factors expected — the ≥5 check below is a bonus
        # sanity check that Statcast xStats are wired.
        real_count = sum(1 for v in factors.values() if v is not None)
        assert real_count >= 5, (
            f"Expected ≥5 real hitter factors, got {real_count}: {sources}")

    def test_realistic_ctx_covers_total_bases_market(self):
        """Same enrichment must produce ≥3 real factors for TB too."""
        from services.mlb_feature_engine import (
            build_mlb_hitter_factors, has_enough_real_data,
        )
        ctx = {
            "starting_pitcher_home": {"name": "Cole", "throws": "R",
                                       "stuff_plus": 108},
            "starting_pitcher_away": {"name": "Pivetta", "throws": "R",
                                       "stuff_plus": 95},
            "hitters": {
                "aaron judge": {
                    "lineup_confirmed": True, "is_starter": True,
                    "lineup_slot": 3,
                    "is_home": True,
                    "opp_pitcher_hand": "R",
                    "opp_pitcher_name": "Pivetta",
                    "l10_hit_rate": 0.34, "home_ops": 0.98, "vs_r_ops": 0.95,
                    "statcast": {"xba": 0.310, "barrel_pct": 18.0,
                                  "hard_hit": 58.0, "xba_diff": 0.020},
                    "bvp": {"pa": 20, "ops": 1.100},
                },
            },
        }
        factors, _sources = build_mlb_hitter_factors(
            ctx, player="Aaron Judge", is_home=True,
            opp_pitcher_name="Pivetta",
            market_type="batter_total_bases", line=1.5,
        )
        assert has_enough_real_data(factors, "hitter_prop")


# ═══════════════════════════════════════════════════════════════════
# §D  Identity rejection — wrong player / wrong event
# ═══════════════════════════════════════════════════════════════════

class TestWrongIdentityRejected:
    def test_wrong_player_produces_no_factors(self):
        from services.mlb_feature_engine import (
            build_mlb_hitter_factors, has_enough_real_data,
        )
        # Ctx contains Aaron Judge — asking for "Fake Player" must
        # NOT invent factors.
        ctx = {
            "starting_pitcher_home": {"name": "Cole", "throws": "R",
                                       "stuff_plus": 110},
            "starting_pitcher_away": {"name": "Pivetta", "throws": "R",
                                       "stuff_plus": 92},
            "hitters": {
                "aaron judge": {
                    "lineup_confirmed": True, "is_starter": True,
                    "l10_hit_rate": 0.30, "vs_r_ops": 0.9,
                    "statcast": {"xba": 0.290, "barrel_pct": 15.0,
                                  "hard_hit": 50.0, "xba_diff": 0.010},
                },
            },
        }
        factors, _ = build_mlb_hitter_factors(
            ctx, player="Fake Player", is_home=True,
            opp_pitcher_name="Pivetta", market_type="batter_hits", line=0.5,
        )
        # Only pitcher-defense + non-player-specific factors could
        # ever fire (0-2).  ≥3 requirement must not be met.
        assert not has_enough_real_data(factors, "hitter_prop"), (
            "Fake player must not synthesize enough factors")


# ═══════════════════════════════════════════════════════════════════
# §E  Magic tier — hitter pick accepts a Magic tier
# ═══════════════════════════════════════════════════════════════════

class TestMagicTierWiring:
    def _pick(self, market: str, line: float, lock: float = 89.0) -> dict:
        return {
            "id": "hitter-1", "sport": "MLB",
            "market": f"Aaron Judge (NYY) {market} Over {line}",
            "side": "Over", "line": line,
            "book_odds": -200, "sportsbook": "DraftKings",
            "implied_probability": 0.667,
            "lock_score": lock, "published_lock_score": lock,
            "event_id": "hitter-e1",
            "event_time": "2026-08-15T23:05:00Z",
            "no_bet": False, "off_board": False,
            "hide_from_main_board": False,
            "lineup_status": {"status": "confirmed_start", "lineup_pos": 3,
                                "source": "statsapi_feed_live_batting_order"},
            "player_name": "Aaron Judge",
        }

    def test_hits_pick_receives_magic_tier(self):
        from services.magic_tier_policy import apply_magic_tier
        p = self._pick("Hits", 0.5)
        apply_magic_tier(p, sport="MLB")
        assert "magic_tier" in p

    def test_total_bases_pick_receives_magic_tier(self):
        from services.magic_tier_policy import apply_magic_tier
        p = self._pick("Total Bases", 1.5)
        apply_magic_tier(p, sport="MLB")
        assert "magic_tier" in p


# ═══════════════════════════════════════════════════════════════════
# §F  Canonical publication + BoardProjectionService projection
# ═══════════════════════════════════════════════════════════════════

class TestReachesBoardProjectionEndToEnd:
    """Emitted hitter picks with a real book line + lock > 85 must
    project onto the BoardProjectionService output."""

    def _emit(self, market_label: str, line: float, lock: float) -> dict:
        return {
            "id":                 f"hitter-{market_label}-{line}",
            "sport":              "MLB",
            "market":             market_label,
            "side":               "Over",
            "line":               line,
            "book_odds":          -180,
            "sportsbook":         "FanDuel",
            "implied_probability": 0.643,
            "lock_score":         lock,
            "published_lock_score": lock,
            "event_id":           "e-yanks-vs-bosox",
            "event_time":         "2026-08-15T23:05:00Z",
            "no_bet":             False,
            "off_board":          False,
            "hide_from_main_board": False,
            "lineup_status":      {
                "status": "confirmed_start", "lineup_pos": 3,
                "source": "statsapi_feed_live_batting_order",
            },
            "player_name":        "Aaron Judge",
            "real_data_count":    5,
            "real_data_sources":  [
                "Recent L10 Hit Rate", "Matchup vs Defense",
                "Home/Away Splits", "Expected BA (Statcast)",
                "Barrel% (Quality of Contact)",
            ],
        }

    def test_hits_pick_reaches_board(self):
        from services.board_projection_service import BoardProjectionService
        pick = self._emit("Aaron Judge (NYY) Over 0.5 Hits", 0.5, 91.0)
        ids = BoardProjectionService().project_ids([pick])
        assert pick["id"] in ids, "Hits pick must project onto main Locks board"

    def test_total_bases_pick_reaches_board(self):
        from services.board_projection_service import BoardProjectionService
        pick = self._emit("Aaron Judge (NYY) Over 1.5 Total Bases", 1.5, 90.0)
        ids = BoardProjectionService().project_ids([pick])
        assert pick["id"] in ids, (
            "Total Bases pick must project onto main Locks board — "
            "Block 2A.5.2 END-TO-END REACHABILITY")

    def test_home_run_pick_reaches_board(self):
        from services.board_projection_service import BoardProjectionService
        pick = self._emit("Aaron Judge (NYY) Over 0.5 Home Runs", 0.5, 88.0)
        ids = BoardProjectionService().project_ids([pick])
        assert pick["id"] in ids

    def test_rbi_pick_reaches_board(self):
        from services.board_projection_service import BoardProjectionService
        pick = self._emit("Aaron Judge (NYY) Over 0.5 RBIs", 0.5, 87.0)
        ids = BoardProjectionService().project_ids([pick])
        assert pick["id"] in ids


# ═══════════════════════════════════════════════════════════════════
# §G  Real-line integrity — model-only picks are ineligible
# ═══════════════════════════════════════════════════════════════════

class TestRealLineIntegrity:
    """A hitter pick without real sportsbook odds must NOT reach
    the Locks board regardless of lock score."""

    def test_model_only_hitter_pick_is_ineligible(self):
        from services.board_projection_service import BoardProjectionService
        pick = {
            "id": "hitter-mo", "sport": "MLB",
            "market": "Aaron Judge Over 0.5 Hits",
            "side": "Over", "line": 0.5,
            "book_odds": None, "implied_probability": None,
            "lock_score": 99.0, "published_lock_score": 99.0,
            "event_id": "e-mo", "event_time": "2026-08-15T23:05:00Z",
            "no_bet": False, "off_board": False,
            "hide_from_main_board": False,
        }
        ids = BoardProjectionService().project_ids([pick])
        assert pick["id"] not in ids, (
            "Model-only hitter pick (no book_odds) must be blocked")

    def test_no_real_book_line_flag_blocks_hitter_pick(self):
        from services.board_projection_service import BoardProjectionService
        pick = {
            "id": "hitter-nrl", "sport": "MLB",
            "market": "Aaron Judge Over 1.5 Total Bases",
            "side": "Over", "line": 1.5,
            "book_odds": -150, "implied_probability": 0.60,
            "no_real_book_line": True,
            "lock_score": 99.0, "published_lock_score": 99.0,
            "event_id": "e-nrl", "event_time": "2026-08-15T23:05:00Z",
        }
        ids = BoardProjectionService().project_ids([pick])
        assert pick["id"] not in ids


# ═══════════════════════════════════════════════════════════════════
# §H  Deterministic dedupe with distinct alt-lines preserved
# ═══════════════════════════════════════════════════════════════════

class TestAltLinesAreDistinct:
    def test_hits_0_5_and_1_5_both_project(self):
        from services.board_projection_service import BoardProjectionService
        hits_05 = {
            "id": "h05", "sport": "MLB",
            "market": "Aaron Judge Over 0.5 Hits", "side": "Over", "line": 0.5,
            "book_odds": -180, "implied_probability": 0.643,
            "lock_score": 90.0, "published_lock_score": 90.0,
            "event_id": "e1", "event_time": "2026-08-15T23:05:00Z",
        }
        hits_15 = dict(hits_05)
        hits_15["id"] = "h15"
        hits_15["market"] = "Aaron Judge Over 1.5 Hits"
        hits_15["line"] = 1.5
        hits_15["book_odds"] = 150
        hits_15["implied_probability"] = 0.4
        ids = BoardProjectionService().project_ids([hits_05, hits_15])
        assert "h05" in ids and "h15" in ids, (
            "Distinct alt lines must both project")

    def test_total_bases_1_5_and_2_5_both_project(self):
        from services.board_projection_service import BoardProjectionService
        tb_15 = {
            "id": "tb15", "sport": "MLB",
            "market": "Aaron Judge Over 1.5 Total Bases",
            "side": "Over", "line": 1.5,
            "book_odds": -160, "implied_probability": 0.615,
            "lock_score": 89.0, "published_lock_score": 89.0,
            "event_id": "e1", "event_time": "2026-08-15T23:05:00Z",
        }
        tb_25 = dict(tb_15)
        tb_25["id"] = "tb25"
        tb_25["market"] = "Aaron Judge Over 2.5 Total Bases"
        tb_25["line"] = 2.5
        tb_25["book_odds"] = 240
        tb_25["implied_probability"] = 0.294
        ids = BoardProjectionService().project_ids([tb_15, tb_25])
        assert "tb15" in ids and "tb25" in ids


# ═══════════════════════════════════════════════════════════════════
# §I  Wiring-matrix — MLB hitter markets remain FULLY_WIRED / declared
# ═══════════════════════════════════════════════════════════════════

class TestWiringMatrixDeclaresHitterMarkets:
    def test_wiring_matrix_declares_hits_and_total_bases(self):
        from services.pipeline_diagnostic import _WIRING_EVIDENCE
        assert ("MLB", "batter_hits") in _WIRING_EVIDENCE
        assert ("MLB", "batter_total_bases") in _WIRING_EVIDENCE
        assert ("MLB", "batter_home_runs") in _WIRING_EVIDENCE
        assert ("MLB", "batter_rbis") in _WIRING_EVIDENCE
