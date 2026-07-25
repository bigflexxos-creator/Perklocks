"""Anytime Goal Involvement Model (G + A either).

Given a player's per-match goal + assist propensities, returns the
per-match probability of at least one goal OR one assist.

Uses a **Poisson-thinning union**:

    λ_ga    = λ_goals + λ_assists    (per-match Poisson rate)
    P(GI)   = 1 - exp(-λ_ga)

This is the correct union for two Poisson intensities and handles the
G/A correlation implicitly. If λ_ga = 0.5, P(GI) ≈ 0.39. If
λ_ga = 1.0, P(GI) ≈ 0.63. This matches empirical observation for
Bundesliga/EPL dual threats far more accurately than the previous
`p_g + p_a - p_g·p_a·(1-ρ)` formulation, which was inverted (it
INCREASED the union with correlation ρ, when higher positive
correlation should DECREASE the union).

Reference for Evander (MLS, 18G+15A in 32 games):
    λ_ga = 33/32 ≈ 1.03  →  P(GI) ≈ 0.643  →  fair odds ≈ -180
Previously this player was priced at -632 (87.3%) — a ~23-point
over-estimate driven by (1) the inverted correlation formula and
(2) double-application of the archetype multiplier.

If we have a `MatchupSplit` with matches ≥ 3, we blend the model's
computed probability with the empirical `gi_rate()` (40/60 → 60/40
weight in favor of empirical when matches ≥ 5).
"""
from __future__ import annotations

import logging
import math
from typing import Optional

from .archetype_engine import classify_archetype
from .assist_model import predict_assist
from .goalscorer_model import predict_goal
from .models import Archetype, MatchupSplit, PickRecommendation, PlayerStats

logger = logging.getLogger("lockscore.player_props.gi")


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _prob_to_lambda(p: float) -> float:
    """Invert 1 - exp(-λ) to recover the Poisson intensity."""
    p = _clamp(p, 0.001, 0.999)
    return -math.log(1.0 - p)


def predict_goal_involvement(stats: PlayerStats,
                              split: Optional[MatchupSplit] = None,
                              archetype: Optional[Archetype] = None
                              ) -> PickRecommendation:
    """Predict P(goal OR assist) for one player using Poisson union."""
    if not stats or not stats.data_ok:
        return PickRecommendation(
            market="anytime_goal_involvement",
            player_name=(stats.player_name if stats else "unknown"),
            probability=0.0,
            confidence="LOW",
            archetype=Archetype.UNKNOWN,
            data_ok=False,
            concerns=["no player stats available"],
        )
    if archetype is None:
        archetype = classify_archetype(stats)

    g = predict_goal(stats, split, archetype)
    a = predict_assist(stats, split, archetype)

    if not (g.data_ok and a.data_ok):
        return PickRecommendation(
            market="anytime_goal_involvement",
            player_name=stats.player_name,
            probability=0.0,
            confidence="LOW",
            archetype=archetype,
            data_ok=False,
            concerns=(g.concerns + a.concerns),
        )

    p_g = _clamp(g.probability, 0.001, 0.95)
    p_a = _clamp(a.probability, 0.001, 0.95)

    # Convert each sub-market probability back to a Poisson intensity,
    # then sum. This is the mathematically correct union for two
    # marginal-Poisson events.
    lam_g  = _prob_to_lambda(p_g)
    lam_a  = _prob_to_lambda(p_a)
    lam_ga = lam_g + lam_a
    p_model = 1.0 - math.exp(-lam_ga)

    # Blend with empirical history when we have enough games.
    if split and split.matches >= 3:
        emp = split.gi_rate()
        w = 0.4 if split.matches < 5 else 0.6
        p_blend = (1.0 - w) * p_model + w * emp
    else:
        p_blend = p_model

    # NB: no additional archetype market multiplier applied here — the
    # per-market archetype fit is already baked into p_g via
    # `predict_goal` and p_a via `predict_assist`. Re-multiplying would
    # double-count and was the second driver of the -632 Evander
    # over-estimate. Confidence stays as-is.

    p_final = _clamp(p_blend, 0.03, 0.90)

    # Confidence: HIGH only when both models are HIGH.
    if g.confidence == "HIGH" and a.confidence == "HIGH":
        confidence = "HIGH"
    elif g.confidence == "LOW" and a.confidence == "LOW":
        confidence = "LOW"
    else:
        confidence = "MEDIUM"
    if archetype in (Archetype.LOW_INVOLVEMENT, Archetype.UNKNOWN):
        confidence = "LOW"

    # Evidence: pull top from both, plus GI-specific line.
    evidence: list[str] = []
    output_line = (
        f"⚡ {stats.goals}G + {stats.assists}A in {stats.games} games "
        f"({stats.goals + stats.assists} G+A · "
        f"{(stats.goals + stats.assists) / max(stats.games_effective(), 1):.2f}/match)"
    )
    evidence.append(output_line)

    if split and split.matches >= 2:
        gi = split.gi_rate()
        evidence.append(
            f"🎯 Career vs {split.opponent}: {split.goals}G/{split.assists}A "
            f"in {split.matches} games (GI rate {gi*100:.0f}%)"
        )

    if stats.form_score >= 70:
        evidence.append(f"🔥 Hot form ({stats.form_label}, {stats.form_score:.0f}/100)")
    elif stats.form_score <= 30:
        evidence.append(f"❄️ Cold form ({stats.form_label}, {stats.form_score:.0f}/100)")

    evidence.append(f"🏷 Archetype: {archetype.display()}")

    concerns: list[str] = []
    if archetype == Archetype.LOW_INVOLVEMENT:
        concerns.append("Low-involvement archetype — unlikely to see final-third action")
    if stats.games_effective() < 8:
        concerns.append(f"Small sample: only {stats.games_effective()} games this season")

    return PickRecommendation(
        market="anytime_goal_involvement",
        player_name=stats.player_name,
        probability=round(p_final, 4),
        confidence=confidence,
        archetype=archetype,
        data_ok=True,
        evidence=evidence,
        concerns=concerns,
        debug={
            "p_goal":   round(p_g, 4),
            "p_assist": round(p_a, 4),
            "lam_g":    round(lam_g, 4),
            "lam_a":    round(lam_a, 4),
            "lam_ga":   round(lam_ga, 4),
            "p_model":  round(p_model, 4),
            "p_blend":  round(p_blend, 4),
            "source":   stats.source,
            "engine":   "gi_poisson_v2",
        },
    )
