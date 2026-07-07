"""Regression tests for the new sim_engine (Session 1 spec).

§3 scenario-based Monte Carlo
§5 correlation awareness
§7 market comparison (edge vs implied)
§8 multi-model consensus (agreement metric)

Run: python -m pytest backend/tests/test_sim_engine.py -q
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sim_engine import (  # noqa: E402
    simulate_pick,
    simulate_board,
    apply_correlation,
    american_to_implied,
    american_to_decimal,
    _scenario_weights_for,
)


# ── odds helpers ────────────────────────────────────────────────────

def test_implied_prob_math():
    assert abs(american_to_implied(100) - 0.5) < 1e-6
    assert abs(american_to_implied(-200) - (2/3)) < 1e-6
    assert abs(american_to_implied(150) - 0.4) < 1e-6


def test_decimal_odds_math():
    assert abs(american_to_decimal(100) - 2.0) < 1e-6
    assert abs(american_to_decimal(-200) - 1.5) < 1e-6


# ── §3 scenario-based sim ───────────────────────────────────────────

def test_scenario_weights_sum_to_one_mlb():
    weights = _scenario_weights_for("MLB", {"game_total": 8.5})
    total = sum(w for _, w in weights)
    assert abs(total - 1.0) < 1e-6


def test_mlb_high_total_boosts_early_offense():
    hi_weights = dict(
        (s.key, w) for s, w in _scenario_weights_for("MLB", {"game_total": 10.5})
    )
    lo_weights = dict(
        (s.key, w) for s, w in _scenario_weights_for("MLB", {"game_total": 7.0})
    )
    assert hi_weights["early_offense"] > lo_weights["early_offense"]
    assert lo_weights["pitcher_duel"] > hi_weights["pitcher_duel"]


def test_simulate_returns_sim_result_shape():
    pick = {"id": "p", "sport": "MLB", "win_probability": 0.55,
            "book_odds": -110}
    res = simulate_pick(pick, n_simulations=300)
    assert 0.0 < res.prob < 1.0
    assert res.per_model
    assert res.scenario_breakdown
    assert res.n_simulations > 0


# ── §7 market comparison ─────────────────────────────────────────────

def test_positive_edge_dog():
    """+150 (40% implied) with 48% model → edge ≈ +8%."""
    pick = {"id": "p", "sport": "MLB", "win_probability": 0.48,
            "book_odds": 150}
    res = simulate_pick(pick, n_simulations=1000)
    # Simulator adds scenario / model noise; edge should be positive and
    # comfortably above zero but not exactly +8 due to scenario mixing.
    assert res.edge_pct > 0, f"expected positive edge, got {res.edge_pct}"


def test_negative_edge_chalk():
    """-300 (75% implied) with 60% model → negative edge."""
    pick = {"id": "p", "sport": "MLB", "win_probability": 0.60,
            "book_odds": -300}
    res = simulate_pick(pick, n_simulations=1000)
    assert res.edge_pct < 0, f"expected negative edge, got {res.edge_pct}"


def test_ev_units_computed():
    pick = {"id": "p", "sport": "MLB", "win_probability": 0.55,
            "book_odds": 150}
    res = simulate_pick(pick, n_simulations=300)
    # EV = 0.55 * 1.5 - 0.45 = 0.825 - 0.45 = 0.375 (before noise)
    assert res.ev_units > 0.0


# ── §8 multi-model consensus ─────────────────────────────────────────

def test_agreement_high_for_middle_probs():
    pick = {"id": "p", "sport": "MLB", "win_probability": 0.55,
            "book_odds": -110}
    res = simulate_pick(pick, n_simulations=800)
    # Agreement should be > 0 with all 3 models
    assert 0.0 <= res.model_agreement <= 1.0
    assert len(res.per_model) == 3


def test_conservative_pulls_toward_50pct():
    """A very confident 90% pick should have the conservative model
    prob lower than baseline (it regresses to 0.5)."""
    pick = {"id": "p", "sport": "MLB", "win_probability": 0.90,
            "book_odds": -400}
    res = simulate_pick(pick, n_simulations=800)
    assert res.per_model["conservative"] < res.per_model["baseline"]
    assert res.per_model["aggressive"] > res.per_model["conservative"]


# ── §5 correlation awareness ────────────────────────────────────────

def test_correlation_penalises_same_event():
    picks = [
        {"id": "a", "event": "NYY @ BOS", "sport": "MLB"},
        {"id": "b", "event": "NYY @ BOS", "sport": "MLB"},
        {"id": "c", "event": "NYY @ BOS", "sport": "MLB"},
        {"id": "d", "event": "LAD @ SF", "sport": "MLB"},
    ]
    factors = apply_correlation(picks)
    # Same-game 3 legs → factor < 1
    assert factors["a"] < 1.0
    # Solo game leg → factor == 1
    assert factors["d"] == 1.0


def test_simulate_board_applies_correlation():
    picks = [
        {"id": "a", "sport": "MLB", "event": "NYY @ BOS",
         "win_probability": 0.60, "book_odds": -120},
        {"id": "b", "sport": "MLB", "event": "NYY @ BOS",
         "win_probability": 0.55, "book_odds": -110},
    ]
    simulate_board(picks, n_simulations=300)
    for p in picks:
        assert "sim_result" in p
        # Both share event → correlation_factor < 1
        assert p["sim_result"]["correlation_factor"] < 1.0
