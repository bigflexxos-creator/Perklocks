"""Bet-Type Classification & Weighted ROI.

Spec:
  - odds ≥ -300            → STRAIGHT BET       (1.00 unit)
  - -300 > odds ≥ -500     → REDUCED STAKE BET  (0.50 unit)
  - odds < -500            → PARLAY ONLY        (0.25 unit)

Rationale: heavy chalk (-400, -500, -750) distorts a flat-$100 ROI calc
because the bookmaker assigns very little payout per win. Real sharps
either reduce stake or only use those as parlay legs. Tracking ROI with
weighted units makes the metric match real-world betting behavior.

Public API:
  - classify_bet_type(american_odds) → str   ("STRAIGHT", "REDUCED", "PARLAY")
  - unit_weight(american_odds)       → float (1.00 / 0.50 / 0.25)
  - weighted_units(american_odds, status) → (units_risked, units_profit)

The classification is applied in two places:
  1. When picks are first generated → `bet_type` + `unit_weight` stored.
  2. When picks are settled → `units_risked` & `units_profit` use the
     bet-type-appropriate stake instead of flat 1.0.
"""
from __future__ import annotations

from typing import Optional

# Thresholds (American odds).
STRAIGHT_FLOOR  = -300   # odds ≥ -300 → straight
REDUCED_FLOOR   = -500   # -300 > odds ≥ -500 → reduced stake

# Unit weights.
WEIGHT_STRAIGHT = 1.00
WEIGHT_REDUCED  = 0.50
WEIGHT_PARLAY   = 0.25


def classify_bet_type(odds: float | int | None) -> str:
    """Return the bet-type label for given American odds."""
    if odds is None:
        return "STRAIGHT"
    try:
        o = float(odds)
    except Exception:
        return "STRAIGHT"
    # Positive odds and slight favorites all straight.
    if o >= STRAIGHT_FLOOR:
        return "STRAIGHT"
    if o >= REDUCED_FLOOR:
        return "REDUCED"
    return "PARLAY"


def unit_weight(odds: float | int | None) -> float:
    """Stake weight (multiplier on 1 unit) for given American odds."""
    bt = classify_bet_type(odds)
    if bt == "STRAIGHT":
        return WEIGHT_STRAIGHT
    if bt == "REDUCED":
        return WEIGHT_REDUCED
    return WEIGHT_PARLAY


def american_decimal_payout(american: float | int) -> float:
    """Net profit per $1 stake at given American odds (excludes returned stake)."""
    try:
        a = float(american)
    except Exception:
        return 0.0
    if a >= 100:
        return a / 100.0
    if a <= -100:
        return 100.0 / abs(a)
    return 0.0


def weighted_units(odds: float | int | None, status: str) -> tuple[float, float]:
    """Return (units_risked, units_profit) weighted by bet-type stake.

    status: "won" | "lost" | "push" | None.
    """
    w = unit_weight(odds)
    if status == "push" or status is None:
        return (0.0 if status == "push" else w, 0.0)
    if status == "won":
        return (w, w * american_decimal_payout(odds))
    # lost
    return (w, -w)
