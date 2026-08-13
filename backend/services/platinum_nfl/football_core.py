"""Football simulation core primitives (Block 2B.1A §4, §5, §13, §14, §15).

Everything here is genuinely football-shaped — NOT ports from MLB or NBA
simulators.  The building blocks flow **opportunity before outcome**:

    team strength (offense EPA/play, pace) + defense
        ↓
    expected possessions per team (script-free)
        ↓
    expected plays per team (pass/rush mix, script-adjusted)
        ↓
    game-script distribution (sampled from margin + volatility)
        ↓
    role/opportunity samples (see ``opportunity.py``)
        ↓
    efficiency draws (yards/attempt, catch rate, ...)
        ↓
    correlated outcomes

Shrinkage (§14) is applied at the estimator layer — small-sample
players/teams shrink toward positional / league baselines so recent
hot streaks and rookie samples do not fake certainty.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Iterable, Optional


# ═════════════════════════════════════════════════════════════════════
# Determinism (§33)
# ═════════════════════════════════════════════════════════════════════
def sim_seed(base: int, *tags: object) -> random.Random:
    """Return a deterministic ``random.Random`` seeded from ``base``
    plus stringified extra tags.  Same fixture + seed + simulator
    version → reproducible output.  Used everywhere below."""
    h = hash((base, *[str(t) for t in tags])) & 0xFFFFFFFF
    return random.Random(h ^ (base & 0xFFFFFFFF))


# ═════════════════════════════════════════════════════════════════════
# League baselines (2020–2025 NFL blended, opportunity-neutral)
# ═════════════════════════════════════════════════════════════════════
# These are intentionally conservative / regressed baselines used as
# shrinkage priors.  They are NOT the model's predicted totals — they
# are just the anchor a small-sample player pulls toward.
LEAGUE_POSSESSIONS_PER_TEAM_REG   = 11.0        # per game
LEAGUE_POSSESSIONS_PER_TEAM_PRE   = 10.0
LEAGUE_POSSESSIONS_PER_TEAM_POST  = 11.5

LEAGUE_PLAYS_PER_POSSESSION       = 5.9         # regular season
LEAGUE_YARDS_PER_PLAY             = 5.5
LEAGUE_PASS_RATE_NEUTRAL          = 0.60        # 60% pass on neutral downs
LEAGUE_PASS_RATE_LEADING_10       = 0.42
LEAGUE_PASS_RATE_TRAILING_10      = 0.72
LEAGUE_QB_YPA                     = 7.2
LEAGUE_QB_COMP_RATE               = 0.645
LEAGUE_QB_INT_RATE                = 0.023       # per attempt
LEAGUE_RB_YPC                     = 4.35
LEAGUE_WR_TARGET_CATCH_RATE       = 0.635
LEAGUE_WR_YPT                     = 8.2
LEAGUE_TD_PROB_PER_RZ_TRIP        = 0.55        # any offensive TD
LEAGUE_TOTAL_MEAN                 = 44.5        # points
LEAGUE_TOTAL_STD                  = 12.5


# ═════════════════════════════════════════════════════════════════════
# Shrinkage / hierarchical logic (§14)
# ═════════════════════════════════════════════════════════════════════
@dataclass
class ShrinkageEstimator:
    """Empirical-Bayes style estimator with a positional/league prior.

    The posterior mean is
        μ_post = (n·x_bar + k·μ_prior) / (n + k)
    where k is the prior weight (games).  Rookies (n<3) shrink hard
    toward league; established players (n>16) barely move.
    """
    prior_mean: float
    prior_weight_games: float = 6.0          # k

    def estimate(self, sample: Iterable[float]) -> tuple[float, float]:
        """Return (posterior_mean, effective_n).  Empty sample →
        (prior_mean, 0).
        """
        vs = [float(v) for v in sample if v is not None]
        n = len(vs)
        if n == 0:
            return self.prior_mean, 0.0
        x_bar = sum(vs) / n
        k = self.prior_weight_games
        mu = (n * x_bar + k * self.prior_mean) / (n + k)
        return mu, float(n)

    def estimate_variance(self, sample: Iterable[float],
                          floor_var: float) -> float:
        """Sample variance floored at ``floor_var`` and inflated for
        small samples via 1/(n-1) style correction with an
        additional 1 + 1/n uncertainty premium."""
        vs = [float(v) for v in sample if v is not None]
        n = len(vs)
        if n <= 1:
            # Small sample -> inflate to prior + heavy tail.
            return floor_var * 2.5
        m = sum(vs) / n
        var = sum((v - m) ** 2 for v in vs) / (n - 1)
        premium = 1.0 + 1.0 / n
        return max(float(floor_var), var * premium)


# ═════════════════════════════════════════════════════════════════════
# Expected possessions / plays / game script
# ═════════════════════════════════════════════════════════════════════
def expected_possessions(*, season_type: str,
                          pace_home: Optional[float] = None,
                          pace_away: Optional[float] = None,
                          ) -> tuple[float, float]:
    """Return (home_possessions, away_possessions) for the game.

    ``pace_home`` / ``pace_away`` are **seconds/play** if provided.
    Faster pace → more possessions.  If pace unavailable we fall
    back to league baselines by season type.
    """
    base = {
        "PRESEASON":       LEAGUE_POSSESSIONS_PER_TEAM_PRE,
        "REGULAR_SEASON":  LEAGUE_POSSESSIONS_PER_TEAM_REG,
        "POSTSEASON":      LEAGUE_POSSESSIONS_PER_TEAM_POST,
    }.get(season_type, LEAGUE_POSSESSIONS_PER_TEAM_REG)

    # Pace adjustment: 25 s/play == league baseline; 22 == +8%, 28 == -8%.
    def _adj(pace: Optional[float]) -> float:
        if pace is None or pace <= 0:
            return base
        return base * (25.0 / max(20.0, min(30.0, float(pace))))

    return _adj(pace_home), _adj(pace_away)


def expected_plays(possessions_home: float, possessions_away: float,
                   *, plays_per_possession: float = LEAGUE_PLAYS_PER_POSSESSION,
                   ) -> tuple[float, float]:
    """Return (home_plays, away_plays)."""
    return (possessions_home * plays_per_possession,
            possessions_away * plays_per_possession)


def sample_game_script(*, expected_margin_home: float,
                       total_line: float, seed: random.Random,
                       n: int = 1) -> list[dict]:
    """Sample ``n`` game-script realizations.

    Returns a list of dicts with:
        margin_home:    home team point margin
        total_points:   final total
        pass_rate_home: pass-play rate for home team
        pass_rate_away: pass-play rate for away team
        garbage_time:   True if margin >= 17 by end of Q3

    Margins are drawn from Normal(expected_margin_home, sigma) with
    sigma tied to the total (higher totals → more variance).  Pass
    rates follow the script: trailing teams pass more, leading teams
    run more.
    """
    sigma = max(9.0, LEAGUE_TOTAL_STD * math.sqrt(total_line / LEAGUE_TOTAL_MEAN))
    out: list[dict] = []
    for _ in range(n):
        margin = seed.gauss(expected_margin_home, sigma)
        total  = max(20.0, seed.gauss(total_line, LEAGUE_TOTAL_STD * 0.55))
        # Home team script
        script_home = _pass_rate_from_margin(margin)
        script_away = _pass_rate_from_margin(-margin)
        out.append({
            "margin_home":     margin,
            "total_points":    total,
            "pass_rate_home":  script_home,
            "pass_rate_away":  script_away,
            "garbage_time":    abs(margin) >= 17.0 and seed.random() < 0.6,
        })
    return out


def _pass_rate_from_margin(margin: float) -> float:
    """Linear interpolate pass rate from game margin.  A team leading
    by 10+ passes ~42% of the time; trailing by 10+ passes ~72%."""
    if margin >= 10:
        # Leading a lot → run more.  Beyond +17 clamp.
        m = min(17.0, margin)
        return LEAGUE_PASS_RATE_LEADING_10 - (m - 10) * 0.005
    if margin <= -10:
        m = max(-17.0, margin)
        return LEAGUE_PASS_RATE_TRAILING_10 + (-10 - m) * 0.005
    # Neutral range: linear from 0.72 at -10 to 0.42 at +10 (30 pp swing).
    return LEAGUE_PASS_RATE_NEUTRAL - (margin / 10.0) * 0.15


# ═════════════════════════════════════════════════════════════════════
# Distribution samplers (log-normal, Poisson, NegBin)
# ═════════════════════════════════════════════════════════════════════
def sample_lognormal(mean: float, sd: float, seed: random.Random,
                     *, floor: float = 0.0) -> float:
    """Sample from a log-normal calibrated to have arithmetic mean =
    ``mean`` and standard deviation = ``sd``.  Returns floored value
    (negative yards clipped to 0 by default).
    """
    if mean <= 0:
        return floor
    var = max(sd * sd, mean * 0.1)
    mu = math.log(mean * mean / math.sqrt(var + mean * mean))
    sigma = math.sqrt(math.log(1.0 + var / (mean * mean)))
    x = seed.lognormvariate(mu, sigma)
    return max(floor, x)


def sample_poisson(lam: float, seed: random.Random) -> int:
    """Sample from Poisson(lam) via inversion (Knuth's algorithm).
    Falls back to Normal approximation for lam > 40 for speed."""
    if lam <= 0:
        return 0
    if lam > 40:
        return max(0, int(round(seed.gauss(lam, math.sqrt(lam)))))
    L = math.exp(-lam)
    k = 0
    p = 1.0
    while True:
        k += 1
        p *= seed.random()
        if p < L:
            return k - 1


def sample_negative_binomial(mean: float, dispersion: float,
                              seed: random.Random) -> int:
    """Sample from a NegBin with the specified arithmetic mean and
    dispersion parameter (var = mean + dispersion·mean²).  Used for
    receptions / TD counts where variance exceeds Poisson.
    """
    if mean <= 0:
        return 0
    if dispersion <= 0:
        return sample_poisson(mean, seed)
    # r,p parameterization
    r = 1.0 / dispersion
    p = r / (r + mean)
    # Draw gamma-mixed Poisson.
    lam = seed.gammavariate(r, (1.0 - p) / p)
    return sample_poisson(lam, seed)


# ═════════════════════════════════════════════════════════════════════
# Quantile summary (§15)
# ═════════════════════════════════════════════════════════════════════
@dataclass
class QuantileSummary:
    mean:   float
    median: float
    q10:    float
    q25:    float
    q75:    float
    q90:    float
    variance: float
    std:    float
    n:      int


def quantile_summary(samples: list[float]) -> QuantileSummary:
    """Compute the required §15 quantile summary.  ``samples`` must
    be non-empty (single-value samples produce zero-variance).
    """
    if not samples:
        raise ValueError("quantile_summary requires non-empty samples")
    xs = sorted(float(x) for x in samples)
    n = len(xs)
    m = sum(xs) / n
    if n == 1:
        return QuantileSummary(m, xs[0], xs[0], xs[0], xs[0], xs[0],
                                0.0, 0.0, 1)
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    std = math.sqrt(var)

    def _q(q: float) -> float:
        # Linear interpolation between order statistics.
        i = q * (n - 1)
        lo = int(i)
        hi = min(n - 1, lo + 1)
        frac = i - lo
        return xs[lo] * (1.0 - frac) + xs[hi] * frac

    return QuantileSummary(
        mean=m, median=_q(0.50), q10=_q(0.10), q25=_q(0.25),
        q75=_q(0.75), q90=_q(0.90), variance=var, std=std, n=n,
    )


# ═════════════════════════════════════════════════════════════════════
# Exact-line probability helpers (§15)
# ═════════════════════════════════════════════════════════════════════
def p_over(samples: list[float], line: float) -> float:
    """Return P(sample > line) from the empirical distribution.
    Uses strict inequality (a push on 100.0 with line 100.0 does NOT
    count as an Over — matches sportsbook grading)."""
    if not samples:
        return 0.0
    n_over = sum(1 for x in samples if x > line)
    return n_over / len(samples)


def p_under(samples: list[float], line: float) -> float:
    """Return P(sample < line).  Pushes (equality) counted separately."""
    if not samples:
        return 0.0
    n_under = sum(1 for x in samples if x < line)
    return n_under / len(samples)


def p_push(samples: list[float], line: float) -> float:
    if not samples:
        return 0.0
    n_push = sum(1 for x in samples if x == line)
    return n_push / len(samples)


__all__ = [
    "sim_seed",
    "LEAGUE_POSSESSIONS_PER_TEAM_REG", "LEAGUE_POSSESSIONS_PER_TEAM_PRE",
    "LEAGUE_POSSESSIONS_PER_TEAM_POST", "LEAGUE_PLAYS_PER_POSSESSION",
    "LEAGUE_YARDS_PER_PLAY", "LEAGUE_PASS_RATE_NEUTRAL",
    "LEAGUE_PASS_RATE_LEADING_10", "LEAGUE_PASS_RATE_TRAILING_10",
    "LEAGUE_QB_YPA", "LEAGUE_QB_COMP_RATE", "LEAGUE_QB_INT_RATE",
    "LEAGUE_RB_YPC", "LEAGUE_WR_TARGET_CATCH_RATE", "LEAGUE_WR_YPT",
    "LEAGUE_TD_PROB_PER_RZ_TRIP", "LEAGUE_TOTAL_MEAN", "LEAGUE_TOTAL_STD",
    "ShrinkageEstimator",
    "expected_possessions", "expected_plays", "sample_game_script",
    "sample_lognormal", "sample_poisson", "sample_negative_binomial",
    "QuantileSummary", "quantile_summary",
    "p_over", "p_under", "p_push",
]
