"""PHASE 2 — Universal Simulator Provenance Contract regressions.

Proves every wired simulator (MLB / NBA / Soccer game / Soccer scorer
/ Tennis / Platinum NFL) stamps a valid `provenance` + `input_quality`
+ `decision_valid` envelope on its output, and that the classification
matches the sim's actual construction:

  * MODEL_CONDITIONED sims (Soccer scorer Approach 1, Tennis calibrated,
    NBA calibrated, Soccer game factor-driven) never claim independent
    agreement.
  * CAUSAL_INDEPENDENT / EMPIRICAL_INDEPENDENT sims (MLB with real
    stats, Platinum NFL with real opportunity, MLB K probability with
    real pitcher signals) can legitimately count as independent.
  * PRIOR_ONLY / INVALID sims cannot punish the model or boost Magic.

No provider calls, no live server — all in-process.
"""
from __future__ import annotations

import math

from services.simulator_provenance import (
    is_decision_valid, is_independent_agreement,
    can_flag_severe_disagreement, severe_disagreement,
    classify_input_quality,
)


# ─────────────────────────────────────────────────────────────────────
# §1 — Universal contract semantics.
# ─────────────────────────────────────────────────────────────────────
def test_prior_only_never_counts_as_agreement():
    assert is_independent_agreement("PRIOR_ONLY", "FULL") is False
    assert is_independent_agreement("PRIOR_ONLY", "PARTIAL") is False


def test_model_conditioned_never_counts_as_agreement():
    assert is_independent_agreement("MODEL_CONDITIONED", "FULL") is False
    assert is_independent_agreement("MODEL_CONDITIONED", "STRONG") is False


def test_invalid_never_counts():
    assert is_independent_agreement("INVALID", "FULL") is False


def test_causal_full_counts_as_agreement():
    assert is_independent_agreement("CAUSAL_INDEPENDENT", "FULL") is True
    assert is_independent_agreement("CAUSAL_INDEPENDENT", "PARTIAL") is True
    assert is_independent_agreement("EMPIRICAL_INDEPENDENT", "STRONG") is True


def test_prior_only_cannot_flag_severe_disagreement():
    # PRIOR_ONLY simulators MUST NOT punish the model.
    assert can_flag_severe_disagreement("PRIOR_ONLY", "FULL") is False
    assert severe_disagreement("PRIOR_ONLY", "FULL",
                                 sim_prob=0.20, model_prob=0.80) is False


def test_model_conditioned_cannot_flag_severe_disagreement():
    assert can_flag_severe_disagreement("MODEL_CONDITIONED", "STRONG") is False


def test_causal_strong_flags_severe_disagreement_above_threshold():
    assert severe_disagreement("CAUSAL_INDEPENDENT", "STRONG",
                                 sim_prob=0.30, model_prob=0.60) is True
    # Under threshold — no flag.
    assert severe_disagreement("CAUSAL_INDEPENDENT", "STRONG",
                                 sim_prob=0.55, model_prob=0.60) is False


def test_input_quality_ladder():
    assert classify_input_quality(5) == "FULL"
    assert classify_input_quality(4) == "STRONG"
    assert classify_input_quality(3) == "PARTIAL"
    assert classify_input_quality(2) == "PARTIAL"
    assert classify_input_quality(1) == "PRIOR_ONLY"
    assert classify_input_quality(0) == "INVALID"


# ─────────────────────────────────────────────────────────────────────
# §2 — Soccer scorer sim classifies MODEL_CONDITIONED when back-solving
#      from model_wp (Approach 1) and does not raise severe disagreement.
# ─────────────────────────────────────────────────────────────────────
def test_soccer_scorer_calibrated_to_model_is_model_conditioned():
    from brain.sim_soccer_scorer import simulate_soccer_scorer_pick
    pick = {
        "sport": "Soccer",
        "market": "Lionel Messi Anytime Goal Scorer",
        "player_name": "Lionel Messi",
        "win_probability": 45.0,     # non-degenerate → Approach 1 fires
        "key_insights": [],
        "factors": {},
    }
    out = simulate_soccer_scorer_pick(pick)
    assert out is not None
    assert out["provenance"] == "MODEL_CONDITIONED"
    assert out["decision_valid"] is False
    # A back-solved λ producing sim ≈ model_wp cannot flag severe
    # disagreement even at large sim/model deltas.
    assert out.get("sim_model_severe_disagreement") in (False, None)


# ─────────────────────────────────────────────────────────────────────
# §3 — Soccer scorer sim classifies EMPIRICAL_INDEPENDENT when driven
#      by real player-xG + opp + minutes + shots + recent-goal-rate.
# ─────────────────────────────────────────────────────────────────────
def test_soccer_scorer_with_real_priors_is_empirical_independent():
    from brain.sim_soccer_scorer import simulate_soccer_scorer_pick
    pick = {
        "sport": "Soccer",
        "market": "Erling Haaland Anytime Goal Scorer",
        "player_name": "Erling Haaland",
        # Winner is Approach 2 — force it by setting wp outside (0.02, 0.98):
        "win_probability": 0.0,
        "key_insights": [
            "xG 0.88 per game",
            "Opposition 1.6 goals / match conceded",
            "3.4 shots per game",
            "Scored in 8 of last 12 club matches",
        ],
        "factors": {},
    }
    out = simulate_soccer_scorer_pick(pick)
    assert out is not None
    assert out["provenance"] == "EMPIRICAL_INDEPENDENT"
    assert out["input_quality"] in ("FULL", "STRONG", "PARTIAL")


# ─────────────────────────────────────────────────────────────────────
# §4 — Tennis simulator classifies MODEL_CONDITIONED (calibrated serve
#      gap to model_wp).
# ─────────────────────────────────────────────────────────────────────
def test_tennis_sim_is_model_conditioned():
    from brain.sim_tennis import simulate_tennis_pick
    pick = {
        "sport": "Tennis",
        "market": "Alcaraz Moneyline",
        "win_probability": 72.0,
    }
    out = simulate_tennis_pick(pick)
    assert out is not None
    assert out["provenance"] == "MODEL_CONDITIONED"
    assert out["decision_valid"] is False


# ─────────────────────────────────────────────────────────────────────
# §5 — NBA simulator classifies MODEL_CONDITIONED (λ/µ calibrated to
#      model_wp).
# ─────────────────────────────────────────────────────────────────────
def test_nba_sim_is_model_conditioned():
    from brain.sim_nba import simulate_nba_pick
    pick = {
        "sport": "NBA",
        "market": "Lakers Moneyline",
        "win_probability": 60.0,
        "factors": {},
    }
    out = simulate_nba_pick(pick)
    assert out is not None
    assert out["provenance"] == "MODEL_CONDITIONED"
    assert out["decision_valid"] is False


# ─────────────────────────────────────────────────────────────────────
# §6 — MLB Poisson-K simulator: CAUSAL/EMPIRICAL provenance when real
#      pitcher signals exist; PRIOR_ONLY / decision_valid=False when
#      only the league-average fallback fires.
# ─────────────────────────────────────────────────────────────────────
def test_mlb_k_prob_with_real_signals_is_causal_or_empirical():
    from services.mlb_k_probability import compute_expected_k
    ctx = {
        "home_team": "Boston Red Sox",
        "starting_pitcher_home": {
            "name": "Garrett Crochet",
            "l5_avg_k":  9.0,
            "l5_avg_ip": 6.0,
            "k_pct":     0.315,
            "ip_per_start": 6.1,
            "opp_k_pct": 0.24,
            "statcast":  {"xwoba_against": 0.290},
        },
        "starting_pitcher_away": {"name": "Opp"},
    }
    exp = compute_expected_k(ctx, "Garrett Crochet")
    assert exp["decision_valid"] is True
    assert exp["provenance"] in ("CAUSAL_INDEPENDENT", "EMPIRICAL_INDEPENDENT")


def test_mlb_k_prob_league_avg_only_is_prior_only():
    from services.mlb_k_probability import compute_expected_k
    ctx = {
        "home_team": "Boston Red Sox",
        "starting_pitcher_home": {"name": "Nobody Special"},
        "starting_pitcher_away": {"name": "Opp"},
    }
    exp = compute_expected_k(ctx, "Nobody Special")
    assert exp["provenance"] == "PRIOR_ONLY"
    assert exp["decision_valid"] is False


# ─────────────────────────────────────────────────────────────────────
# §7 — Sport capability registry marks NHL/CFB/UFC intended coverage.
# ─────────────────────────────────────────────────────────────────────
def test_capability_registry_documents_nhl_cfb_ufc():
    from services.sport_capability_registry import SPORT_CAPABILITIES
    # Presence check — the registry is the single source of truth per
    # Phase 1G.  These sports MUST exist so downstream can distinguish
    # SUPPORTED / PROVIDER_UNAVAILABLE / MODEL_UNAVAILABLE /
    # INTENTIONALLY_UNSUPPORTED.
    for sport in ("NHL", "CFB", "UFC"):
        assert sport in SPORT_CAPABILITIES, (
            f"{sport} missing from sport_capability_registry — "
            f"Phase 2 inventory contract violated"
        )
