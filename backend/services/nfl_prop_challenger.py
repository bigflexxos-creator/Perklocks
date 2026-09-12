"""
NFL Prop Challenger — Shadow Distribution Model
================================================

READ-ONLY, additive challenger for NFL player-prop probability
estimation.  Emits a COHERENT underlying distribution per player
per family so alt-ladder monotonicity is guaranteed by construction:

    P(>=175) > P(>=200) > P(>=225)

Champion (current system): per-rung independent probability + factor
blend, computed inside the existing NFL Player-Prop Lock authority
in ``sports_engine.compute_lock_score``.

Challenger (this module): SHADOW ONLY.  Called from a backtest
harness; NEVER wired into the live scoring path unless a formal
Champion-vs-Challenger comparison proves out-of-sample improvement.

Contract:
- Input: last-N per-player weekly stat rows + optional opponent /
  environment context.
- Output: ``ChallengerDistribution`` with (mu, sigma, family,
  provenance) plus a ``prob_over(threshold)`` method that returns
  a monotonically-decreasing probability as threshold rises.
- Provenance per input: OBSERVED / MODEL_DERIVED / PRIOR_ONLY / MISSING.
- If provenance is PRIOR_ONLY/MISSING alone → challenger returns
  low-confidence distribution (wide sigma).  Never inflates probability.

References:
- Distribution: log-normal for yardage families (right-skew), Poisson
  for count families (receptions, attempts) — matches empirical fits
  on nfl_player_weekly 2019-2024.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

# Empirical league priors from nfl_player_weekly 2019-2024 REG
# (means / CVs per family × position).  Used when player has <3
# real games — never as authority.
_LEAGUE_PRIORS: dict[tuple[str, str], tuple[float, float]] = {
    # (family, position) → (mean, cv)
    ("passing_yards", "QB"):   (238.4, 0.31),
    ("rushing_yards", "QB"):   (12.5, 1.20),
    ("rushing_yards", "RB"):   (58.7, 0.62),
    ("rushing_yards", "WR"):   ( 6.2, 1.40),
    ("receiving_yards", "WR"): (52.4, 0.78),
    ("receiving_yards", "TE"): (35.1, 0.85),
    ("receiving_yards", "RB"): (23.5, 0.95),
    ("receptions", "WR"):      ( 4.8, 0.55),
    ("receptions", "TE"):      ( 3.6, 0.60),
    ("receptions", "RB"):      ( 3.1, 0.65),
    ("passing_attempts", "QB"):    (34.2, 0.18),
    ("passing_completions", "QB"): (22.5, 0.20),
    ("rushing_attempts", "RB"):    (14.5, 0.55),
}


@dataclass
class ChallengerDistribution:
    mu: float
    sigma: float
    family: str          # passing_yards / rushing_yards / etc.
    position: str
    provenance: dict     # per-input provenance labels
    dist_type: str       # "lognormal" | "poisson"
    n_games: int
    confidence: float = 1.0    # 0..1 — reduces when data thin
    reasons: list = field(default_factory=list)

    def prob_over(self, threshold: float) -> float:
        """P(stat >= threshold).  Monotonically decreasing in threshold."""
        if threshold <= 0:
            return 1.0
        if self.dist_type == "lognormal":
            if self.mu <= 0 or self.sigma <= 0:
                return 0.0
            # Log-normal parametrized via mean=mu, cv=sigma/mu.
            cv = max(0.05, self.sigma / max(self.mu, 1.0))
            sigma_log = math.sqrt(math.log(1.0 + cv * cv))
            mu_log = math.log(max(self.mu, 1e-6)) - 0.5 * sigma_log * sigma_log
            z = (math.log(threshold) - mu_log) / max(sigma_log, 1e-6)
            return 1.0 - _phi(z)
        # Poisson survival function.
        if self.mu <= 0:
            return 0.0
        lam = max(self.mu, 1e-6)
        k = int(math.floor(threshold))
        # P(X >= k) = 1 - P(X <= k-1)
        return max(0.0, min(1.0, 1.0 - _poisson_cdf(k - 1, lam)))


def _phi(z: float) -> float:
    """Standard normal CDF."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _poisson_cdf(k: int, lam: float) -> float:
    if k < 0:
        return 0.0
    s = 0.0
    log_lam = math.log(lam)
    log_fact = 0.0
    for i in range(0, k + 1):
        if i > 0:
            log_fact += math.log(i)
        # e^(-lam) * lam^i / i!
        s += math.exp(-lam + i * log_lam - log_fact)
    return min(1.0, s)


def _family_to_stat_col(family: str) -> str:
    return {
        "passing_yards": "passing_yards",
        "rushing_yards": "rushing_yards",
        "receiving_yards": "receiving_yards",
        "receptions": "receptions",
        "passing_attempts": "attempts",
        "passing_completions": "completions",
        "rushing_attempts": "carries",
    }.get(family, family)


def _dist_type(family: str) -> str:
    if family in ("receptions", "passing_attempts",
                  "passing_completions", "rushing_attempts"):
        return "poisson"
    return "lognormal"


def build_challenger_distribution(
    weekly_rows: list[dict],
    family: str,
    position: str,
    *,
    opponent_context: Optional[dict] = None,
    injury_context: Optional[dict] = None,
) -> ChallengerDistribution:
    """Fit the challenger distribution for one player × family.

    ``weekly_rows`` — chronologically-ordered nfl_player_weekly rows
    (most recent last).  Uses up to last 8 games with a 0.75 decay
    factor for the L5-weighted mean.

    Missing data fails gracefully to league PRIOR_ONLY; provenance
    is stamped so downstream cannot mistake a prior for elite
    authority.
    """
    stat = _family_to_stat_col(family)
    provenance: dict[str, str] = {}
    reasons: list[str] = []

    # ── Recent-form mean (recency weighted) ──────────────────────
    real: list[float] = []
    for r in (weekly_rows or [])[-8:]:
        v = r.get(stat)
        if isinstance(v, (int, float)) and math.isfinite(v):
            real.append(float(v))
    n = len(real)

    if n >= 3:
        # Recency-decay weighted mean.
        weights = [0.75 ** (n - 1 - i) for i in range(n)]
        w_sum = sum(weights)
        mu = sum(w * v for w, v in zip(weights, real)) / max(w_sum, 1e-6)
        # Sample stdev.
        var = sum((v - mu) ** 2 for v in real) / max(n - 1, 1)
        sigma = math.sqrt(max(var, 1e-6))
        provenance["volume_mean"] = "OBSERVED"
        confidence = min(1.0, n / 6.0)   # full confidence at 6+ games
        reasons.append(f"L{n} weighted mean {mu:.1f}")
    else:
        # ── Fail gracefully to league prior ──
        mean_prior, cv_prior = _LEAGUE_PRIORS.get(
            (family, position), (0.0, 0.5))
        mu = mean_prior
        sigma = mean_prior * cv_prior
        provenance["volume_mean"] = "PRIOR_ONLY"
        confidence = 0.30
        reasons.append(f"league prior (n<3 real games)")

    # ── Opponent context adjustment (real, capped) ───────────────
    # Multiplier bounded ±20% so opponent data can shift μ but never
    # dominate.  Provenance flagged accordingly.
    opp_mult = 1.0
    if opponent_context and isinstance(opponent_context, dict):
        raw_mult = opponent_context.get(f"{family}_allowed_multiplier")
        if isinstance(raw_mult, (int, float)) and 0.6 <= raw_mult <= 1.6:
            opp_mult = max(0.80, min(1.20, raw_mult))
            provenance["opponent_context"] = "OBSERVED"
            reasons.append(f"opp mult {opp_mult:.2f}")
        else:
            provenance["opponent_context"] = "MISSING"
    else:
        provenance["opponent_context"] = "MISSING"

    mu *= opp_mult

    # ── Injury / role redistribution (bounded ±15%) ──────────────
    # Only surfaces when a WR1/RB1 is OUT and role redistribution
    # is meaningful.  Otherwise flagged MISSING so we don't
    # fabricate signal.
    if injury_context and isinstance(injury_context, dict):
        role_mult = injury_context.get("role_redistribution_multiplier")
        if isinstance(role_mult, (int, float)) and 0.85 <= role_mult <= 1.15:
            mu *= role_mult
            provenance["injury_role"] = "OBSERVED"
            reasons.append(f"role mult {role_mult:.2f}")
        else:
            provenance["injury_role"] = "MISSING"
    else:
        provenance["injury_role"] = "MISSING"

    return ChallengerDistribution(
        mu=round(mu, 3),
        sigma=round(sigma, 3),
        family=family,
        position=position,
        provenance=provenance,
        dist_type=_dist_type(family),
        n_games=n,
        confidence=round(confidence, 3),
        reasons=reasons,
    )


__all__ = ["ChallengerDistribution", "build_challenger_distribution"]
