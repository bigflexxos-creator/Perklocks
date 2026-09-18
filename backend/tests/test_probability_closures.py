"""Probability closures — Soccer minutes/lineup, NBA threshold distribution,
CFB Student-t predictive, Tennis provenance, family-key routing."""
import math

import pytest

from services import probability_authority as pa
from services import probability_closures as pc
from services.cfb_game_model import cfb_over_probability
from services import cfb_total_residuals as cres


# ── family routing ─────────────────────────────────────────────────────
@pytest.mark.parametrize("pick,expected", [
    ({"sport": "NFL", "market": "Trevor Lawrence 150+ Passing Yards", "player_name": "Trevor Lawrence"}, "NFL_PASS_YARDS"),
    ({"sport": "NFL", "market": "James Cook 40+ Rushing Yards  · ALT LOCK", "player_name": "James Cook"}, "NFL_RUSH_YARDS"),
    ({"sport": "NFL", "market": "Sam LaPorta 3+ Receptions vs Buffalo Bills", "player_name": "Sam LaPorta"}, "NFL_RECEPTIONS"),
    ({"sport": "NFL", "market": "Trevor Lawrence Over 19.5 Player Pass Completions", "player_name": "x"}, "NFL_PASS_COMPLETIONS"),
    ({"sport": "NFL", "market": "Bijan Robinson Anytime Touchdown", "player_name": "x"}, "NFL_ATD"),
    ({"sport": "Soccer", "market": "Kylian Mbappe To Score or Assist", "player_name": "x"}, "SOCCER_GOAL_CONTRIBUTION"),
    ({"sport": "Soccer", "market": "Diego Rossi Anytime Goal Scorer", "player_name": "x"}, "SOCCER_GOALSCORER"),
    ({"sport": "Soccer", "market": "Nicolás Fernández Anytime Assist", "player_name": "x"}, "SOCCER_ASSISTS"),
    ({"sport": "NBA", "market": "Jayson Tatum Over 27.5 Points", "player_name": "x"}, "NBA_POINTS"),
])
def test_family_key_routing(pick, expected):
    assert pa.market_family_key(pick) == expected


def test_team_total_points_is_not_a_player_family():
    assert pa.market_family_key({"sport": "CFB", "market": "Total Points Over 58.5"}) == "CFB_TOTAL"


# ── soccer minutes / lineup ────────────────────────────────────────────
def _soccer(status, raw=0.35, market="Diego Rossi Anytime Goal Scorer", **kw):
    p = {"sport": "Soccer", "market": market, "player_name": "Diego Rossi",
         "win_probability": raw * 100, "book_odds": 180, "lineup_status": {"status": status}}
    p.update(kw)
    return p


def test_soccer_confirmed_starter_barely_moves_probability():
    c = pc.soccer_player_closure(_soccer("confirmed"), "SOCCER_GOALSCORER", 0.35)
    assert c.publication_eligible and 0.30 <= c.probability <= 0.35


def test_soccer_projected_reduces_and_bench_reduces_more():
    p_conf = pc.soccer_player_closure(_soccer("confirmed"), "SOCCER_GOALSCORER", 0.35).probability
    p_proj = pc.soccer_player_closure(_soccer("projected"), "SOCCER_GOALSCORER", 0.35).probability
    p_bench = pc.soccer_player_closure(_soccer("bench"), "SOCCER_GOALSCORER", 0.35).probability
    assert p_conf > p_proj > p_bench > 0.0


def test_soccer_out_fails_closed():
    c = pc.soccer_player_closure(_soccer("out"), "SOCCER_GOALSCORER", 0.35)
    assert c.publication_eligible is False and c.fallback_reason == "LINEUP_OUT"
    contract = pa.evaluate(_soccer("out"))
    assert contract.publication_eligible is False and contract.fallback_reason == "LINEUP_OUT"


def test_soccer_shots_threshold_uses_poisson_count():
    c = pc.soccer_player_closure(_soccer("confirmed", raw=0.6, market="Diego Rossi 2+ Shots"), "SOCCER_SHOTS", 0.6)
    assert c.details["k"] == 2
    lam = c.details["lambda_90"]
    assert abs(pc._pois_tail(lam, 2) - 0.6) < 1e-3


def test_soccer_upstream_minutes_conditioned_passthrough():
    c = pc.soccer_player_closure(_soccer("confirmed", lineup_minutes_applied=True), "SOCCER_GOALSCORER", 0.35)
    assert c.details.get("passthrough") and abs(c.probability - 0.35 * 0.99) < 1e-3
    v3 = pc.soccer_player_closure(_soccer("projected", pick_rationale={"engine": "goal_scorer_v3"}), "SOCCER_GOALSCORER", 0.35)
    assert v3.details.get("passthrough") and abs(v3.probability - 0.35 * 0.88) < 1e-3


def test_soccer_closure_recorded_in_contract_shadow_mode(monkeypatch):
    monkeypatch.setenv("PROBABILITY_AUTHORITY_MODE", "shadow")
    p = _soccer("projected")
    c = pa.evaluate(p)
    assert c.closure and c.closure["method"] == "soccer_minutes_lineup.v1"
    assert c.closure_probability < c.raw_model_probability
    pa.stamp(p)
    assert p["win_probability"] == 35.0  # shadow never mutates truth


# ── NBA threshold distribution ─────────────────────────────────────────
def test_nba_threshold_over_under_coherent_and_role_uncertainty_widens():
    a = pc.nba_threshold_probability(27.0, 6.0, 24.5, "over", n_games=20)
    b = pc.nba_threshold_probability(27.0, 6.0, 24.5, "under", n_games=20)
    assert abs(a["p"] + b["p"] - 1.0) < 1e-6
    wide = pc.nba_threshold_probability(27.0, 6.0, 24.5, "over", minutes_mean=34, minutes_sd=6, n_games=20)
    assert wide["p"] < a["p"]  # more variance → less confident on the favoured side
    small_n = pc.nba_threshold_probability(27.0, 6.0, 24.5, "over", n_games=5)
    assert small_n["p"] < a["p"]


def test_nba_closure_replaces_probability_when_distribution_available():
    p = {"sport": "NBA", "market": "Jayson Tatum Over 24.5 Points", "player_name": "Jayson Tatum",
         "win_probability": 90.0, "book_odds": -115, "line": 24.5,
         "projection_mean": 27.0, "projection_sd": 6.0, "sample_games": 20}
    c = pa.evaluate(p)
    assert c.closure["method"] == "nba_threshold_distribution.v1"
    assert 0.6 < c.closure_probability < 0.75


def test_nba_book_seed_fails_closed():
    p = {"sport": "NBA", "market": "Jayson Tatum Over 24.5 Points", "player_name": "x",
         "win_probability": 60.0, "book_odds": -115, "probability_source": "book_implied_seed"}
    assert pa.evaluate(p).fallback_reason == "BOOK_IMPLIED_SEED_NOT_PUBLISHABLE"


# ── CFB Student-t predictive ───────────────────────────────────────────
def test_cfb_miami_wake_fixture_no_longer_fixed_sigma_normal():
    cres._cache.update({"sigma_raw": None, "n": 0})
    miami = cfb_over_probability(70.0, 46.5, True, 16.2)
    wake = cfb_over_probability(31.2, 50.5, False, 16.2)
    assert miami < 0.9266 - 0.01 and 0.85 < miami < 0.92   # legacy normal was 0.9266
    assert wake < 0.8832 - 0.01
    assert abs(cfb_over_probability(55, 52, True, 13.5) + cfb_over_probability(55, 52, False, 13.5) - 1.0) < 1e-6


def test_cfb_predictive_converges_to_normal_as_residuals_accumulate():
    cres._cache.update({"sigma_raw": 16.2, "n": 0})
    p0 = cres.predictive_total_over_probability(70.0, 46.5, 16.2)["p_over"]
    cres._cache.update({"sigma_raw": 16.2, "n": 5000})
    p_inf = cres.predictive_total_over_probability(70.0, 46.5, 16.2)["p_over"]
    cres._cache.update({"sigma_raw": None, "n": 0})
    assert p0 < p_inf and abs(p_inf - 0.9266) < 0.003


def test_cfb_predictive_monotone_in_line():
    ps = [cres.predictive_total_over_probability(60.0, ln, 14.0)["p_over"] for ln in (50.5, 55.5, 60.5, 65.5)]
    assert ps == sorted(ps, reverse=True)


# ── Tennis provenance ─────────────────────────────────────────────────
def test_publication_boundary_rejects_lineup_out_player_prop():
    from services.canonical_publication_boundary import evaluate_publication, RejectionReason
    p = {"id": "t-out-1", "sport": "Soccer", "market": "Diego Rossi Anytime Goal Scorer",
         "player_name": "Diego Rossi", "win_probability": 35.0, "book_odds": 180,
         "lineup_status": {"status": "out"}, "model_source": "goal_scorer_v3",
         "canonical_player_id": "sp:1", "canonical_event_id": "ev:1"}
    v = evaluate_publication(p)
    assert RejectionReason.PROBABILITY_AUTHORITY_INELIGIBLE.value in (getattr(v, "reasons", None) or getattr(v, "rejection_reasons", None) or [])
    ok = dict(p, lineup_status={"status": "confirmed"}, id="t-ok-1")
    v2 = evaluate_publication(ok)
    assert RejectionReason.PROBABILITY_AUTHORITY_INELIGIBLE.value not in (getattr(v2, "reasons", None) or getattr(v2, "rejection_reasons", None) or [])

def test_tennis_closure_flags_legacy_lock_construction():
    ok = pa.evaluate({"sport": "Tennis", "market": "Medvedev Moneyline", "win_probability": 70.0,
                      "book_odds": -150, "lock_score_authority": "canonical_compute_lock_score"})
    assert ok.closure["evidence_note"] is None
    legacy = pa.evaluate({"sport": "Tennis", "market": "Medvedev Moneyline", "win_probability": 70.0, "book_odds": -150})
    assert legacy.closure["evidence_note"] == "LEGACY_LOCK_CONSTRUCTION"
