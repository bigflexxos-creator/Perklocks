"""Anytime Assist Model.

Given a `PlayerStats` (and optional `MatchupSplit`), returns the
per-match probability that the player records at least one assist.

Formula:
    base   = min(a/90 * 0.90, 0.72)
    kp_boost  = 1.0 + clamp((kp90 - 2.0) * 0.05, 0.0, 0.15)   # creator bonus
    form      = 1.0 + ((form_score-50)/50) * 0.10       (±10% — form matters
                                                          less for assists)
    match     = 1.0 + clamp(matchup_bonus, -0.15, +0.25)
    arch      = archetype_multiplier(archetype, "assist_market")
    prob      = clamp(base * kp_boost * form * match * arch, 0.02, 0.75)
"""
from __future__ import annotations

import logging
from typing import Optional

from .archetype_engine import archetype_multiplier, classify_archetype
from .models import Archetype, MatchupSplit, PickRecommendation, PlayerStats

logger = logging.getLogger("lockscore.player_props.assist")


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _base_prob(stats: PlayerStats) -> float:
    """Convert per-match assist intensity → P(≥1 assist per match).

    Uses the Poisson-tail identity  P(N≥1) = 1 - e^-λ  where λ is the
    per-match assist rate. Previous linear approximation
    (`a/90 * 0.9`) systematically over-estimated for high-A/90 players
    (Olise A/90=0.75 → 0.68 vs correct 0.53).
    """
    import math
    if stats.assists_per_90 > 0:
        # A/90 is already an intensity per 90-minute match — use directly.
        lam = min(1.5, stats.assists_per_90)
    else:
        lam = min(1.5, stats.apm())
    if lam <= 0:
        return 0.02
    return min(0.65, 1.0 - math.exp(-lam))


def _kp_boost(kp90: float) -> float:
    """Chances-created bonus for high-key-pass creators."""
    if kp90 <= 0:
        return 1.0
    return 1.0 + _clamp((kp90 - 2.0) * 0.05, 0.0, 0.15)


def _form_multiplier(form_score: float) -> float:
    delta = (form_score - 50.0) / 50.0
    return 1.0 + _clamp(delta, -1.0, 1.0) * 0.10


def _matchup_multiplier(split: Optional[MatchupSplit],
                        stats: PlayerStats) -> tuple[float, list[str]]:
    evidence: list[str] = []
    if not split or split.matches < 2:
        return 1.0, evidence

    season_apm = stats.apm() or (stats.assists_per_90 / 1.05 if stats.assists_per_90 else 0.15)
    opp_apm = split.apm()
    delta = opp_apm - season_apm
    mult = 1.0 + _clamp(delta * 0.6, -0.15, 0.25)

    if opp_apm >= 0.6:
        evidence.append(
            f"🎯 {split.assists}A in {split.matches} vs {split.opponent} "
            f"({opp_apm:.2f} A/match — creator vs this side)"
        )
    elif opp_apm >= 0.3:
        evidence.append(
            f"🎯 {split.assists}A in {split.matches} vs {split.opponent} "
            f"({opp_apm:.2f} A/match)"
        )
    elif split.assists == 0 and split.matches >= 3:
        evidence.append(
            f"❄️ 0A in {split.matches} vs {split.opponent}"
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


def predict_assist(stats: PlayerStats,
                   split: Optional[MatchupSplit] = None,
                   archetype: Optional[Archetype] = None
                   ) -> PickRecommendation:
    """Predict P(anytime assist) for one player."""
    if not stats or not stats.data_ok:
        return PickRecommendation(
            market="anytime_assist",
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
    kp_m = _kp_boost(stats.key_passes_per_90)
    form_m = _form_multiplier(stats.form_score)
    match_m, match_ev = _matchup_multiplier(split, stats)
    arch_m = archetype_multiplier(archetype, "assist_market")

    raw = base * kp_m * form_m * match_m * arch_m
    prob = round(_clamp(raw, 0.02, 0.65), 4)

    confidence, data_ok = _confidence(stats)
    if archetype in (Archetype.LOW_INVOLVEMENT, Archetype.UNKNOWN):
        confidence = "LOW"

    evidence: list[str] = []
    if stats.assists_per_90 >= 0.30:
        evidence.append(
            f"🎯 Elite creator: {stats.assists_per_90:.2f} A/90 "
            f"({stats.assists}A in {stats.games} games)"
        )
    elif stats.assists_per_90 >= 0.20:
        evidence.append(
            f"🎯 Strong creator: {stats.assists_per_90:.2f} A/90 "
            f"({stats.assists}A in {stats.games} games)"
        )
    elif stats.assists >= 5 and stats.apm() >= 0.30:
        evidence.append(
            f"🎯 {stats.assists}A in {stats.games} games "
            f"({stats.apm():.2f} A/match)"
        )
    if stats.key_passes_per_90 >= 3.0:
        evidence.append(f"🔑 Elite chances created: {stats.key_passes_per_90:.1f} KP/90")
    elif stats.key_passes_per_90 >= 2.0:
        evidence.append(f"🔑 Strong chances created: {stats.key_passes_per_90:.1f} KP/90")
    if stats.form_score >= 70:
        evidence.append(f"🔥 Hot form ({stats.form_label}, {stats.form_score:.0f}/100)")
    elif stats.form_score <= 30:
        evidence.append(f"❄️ Cold form ({stats.form_label}, {stats.form_score:.0f}/100)")
    evidence.extend(match_ev)
    evidence.append(f"🏷 Archetype: {archetype.display()}")

    concerns: list[str] = []
    if archetype == Archetype.LOW_INVOLVEMENT:
        concerns.append("Low-involvement archetype — not a playmaker profile")
    if archetype == Archetype.GOAL_SCORER and stats.assists_per_90 < 0.15:
        concerns.append("Pure goal scorer — historically low assist output")
    if stats.games_effective() < 8:
        concerns.append(f"Small sample: only {stats.games_effective()} games this season")
    if stats.assists < 3:
        concerns.append(f"Only {stats.assists} season assists — thin creation history")

    return PickRecommendation(
        market="anytime_assist",
        player_name=stats.player_name,
        probability=prob,
        confidence=confidence,
        archetype=archetype,
        data_ok=data_ok,
        evidence=evidence,
        concerns=concerns,
        debug={
            "base": round(base, 4),
            "kp_mult": round(kp_m, 4),
            "form_mult": round(form_m, 4),
            "match_mult": round(match_m, 4),
            "arch_mult": round(arch_m, 4),
            "source": stats.source,
        },
    )
