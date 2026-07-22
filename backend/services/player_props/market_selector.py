"""Market Selector — Phase 3.

Given a player + their archetype + matchup context, decides which
market(s) to actually emit picks for. Prevents emitting Anytime Assist
picks for pure Goal Scorers with 3 season assists, and vice versa.

Also computes a `market_fit` score (0-100) that gets surfaced in the
UI so the user can see "why this player is on this market".
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from .models import Archetype, PlayerStats, MatchupSplit, PickRecommendation
from .archetype_engine import archetype_multiplier
from .goalscorer_model import predict_goal
from .assist_model import predict_assist
from .goal_involvement_model import predict_goal_involvement
from .matchup_intelligence import MatchupContext, apply_matchup_context

logger = logging.getLogger("lockscore.player_props.market_selector")


@dataclass
class MarketRoute:
    """One market emission decision."""
    market: str                    # "anytime_goal_scorer" | ...
    label: str                     # display name
    probability: float             # post-matchup adjustment
    confidence: str                # HIGH | MEDIUM | LOW
    market_fit: int                # 0-100 fit score for archetype+market
    recommendation: PickRecommendation


# Baseline fit scores per archetype × market.
_FIT = {
    Archetype.GOAL_SCORER: {
        "anytime_goal_scorer":       92,
        "anytime_assist":            25,
        "anytime_goal_involvement":  75,
    },
    Archetype.CREATOR: {
        "anytime_goal_scorer":       35,
        "anytime_assist":            92,
        "anytime_goal_involvement":  78,
    },
    Archetype.DUAL_THREAT: {
        "anytime_goal_scorer":       88,
        "anytime_assist":            80,
        "anytime_goal_involvement":  95,
    },
    Archetype.PLAYMAKER: {
        "anytime_goal_scorer":       25,
        "anytime_assist":            85,
        "anytime_goal_involvement":  65,
    },
    Archetype.LOW_INVOLVEMENT: {
        "anytime_goal_scorer":       10,
        "anytime_assist":            10,
        "anytime_goal_involvement":  15,
    },
    Archetype.UNKNOWN: {
        "anytime_goal_scorer":       50,
        "anytime_assist":            50,
        "anytime_goal_involvement":  50,
    },
}


# Per-market probability floors. Below → skip the pick entirely.
_PROB_FLOOR = {
    "anytime_goal_scorer":       0.10,
    "anytime_assist":            0.08,
    "anytime_goal_involvement":  0.15,
}

# Fit score floor to emit a pick. Blocks obvious mismatches (Malachi
# Jones-style defenders in an Anytime Goal Scorer bucket).
_FIT_FLOOR = 30


def _adjust_confidence(base: str, market_fit: int) -> str:
    """Downgrade confidence when market_fit is weak."""
    if market_fit < 40 and base == "HIGH":
        return "MEDIUM"
    if market_fit < 30 and base == "MEDIUM":
        return "LOW"
    return base


def select_markets(stats: PlayerStats,
                   archetype: Archetype,
                   split: Optional[MatchupSplit],
                   matchup_ctx: Optional[MatchupContext] = None
                   ) -> list[MarketRoute]:
    """Return the list of markets this player should be emitted for.

    Ordered by (probability, market_fit) descending.
    """
    if archetype in (Archetype.LOW_INVOLVEMENT, Archetype.UNKNOWN):
        return []

    fits = _FIT.get(archetype, _FIT[Archetype.UNKNOWN])
    routes: list[MarketRoute] = []

    market_models = [
        ("anytime_goal_scorer", "Anytime Goal Scorer",
         predict_goal(stats, split, archetype)),
        ("anytime_assist", "Anytime Assist",
         predict_assist(stats, split, archetype)),
        ("anytime_goal_involvement", "To Score or Assist",
         predict_goal_involvement(stats, split, archetype)),
    ]

    for market, label, rec in market_models:
        if not rec.data_ok:
            continue

        fit = fits.get(market, 50)
        if fit < _FIT_FLOOR:
            continue

        # Apply matchup context multiplier to the base probability.
        if matchup_ctx is not None:
            p = apply_matchup_context(rec.probability, matchup_ctx)
        else:
            p = rec.probability

        if p < _PROB_FLOOR.get(market, 0.10):
            continue

        confidence = _adjust_confidence(rec.confidence, fit)

        # Merge matchup-context evidence into the recommendation.
        if matchup_ctx is not None:
            rec.evidence = list(rec.evidence) + list(matchup_ctx.evidence)
            rec.concerns = list(rec.concerns) + list(matchup_ctx.concerns)
            rec.debug["matchup_mult"] = round(matchup_ctx.total_multiplier(), 4)
            rec.probability = p           # update to post-context value

        routes.append(MarketRoute(
            market=market,
            label=label,
            probability=p,
            confidence=confidence,
            market_fit=fit,
            recommendation=rec,
        ))

    # Sort: highest-fit markets first, then by probability.
    routes.sort(key=lambda r: (r.market_fit, r.probability), reverse=True)
    return routes


def best_market(stats: PlayerStats,
                archetype: Archetype,
                split: Optional[MatchupSplit],
                matchup_ctx: Optional[MatchupContext] = None
                ) -> Optional[MarketRoute]:
    """Return the single best-fit market — for callers who want one pick."""
    routes = select_markets(stats, archetype, split, matchup_ctx)
    return routes[0] if routes else None
