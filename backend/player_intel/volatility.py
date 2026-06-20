"""Volatility + Trend computation from settled picks.

Volatility 0–100 — 100 = most consistent (low variance in hit rate across
recent window).

Usage intensity — derived from how often the player shows up over a lookback:
  * ≥ 12 picks → "high"
  * 5–11 picks → "medium"
  * < 5 picks  → "low"

Trend — last 5/10 hit rate + current streak.
"""
from __future__ import annotations

import math
import statistics


def compute_volatility(results: list[bool]) -> int:
    """Map a sequence of W/L outcomes → 0–100 consistency score.

    Method: variance of a rolling 3-game hit rate. Lower variance → higher
    score. With <4 picks we return 50 (insufficient evidence — "medium").
    """
    n = len(results)
    if n < 4:
        return 50
    windows = [sum(1 for x in results[i:i+3] if x) / 3.0
               for i in range(0, n - 2)]
    if len(windows) < 2:
        return 50
    var = statistics.pvariance(windows)
    # var in [0, 0.25] → soft exponential decay to [0, 100]
    score = 100.0 * math.exp(-var * 12.0)
    return max(0, min(100, int(round(score))))


def classify_usage(n_picks_last_30d: int) -> str:
    if n_picks_last_30d >= 12:
        return "high"
    if n_picks_last_30d >= 5:
        return "medium"
    return "low"


def summarise_trend(results: list[dict]) -> dict:
    """Build trend payload: last5_hit, last10_hit, current_streak."""
    n = len(results)
    if not n:
        return {"last5_hit": None, "last10_hit": None, "current_streak": 0}
    last5 = results[-5:]
    last10 = results[-10:]
    h5 = sum(1 for r in last5 if r.get("won")) / len(last5)
    h10 = sum(1 for r in last10 if r.get("won")) / len(last10)
    streak = 0
    last_won = bool(results[-1].get("won"))
    for r in reversed(results):
        if bool(r.get("won")) == last_won:
            streak += 1 if last_won else -1
        else:
            break
    return {
        "last5_hit":      round(h5, 3),
        "last10_hit":     round(h10, 3),
        "current_streak": streak,
    }
