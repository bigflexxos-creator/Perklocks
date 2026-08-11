"""Exact-Threshold Engine — sport-agnostic.

Rules (Phase 5.3 §4, §8):

  OVER:      actual >  threshold  → win
              actual <  threshold  → loss
              actual == threshold  → push  (excluded from decisions)

  UNDER:     actual <  threshold  → win
              actual >  threshold  → loss
              actual == threshold  → push

  MILESTONE (Phase 5.3 §8 — 25+ yards, 1+ hit, Anytime TD):
              actual >= threshold  → hit
              actual <  threshold  → miss
              NEVER push (>= semantics never pushes).

Pushes are EXCLUDED from the decision denominator — never counted
as wins or losses.  hit_rate = wins / decisions.

MISSING DATA (actual is None) is EXCLUDED from ALL counts — never
becomes 0.
"""
from __future__ import annotations

from statistics import median as _stats_median
from typing import Iterable, Optional

from .models import ThresholdResult

# Whole-number lines can push (Over 2.0, Under 1.0 etc.).  Half-lines
# (.5) mathematically cannot push in the strict > / < semantics used
# by every mainstream US sportsbook.  We use a small epsilon to
# tolerate floating-point noise on int-valued lines like 2.0.
PUSH_TOLERANCE = 1e-6

# Minimum sample size before we compute quantiles.  Below this the
# quantile fields remain None (Phase 5.3 §12 sample-size truth).
QUANTILE_MIN_SAMPLE = 3


def _linear_quantile(sorted_vals: list[float], q: float) -> Optional[float]:
    """Standard linear interpolation quantile.  Returns ``None`` when
    the sample is empty."""
    n = len(sorted_vals)
    if n == 0:
        return None
    if n == 1:
        return sorted_vals[0]
    pos = (n - 1) * q
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac


def _variance(vals: list[float]) -> Optional[float]:
    n = len(vals)
    if n < 2:
        return None
    mean = sum(vals) / n
    return sum((v - mean) ** 2 for v in vals) / (n - 1)


def _attach_distribution(result: ThresholdResult) -> None:
    """Populate q25/median/q75/variance from ``result.actual_values``
    when the sample is large enough (>= QUANTILE_MIN_SAMPLE).  Never
    fabricates a value when the sample is too small — the fields
    remain ``None``."""
    if len(result.actual_values) < QUANTILE_MIN_SAMPLE:
        return
    sorted_vals = sorted(result.actual_values)
    result.q25 = _linear_quantile(sorted_vals, 0.25)
    result.median = _stats_median(sorted_vals)
    result.q75 = _linear_quantile(sorted_vals, 0.75)
    result.variance = _variance(sorted_vals)


def _is_push(actual: float, threshold: float) -> bool:
    """Whole-number line equal to actual → push.  Only fires when
    the threshold has no fractional component."""
    if abs(threshold - round(threshold)) > PUSH_TOLERANCE:
        return False   # .5 lines cannot push
    return abs(actual - threshold) <= PUSH_TOLERANCE


def evaluate_threshold(
    actuals: Iterable[Optional[float]],
    threshold: float,
    direction: str = "over",
    *,
    quantiles: bool = True,
) -> ThresholdResult:
    """Evaluate an iterable of raw per-game actuals against a single
    threshold.  ``None`` values are EXCLUDED — never treated as 0.

    When ``quantiles=True`` (default) the result also carries
    q25/median/q75/variance derived from the raw actuals — never
    fabricated when the sample is too small.

    Returns a fresh ThresholdResult.
    """
    if direction not in ("over", "under"):
        raise ValueError(f"direction must be 'over' or 'under', got {direction!r}")
    valid_actuals: list[float] = []
    wins = losses = pushes = 0
    for a in actuals:
        if a is None:
            continue
        try:
            v = float(a)
        except (TypeError, ValueError):
            continue
        valid_actuals.append(v)
        if _is_push(v, threshold):
            pushes += 1
            continue
        if direction == "over":
            if v > threshold:
                wins += 1
            else:
                losses += 1
        else:   # under
            if v < threshold:
                wins += 1
            else:
                losses += 1
    decisions = wins + losses
    result = ThresholdResult(
        wins=wins, losses=losses, pushes=pushes,
        decisions=decisions,
        sample_size=len(valid_actuals),
        hit_rate=(wins / decisions) if decisions > 0 else None,
        average_actual=(sum(valid_actuals) / len(valid_actuals))
                        if valid_actuals else None,
        actual_values=valid_actuals,
    )
    if quantiles:
        _attach_distribution(result)
    return result


def evaluate_milestone(
    actuals: Iterable[Optional[float]],
    threshold: float,
    semantics: str = "gte",
    *,
    quantiles: bool = True,
) -> ThresholdResult:
    """Milestone evaluation (Anytime TD, 1+ Hit, 25+ yards).

    Semantics:
      "gte" — actual >= threshold → hit (default, matches product
              markets "1+ Hit", "25+ yards", "Anytime TD" which are
              inclusive lower bounds).
      "gt"  — actual >  threshold → hit (rare — the strict Over
              semantics used by O/U markets).

    Milestones NEVER push — a whole-number 1+ Hit with actual=1 IS a
    hit, not a push (>= semantics).
    """
    if semantics not in ("gte", "gt"):
        raise ValueError(f"semantics must be 'gte' or 'gt', got {semantics!r}")
    valid_actuals: list[float] = []
    wins = losses = 0
    for a in actuals:
        if a is None:
            continue
        try:
            v = float(a)
        except (TypeError, ValueError):
            continue
        valid_actuals.append(v)
        hit = (v >= threshold) if semantics == "gte" else (v > threshold)
        if hit:
            wins += 1
        else:
            losses += 1
    decisions = wins + losses
    result = ThresholdResult(
        wins=wins, losses=losses, pushes=0,
        decisions=decisions,
        sample_size=len(valid_actuals),
        hit_rate=(wins / decisions) if decisions > 0 else None,
        average_actual=(sum(valid_actuals) / len(valid_actuals))
                        if valid_actuals else None,
        actual_values=valid_actuals,
    )
    if quantiles:
        _attach_distribution(result)
    return result


__all__ = ["evaluate_threshold", "evaluate_milestone",
             "PUSH_TOLERANCE", "QUANTILE_MIN_SAMPLE"]
