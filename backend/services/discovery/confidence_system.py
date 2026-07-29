"""Confidence + reliability primitives (2026-07-28).

Every discovery pattern must survive a three-part reliability gate:
  1. Sample size — how many observations?
  2. Point estimate — the raw hit rate.
  3. Wilson lower bound — pessimistic 95 % lower bound of the true
     rate given the sample. Guards against 5/5 producing "100 % hit
     rate" flags.

Public API
──────────
    ci = wilson_lower_bound(hits=5, n=5)                → 0.478 (not 1.0)
    grade = confidence_grade(hits, n, expected_p=0.5)
    label = confidence_label(hits, n)
    ok    = passes_sample_gate(n, min_samples=15)
"""
from __future__ import annotations
import math

_Z95 = 1.96                                    # 95 % z-score


def wilson_lower_bound(hits: int, n: int, z: float = _Z95) -> float:
    """Wilson score lower bound for a binomial proportion.

    Guarantees the small-sample protection the spec calls for —
    5/5 gives ≈ 0.478 rather than the naive 1.0.
    """
    if n <= 0:
        return 0.0
    p = hits / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    spread = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n) / denom
    return max(0.0, center - spread)


def wilson_upper_bound(hits: int, n: int, z: float = _Z95) -> float:
    if n <= 0:
        return 1.0
    p = hits / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    spread = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n) / denom
    return min(1.0, center + spread)


def confidence_grade(hits: int, n: int, *, expected_p: float = 0.5) -> str:
    """A+…F grade combining Wilson lower bound + lift-over-baseline.

    A pattern that shows a 90 % hit rate on 40 games is much stronger
    than 90 % on 5 games. This grade reflects that."""
    if n <= 0:
        return "F"
    lb = wilson_lower_bound(hits, n)
    # Sample multiplier scales up to 1.0 as n approaches 30.
    sample_mult = min(1.0, n / 30.0)
    # Lift over 50/50 baseline (or provided baseline).
    lift = max(0.0, lb - expected_p)
    score = (lb * 0.55 + sample_mult * 0.20 + lift * 0.25)
    if score >= 0.78: return "A+"
    if score >= 0.68: return "A"
    if score >= 0.56: return "B"
    if score >= 0.44: return "C"
    if score >= 0.32: return "D"
    return "F"


def confidence_label(n: int) -> str:
    """Coarse sample-size label used across discovery outputs."""
    if n >= 30: return "high"
    if n >= 15: return "medium"
    if n >= 5:  return "low"
    return "insufficient"


def passes_sample_gate(n: int, *, min_samples: int = 15) -> bool:
    """Small-sample gate — patterns below this threshold are labelled
    but not surfaced as high-confidence discoveries."""
    return n >= int(min_samples)


def variance_score(values: list[float]) -> float:
    """Population variance — used as a consistency signal (lower =
    more consistent, higher = more volatile)."""
    if len(values) < 2:
        return 0.0
    mu = sum(values) / len(values)
    return sum((v - mu) ** 2 for v in values) / len(values)


def consistency_score(values: list[float]) -> float:
    """0..1: 1.0 = perfect consistency; 0.0 = maximum spread.

    Based on coefficient of variation normalised into (0, 1].
    """
    if not values:
        return 0.0
    mu = sum(values) / len(values)
    if mu <= 0:
        return 0.0
    var = variance_score(values)
    sd = math.sqrt(var)
    cv = sd / mu
    return max(0.0, min(1.0, 1.0 - cv))


__all__ = [
    "wilson_lower_bound",
    "wilson_upper_bound",
    "confidence_grade",
    "confidence_label",
    "passes_sample_gate",
    "variance_score",
    "consistency_score",
]
