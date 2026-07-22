"""Anytime Goal Scorer Model.

Given a `PlayerStats` (and optional `MatchupSplit`), returns the
per-match probability that the player scores at least one goal.

Formula:
    base   = min(g/90 * 0.95, 0.75)      — per-90 → per-match approx
    form   = 1.0 + ((form_score-50)/50) * 0.15    (±15%)
    match  = 1.0 + clamp(matchup_bonus, -0.15, +0.25)
    arch   = archetype_multiplier(archetype, "goal_scorer_market")
    prob   = clamp(base * form * match * arch, 0.02, 0.85)

Confidence:
    HIGH   — games ≥ 15 AND (source=understat OR games ≥ 20)
    MEDIUM — games ≥ 8
    LOW    — games ≥ 3
    (games < 3 → data_ok=False)
"""
from __future__ import annotations

import logging
from typing import Optional

from .archetype_engine import archetype_multiplier, classify_archetype
from .models import Archetype, MatchupSplit, PickRecommendation, PlayerStats

logger = logging.getLogger("lockscore.player_props.goalscorer")


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _base_prob(stats: PlayerStats) -> float:
    """Per-match goal probability approximation."""
    # Prefer per-90 (Understat/MLS-derived).
    if stats.goals_per_90 > 0:
        return min(stats.goals_per_90 * 0.95, 0.75)
    # Fallback: goals/games.
    return min(stats.gpm() * 0.90, 0.72)


def _form_multiplier(form_score: float) -> float:
    """form_score is 0-100 (50 = neutral). Range: ±15%."""
    delta = (form_score - 50.0) / 50.0  # -1..+1
    return 1.0 + _clamp(delta, -1.0, 1.0) * 0.15


def _matchup_multiplier(split: Optional[MatchupSplit],
                        stats: PlayerStats) -> tuple[float, list[str]]:
    """History vs opponent adjustment.

    If gpm_vs_opp > season gpm  →  boost.
    If matches ≥ 2 and no goals →  penalty.
    """
    evidence: list[str] = []
    if not split or split.matches < 2:
        return 1.0, evidence

    season_gpm = stats.gpm() or (stats.goals_per_90 / 1.05 if stats.goals_per_90 else 0.3)
    opp_gpm = split.gpm()
    delta = opp_gpm - season_gpm

    # Clamp scale
    mult = 1.0 + _clamp(delta * 0.5, -0.15, 0.25)

    if opp_gpm >= 1.0:
        evidence.append(
            f"🔥 {split.goals}G in {split.matches} vs {split.opponent} "
            f"({opp_gpm:.2f} G/match — dominates matchup)"
        )
    elif opp_gpm >= 0.5:
        evidence.append(
            f"🎯 {split.goals}G in {split.matches} vs {split.opponent} "
            f"({opp_gpm:.2f} G/match)"
        )
    elif split.goals == 0 and split.matches >= 3:
        evidence.append(
            f"❄️ 0G in {split.matches} vs {split.opponent} "
            f"(historical block)"
        )

    return mult, evidence


def _confidence(stats: PlayerStats) -> tuple[str, bool]:
    games = stats.games_effective()
    if games < 3:
        return "LOW", False
    if games >= 15 and (stats.source == "understat" or games >= 20):
        return "HIGH", True
    if games >= 8:
        return "MEDIUM", True
    return "LOW", True


def predict_goal(stats: PlayerStats,
                 split: Optional[MatchupSplit] = None,
                 archetype: Optional[Archetype] = None
                 ) -> PickRecommendation:
    """Predict P(anytime goal) for one player."""
    if not stats or not stats.data_ok:
        return PickRecommendation(
            market="anytime_goal_scorer",
            player_name=(stats.player_name if stats else "unknown"),
            probability=0.0,
            confidence="LOW",
            archetype=Archetype.UNKNOWN,
            data_ok=False,
            concerns=["no player stats available"],
        )
    if archetype is None:
        archetype = classify_archetype(stats)

    base = _base_prob(stats)
    form_m = _form_multiplier(stats.form_score)
    match_m, match_ev = _matchup_multiplier(split, stats)
    arch_m = archetype_multiplier(archetype, "goal_scorer_market")

    raw = base * form_m * match_m * arch_m
    prob = round(_clamp(raw, 0.02, 0.85), 4)

    confidence, data_ok = _confidence(stats)
    if archetype in (Archetype.LOW_INVOLVEMENT, Archetype.UNKNOWN):
        confidence = "LOW"

    evidence: list[str] = []
    if stats.goals_per_90 >= 0.35:
        evidence.append(
            f"⚡ Elite scoring rate: {stats.goals_per_90:.2f} G/90 "
            f"({stats.goals}G in {stats.games} games)"
        )
    elif stats.goals_per_90 >= 0.25:
        evidence.append(
            f"⚡ Strong scoring rate: {stats.goals_per_90:.2f} G/90 "
            f"({stats.goals}G in {stats.games} games)"
        )
    elif stats.goals >= 5 and stats.gpm() >= 0.35:
        evidence.append(
            f"⚡ {stats.goals}G in {stats.games} games "
            f"({stats.gpm():.2f} G/match)"
        )
    if stats.form_score >= 70:
        evidence.append(f"🔥 Hot form ({stats.form_label}, {stats.form_score:.0f}/100)")
    elif stats.form_score <= 30:
        evidence.append(f"❄️ Cold form ({stats.form_label}, {stats.form_score:.0f}/100)")
    if stats.npxg_per_90 >= 0.30:
        evidence.append(f"📈 High xG generation: {stats.npxg_per_90:.2f} npxG/90")
    evidence.extend(match_ev)
    evidence.append(f"🏷 Archetype: {archetype.display()}")

    concerns: list[str] = []
    if archetype == Archetype.LOW_INVOLVEMENT:
        concerns.append("Low-involvement archetype — not a scorer profile")
    if stats.games_effective() < 8:
        concerns.append(f"Small sample: only {stats.games_effective()} games this season")
    if stats.goals < 3:
        concerns.append(f"Only {stats.goals} season goals — thin scoring history")

    return PickRecommendation(
        market="anytime_goal_scorer",
        player_name=stats.player_name,
        probability=prob,
        confidence=confidence,
        archetype=archetype,
        data_ok=data_ok,
        evidence=evidence,
        concerns=concerns,
        debug={
            "base": round(base, 4),
            "form_mult": round(form_m, 4),
            "match_mult": round(match_m, 4),
            "arch_mult": round(arch_m, 4),
            "source": stats.source,
        },
    )
