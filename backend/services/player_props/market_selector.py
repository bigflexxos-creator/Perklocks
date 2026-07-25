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

# Feature flag — allows quick rollback if v3 has a data issue in prod.
_USE_V3_GOAL_SCORER = True


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


# ── V3 async variant ────────────────────────────────────────────────
async def select_markets_v3(db,
                             stats: PlayerStats,
                             archetype: Archetype,
                             split: Optional[MatchupSplit],
                             matchup_ctx: Optional[MatchupContext] = None,
                             *,
                             opp_team_name: str = "",
                             sport_key: str = "",
                             is_home: bool = True,
                             lineup_status: str = "unknown",
                             ) -> list[MarketRoute]:
    """V3 variant of `select_markets` — uses the layered GoalScorer Engine
    v3 for the anytime_goal_scorer market, falls back to sync models for
    assist / goal_involvement.

    The v3 engine reads team-strength priors (2 seasons of match data
    per user directive) plus per-player Understat xG/npxG stats and
    runs a correlated Monte Carlo team-goal simulation.
    """
    if archetype in (Archetype.LOW_INVOLVEMENT, Archetype.UNKNOWN):
        return []

    fits = _FIT.get(archetype, _FIT[Archetype.UNKNOWN])
    routes: list[MarketRoute] = []

    # ── Anytime Goal Scorer via v3 ──────────────────────────────────
    if _USE_V3_GOAL_SCORER:
        try:
            from .goal_scorer_v3 import (
                LineupInfo, predict_goal_v3, to_pick_recommendation,
            )
            v3_out = await predict_goal_v3(
                db, stats, opp_team_name,
                sport_key=sport_key,
                is_home=is_home,
                lineup=LineupInfo(status=lineup_status),
                split=split, archetype=archetype,
            )
            rec_g = to_pick_recommendation(v3_out, stats, archetype)
        except Exception as e:
            logger.warning("v3 goal predict failed for %s (%s) — "
                            "falling back to v1: %s",
                            stats.player_name, sport_key, e)
            rec_g = predict_goal(stats, split, archetype)
    else:
        rec_g = predict_goal(stats, split, archetype)

    market_models = [
        ("anytime_goal_scorer", "Anytime Goal Scorer", rec_g),
        ("anytime_assist", "Anytime Assist",
         predict_assist(stats, split, archetype)),
        ("anytime_goal_involvement", "To Score or Assist",
         predict_goal_involvement(stats, split, archetype)),
    ]

    # ── GI recompute using v3 goal-prob ─────────────────────────────
    # The default `predict_goal_involvement` internally calls v1's
    # `predict_goal`, which historically overshoots (Evander example:
    # v1 P(goal)=0.56 vs v3 P(goal)=0.34). To keep the "Score or
    # Assist" market consistent with the v3 anytime scorer engine,
    # we recompute p_gi using the v3 goal-probability + v1 assist-
    # probability with the same Poisson-union math.
    if _USE_V3_GOAL_SCORER and rec_g.data_ok:
        try:
            import math
            p_g_v3 = max(0.001, min(0.95, rec_g.probability))
            rec_gi = market_models[2][2]     # PickRecommendation
            rec_a  = market_models[1][2]
            if rec_gi.data_ok and rec_a.data_ok:
                p_a = max(0.001, min(0.95, rec_a.probability))
                lam_g_v3 = -math.log(1.0 - p_g_v3)
                lam_a    = -math.log(1.0 - p_a)
                lam_ga   = lam_g_v3 + lam_a
                p_model_new = 1.0 - math.exp(-lam_ga)

                # Blend with empirical history when we have games.
                if split and split.matches >= 3:
                    emp = split.gi_rate()
                    w = 0.4 if split.matches < 5 else 0.6
                    p_blend_new = (1.0 - w) * p_model_new + w * emp
                else:
                    p_blend_new = p_model_new

                p_final_new = max(0.03, min(0.90, p_blend_new))
                # Rewrite the GI recommendation with the v3-aligned math.
                rec_gi.probability = round(p_final_new, 4)
                rec_gi.debug.update({
                    "p_goal":    round(p_g_v3, 4),
                    "p_assist":  round(p_a, 4),
                    "lam_g":     round(lam_g_v3, 4),
                    "lam_a":     round(lam_a, 4),
                    "lam_ga":    round(lam_ga, 4),
                    "p_model":   round(p_model_new, 4),
                    "p_blend":   round(p_blend_new, 4),
                    "engine":    "gi_poisson_v3",
                    "p_goal_source": "goal_scorer_v3",
                })
        except Exception as _gi_err:
            logger.debug("GI v3-align skipped: %s", _gi_err)

    for market, label, rec in market_models:
        if not rec.data_ok:
            continue

        fit = fits.get(market, 50)
        if fit < _FIT_FLOOR:
            continue

        # Apply matchup context multiplier — but for v3 goal scorer we
        # already baked team/opp strength INTO the probability. So we
        # only apply a small residual context (form extremes + rest).
        # Same half-weight applies to GI since we recomputed it above
        # using the v3 goal-prob.
        if matchup_ctx is not None:
            if (market in ("anytime_goal_scorer", "anytime_goal_involvement")
                    and _USE_V3_GOAL_SCORER):
                # Half-weight the context so we don't double-count.
                residual = 1.0 + 0.5 * (matchup_ctx.total_multiplier() - 1.0)
                p = max(0.02, min(0.90, rec.probability * residual))
            else:
                p = apply_matchup_context(rec.probability, matchup_ctx)
        else:
            p = rec.probability

        if p < _PROB_FLOOR.get(market, 0.10):
            continue

        confidence = _adjust_confidence(rec.confidence, fit)

        if matchup_ctx is not None:
            rec.evidence = list(rec.evidence) + list(matchup_ctx.evidence)
            rec.concerns = list(rec.concerns) + list(matchup_ctx.concerns)
            rec.debug["matchup_mult"] = round(matchup_ctx.total_multiplier(), 4)
            rec.probability = p

        routes.append(MarketRoute(
            market=market,
            label=label,
            probability=p,
            confidence=confidence,
            market_fit=fit,
            recommendation=rec,
        ))

    routes.sort(key=lambda r: (r.market_fit, r.probability), reverse=True)
    return routes
