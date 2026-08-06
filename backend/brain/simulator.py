"""Phase 4B — Posterior Uncertainty (formerly ``brain/simulator.py``).

⚠️  RECLASSIFIED 2026-08-06 (Phase 4B):

This module was historically labelled "Hidden Monte Carlo Simulator"
but the Phase 4A audit confirmed it is a **posterior Beta-Bernoulli
sampler seeded from the caller's own model probability** (``mu``).
Its output is NOT independent evidence — it merely quantifies the
uncertainty around the model's own belief.

What this module DOES
=====================
Given a pick with a model-estimated ``confidence_calibrated`` (μ):
  • Fit a Beta(α, β) posterior centered on μ (with pseudo-count
    strength from historical bucket data if available).
  • Draw N samples to compute a **credible interval** and
    **standard error** of μ.
  • Report ``posterior_mean`` (≈ μ), ``lower_bound``, ``upper_bound``,
    ``uncertainty_width``, ``effective_sample_size``.

What this module DOES NOT do
============================
  • It does NOT produce an independent second probability estimate.
  • Its ``posterior_mean`` is tied to the input μ by construction.
  • It MUST NOT be counted as a "second model vote".
  • It CANNOT raise a lock score, edge, EV, or confidence.
  • ``independent_evidence`` is always **False** in the emitted
    :class:`SimulatorResult`.

Interpretation
==============
  • **Narrow band** (uncertainty_width small) → the model's belief
    μ is stable given the bucket's history.  Report as-is.
  • **Wide band** → the model's μ is fragile.  Downstream consumers
    MAY use ``uncertainty_width`` to CAP confidence, but they may
    NEVER use it to INCREASE confidence.

Backward compatibility
======================
The old ``run_simulator(picks, memory)`` wrapper is retained and
delegates to :func:`run_posterior_uncertainty`.  The emitted
``pick["brain"]["simulator"]`` dict keeps the legacy keys
(``win_probability``, ``expected_value``, ``variance``,
``agreement_score``) but ADDS the truthful fields
(``method="beta_bernoulli_posterior"``, ``independent_evidence=False``,
``simulator_type="posterior_uncertainty"``, ``posterior_mean``,
``lower_bound``, ``upper_bound``, ``uncertainty_width``,
``input_probability``, ``effective_sample_size``, ``seed``,
``simulator_version``).

Downstream callers (:mod:`brain.filter`) have been updated to stop
treating these outputs as independent evidence — see Phase 4B
``filter.py`` for the migration.
"""
from __future__ import annotations

import logging
import random
import statistics
import time
from math import lgamma, exp                        # noqa: F401 (kept for API)

from .memory import BrainMemory
from .simulator_contract import SimulatorResult
from services.simulation_seed import build_seed, SeedError

logger = logging.getLogger("lockscore.brain.posterior_uncertainty")

N_SAMPLES = 1500
SIMULATOR_NAME = "posterior_uncertainty"
SIMULATOR_VERSION = "2.0.0"      # bumped 2026-08-06 (Phase 4B rebrand)
SIMULATOR_TYPE = "posterior_uncertainty"


def _american_to_decimal(american: int) -> float:
    a = float(american)
    if a >= 0:
        return 1.0 + a / 100.0
    return 1.0 + 100.0 / abs(a)


def _beta_sample(rng: random.Random, alpha: float, beta: float) -> float:
    """Pure-Python Beta(α, β) via two Gammas — seeded RNG only."""
    g1 = rng.gammavariate(max(0.001, alpha), 1.0)
    g2 = rng.gammavariate(max(0.001, beta), 1.0)
    return g1 / (g1 + g2) if (g1 + g2) > 0 else 0.5


def _agreement_score(pick: dict) -> float:
    """0..1 — high when factor values agree (low stdev).

    This is a factor-variance FRAGILITY signal, NOT an independent
    model vote.  Downstream consumers must not treat this as a
    second opinion.
    """
    f = pick.get("factors") or {}
    vals = [float(v) / 100.0 if v > 1 else float(v) for v in f.values()] if f else []
    if len(vals) < 2:
        return 0.5
    sd = statistics.pstdev(vals)
    return max(0.0, min(1.0, 1.0 - sd * 3.3))


def _posterior_one(pick: dict, memory: BrainMemory) -> dict:
    """Run a Beta-Bernoulli POSTERIOR UNCERTAINTY sampler for one pick.

    Returns a dict shaped to match the pre-Phase-4B legacy keys AND
    the new truthful keys.  ``independent_evidence`` is hard-coded
    False.
    """
    t0 = time.monotonic()
    sport = pick.get("sport") or ""
    sv2 = pick.get("selection_v2") or {}
    family = (sv2.get("market") or {}).get("family") or "other"
    bucket = memory.market(sport, family)

    # Prior anchor: the model's OWN calibrated confidence.  This is
    # what makes the sampler a POSTERIOR — μ comes from the caller.
    brain = pick.get("brain") or {}
    mu = float(brain.get("confidence_calibrated")
                or (pick.get("win_probability", 50) / 100.0))
    mu = max(0.02, min(0.98, mu))
    input_probability = mu

    if bucket and bucket.n >= 25:
        wins, losses = bucket.won, bucket.lost
        anchor_n = 10
        anchor_wins = anchor_n * mu
        alpha = wins + anchor_wins + 1
        beta = losses + (anchor_n - anchor_wins) + 1
        eff_n = int(wins + losses + anchor_n)
    else:
        strength = 20
        alpha = mu * strength + 1
        beta = (1 - mu) * strength + 1
        eff_n = strength

    # ── Deterministic per-pick seed ────────────────────────────────
    try:
        seed = build_seed(pick, SIMULATOR_NAME, SIMULATOR_VERSION,
                            allow_name_only_fallback=True)
    except SeedError as e:
        logger.debug("posterior_uncertainty seed fallback: %s", e)
        seed = 0
    rng = random.Random(seed)

    american = int(pick.get("book_odds") or 0)
    decimal = _american_to_decimal(american) if american else 2.0
    payout_win = decimal - 1.0
    payout_loss = -1.0

    samples: list[float] = []
    wins = 0
    payout_sum = 0.0
    payout_sq_sum = 0.0
    for _ in range(N_SAMPLES):
        p = _beta_sample(rng, alpha, beta)
        samples.append(p)
        outcome = 1 if rng.random() < p else 0
        payoff = payout_win if outcome else payout_loss
        if outcome:
            wins += 1
        payout_sum += payoff
        payout_sq_sum += payoff * payoff

    samples.sort()
    posterior_mean = sum(samples) / N_SAMPLES
    lo_idx = int(0.025 * N_SAMPLES)
    hi_idx = int(0.975 * N_SAMPLES)
    lower_bound = samples[lo_idx]
    upper_bound = samples[min(N_SAMPLES - 1, hi_idx)]
    uncertainty_width = upper_bound - lower_bound
    # Beta posterior stderr = sqrt(α·β / ((α+β)²(α+β+1)))
    total = alpha + beta
    std_err = ((alpha * beta) / (total * total * (total + 1))) ** 0.5

    mean_payout = payout_sum / N_SAMPLES
    var_payout = (payout_sq_sum / N_SAMPLES) - (mean_payout ** 2)

    duration_ms = round((time.monotonic() - t0) * 1000.0, 2)

    # Legacy keys (kept for pre-Phase-4B analytics consumers that
    # read these fields) — clearly re-labelled via the new
    # ``method`` and ``independent_evidence`` fields.
    out = {
        # ─── LEGACY KEYS (retained for schema compat) ───────────
        "win_probability":     round(posterior_mean, 4),
        "expected_value":      round(mean_payout, 4),
        "variance":            round(max(0.0, var_payout), 4),
        "agreement_score":     round(_agreement_score(pick), 4),
        "prior_alpha":         round(alpha, 2),
        "prior_beta":          round(beta, 2),
        "n_samples":           N_SAMPLES,
        # ─── PHASE 4B TRUTHFUL LABELS ───────────────────────────
        "method":              "beta_bernoulli_posterior",
        "simulator_type":      SIMULATOR_TYPE,
        "simulator_name":      SIMULATOR_NAME,
        "simulator_version":   SIMULATOR_VERSION,
        "independent_evidence": False,
        "input_probability":   round(input_probability, 4),
        "posterior_mean":      round(posterior_mean, 4),
        "lower_bound":         round(lower_bound, 4),
        "upper_bound":         round(upper_bound, 4),
        "uncertainty_width":   round(uncertainty_width, 4),
        "standard_error":      round(std_err, 4),
        "effective_sample_size": eff_n,
        "seed":                seed,
        "duration_ms":         duration_ms,
        # ─── SIMULATOR CONTRACT (typed) ─────────────────────────
        "contract": SimulatorResult(
            simulator_name=SIMULATOR_NAME,
            simulator_version=SIMULATOR_VERSION,
            simulator_type=SIMULATOR_TYPE,
            seed=seed,
            iterations=N_SAMPLES,
            input_line=None,
            input_side=pick.get("side"),
            raw_probability=round(posterior_mean, 4),
            stabilized_probability=round(posterior_mean, 4),
            standard_error=round(std_err, 4),
            lower_bound=round(lower_bound, 4),
            upper_bound=round(upper_bound, 4),
            push_probability=None,
            valid=True,
            invalid_reason=None,
            independent_evidence=False,
            duration_ms=duration_ms,
            method="beta_bernoulli_posterior",
            extras={"input_probability": round(input_probability, 4),
                     "effective_sample_size": eff_n},
        ).to_dict(),
    }
    return out


def run_posterior_uncertainty(
    picks: list[dict],
    memory: BrainMemory,
) -> dict:
    """Compute posterior uncertainty for TOP-K flagged picks.

    Mutates each pick's ``brain.simulator`` with the truthful posterior
    fields.  Also stamps ``brain.posterior_uncertainty`` (same content,
    new home) so downstream code can migrate off the legacy key over
    time without a schema break.

    Returns aggregate counts.
    """
    n_run = 0
    for p in picks:
        brain = p.setdefault("brain", {})
        if not brain.get("top_k"):
            continue
        try:
            result = _posterior_one(p, memory)
            brain["simulator"] = result           # legacy key preserved
            brain["posterior_uncertainty"] = result
            n_run += 1
        except Exception as e:                    # pragma: no cover
            logger.warning("posterior_uncertainty failed for pick %s: %s",
                            p.get("id"), e)
    return {
        "simulated": n_run,
        "simulator_type": SIMULATOR_TYPE,
        "independent_evidence": False,
        "version": SIMULATOR_VERSION,
    }


# ── Backward-compat wrapper ────────────────────────────────────────
def run_simulator(picks: list[dict], memory: BrainMemory) -> dict:
    """Deprecated alias — routes to :func:`run_posterior_uncertainty`.

    Kept so pre-Phase-4B callers (`brain.pipeline`) continue to work
    without an emergency migration.  The emitted result now truthfully
    labels itself as posterior uncertainty via ``method``,
    ``simulator_type``, and ``independent_evidence=False``.
    """
    result = run_posterior_uncertainty(picks, memory)
    result["deprecated_alias"] = "run_simulator"
    result["recommended_name"] = "run_posterior_uncertainty"
    return result


__all__ = [
    "run_posterior_uncertainty",
    "run_simulator",
    "SIMULATOR_NAME",
    "SIMULATOR_VERSION",
    "SIMULATOR_TYPE",
    "N_SAMPLES",
]
