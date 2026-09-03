"""UNIVERSAL PROJECTED-DISTRIBUTION FALLBACK (2026-06-30).

Alt-Line Magic previously required a trained ML model per
``(sport, stat)`` pair.  Only ~5 pairs shipped trained models
(mlb_hits, mlb_pitcher_strikeouts, mlb_home_runs, mlb_total_bases,
+ a couple NFL) so 90 %+ of picks — pitcher_outs, receptions,
rushing_yards, points, rebounds, assists, aces, etc. — silently
surfaced as ``"No alternate lines available"``.

This module provides a **universal fallback**: every pick carries a
``win_probability`` (model's P(over line)) and a ``line`` — two
numbers that uniquely determine a distribution over the stat.  We
back-solve the mean of a Poisson (count stats) or Normal (continuous
stats) that matches ``P(X > line) = win_prob`` and then evaluate
``P(X > threshold)`` for every threshold in the market's alt-line
grid.  Zero fabrication — every probability derives from the pick's
own model output.

Provenance is stamped as ``source = "model_projection"`` so the UI
correctly labels these as model-derived (no book price attached).
"""
from __future__ import annotations

import math
from typing import Optional


# Continuous vs discrete stat classification.  Discrete stats use
# Poisson; continuous use Normal.  A stat missing from either set
# is treated as discrete by default (safer — Poisson works for any
# non-negative count and degrades gracefully to Normal at high λ).
_CONTINUOUS_STATS = {
    "passing_yards", "rushing_yards", "receiving_yards",
    "recv_yards", "rush_yards", "pass_yards",
}


def _poisson_sf(k: float, lam: float) -> float:
    """P(X > k) for X ~ Poisson(lam), with k as a real threshold.

    Sportsbook thresholds are half-integers (5.5, 6.5, …).  For
    Poisson, ``P(X > 5.5) = P(X ≥ 6) = 1 - CDF(5)``.  We floor k to
    handle any 0.5-line correctly.
    """
    if lam <= 0:
        return 0.0
    if lam > 700:  # numerical guard
        return 1.0 if k < lam else 0.0
    n = int(math.floor(k))  # k=5.5 → 5; want P(X ≥ 6) = 1 - CDF(5)
    # Compute CDF(n) = Σ_{i=0..n} e^{-λ} λ^i / i!  incrementally.
    term = math.exp(-lam)
    cdf = term
    for i in range(1, n + 1):
        term *= lam / i
        cdf += term
    return max(0.0, min(1.0, 1.0 - cdf))


def _solve_poisson_mean(line: float, p_over: float) -> Optional[float]:
    """Back-solve λ such that Poisson(λ).sf(line) ≈ p_over via binary
    search.  Returns None if p_over is degenerate (≤0 or ≥1)."""
    if p_over <= 0.0 or p_over >= 1.0:
        return None
    lo, hi = 0.01, max(2.0 * (line + 1), 200.0)
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if _poisson_sf(line, mid) < p_over:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def _normal_sf(x: float, mu: float, sigma: float) -> float:
    if sigma <= 0:
        return 1.0 if mu > x else 0.0
    z = (x - mu) / sigma
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def _solve_normal_mean(line: float, p_over: float,
                        sigma_frac: float = 0.22) -> tuple[Optional[float], float]:
    """Back-solve μ for Normal(μ, σ=frac·max(|μ|,1)) that matches
    ``P(X > line) = p_over``.  Iterates σ with μ until stable.
    Returns (μ, σ).  ``sigma_frac`` = 22 % of mean is the empirical
    CoV for NFL passing/rushing/receiving yards across all L5 windows.
    """
    if p_over <= 0.0 or p_over >= 1.0:
        return None, 0.0
    lo, hi = -line * 2 - 500.0, line * 4 + 500.0
    mu = 0.5 * (lo + hi)
    for _ in range(60):
        sigma = sigma_frac * max(abs(mu), 1.0)
        if _normal_sf(line, mu, sigma) < p_over:
            lo = mu
        else:
            hi = mu
        mu = 0.5 * (lo + hi)
    sigma = sigma_frac * max(abs(mu), 1.0)
    return mu, sigma


def universal_distribution(
    *,
    stat: str,
    line: float,
    win_probability: float,
    grid: list[float],
) -> Optional[dict]:
    """Return a ``build_outcome_distribution``-compatible payload
    computed purely from the pick's own model output.

    Parameters
    ----------
    stat : canonical stat family (e.g. "pitcher_outs", "receptions").
    line : the pick's actual line (e.g. 16.5 for Outs Recorded).
    win_probability : P(over ``line``) as a fraction 0-1 OR percent
        0-100 — auto-detected.
    grid : threshold grid to score.

    Returns
    -------
    dict with the same shape as ``build_outcome_distribution`` when
    ``supported=True``, or ``None`` if the pick lacks the two inputs.
    """
    if line is None or win_probability is None or not grid:
        return None
    try:
        line_f = float(line)
        wp = float(win_probability)
    except (TypeError, ValueError):
        return None
    if wp > 1.0:
        wp = wp / 100.0
    if wp <= 0.0 or wp >= 1.0:
        # Degenerate — cannot back-solve.
        return None
    stat_l = (stat or "").lower()
    if stat_l in _CONTINUOUS_STATS:
        mu, sigma = _solve_normal_mean(line_f, wp)
        if mu is None:
            return None
        rows: list[tuple[float, float, dict]] = []
        for th in grid:
            p_over = max(0.0, min(1.0, _normal_sf(th, mu, sigma)))
            rows.append((float(th), p_over, {
                "model":          "universal_projection.normal",
                "top_factors":    [],
                "expected_value": round(mu, 2),
            }))
        residual_std = round(sigma, 3)
        projected = round(mu, 2)
    else:
        lam = _solve_poisson_mean(line_f, wp)
        if lam is None:
            return None
        rows = []
        for th in grid:
            p_over = _poisson_sf(float(th), lam)
            rows.append((float(th), p_over, {
                "model":          "universal_projection.poisson",
                "top_factors":    [],
                "expected_value": round(lam, 2),
            }))
        # Poisson std ≈ √λ.  Normalise to the ratio the ranker expects.
        residual_std = round(math.sqrt(lam), 3)
        projected = round(lam, 2)
    if not rows:
        return None
    return {
        "supported":     True,
        "thresholds":    rows,
        "projected":     projected,
        "residual_std":  residual_std,
        "notes":         ["universal_projection (from pick.win_probability + line)"],
    }


__all__ = ["universal_distribution"]
