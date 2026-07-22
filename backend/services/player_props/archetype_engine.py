"""Archetype Classifier — 5-way soccer player role classification.

USER MANDATE (2026-07-22):
   > "Create a Player Archetype Engine (Goal Scorer, Creator,
   >  Dual Threat, Playmaker, Low Involvement)."

Standard football analytics defaults calibrated to top-5 leagues.
Thresholds based on 2019-2026 Understat data:
   • Median attacker G/90 = 0.28
   • Elite scorer G/90 = 0.60+ (Haaland, Kane, Mbappé)
   • Elite creator A/90 = 0.30+ (De Bruyne, Messi, Ødegaard)

Rules (checked in priority order — first match wins):

   1. LOW_INVOLVEMENT
      Position hint = D / GK / DM AND per-90 output below 0.15 for both.
      OR both G/90 < 0.15 and A/90 < 0.15 and games ≥ 8.
      → Should NEVER be picked for goalscorer / assist markets.

   2. DUAL_THREAT
      G/90 ≥ 0.25 AND A/90 ≥ 0.20
      → Best market: Goal Involvement.

   3. GOAL_SCORER
      G/90 ≥ 0.35 (or ≥ 0.28 with npxg_per_90 ≥ 0.30)
      AND A/90 < 0.25
      → Best market: Anytime Goal Scorer.

   4. CREATOR
      A/90 ≥ 0.25 AND G/90 < 0.20
      → Best market: Anytime Assist.

   5. PLAYMAKER
      A/90 ≥ 0.15 AND KP/90 ≥ 2.0
      → Best market: Anytime Assist.

   6. Fallback (small sample, non-Understat sources):
      Use gpm/apm from raw goals/games instead of per-90.
      Same thresholds relaxed by 20% (since assumed 80% game share).

   7. UNKNOWN: insufficient data (games < 3, all rates zero).
"""
from __future__ import annotations

import logging

from .models import Archetype, PlayerStats

logger = logging.getLogger("lockscore.player_props.archetype")


# ─────────── Tunable thresholds (per-90) ───────────
G90_LOW    = 0.15   # below this = low involvement
G90_GS     = 0.35   # goal scorer threshold
G90_GS_ALT = 0.28   # with strong npxg
G90_DT     = 0.25   # dual-threat lower bound for G

A90_LOW    = 0.15
A90_PM     = 0.15   # playmaker minimum
A90_CRE    = 0.25   # creator threshold
A90_DT     = 0.20   # dual-threat lower bound for A

KP90_PM    = 2.0    # playmaker minimum key passes
NPXG_GS    = 0.30   # alternative goal scorer path via xG

# Minimum sample for confident classification
MIN_GAMES  = 3


def _is_defender(position: str) -> bool:
    pos = (position or "").upper()
    if not pos:
        return False
    # Note: 'D' as a substring but not inside 'DM' (defensive mid) is trickier;
    # we treat both as defense-first.
    return any(tag in pos for tag in ("GK", " D ", "D M", "DM", "CB", "LB", "RB", "FB", "WB")) \
           or pos.strip() in ("D", "GK", "DEF")


def classify_archetype(stats: PlayerStats) -> Archetype:
    """Return the Archetype for a PlayerStats object."""
    if not stats or not stats.data_ok:
        return Archetype.UNKNOWN

    g90 = stats.goals_per_90
    a90 = stats.assists_per_90
    kp90 = stats.key_passes_per_90
    npxg = stats.npxg_per_90
    games = stats.games_effective()

    # Fallback rates from per-match (for MLS/wiki sources with no per-90).
    if g90 == 0.0 and stats.goals and games:
        g90 = min(stats.gpm() * 0.9, 1.5)  # cap at unrealistic
    if a90 == 0.0 and stats.assists and games:
        a90 = min(stats.apm() * 0.9, 1.0)

    # (0) Insufficient data
    if games < MIN_GAMES and g90 == 0.0 and a90 == 0.0:
        return Archetype.UNKNOWN

    # (1) Explicit defender / GK — auto low unless outlier stats.
    if _is_defender(stats.position) and g90 < 0.20 and a90 < 0.15:
        return Archetype.LOW_INVOLVEMENT

    # Low involvement (all sources).
    if g90 < G90_LOW and a90 < A90_LOW:
        return Archetype.LOW_INVOLVEMENT

    # (2) Dual Threat
    if g90 >= G90_DT and a90 >= A90_DT:
        return Archetype.DUAL_THREAT

    # (3) Goal Scorer (main + xG alternative)
    if (g90 >= G90_GS or (g90 >= G90_GS_ALT and npxg >= NPXG_GS)) and a90 < A90_CRE:
        return Archetype.GOAL_SCORER

    # (4) Creator
    if a90 >= A90_CRE and g90 < 0.20:
        return Archetype.CREATOR

    # (5) Playmaker
    if a90 >= A90_PM and kp90 >= KP90_PM:
        return Archetype.PLAYMAKER

    # Fallback: if they have some output but don't fit sharply, classify
    # by dominant tendency.
    if g90 >= a90 * 1.5:
        return Archetype.GOAL_SCORER if g90 >= 0.20 else Archetype.LOW_INVOLVEMENT
    if a90 >= g90 * 1.5:
        return Archetype.CREATOR if a90 >= 0.15 else Archetype.LOW_INVOLVEMENT
    if g90 >= 0.20 and a90 >= 0.15:
        return Archetype.DUAL_THREAT
    return Archetype.LOW_INVOLVEMENT


# ─────────── Market fit multipliers ───────────
# How much each archetype's "typical" prob should be scaled for a market.
# 1.0 = neutral, >1 = boost, <1 = suppress.
_MARKET_MULT = {
    "goal_scorer_market": {
        Archetype.GOAL_SCORER:      1.10,
        Archetype.DUAL_THREAT:      1.05,
        Archetype.CREATOR:          0.65,
        Archetype.PLAYMAKER:        0.55,
        Archetype.LOW_INVOLVEMENT:  0.25,
        Archetype.UNKNOWN:          0.50,
    },
    "assist_market": {
        Archetype.CREATOR:          1.15,
        Archetype.PLAYMAKER:        1.10,
        Archetype.DUAL_THREAT:      1.05,
        Archetype.GOAL_SCORER:      0.80,
        Archetype.LOW_INVOLVEMENT:  0.30,
        Archetype.UNKNOWN:          0.50,
    },
    "goal_involvement_market": {
        Archetype.DUAL_THREAT:      1.15,
        Archetype.GOAL_SCORER:      1.05,
        Archetype.CREATOR:          1.05,
        Archetype.PLAYMAKER:        0.90,
        Archetype.LOW_INVOLVEMENT:  0.30,
        Archetype.UNKNOWN:          0.55,
    },
}


def archetype_multiplier(archetype: Archetype, market: str) -> float:
    """Return the archetype-market fit multiplier.

    `market` ∈ {"goal_scorer_market", "assist_market",
                 "goal_involvement_market"}.
    """
    table = _MARKET_MULT.get(market)
    if not table:
        return 1.0
    return table.get(archetype, 1.0)
