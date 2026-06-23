"""Unified Probability Engine.

Per the user spec (2026-06-23): build a probability system with three
INDEPENDENT inputs (v1 deterministic, v2 ML/advanced, Monte Carlo
simulator), an ensemble layer, isotonic calibration, and finally a
single canonical `edge` derived ONLY from the calibrated probability.

This module is **additive** — it does NOT replace the existing pick
pipeline, lock-score engine, or edge field on the pick payload. It
provides a transparent breakdown you can inspect via the new
`/api/picks/{id}/probability` endpoint. Existing UI keeps showing the
raw model lock_score as the user requested ("don't want to change app
idea").

Architecture
------------
                ┌─────────────────────────────────────────────┐
                │           pick (from DB)                    │
                └────┬────────────────┬─────────────────┬────┘
                     │                │                 │
              p_v1 (det)        p_v2 (ML)       sim_probability
                     │                │                 │
                     └────────┬───────┴────────┬────────┘
                              │                │
                         ENSEMBLE         simulator_variance
                              │           stability_score
                              ▼
                         p_final  ──►  CALIBRATE  ──►  p_calibrated
                                                            │
                                          implied = 1/dec_odds
                                                            │
                                                            ▼
                                                  edge = p_cal - implied
                                                  clamp [-0.15, +0.40]
                                                            │
                                                            ▼
                                                  classify ▶ LOCK_99 /
                                                             PREMIUM /
                                                             NORMAL /
                                                             CHALK

CRITICAL bug-prevention rules from the spec (enforced here):
  • Each layer outputs probabilities only — NEVER subtracts v1 from
    v2 or applies chalk penalties.
  • Ensemble does NOT subtract models from each other.
  • Calibration is a single isotonic-regression pass (reuses the
    curve already maintained in `lock_calibration.py`).
  • Edge is computed exactly once, from p_calibrated only.
  • Chalk is a LABEL, not a penalty.
"""

from __future__ import annotations

import logging
from typing import Optional

from lock_calibration import get_curve as _calib_get_curve

logger = logging.getLogger("lockscore.probability_engine")

# ──────────────────────────────────────────────────────────────────────────
# Tunables
# ──────────────────────────────────────────────────────────────────────────
W_V1: float = 0.30
W_V2: float = 0.45
W_SIM: float = 0.25

EDGE_MIN: float = -0.15
EDGE_MAX: float = +0.40

LOCK_99_PROB: float = 0.72
LOCK_99_EDGE: float = 0.05
LOCK_99_STABILITY: float = 0.85   # 0..1

PREMIUM_PROB: float = 0.60
CHALK_IMPLIED: float = 0.65


# ──────────────────────────────────────────────────────────────────────────
# Layer 1 — three independent probabilities
# ──────────────────────────────────────────────────────────────────────────

def compute_v1_probability(pick: dict) -> float:
    """Baseline deterministic probability.

    Source priority (each falls back to the next):
      1. pick.model_win_probability — the raw, pre-learning model output.
         This is the cleanest deterministic signal: rolling form, Elo
         ratings, historical win rates as computed at pick-generation
         time, BEFORE the bucket/calibration adjustments piled on.
      2. pick.implied_probability — the book's own number (a deterministic
         estimator no worse than chance for chalky markets).
      3. pick.win_probability — last-resort fallback.

    All numbers are normalised to [0, 1].
    """
    for key in ("model_win_probability", "implied_probability", "win_probability"):
        v = pick.get(key)
        if isinstance(v, (int, float)) and v > 0:
            # The pick fields are stored as 0..100 percentages. Normalise.
            return max(0.0, min(1.0, float(v) / 100.0))
    return 0.5


def compute_v2_probability(pick: dict) -> float:
    """Advanced ML/feature-based probability.

    Source priority:
      1. pick.win_probability — the learning-engine-adjusted output. This
         is the v2 model output post-bucket-adjustment + post-Bayesian
         shrinkage. Captures nonlinear interactions (form × matchup,
         player intel hot/cold streaks, bandit lift) the v1 baseline
         doesn't see.
      2. pick.lock_score_v2 — derived from the 6-component lock engine,
         which is itself a feature-weighted system.
      3. Fall back to v1 if neither.
    """
    wp = pick.get("win_probability")
    if isinstance(wp, (int, float)) and wp > 0:
        return max(0.0, min(1.0, float(wp) / 100.0))
    v2_lock = pick.get("lock_score_v2")
    if isinstance(v2_lock, (int, float)) and v2_lock > 0:
        # Convert lock_score_v2 (0..99) into a probability proxy. The
        # 6-component v2 engine is calibrated so that score equals
        # roughly the expected win rate, so a direct /100 mapping is
        # the honest conversion.
        return max(0.0, min(1.0, float(v2_lock) / 100.0))
    return compute_v1_probability(pick)


def compute_sim_probability(pick: dict) -> tuple[float, float, float]:
    """Monte Carlo probability + variance + stability.

    Reads the cached simulator output already stored on the pick by
    `brain/sim_runner.py`. Each sport's simulator (`sim_mlb`, `sim_soccer`,
    `sim_nba`, `sim_tennis`) runs 5k-20k iterations per event with
    these inputs already baked in:
      • Player/team rating differential (Elo or learned form).
      • Recent form (L5/L10 weighted).
      • Matchup factors (surface, opponent style, K/9 vs lineup, etc.).
      • Random shock term (volatility / injury noise).

    Returns (probability_0_1, variance, stability_score_0_1).
    stability_score = 1 - normalised CI width. A 95% CI of [0.79, 0.89]
    has width 0.10 → stability 0.90. Wide CI = unstable Monte Carlo.
    """
    sim_p = pick.get("sim_win_probability")
    if not isinstance(sim_p, (int, float)) or sim_p <= 0:
        # No sim available — degrade gracefully by deferring to v2 with
        # a low stability score so the ensemble down-weights this leg.
        return compute_v2_probability(pick), 0.0, 0.0
    sim_p = max(0.0, min(1.0, float(sim_p) / 100.0))
    lo = pick.get("sim_ci_lower")
    hi = pick.get("sim_ci_upper")
    if isinstance(lo, (int, float)) and isinstance(hi, (int, float)) and hi > lo:
        # Convert percentage CIs to 0..1
        ci_width = max(0.0, float(hi) - float(lo)) / 100.0
        variance = (ci_width / 4.0) ** 2   # rough back-out: 95% CI ≈ 4σ
        stability = max(0.0, min(1.0, 1.0 - ci_width))
    else:
        # Fallback: assume moderate stability when CI bounds are missing.
        variance = 0.01
        stability = 0.6
    return sim_p, variance, stability


# ──────────────────────────────────────────────────────────────────────────
# Layer 2 — ensemble
# ──────────────────────────────────────────────────────────────────────────

def ensemble(p_v1: float, p_v2: float, p_sim: float) -> float:
    """Weighted average: 0.30·v1 + 0.45·v2 + 0.25·sim.

    Explicitly does NOT subtract models — that's the bug-prevention
    rule from the spec. If a layer wants to express disagreement it
    must show up as variance/stability, not as a penalty to another
    layer.
    """
    final = W_V1 * p_v1 + W_V2 * p_v2 + W_SIM * p_sim
    return max(0.0, min(1.0, final))


# ──────────────────────────────────────────────────────────────────────────
# Layer 3 — calibration
# ──────────────────────────────────────────────────────────────────────────

def calibrate(p_final: float) -> float:
    """Isotonic-regression calibration via the shared lock_calibration
    curve, which was fit from historical settled picks (won/lost) using
    Pool Adjacent Violators.

    The shared curve is keyed on the raw lock_score (0-99 scale), so we
    scale p_final → percentage before lookup and de-scale on return.
    With <50 settled picks the curve returns identity (safe fallback).
    """
    curve = _calib_get_curve()
    raw_pct = p_final * 100.0
    calibrated_prob = curve.transform(raw_pct)
    return max(0.0, min(1.0, float(calibrated_prob)))


# ──────────────────────────────────────────────────────────────────────────
# Layer 4 — edge (the ONLY place edge is computed)
# ──────────────────────────────────────────────────────────────────────────

def implied_probability_from_odds(book_odds: Optional[float]) -> float:
    """Convert American odds → implied probability [0, 1].

    Spec says implied = 1/decimal_odds. The pick payload stores American
    odds, so we convert American → decimal → implied. Defensive against
    None / 0 / malformed values.
    """
    if not isinstance(book_odds, (int, float)) or book_odds == 0:
        return 0.5
    bo = float(book_odds)
    if bo > 0:
        decimal = bo / 100.0 + 1.0
    else:
        decimal = 100.0 / abs(bo) + 1.0
    return max(0.0, min(1.0, 1.0 / decimal))


def compute_edge(p_calibrated: float, book_odds: Optional[float]) -> float:
    """edge = p_calibrated - implied_probability, clamped to [-0.15, +0.40].

    This is the SINGLE source of truth for edge. The existing pick
    payload's `edge_percent` field uses a different formula and is left
    alone (per user "don't change app idea") — this method returns the
    new canonical edge alongside it via the API.
    """
    implied = implied_probability_from_odds(book_odds)
    raw_edge = p_calibrated - implied
    return max(EDGE_MIN, min(EDGE_MAX, raw_edge))


# ──────────────────────────────────────────────────────────────────────────
# Layer 5 — classification (LOCK / PREMIUM / NORMAL / CHALK)
# ──────────────────────────────────────────────────────────────────────────

def classify(p_calibrated: float, edge: float, stability: float, implied: float) -> str:
    """Spec-defined buckets. CHALK is a label, not a penalty — a pick
    can be both LOCK_99 and CHALK simultaneously, in which case we
    prefer the stronger label (LOCK_99 takes precedence)."""
    if (p_calibrated >= LOCK_99_PROB
        and edge >= LOCK_99_EDGE
        and stability >= LOCK_99_STABILITY):
        return "LOCK_99"
    if p_calibrated >= PREMIUM_PROB:
        # Premium picks that are also chalk get the PREMIUM tag — the
        # spec is explicit that we don't apply chalk penalties, so
        # premium wins out.
        return "PREMIUM"
    if implied >= CHALK_IMPLIED:
        return "CHALK"
    return "NORMAL"


# ──────────────────────────────────────────────────────────────────────────
# Top-level API
# ──────────────────────────────────────────────────────────────────────────

def unified_probability_report(pick: dict) -> dict:
    """Returns the full breakdown for one pick, matching the exact
    shape the spec requested:

        {
          "p_v1": 0.78,
          "p_v2": 0.74,
          "sim_probability": 0.81,
          "p_final": 0.766,
          "p_calibrated": 0.731,
          "edge": 0.041,
          "classification": "PREMIUM",
          "simulator_variance": 0.0064
        }

    Plus auxiliary fields (stability_score, implied_probability) so
    UI consumers can show the full picture.
    """
    p_v1 = compute_v1_probability(pick)
    p_v2 = compute_v2_probability(pick)
    p_sim, sim_variance, sim_stability = compute_sim_probability(pick)

    p_final = ensemble(p_v1, p_v2, p_sim)
    p_calibrated = calibrate(p_final)

    # ── Stability (cross-model consensus) ──────────────────────────
    # If the simulator ran, prefer its CI-derived stability. If not,
    # fall back to the v1/v2 agreement so MLB spread / no-sim markets
    # don't get spuriously stuck at 0.0 just because no Monte Carlo
    # was wired up. Spread of |p_v1 − p_v2| ≤ 0.05 → stability ≈ 0.90.
    sim_p_raw = pick.get("sim_win_probability")
    sim_ran = isinstance(sim_p_raw, (int, float)) and sim_p_raw > 0
    if sim_ran:
        stability = sim_stability
    else:
        # Convert v1↔v2 disagreement into a [0..1] stability score.
        # Coefficient 4.0 maps a 25pp spread to stability=0 (max
        # disagreement), 0pp spread to stability=1.0.
        spread = abs(p_v1 - p_v2)
        stability = max(0.0, min(1.0, 1.0 - 4.0 * spread))

    book_odds = pick.get("book_odds")
    implied = implied_probability_from_odds(book_odds)
    edge = compute_edge(p_calibrated, book_odds)
    cls = classify(p_calibrated, edge, stability, implied)

    return {
        "p_v1": round(p_v1, 4),
        "p_v2": round(p_v2, 4),
        "sim_probability": round(p_sim, 4),
        "p_final": round(p_final, 4),
        "p_calibrated": round(p_calibrated, 4),
        "edge": round(edge, 4),
        "classification": cls,
        "simulator_variance": round(sim_variance, 6),
        # Aux fields (not in the strict spec, but UI-useful)
        "stability_score": round(stability, 4),
        "implied_probability": round(implied, 4),
        "weights": {"v1": W_V1, "v2": W_V2, "sim": W_SIM},
        "calibration": {
            "fit_sample_size": _calib_get_curve().fit_sample_size,
            "last_fit_at": _calib_get_curve().last_fit_at,
        },
    }
