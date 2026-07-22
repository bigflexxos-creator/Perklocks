"""Anytime Goal Involvement Model (G + A either).

Given the outputs of the goalscorer and assist models plus optional
matchup history, returns the per-match probability of at least one
goal OR one assist.

Uses a correlation-adjusted union:
    p_gi = p_g + p_a - p_g * p_a * (1 - ρ)      ρ ≈ 0.35 for attackers

Rationale for correlation:
   • A player who scores often also assists more (both hinge on being
     involved in the final third). Empirically ρ ≈ 0.30-0.40 in the
     top-5 leagues.
   • For pure goal scorers or pure creators, ρ drops to ~0.10.
   • For Dual Threats, ρ can climb to ~0.45.

If we have a `MatchupSplit` with matches ≥ 3, we blend the model's
computed probability with the empirical `gi_rate()` (60/40 weight
in favor of the empirical when matches ≥ 5).
"""
from __future__ import annotations

import logging
from typing import Optional

from .archetype_engine import archetype_multiplier, classify_archetype
from .assist_model import predict_assist
from .goalscorer_model import predict_goal
from .models import Archetype, MatchupSplit, PickRecommendation, PlayerStats

logger = logging.getLogger("lockscore.player_props.gi")


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


# Archetype → correlation between goal and assist events per match.
_RHO = {
    Archetype.DUAL_THREAT:      0.45,
    Archetype.GOAL_SCORER:      0.20,
    Archetype.CREATOR:          0.25,
    Archetype.PLAYMAKER:        0.20,
    Archetype.LOW_INVOLVEMENT:  0.15,
    Archetype.UNKNOWN:          0.30,
}


def predict_goal_involvement(stats: PlayerStats,
                              split: Optional[MatchupSplit] = None,
                              archetype: Optional[Archetype] = None
                              ) -> PickRecommendation:
    """Predict P(goal OR assist) for one player."""
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

    p_g = g.probability
    p_a = a.probability
    rho = _RHO.get(archetype, 0.30)

    # Correlated union: p_gi = p_g + p_a - p_g * p_a * (1 - ρ)
    # (Reduces double-count when ρ high.)
    p_model = p_g + p_a - p_g * p_a * (1.0 - rho)

    # Blend with empirical history when we have enough games.
    if split and split.matches >= 3:
        emp = split.gi_rate()
        w = 0.4 if split.matches < 5 else 0.6
        p_blend = (1.0 - w) * p_model + w * emp
    else:
        p_blend = p_model

    # Archetype market multiplier (fine-tunes for market fit)
    arch_m = archetype_multiplier(archetype, "goal_involvement_market")
    p_final = _clamp(p_blend * arch_m, 0.03, 0.90)

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
        gi_ct = split.scored_matches + split.assist_matches
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
            "p_goal": round(p_g, 4),
            "p_assist": round(p_a, 4),
            "rho": rho,
            "p_model": round(p_model, 4),
            "p_blend": round(p_blend, 4),
            "arch_mult": round(arch_m, 4),
            "source": stats.source,
        },
    )
