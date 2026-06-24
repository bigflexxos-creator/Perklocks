"""Distribution helpers — turn a Monte Carlo sample list into the
percentile summary the Risk Meter UI needs.

Why this exists: the per-sport simulators (sim_mlb / sim_nba /
sim_soccer / sim_tennis) all already produce a `distribution` list of
ints sampled from each player's underlying stat process. The UI wants
to render that distribution as a P10–P90 spread with the betting line
marked, so the user can see WHERE in the player's outcome cone their
bet sits. This module computes the 5-point summary
(P10/P25/P50/P75/P90) plus the % of samples that beat the threshold
either direction.

Keep this file free of sport-specific logic — it should work on ANY
sample list of numbers.
"""
from __future__ import annotations

from typing import Iterable, Optional


def _quantile(sorted_xs: list[float], q: float) -> float:
    """Linear-interpolated quantile (NumPy's default `linear` method).
    `sorted_xs` must already be sorted ascending. `q` in [0, 1].
    """
    if not sorted_xs:
        return 0.0
    n = len(sorted_xs)
    if n == 1:
        return float(sorted_xs[0])
    pos = q * (n - 1)
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return float(sorted_xs[lo]) * (1 - frac) + float(sorted_xs[hi]) * frac


def compute_percentiles(
    distribution: Iterable[float],
    threshold: Optional[float] = None,
) -> dict:
    """Five-number summary plus threshold-relative stats.

    Returns:
      sim_pctl_p10/p25/p50/p75/p90  — quantile values
      sim_pctl_min/max              — actual range of samples
      sim_pctl_n                    — sample count
      sim_pctl_line                 — echo of the threshold (if given)
      sim_pctl_line_quantile_pct    — % of samples ≤ threshold (where
                                      the line falls in the distribution).
                                      Useful for the UI to position the
                                      line marker on the meter.
    """
    xs = sorted(float(x) for x in distribution)
    n = len(xs)
    if n == 0:
        return {
            "sim_pctl_p10": None, "sim_pctl_p25": None, "sim_pctl_p50": None,
            "sim_pctl_p75": None, "sim_pctl_p90": None,
            "sim_pctl_min": None, "sim_pctl_max": None,
            "sim_pctl_n": 0, "sim_pctl_line": threshold,
            "sim_pctl_line_quantile_pct": None,
        }

    out = {
        "sim_pctl_p10": round(_quantile(xs, 0.10), 2),
        "sim_pctl_p25": round(_quantile(xs, 0.25), 2),
        "sim_pctl_p50": round(_quantile(xs, 0.50), 2),
        "sim_pctl_p75": round(_quantile(xs, 0.75), 2),
        "sim_pctl_p90": round(_quantile(xs, 0.90), 2),
        "sim_pctl_min": round(xs[0], 2),
        "sim_pctl_max": round(xs[-1], 2),
        "sim_pctl_n":   n,
    }

    if threshold is not None:
        at_or_below = sum(1 for x in xs if x <= threshold)
        out["sim_pctl_line"] = float(threshold)
        out["sim_pctl_line_quantile_pct"] = round(at_or_below / n * 100, 1)
    else:
        out["sim_pctl_line"] = None
        out["sim_pctl_line_quantile_pct"] = None

    return out
