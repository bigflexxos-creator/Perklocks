"""NBA Prop Simulator — per-possession outcome model anchored to model WP.

Phase B. NBA picks carry one-sided factor ratings (usage, pace, matchup,
form) but no raw opponent stats, so a free-floating Poisson sim can wildly
disagree with the matchup-aware model. We instead CALIBRATE: solve for the
distribution parameters (λ for Poisson props, expected-margin µ for ML) such
that the sim's P(over) ≈ model's win_probability AT THE GIVEN LINE. Then
re-run Monte Carlo to produce:
  • sim_win_probability  — should land within 2-3% of model_wp (consistency)
  • sim_ci_lower/upper   — 95% Wilson CI quantifying variance
  • sim_expected_stat    — the implied projection (e.g. "3.6 assists")
  • sim_alt_lines        — alt-line table at line ± 0.5 / ± 1.0 / ± 1.5

The factors then act as ±5% adjustments around the model-anchored λ — high
form / great matchup nudges λ up, cold form nudges down. Output disagreement
flags genuine inconsistency (small) rather than systematic bias.

Markets routed:
  • Player Points / Rebounds / Assists / 3-Pointers / PRA Over/Under
  • Total Points (game) Over/Under
  • Moneyline / Match Winner
"""
from __future__ import annotations
import math
import random
import re
from typing import Optional

RUNS = 10_000


def _wilson_ci(p: float, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt((p * (1 - p) / n) + (z * z / (4 * n * n))) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def _extract_threshold(market: str) -> float:
    m = re.search(r"(?:over|under)\s+(\d+(?:\.\d+)?)", (market or "").lower())
    return float(m.group(1)) if m else 0.5


def _is_under(market: str) -> bool:
    return "under " in (market or "").lower()


def _poisson(lam: float) -> int:
    if lam <= 0:
        return 0
    if lam > 25:
        return max(0, int(round(random.gauss(lam, math.sqrt(lam)))))
    L = math.exp(-lam)
    k = 0
    p = 1.0
    while True:
        k += 1
        p *= random.random()
        if p <= L:
            return k - 1


def _factor(pick: dict, key: str, default: float = 50.0) -> float:
    f = (pick.get("factors") or {})
    try:
        return float(f.get(key, default))
    except (TypeError, ValueError):
        return default


def _classify_nba_market(market: str) -> str:
    m = (market or "").lower()
    if "3-pointer" in m or "3 pointer" in m or "threes" in m or "three-pointer" in m:
        return "threes"
    if "assists" in m and "points" not in m:
        return "assists"
    if "rebounds" in m and "points" not in m:
        return "rebounds"
    if "pra" in m or ("points + rebounds + assists" in m):
        return "pra"
    if "moneyline" in m:
        return "moneyline"
    if "total points" in m or m.startswith("total ") and "points" in m:
        return "team_total"
    if "points" in m:
        return "points"
    return "unknown"


def _calibrate_lambda(line: float, target_over_prob: float, is_under: bool, count_dist: str = "poisson") -> float:
    """Find λ such that P(X > line) == target (or P(X < line) for under).

    Uses Brent's-method-style bisection on λ ∈ [0.1, line*5]. Works for both
    Poisson (counting stats) and Normal (points/PRA where Normal is used).
    """
    if is_under:
        # If under, target is P(X < line); we want over-prob = 1 - target
        target_over = 1.0 - target_over_prob
    else:
        target_over = target_over_prob

    lo, hi = 0.1, max(line * 3 + 5, 10.0)

    def over_prob(lam: float) -> float:
        if count_dist == "normal":
            sigma = max(2.0, math.sqrt(lam) * 1.5)
            # Survival function of normal at `line`
            from math import erf, sqrt
            z = (line - lam) / sigma
            return 0.5 * (1 - erf(z / sqrt(2)))
        else:
            # Poisson CDF — for line in halves the comparison is integer
            k = int(math.floor(line))
            # P(X > k) = 1 - CDF(k) using Poisson
            cdf = 0.0
            term = math.exp(-lam)
            cdf += term
            for i in range(1, k + 1):
                term *= lam / i
                cdf += term
            return 1.0 - cdf

    # Bisect
    for _ in range(40):
        mid = (lo + hi) / 2
        op = over_prob(mid)
        if abs(op - target_over) < 0.0005:
            return mid
        if op < target_over:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def _factor_adjustment(pick: dict, cat: str) -> float:
    """Returns a small ±5-10% multiplier on calibrated λ from factor ratings.
    Form / usage / matchup tilt the projection slightly above/below the model."""
    if cat in ("moneyline", "team_total"):
        return 1.0
    form = _factor(pick, "Last 10 Hit Rate", 60.0)
    matchup = _factor(pick, "Matchup vs Defense", 50.0)
    usage = _factor(pick, "Recent Volume / Usage", 60.0)
    # Each factor contributes up to ±2%
    adj = 1.0
    adj *= 0.97 + (form / 100.0) * 0.06          # 0.97..1.03
    adj *= 0.98 + (matchup / 100.0) * 0.04       # 0.98..1.02
    adj *= 0.97 + (usage / 100.0) * 0.06         # 0.97..1.03
    return max(0.90, min(1.10, adj))


def simulate_nba_pick(pick: dict) -> Optional[dict]:
    if (pick.get("sport") or "") != "NBA":
        return None
    market = pick.get("market") or ""
    cat = _classify_nba_market(market)
    if cat == "unknown":
        return None

    threshold = _extract_threshold(market)
    is_under = _is_under(market)
    model_wp = float(pick.get("win_probability") or 0) / 100.0
    if model_wp <= 0 or model_wp >= 1:
        # Clamp degenerate WPs
        model_wp = max(0.05, min(0.95, model_wp))

    distribution: list[int] = []

    if cat == "moneyline":
        # Calibrate expected margin µ such that P(N(µ, 12) > 0) == model_wp
        # P(>0) = 1 - Φ(-µ/σ) = Φ(µ/σ) → µ = σ · invΦ(model_wp)
        # Use erfinv approximation
        sigma = 12.0   # typical NBA full-game point std-dev
        # inverse normal CDF
        z = _norminv(model_wp)
        mu = z * sigma
        # Apply small form adjustment
        recent = _factor(pick, "Recent Form (L10)", 50.0)
        mu *= 0.95 + (recent / 100.0) * 0.10
        wins = sum(1 for _ in range(RUNS) if random.gauss(mu, sigma) > 0)
        p_win = wins / RUNS
        ci_lo, ci_hi = _wilson_ci(p_win, RUNS)
        sim_wp_pct = round(p_win * 100, 1)
        disagreement = round(sim_wp_pct - model_wp * 100, 2)
        signal = _signal(disagreement)
        return {
            "sim_win_probability": sim_wp_pct,
            "sim_ci_lower": round(ci_lo * 100, 1),
            "sim_ci_upper": round(ci_hi * 100, 1),
            "sim_runs": RUNS,
            "sim_expected_margin": round(mu, 2),
            "sim_market_category": cat,
            "sim_disagreement_with_model": disagreement,
            "sim_signal": signal,
        }

    elif cat == "team_total":
        sigma = max(10.0, math.sqrt(threshold) * 1.6)
        # Calibrate mu so P(N>line) == target
        target = 1.0 - model_wp if is_under else model_wp
        z = _norminv(target)
        mu = threshold + z * sigma
        wins = sum(1 for _ in range(RUNS) if (random.gauss(mu, sigma) > threshold) != is_under)
        p_win = wins / RUNS
        ci_lo, ci_hi = _wilson_ci(p_win, RUNS)
        sim_wp_pct = round(p_win * 100, 1)
        disagreement = round(sim_wp_pct - model_wp * 100, 2)
        return {
            "sim_win_probability": sim_wp_pct,
            "sim_ci_lower": round(ci_lo * 100, 1),
            "sim_ci_upper": round(ci_hi * 100, 1),
            "sim_runs": RUNS,
            "sim_expected_total": round(mu, 1),
            "sim_market_category": cat,
            "sim_disagreement_with_model": disagreement,
            "sim_signal": _signal(disagreement),
            "sim_threshold": threshold,
            "sim_is_under": is_under,
        }

    # Player counting props
    if cat in ("points", "pra"):
        count_dist = "normal"
    else:
        count_dist = "poisson"

    lam = _calibrate_lambda(threshold, model_wp, is_under, count_dist=count_dist)
    lam *= _factor_adjustment(pick, cat)

    if count_dist == "normal":
        sigma = max(2.0, math.sqrt(lam) * 1.5)
        distribution = [max(0, int(round(random.gauss(lam, sigma)))) for _ in range(RUNS)]
    else:
        distribution = [_poisson(lam) for _ in range(RUNS)]

    wins = sum(1 for x in distribution if (x < threshold if is_under else x > threshold))
    n = len(distribution)
    p_win = wins / n
    ci_lo, ci_hi = _wilson_ci(p_win, n)
    sim_wp_pct = round(p_win * 100, 1)
    disagreement = round(sim_wp_pct - model_wp * 100, 2)

    # Alt-line sensitivity table
    alt_lines: dict = {}
    for delta in (-1.5, -1.0, -0.5, 0.5, 1.0, 1.5):
        alt = round(threshold + delta, 1)
        if alt < 0:
            continue
        over_hits = sum(1 for x in distribution if x > alt)
        alt_lines[str(alt)] = round(over_hits / n * 100, 1)

    expected_stat = sum(distribution) / max(1, len(distribution))

    return {
        "sim_win_probability": sim_wp_pct,
        "sim_ci_lower": round(ci_lo * 100, 1),
        "sim_ci_upper": round(ci_hi * 100, 1),
        "sim_runs": n,
        "sim_threshold": threshold,
        "sim_is_under": is_under,
        "sim_expected_stat": round(expected_stat, 2),
        "sim_lambda": round(lam, 2),
        "sim_alt_lines": alt_lines,
        "sim_market_category": cat,
        "sim_disagreement_with_model": disagreement,
        "sim_signal": _signal(disagreement),
    }


def _signal(disagreement: float) -> str:
    if disagreement > 5:
        return "stronger"
    if disagreement < -5:
        return "weaker"
    return "neutral"


def _norminv(p: float) -> float:
    """Inverse normal CDF (Beasley-Springer-Moro approximation)."""
    p = max(1e-6, min(1 - 1e-6, p))
    # Coefficients
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow = 0.02425
    phigh = 1 - plow
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
                ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
           (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)
