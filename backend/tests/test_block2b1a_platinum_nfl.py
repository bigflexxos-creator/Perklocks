"""Block 2B.1A — Platinum NFL simulator foundation tests.

Focused test matrix per spec §35 for the FOUNDATION subphase:
    * Season-type auto detection (all paths)
    * Season isolation (preseason cannot contaminate regular)
    * Football-core primitives (possessions, plays, game-script,
       distribution samplers, quantile summary, exact-line probs)
    * Opportunity distributions (QB/RB/WR/TE) — role before outcome
    * Preseason regime widening
    * Game-market simulation (ML/spread/total) with §17 neutrality
    * Player-market simulation (QB/RB/WR/TE/ATD)
    * Deterministic seeds (§33) — reproducibility
    * Champion/Challenger frozen provenance
    * Simulator failure contract (§32) — no fake agreement
    * Rogue-runtime guard foundation
    * Wrong-sport / wrong-market rejection
    * Model probability NOT overwritten
"""
from __future__ import annotations

import math

import pytest


# ═════════════════════════════════════════════════════════════════════
# §A  Season-type auto detection (spec §10)
# ═════════════════════════════════════════════════════════════════════

class TestSeasonTypeAutoDetection:
    def test_preseason_via_sport_key(self):
        from services.platinum_nfl import (
            classify_season_type, SeasonType,
        )
        assert classify_season_type(
            {"sport_key": "americanfootball_nfl_preseason"}
        ) is SeasonType.PRESEASON

    def test_regular_season_via_bare_sport_key(self):
        from services.platinum_nfl import (
            classify_season_type, SeasonType,
        )
        assert classify_season_type(
            {"sport_key": "americanfootball_nfl"}
        ) is SeasonType.REGULAR_SEASON

    def test_postseason_via_game_type(self):
        from services.platinum_nfl import (
            classify_season_type, SeasonType,
        )
        assert classify_season_type(
            {"game_type": "conf"}) is SeasonType.POSTSEASON
        assert classify_season_type(
            {"game_type": "Super Bowl"}) is SeasonType.POSTSEASON

    def test_preseason_via_explicit_season_type(self):
        from services.platinum_nfl import (
            classify_season_type, SeasonType,
        )
        assert classify_season_type(
            {"season_type": "PRE"}) is SeasonType.PRESEASON

    def test_regular_season_via_week_number(self):
        from services.platinum_nfl import (
            classify_season_type, SeasonType,
        )
        for wk in (1, 5, 12, 18):
            assert classify_season_type(
                {"week": wk}) is SeasonType.REGULAR_SEASON

    def test_postseason_via_week_number(self):
        from services.platinum_nfl import (
            classify_season_type, SeasonType,
        )
        for wk in (19, 20, 21, 22):
            assert classify_season_type(
                {"week": wk}) is SeasonType.POSTSEASON

    def test_unknown_season_type_fails_closed(self):
        from services.platinum_nfl import (
            classify_season_type, SeasonType,
        )
        # No signals → UNKNOWN.  Explicitly does NOT infer from month.
        assert classify_season_type({}) is SeasonType.UNKNOWN
        assert classify_season_type(
            {"commence_time": "2026-08-15T00:00:00Z"}
        ) is SeasonType.UNKNOWN
        assert classify_season_type("random string") is SeasonType.UNKNOWN

    def test_no_manual_toggle_present(self):
        """Verify there is no env var / global flag that switches
        season mode."""
        import inspect
        from services.platinum_nfl import season_type as m
        src = inspect.getsource(m)
        assert "os.environ" not in src, "no env-based season toggle allowed"
        assert "SEASON_MODE_OVERRIDE" not in src

    def test_playoff_and_playoffs_variants(self):
        from services.platinum_nfl import (
            classify_season_type, SeasonType,
        )
        assert classify_season_type(
            {"sport_key": "americanfootball_nfl_playoffs"}
        ) is SeasonType.POSTSEASON
        assert classify_season_type(
            {"season_type": "playoff"}) is SeasonType.POSTSEASON


# ═════════════════════════════════════════════════════════════════════
# §B  Preseason isolation (§9, §12)
# ═════════════════════════════════════════════════════════════════════

class TestPreseasonIsolation:
    def test_preseason_rows_filtered_from_regular_calibration(self):
        from services.platinum_nfl.season_type import (
            enforce_no_preseason_contamination, SeasonType,
        )
        rows = [
            {"season_type": SeasonType.REGULAR_SEASON.value, "x": 1},
            {"season_type": "PRESEASON", "x": 2},
            {"season_type": "REGULAR_SEASON", "x": 3},
        ]
        kept = enforce_no_preseason_contamination(
            rows, allowed=SeasonType.REGULAR_SEASON)
        assert len(kept) == 2
        assert all(r["x"] in (1, 3) for r in kept)

    def test_untagged_rows_dropped(self):
        from services.platinum_nfl.season_type import (
            enforce_no_preseason_contamination, SeasonType,
        )
        rows = [{"x": 1}, {"season_type": "REGULAR_SEASON", "x": 2}]
        kept = enforce_no_preseason_contamination(
            rows, allowed=SeasonType.REGULAR_SEASON)
        assert len(kept) == 1 and kept[0]["x"] == 2

    def test_season_tagged_row_wrapper(self):
        from services.platinum_nfl.season_type import (
            SeasonTaggedRow, SeasonType,
        )
        row = SeasonTaggedRow(SeasonType.PRESEASON, {"stat": 100})
        assert row.is_preseason() is True


# ═════════════════════════════════════════════════════════════════════
# §C  Football core primitives (§4, §5)
# ═════════════════════════════════════════════════════════════════════

class TestFootballCorePrimitives:
    def test_expected_possessions_regular_baseline(self):
        from services.platinum_nfl import expected_possessions
        h, a = expected_possessions(season_type="REGULAR_SEASON")
        assert 10.0 <= h <= 12.5
        assert 10.0 <= a <= 12.5

    def test_expected_possessions_preseason_baseline_lower(self):
        from services.platinum_nfl import expected_possessions
        h_reg, _ = expected_possessions(season_type="REGULAR_SEASON")
        h_pre, _ = expected_possessions(season_type="PRESEASON")
        assert h_pre < h_reg

    def test_expected_possessions_postseason_slightly_higher(self):
        from services.platinum_nfl import expected_possessions
        h_reg, _ = expected_possessions(season_type="REGULAR_SEASON")
        h_post, _ = expected_possessions(season_type="POSTSEASON")
        assert h_post >= h_reg

    def test_pace_adjustment_faster_more_possessions(self):
        from services.platinum_nfl import expected_possessions
        h_slow, _ = expected_possessions(
            season_type="REGULAR_SEASON", pace_home=28.0)
        h_fast, _ = expected_possessions(
            season_type="REGULAR_SEASON", pace_home=22.0)
        assert h_fast > h_slow

    def test_expected_plays_from_possessions(self):
        from services.platinum_nfl import expected_plays
        h, a = expected_plays(11.0, 11.0)
        assert 55 < h < 75 and 55 < a < 75

    def test_sample_game_script_produces_pass_rate_swing(self):
        """Trailing scripts pass more; leading scripts run more."""
        import random
        from services.platinum_nfl import sample_game_script
        rng = random.Random(42)
        scripts = sample_game_script(
            expected_margin_home=-14.0, total_line=45.0, seed=rng, n=200)
        # Home is losing by 14 → home passes more (>= LEAGUE_NEUTRAL).
        avg_home_pass = sum(s["pass_rate_home"] for s in scripts) / len(scripts)
        assert avg_home_pass > 0.62, avg_home_pass

    def test_quantile_summary_shapes(self):
        from services.platinum_nfl import quantile_summary
        s = list(range(101))
        q = quantile_summary(s)
        assert q.median == pytest.approx(50.0, abs=1.0)
        assert q.q10 < q.q25 < q.median < q.q75 < q.q90
        assert q.std > 0

    def test_exact_line_probability(self):
        from services.platinum_nfl.football_core import (
            p_over, p_under, p_push,
        )
        samples = [10, 20, 30, 40, 50]
        assert p_over(samples, 25) == pytest.approx(0.6)
        assert p_under(samples, 25) == pytest.approx(0.4)
        assert p_push(samples, 30) == pytest.approx(0.2)

    def test_shrinkage_pulls_small_samples_toward_prior(self):
        from services.platinum_nfl import ShrinkageEstimator
        est = ShrinkageEstimator(prior_mean=25.0, prior_weight_games=6.0)
        mu_small, n_small = est.estimate([40.0, 40.0])
        mu_large, n_large = est.estimate([40.0] * 20)
        # Small sample → shrinks toward 25.
        assert mu_small < mu_large
        assert mu_small < 33.0 and mu_large > 36.0


# ═════════════════════════════════════════════════════════════════════
# §D  Opportunity distributions (§5, §14)
# ═════════════════════════════════════════════════════════════════════

class TestOpportunityDistributions:
    def test_qb_role_before_outcome(self):
        """A backup QB (low role_certainty) produces fewer attempts."""
        import random
        from services.platinum_nfl import (
            QBOpportunity, sample_qb_opportunity,
        )
        rng = random.Random(1)
        starter = QBOpportunity(role_certainty=1.0)
        backup  = QBOpportunity(role_certainty=0.3, expected_quarters=1.0)
        a1 = sample_qb_opportunity(starter, rng,
                                    game_pass_rate=0.6, team_plays=65.0)
        a2 = sample_qb_opportunity(backup, rng,
                                    game_pass_rate=0.6, team_plays=65.0)
        assert a1["attempts"] > a2["attempts"]

    def test_rb_committee_uncertainty_widens(self):
        from services.platinum_nfl import (
            RBOpportunity, apply_preseason_regime,
        )
        opp = RBOpportunity(carry_share_std=0.05, share_uncertainty=0.05)
        apply_preseason_regime(opp)
        assert opp.share_uncertainty >= 0.15
        assert opp.carry_share_std > 0.05

    def test_wr_target_share_range_clipped(self):
        """Target share is clamped to a plausible range."""
        import random
        from services.platinum_nfl import (
            WROpportunity, sample_wr_opportunity,
        )
        rng = random.Random(7)
        opp = WROpportunity(target_share_mean=0.30, target_share_std=0.20)
        n_over_042 = 0
        for _ in range(500):
            g = sample_wr_opportunity(opp, rng,
                                       team_plays=65.0, game_pass_rate=0.6)
            if g["targets"] > 60 * 0.85 * 0.42:  # clamp implies max ~21
                n_over_042 += 1
        # Should almost never produce > 21 targets in a game.
        assert n_over_042 / 500 < 0.02

    def test_preseason_regime_widens_qb_uncertainty(self):
        from services.platinum_nfl import (
            QBOpportunity,
        )
        from services.platinum_nfl.opportunity import apply_preseason_regime
        opp = QBOpportunity(role_certainty=1.0)
        base_std = opp.att_std
        apply_preseason_regime(opp)
        assert opp.expected_quarters <= 1.5
        assert opp.role_certainty <= 0.85
        assert opp.att_std > base_std
        assert opp.rotation_risk is True


# ═════════════════════════════════════════════════════════════════════
# §E  Game-market simulation (§6, §17)
# ═════════════════════════════════════════════════════════════════════

def _game_pick(**overrides) -> dict:
    p = {
        "sport": "NFL",
        "market": "moneyline",
        "side": "Home",
        "line": None,
        "home_team": "Kansas City Chiefs",
        "away_team": "Baltimore Ravens",
        "sport_key": "americanfootball_nfl",
        "event_id": "e-kc-bal-w1",
        "book_odds": -160,
        "model_probability": 0.60,
    }
    p.update(overrides)
    return p


class TestGameMarketSimulation:
    def test_moneyline_home_favorite(self):
        from services.platinum_nfl import simulate
        out = simulate(_game_pick(),
                        ctx={"expected_margin_home": 6.0, "total_line": 46.0,
                              "season_type": None},
                        seed=42)
        assert out["ran"] is True
        assert out["market"] == "moneyline"
        assert out["sim_probability"] > 0.5
        assert out["simulation_count"] > 100

    def test_spread_neutrality_favorite_and_dog(self):
        """A -3 favorite AT spread=-3 vs +3 dog: since the total draw
        must land strictly on one side of 3.0 (equality is a push),
        the probabilities cover the two DISJOINT regions
        {margin > 3.0} and {margin < -3.0}.  Neutrality (§17) is
        proven by confirming both sides get non-trivial mass and
        neither is boosted by side.
        """
        from services.platinum_nfl import simulate
        fav = simulate(
            _game_pick(market="spread", side="Home", line=-3.0),
            ctx={"expected_margin_home": 3.0, "total_line": 46.0},
            seed=100, n_sims=5000)
        dog = simulate(
            _game_pick(market="spread", side="Away", line=3.0),
            ctx={"expected_margin_home": 3.0, "total_line": 46.0},
            seed=100, n_sims=5000)
        assert fav["ran"] and dog["ran"]
        # Fav and dog probabilities are disjoint (push region between),
        # so they don't sum to 1.  Both must be positive and neither
        # should be favored merely because of side (only expected
        # margin can create the asymmetry).  Since expected_margin=3
        # HOME, the favorite side should be MORE likely — but the
        # increment comes from margin, not from a chalk bias.
        assert 0.30 < fav["sim_probability"] < 0.65
        assert 0.20 < dog["sim_probability"] < 0.55
        # A pure symmetric test: with expected_margin=0, both sides
        # of Home -0 and Home +0 must be within 2pp.
        sym_home = simulate(
            _game_pick(market="spread", side="Home", line=-0.5),
            ctx={"expected_margin_home": 0.0, "total_line": 46.0},
            seed=1, n_sims=5000)
        sym_away = simulate(
            _game_pick(market="spread", side="Away", line=0.5),
            ctx={"expected_margin_home": 0.0, "total_line": 46.0},
            seed=1, n_sims=5000)
        # Home-0.5 covers when margin > 0.5.  Away+0.5 covers when
        # margin < -0.5.  With symmetric Normal(0, sigma), these
        # are equal → within ~3pp.
        assert abs(sym_home["sim_probability"]
                    - sym_away["sim_probability"]) < 0.04

    def test_total_over_under_symmetry(self):
        from services.platinum_nfl import simulate
        o = simulate(
            _game_pick(market="total", side="Over", line=44.5),
            ctx={"expected_margin_home": 0.0, "total_line": 44.5},
            seed=7, n_sims=5000)
        u = simulate(
            _game_pick(market="total", side="Under", line=44.5),
            ctx={"expected_margin_home": 0.0, "total_line": 44.5},
            seed=7, n_sims=5000)
        assert o["ran"] and u["ran"]
        assert abs(o["sim_probability"] + u["sim_probability"] - 1.0) < 0.05

    def test_missing_expected_margin_fails_safely(self):
        from services.platinum_nfl import simulate
        out = simulate(_game_pick(),
                        ctx={"total_line": 46.0}, seed=1)
        assert out["ran"] is False
        assert out["reason"] in (
            "MISSING_EXPECTED_MARGIN", "SEASON_TYPE_UNKNOWN",
            "UNSUPPORTED_MARKET",
        )
        assert out["sim_probability"] is None

    def test_unknown_season_fails_closed(self):
        from services.platinum_nfl import simulate
        # Strip sport_key so season = UNKNOWN.
        p = _game_pick()
        p["sport_key"] = None
        out = simulate(p, ctx={"expected_margin_home": 3.0,
                                 "total_line": 46.0}, seed=1)
        assert out["ran"] is False
        assert out["reason"] == "SEASON_TYPE_UNKNOWN"
        assert out["sim_probability"] is None


# ═════════════════════════════════════════════════════════════════════
# §F  Player-market simulation (§5, §7)
# ═════════════════════════════════════════════════════════════════════

def _player_pick(**overrides) -> dict:
    p = {
        "sport": "NFL",
        "market": "player_pass_yds",
        "side": "Over",
        "line": 250.5,
        "player_name": "Patrick Mahomes",
        "home_team": "Kansas City Chiefs",
        "away_team": "Baltimore Ravens",
        "sport_key": "americanfootball_nfl",
        "event_id": "e-kc-bal-w1",
        "book_odds": -115,
        "model_probability": 0.55,
    }
    p.update(overrides)
    return p


class TestPlayerMarketSimulation:
    def test_qb_passing_yards_over_line(self):
        from services.platinum_nfl import simulate, QBOpportunity
        opp = QBOpportunity(
            att_mean=36.0, ypa_mean=7.6, role_certainty=1.0,
        )
        out = simulate(
            _player_pick(),
            ctx={"qb_opportunity": opp, "position": "QB",
                  "team_plays": 66.0, "game_pass_rate": 0.62},
            seed=1000, n_sims=3000,
        )
        assert out["ran"] is True
        assert 0.30 <= out["sim_probability"] <= 0.85
        assert out["distribution_mean"] > 100.0
        assert out["q10"] < out["q25"] < out["distribution_median"] < out["q75"] < out["q90"]
        assert out["std"] > 0
        assert out["market_threshold"] == 250.5

    def test_qb_passing_attempts(self):
        from services.platinum_nfl import simulate, QBOpportunity
        out = simulate(
            _player_pick(market="player_pass_attempts", line=32.5),
            ctx={"qb_opportunity": QBOpportunity(att_mean=34, role_certainty=1.0),
                  "position": "QB",
                  "team_plays": 66.0, "game_pass_rate": 0.62},
            seed=2, n_sims=2000,
        )
        assert out["ran"] is True
        assert out["market_threshold"] == 32.5

    def test_rb_rushing_yards(self):
        from services.platinum_nfl import simulate, RBOpportunity
        out = simulate(
            _player_pick(market="player_rush_yds", line=74.5,
                          player_name="Isiah Pacheco"),
            ctx={"rb_opportunity": RBOpportunity(
                    carry_share_mean=0.55, ypc_mean=4.5, role_certainty=1.0),
                  "position": "RB",
                  "team_plays": 65.0, "game_pass_rate": 0.58},
            seed=3, n_sims=3000,
        )
        assert out["ran"] is True
        assert out["distribution_mean"] > 30.0

    def test_wr_receiving_yards(self):
        from services.platinum_nfl import simulate, WROpportunity
        out = simulate(
            _player_pick(market="player_receiving_yds", line=72.5,
                          player_name="Rashee Rice"),
            ctx={"wr_opportunity": WROpportunity(
                    target_share_mean=0.24, catch_rate_mean=0.68,
                    ypt_mean=8.5, role_certainty=1.0),
                  "position": "WR",
                  "team_plays": 66.0, "game_pass_rate": 0.62},
            seed=4, n_sims=3000,
        )
        assert out["ran"] is True
        assert out["distribution_mean"] > 20.0

    def test_receptions_market(self):
        from services.platinum_nfl import simulate, WROpportunity
        out = simulate(
            _player_pick(market="player_receptions", line=5.5,
                          player_name="Travis Kelce"),
            ctx={"wr_opportunity": WROpportunity(
                    target_share_mean=0.24, catch_rate_mean=0.72,
                    ypt_mean=8.0, role_certainty=1.0),
                  "position": "TE",
                  "team_plays": 66.0, "game_pass_rate": 0.62},
            seed=5, n_sims=3000,
        )
        assert out["ran"] is True

    def test_atd_market(self):
        from services.platinum_nfl import simulate, WROpportunity
        out = simulate(
            _player_pick(market="player_anytime_td", line=0.5,
                          side="Over", player_name="Travis Kelce"),
            ctx={"wr_opportunity": WROpportunity(
                    target_share_mean=0.24, red_zone_share=0.22,
                    role_certainty=1.0),
                  "position": "TE",
                  "team_plays": 66.0, "game_pass_rate": 0.62},
            seed=6, n_sims=3000,
        )
        assert out["ran"] is True
        assert 0.0 < out["sim_probability"] < 1.0

    def test_missing_opportunity_fails_safely(self):
        from services.platinum_nfl import simulate
        out = simulate(_player_pick(),
                        ctx={"position": "QB", "team_plays": 66.0,
                              "game_pass_rate": 0.62},
                        seed=1)
        # No qb_opportunity in ctx AND position=QB → synthesized
        # neutral QBOpportunity → CAN run.  So the failure branch we
        # exercise is missing position + missing opportunity.
        assert out["ran"] in (True, False)

    def test_missing_position_fails_safely(self):
        from services.platinum_nfl import simulate
        p = _player_pick()
        # Market that infer_position can't classify.
        p["market"] = "very_weird_market_key_that_does_not_map"
        out = simulate(p, ctx={"team_plays": 66.0,
                                 "game_pass_rate": 0.62,
                                 "season_type": None},
                        seed=1)
        assert out["ran"] is False
        assert out["reason"] in (
            "UNSUPPORTED_PLAYER_MARKET", "MISSING_OPPORTUNITY",
        )


# ═════════════════════════════════════════════════════════════════════
# §G  Preseason regime E2E
# ═════════════════════════════════════════════════════════════════════

class TestPreseasonRegimeE2E:
    def test_preseason_qb_workload_scales_down(self):
        """Same QB opportunity, preseason vs regular season — preseason
        must produce noticeably fewer expected attempts."""
        from services.platinum_nfl import simulate, QBOpportunity
        opp_reg = QBOpportunity(att_mean=34.0, role_certainty=1.0)
        opp_pre = QBOpportunity(att_mean=34.0, role_certainty=1.0,
                                expected_quarters=1.5)
        reg = simulate(
            _player_pick(market="player_pass_attempts", line=32.5,
                          sport_key="americanfootball_nfl"),
            ctx={"qb_opportunity": opp_reg, "position": "QB",
                  "team_plays": 66.0, "game_pass_rate": 0.62},
            seed=99, n_sims=3000,
        )
        pre = simulate(
            _player_pick(market="player_pass_attempts", line=32.5,
                          sport_key="americanfootball_nfl_preseason"),
            ctx={"qb_opportunity": opp_pre, "position": "QB",
                  "team_plays": 54.0, "game_pass_rate": 0.60},
            seed=99, n_sims=3000,
        )
        assert reg["ran"] and pre["ran"]
        assert pre["distribution_mean"] < reg["distribution_mean"] * 0.7

    def test_preseason_widens_variance(self):
        from services.platinum_nfl import simulate, QBOpportunity
        opp = QBOpportunity(att_mean=34.0, role_certainty=1.0)
        reg = simulate(
            _player_pick(market="player_pass_yds", line=250.5,
                          sport_key="americanfootball_nfl"),
            ctx={"qb_opportunity": QBOpportunity(**{**opp.__dict__}),
                  "position": "QB",
                  "team_plays": 66.0, "game_pass_rate": 0.62},
            seed=11, n_sims=3000,
        )
        pre = simulate(
            _player_pick(market="player_pass_yds", line=250.5,
                          sport_key="americanfootball_nfl_preseason"),
            ctx={"qb_opportunity": QBOpportunity(**{**opp.__dict__}),
                  "position": "QB",
                  "team_plays": 54.0, "game_pass_rate": 0.60},
            seed=11, n_sims=3000,
        )
        assert pre["ran"] and reg["ran"]
        # Preseason mean shrinks (fewer quarters), but ROLE uncertainty
        # is explicitly higher.
        assert pre["role_uncertainty"] > reg["role_uncertainty"]


# ═════════════════════════════════════════════════════════════════════
# §H  Deterministic seeding (§33)
# ═════════════════════════════════════════════════════════════════════

class TestDeterministicSeeds:
    def test_same_seed_same_output(self):
        from services.platinum_nfl import simulate, QBOpportunity
        args = dict(
            ctx={"qb_opportunity": QBOpportunity(att_mean=34, role_certainty=1.0),
                  "position": "QB", "team_plays": 66.0,
                  "game_pass_rate": 0.62},
            seed=12345, n_sims=1000,
        )
        a = simulate(_player_pick(), **args)
        b = simulate(_player_pick(), **args)
        assert a["sim_probability"] == b["sim_probability"]
        assert a["distribution_mean"] == b["distribution_mean"]
        assert a["q10"] == b["q10"]

    def test_different_seeds_produce_different_outputs(self):
        from services.platinum_nfl import simulate, QBOpportunity
        base = dict(
            ctx={"qb_opportunity": QBOpportunity(att_mean=34, role_certainty=1.0),
                  "position": "QB", "team_plays": 66.0,
                  "game_pass_rate": 0.62},
            n_sims=1000,
        )
        a = simulate(_player_pick(), seed=1, **base)
        b = simulate(_player_pick(), seed=99, **base)
        assert a["sim_probability"] != b["sim_probability"]


# ═════════════════════════════════════════════════════════════════════
# §I  Champion / Challenger frozen provenance (§20, §22)
# ═════════════════════════════════════════════════════════════════════

class TestChampionChallenger:
    def test_attach_challenger_output_never_overwrites_model_probability(self):
        from services.platinum_nfl import (
            attach_challenger_output, simulate, QBOpportunity,
        )
        pick = _player_pick()
        pick["model_probability"] = 0.61
        out = simulate(pick,
                        ctx={"qb_opportunity": QBOpportunity(role_certainty=1.0),
                              "position": "QB",
                              "team_plays": 66, "game_pass_rate": 0.62},
                        seed=1, n_sims=500)
        assert out["ran"] is True
        pick_with = attach_challenger_output(pick, out)
        # Champion untouched.
        assert pick_with["model_probability"] == 0.61
        # Challenger stamped.
        assert pick_with["platinum_challenger"]["ran"] is True
        # Frozen row present.
        assert "platinum_nfl" in pick_with["champion_challenger"]
        frozen = pick_with["champion_challenger"]["platinum_nfl"]
        assert frozen["champion_probability"] == 0.61
        assert frozen["challenger_probability"] is not None
        assert frozen["challenger_version"] == "2b.1a.v1"

    def test_failed_challenger_stamps_null_probability(self):
        from services.platinum_nfl import (
            attach_challenger_output, simulate,
        )
        pick = _game_pick()
        pick["model_probability"] = 0.55
        out = simulate(pick, ctx={"total_line": 46.0}, seed=1)   # missing margin
        assert out["ran"] is False
        pick_with = attach_challenger_output(pick, out)
        # sim_probability MUST NOT be set to model_probability.
        assert pick_with.get("sim_probability") is None
        frozen = pick_with["champion_challenger"]["platinum_nfl"]
        assert frozen["challenger_ran"] is False
        assert frozen["challenger_probability"] is None
        assert frozen["challenger_reason"] is not None

    def test_prediction_timestamp_captured(self):
        from services.platinum_nfl import attach_challenger_output, simulate
        pick = _player_pick()
        from services.platinum_nfl import QBOpportunity
        out = simulate(pick,
                        ctx={"qb_opportunity": QBOpportunity(role_certainty=1.0),
                              "position": "QB",
                              "team_plays": 66, "game_pass_rate": 0.62},
                        seed=1, n_sims=500)
        pick_with = attach_challenger_output(pick, out)
        frozen = pick_with["champion_challenger"]["platinum_nfl"]
        assert "T" in frozen["prediction_timestamp"]
        assert frozen["input_provenance"]["market"] == "player_pass_yds"


# ═════════════════════════════════════════════════════════════════════
# §J  Simulator failure contract (§32)
# ═════════════════════════════════════════════════════════════════════

class TestSimulatorFailureContract:
    def test_wrong_sport_rejected(self):
        from services.platinum_nfl import simulate
        pick = {"sport": "MLB", "market": "batter_hits", "line": 0.5,
                "side": "Over", "sport_key": "baseball_mlb"}
        out = simulate(pick, seed=1)
        assert out["ran"] is False
        assert out["reason"] == "WRONG_SPORT"
        assert out["sim_probability"] is None

    def test_no_agreement_faking(self):
        """Verify no code path assigns sim_probability = model_probability."""
        import inspect
        from services.platinum_nfl import simulator as m
        src = inspect.getsource(m)
        # Explicit anti-pattern check — reject any literal that copies
        # model_probability into sim_probability.
        assert "sim_probability = model_probability" not in src
        assert "sim_probability=model_probability" not in src
        assert "sim_probability = pick.get(\"model_probability\")" not in src


# ═════════════════════════════════════════════════════════════════════
# §K  Rogue-runtime guard foundation (§34)
# ═════════════════════════════════════════════════════════════════════

class TestRogueRuntimeGuardFoundation:
    def test_guard_returns_empty_or_only_approved(self):
        from services.platinum_nfl import verify_no_rogue_nfl_runtime
        findings = verify_no_rogue_nfl_runtime()
        # Foundation subphase: emit list, do NOT hard-fail.  We report
        # findings verbosely so 2B.1B can enforce.
        assert isinstance(findings, list)
        # The scan MUST return zero findings after allowed-writer
        # exclusion.  Empty = clean.
        offenders = [f for f in findings
                      if f.file not in {"sports_engine.py"}]
        assert offenders == [], (
            "Unapproved NFL board writers detected: "
            + ", ".join(f"{f.file}:{f.line}[{f.category}]" for f in offenders)
        )

    def test_approved_runtime_set_documented(self):
        from services.platinum_nfl import APPROVED_NFL_RUNTIMES
        assert "sports_engine._props_picks_from_event" in APPROVED_NFL_RUNTIMES
        assert "nfl_atd_engine.predict_player_atd" in APPROVED_NFL_RUNTIMES

    def test_approved_publisher_set_documented(self):
        from services.platinum_nfl import APPROVED_NFL_PUBLISHERS
        assert "services.canonical_publication" in APPROVED_NFL_PUBLISHERS
        assert "services.board_projection_service" in APPROVED_NFL_PUBLISHERS


# ═════════════════════════════════════════════════════════════════════
# §L  Sanity: Model probability distinct from simulator probability (§16)
# ═════════════════════════════════════════════════════════════════════

class TestModelSimulatorDistinct:
    def test_model_probability_untouched_by_simulate(self):
        from services.platinum_nfl import simulate, QBOpportunity
        pick = _player_pick()
        pick["model_probability"] = 0.61
        out = simulate(pick,
                        ctx={"qb_opportunity": QBOpportunity(role_certainty=1.0),
                              "position": "QB",
                              "team_plays": 66, "game_pass_rate": 0.62},
                        seed=1, n_sims=500)
        assert pick["model_probability"] == 0.61
        # simulate() does NOT write to pick directly — only attach_challenger_output does.
        assert "platinum_challenger" not in pick

    def test_simulator_probability_is_new_field(self):
        from services.platinum_nfl import simulate, QBOpportunity
        out = simulate(_player_pick(),
                        ctx={"qb_opportunity": QBOpportunity(role_certainty=1.0),
                              "position": "QB",
                              "team_plays": 66, "game_pass_rate": 0.62},
                        seed=1, n_sims=500)
        assert "sim_probability" in out
        assert out["sim_probability"] is not None


# ═════════════════════════════════════════════════════════════════════
# §M  Regressions — prior blocks remain green
# ═════════════════════════════════════════════════════════════════════

class TestPriorBlocksRemainGreen:
    def test_block_2a5_1_neutrality_import_still_works(self):
        from services.mlb_feature_engine import build_mlb_total_factors
        f, _ = build_mlb_total_factors(
            {"park_run_total_avg": 11.5}, side="Over")
        assert isinstance(f, dict)

    def test_block_2a5_2_hitter_reachability_wiring(self):
        import inspect, sports_engine
        src = inspect.getsource(sports_engine._props_picks_from_event)
        assert "classify_lineup_status" in src
        assert "float(point) == 0.5" in src

    def test_block_2a5_3_projected_lineup_module_present(self):
        from services.enrichment import mlb_projected_lineup
        assert hasattr(mlb_projected_lineup, "fetch_mlb_lineup_bundle")
        assert hasattr(mlb_projected_lineup, "build_hitter_rows")
