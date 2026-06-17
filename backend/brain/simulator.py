"""Hidden Monte Carlo Simulator.

For each TOP-K candidate (flagged by candidates.py) we run a small
Beta-Bernoulli simulation to estimate:

  • win_probability   — posterior mean hit rate
  • expected_value    — E[ payout ] per $1 stake
  • variance          — Var[ payout ] per $1 stake (volatility signal)
  • agreement_score   — proxy for prediction stability (factor variance)

The simulator NEVER surfaces in the UI. Its outputs feed the Decision
Filter (next module) which decides whether the pick is allowed through.

Numerics:
  • Prior  Beta(α, β) where (α, β) come from the bucket's wins/losses
    + the spec band hit rate as a pseudo-count anchor (Bayesian shrink).
  • N = 1500 samples per candidate. With 50 top candidates that's 75k
    samples total per refresh — runs in <200ms in pure Python.

This module is intentionally dependency-free (uses Python `random`) so the
brain layer doesn't require numpy/scipy in the container.
"""
from __future__ import annotations

import logging
import random
import statistics
from math import lgamma, exp

from .memory import BrainMemory

logger = logging.getLogger("lockscore.brain.simulator")

N_SAMPLES = 1500
_RNG = random.Random()


def _american_to_decimal(american: int) -> float:
    a = float(american)
    if a >= 0:
        return 1.0 + a / 100.0
    return 1.0 + 100.0 / abs(a)


def _beta_sample(alpha: float, beta: float) -> float:
    """Pure-Python Beta(α, β) sample via two Gammas (no numpy)."""
    # random.gammavariate(alpha, 1) gives Gamma(α, 1)
    g1 = _RNG.gammavariate(max(0.001, alpha), 1.0)
    g2 = _RNG.gammavariate(max(0.001, beta), 1.0)
    return g1 / (g1 + g2) if (g1 + g2) > 0 else 0.5


def _agreement_score(pick: dict) -> float:
    """0..1 — high when factor values agree (low stdev)."""
    f = pick.get("factors") or {}
    vals = [float(v) / 100.0 if v > 1 else float(v) for v in f.values()] if f else []
    if len(vals) < 2:
        return 0.5
    sd = statistics.pstdev(vals)
    return max(0.0, min(1.0, 1.0 - sd * 3.3))


def _simulate_one(pick: dict, memory: BrainMemory) -> dict:
    """Run a Beta-Bernoulli MC for one candidate."""
    sport = pick.get("sport") or ""
    sv2 = pick.get("selection_v2") or {}
    family = (sv2.get("market") or {}).get("family") or "other"
    bucket = memory.market(sport, family)

    # Prior anchor: the brain's calibrated confidence (or band expected).
    brain = pick.get("brain") or {}
    mu = float(brain.get("confidence_calibrated") or (pick.get("win_probability", 50) / 100.0))
    mu = max(0.02, min(0.98, mu))

    # Pseudo-count weights: with abundant data, lean on the bucket; with
    # little data, lean on the spec band expectation.
    if bucket and bucket.n >= 25:
        wins, losses = bucket.won, bucket.lost
        # Smooth toward the band's expected hit rate so a 2-3 game streak
        # doesn't blow up the prior.
        anchor_n = 10
        anchor_wins = anchor_n * mu
        alpha = wins + anchor_wins + 1
        beta  = losses + (anchor_n - anchor_wins) + 1
    else:
        # Anchor-only prior — strength 20 (mean-anchored Beta).
        strength = 20
        alpha = mu * strength + 1
        beta  = (1 - mu) * strength + 1

    american = int(pick.get("book_odds") or 0)
    decimal = _american_to_decimal(american) if american else 2.0
    payout_win = decimal - 1.0   # profit per $1 stake on win
    payout_loss = -1.0

    wins = 0
    payout_sum = 0.0
    payout_sq_sum = 0.0
    for _ in range(N_SAMPLES):
        p = _beta_sample(alpha, beta)
        outcome = 1 if _RNG.random() < p else 0
        payoff = payout_win if outcome else payout_loss
        if outcome:
            wins += 1
        payout_sum += payoff
        payout_sq_sum += payoff * payoff

    mean_payout = payout_sum / N_SAMPLES
    var_payout = (payout_sq_sum / N_SAMPLES) - (mean_payout ** 2)

    return {
        "win_probability":  round(wins / N_SAMPLES, 4),
        "expected_value":   round(mean_payout, 4),
        "variance":         round(max(0.0, var_payout), 4),
        "agreement_score":  round(_agreement_score(pick), 4),
        "prior_alpha":      round(alpha, 2),
        "prior_beta":       round(beta, 2),
        "n_samples":        N_SAMPLES,
    }


def run_simulator(picks: list[dict], memory: BrainMemory) -> dict:
    """Run MC on top-K flagged picks; mutate `brain.simulator` per pick."""
    n_run = 0
    for p in picks:
        brain = p.setdefault("brain", {})
        if not brain.get("top_k"):
            continue
        try:
            brain["simulator"] = _simulate_one(p, memory)
            n_run += 1
        except Exception as e:                 # pragma: no cover
            logger.warning("simulator failed for pick %s: %s", p.get("id"), e)
    return {"simulated": n_run}
