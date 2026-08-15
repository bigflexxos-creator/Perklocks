"""Multi-season scorer strength — Phase 2A.5D AMENDMENT (2026-08).

DELTA — persistent elite attacker recognition via multi-season empirical
Bayes.  Reuses existing ``soccer_scorer_bridge`` output; only replaces
the raw rate inputs with sample-size-aware posteriors that blend prior-
season evidence with current-season observations.

Contract
--------
1. Player attacking strength = posterior blend of prior season + current
   season, weighted by minutes/matches.
2. Small current-season samples ⇒ prior dominates.  Large samples ⇒
   current dominates.  No hard date cutoff.
3. Minutes > games for sample weighting.
4. Current role modifies but does not erase long-term ability.
5. Availability overrides publication — an OUT player never surfaces
   even with elite prior.
6. Club/league changes add uncertainty via the ``env_shift`` term but
   preserve historical ability.
7. No hardcoded star names.  ELITE / STRONG / ABOVE_AVERAGE / AVERAGE /
   LIMITED profiles derived from posterior evidence only.
8. Finishing shrinkage retained (delegated to soccer_scorer_bridge).
9. No Lock Score manipulation — this improves MODEL probability +
   evidence quality, not the composite.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("lockscore.soccer_multi_season")


# ─── Constants ──────────────────────────────────────────────────────
# Prior season weight anchor (minutes-based).  When current + prior
# combined minutes are dominated by prior, prior wins.  Equivalent to
# "prior has 3000 phantom minutes" — roughly 1 season.
PRIOR_ANCHOR_MINUTES = 2500.0
# Older second-prior weight (down-weight).
SECOND_PRIOR_DECAY = 0.5

# Environment-shift multiplicative uncertainty for club/league changes.
ENV_SHIFT_TEAM_CHANGE = 0.85
ENV_SHIFT_LEAGUE_CHANGE = 0.75


@dataclass
class SeasonSample:
    """One season of a player's attacking output."""
    minutes: float = 0.0
    games: int = 0
    starts: int = 0
    goals: float = 0.0
    npg: Optional[float] = None       # non-penalty goals
    assists: float = 0.0
    xg: float = 0.0
    npxg: Optional[float] = None
    xa: float = 0.0
    shots: float = 0.0
    sot: float = 0.0
    penalty_share: float = 0.0        # 0..1
    setpiece_share: float = 0.0       # 0..1
    team: Optional[str] = None
    league: Optional[str] = None


@dataclass
class MultiSeasonPosterior:
    minutes_total: float
    # Per-90 posterior estimates
    goals_per_90: float
    assists_per_90: float
    xg_per_90: float
    npxg_per_90: float
    xa_per_90: float
    shots_per_90: float
    sot_per_90: float
    # Shares & role
    penalty_share: float
    setpiece_share: float
    # Weights actually used
    prior_weight: float
    current_weight: float
    # Meta
    quality_profile: str
    env_shift: float
    sources: list[str] = field(default_factory=list)
    reason_if_unavailable: Optional[str] = None


# ─── Helpers ────────────────────────────────────────────────────────
def _per_90(total: float, minutes: float) -> float:
    if minutes <= 0:
        return 0.0
    return (total / minutes) * 90.0


def _blend(prior: float, current: float,
           prior_w: float, current_w: float) -> float:
    """Weighted mean, safe against 0/0."""
    denom = prior_w + current_w
    if denom <= 0:
        return 0.0
    return (prior * prior_w + current * current_w) / denom


def _classify_attack_profile(xg90: float, xa90: float,
                              minutes_total: float) -> str:
    """Data-derived attacking-strength classification.  NO hardcoded
    names — the same evidence produces the same profile for anyone."""
    if minutes_total < 500:
        return "LIMITED"
    # Goal contribution per 90 (approx xG + xA).
    contribution = xg90 + xa90
    if contribution >= 1.20 and xg90 >= 0.60:
        return "ELITE_ATTACKING_PROFILE"
    if contribution >= 0.85 and xg90 >= 0.40:
        return "STRONG_ATTACKING_PROFILE"
    if contribution >= 0.55:
        return "ABOVE_AVERAGE"
    if contribution >= 0.30:
        return "AVERAGE"
    return "LIMITED"


# ─── Main API ───────────────────────────────────────────────────────
def compute_multi_season_posterior(
    *,
    current: SeasonSample,
    prior: Optional[SeasonSample] = None,
    second_prior: Optional[SeasonSample] = None,
    availability: str = "active",   # active | questionable | out | suspended
    expected_minutes: Optional[float] = None,
    current_team: Optional[str] = None,
    current_league: Optional[str] = None,
) -> MultiSeasonPosterior:
    """Empirical-Bayes multi-season attacking posterior.

    Weights:
        prior_w    ≈ min(prior.minutes, PRIOR_ANCHOR_MINUTES)
        current_w  ≈ current.minutes
    So a player with 200 current minutes + 2700 prior minutes retains
    strong prior influence; at 1500 current + 2700 prior the current
    signal starts to dominate; at 3000+ current the prior fades.
    """
    # Availability override — historical ability never overrides an OUT
    # player.  Return a zero-minutes posterior tagged with the reason.
    if str(availability).lower() in ("out", "suspended", "not_in_squad"):
        return MultiSeasonPosterior(
            minutes_total=0.0,
            goals_per_90=0.0, assists_per_90=0.0,
            xg_per_90=0.0, npxg_per_90=0.0, xa_per_90=0.0,
            shots_per_90=0.0, sot_per_90=0.0,
            penalty_share=0.0, setpiece_share=0.0,
            prior_weight=0.0, current_weight=0.0,
            quality_profile="LIMITED",
            env_shift=0.0,
            sources=["availability_gate"],
            reason_if_unavailable=f"PLAYER_UNAVAILABLE:{availability}",
        )

    # Per-90 rates from raw totals.
    c_g90  = _per_90(current.goals, current.minutes)
    c_a90  = _per_90(current.assists, current.minutes)
    c_xg90 = _per_90(current.xg, current.minutes)
    c_np90 = _per_90(current.npxg if current.npxg is not None else current.xg,
                      current.minutes)
    c_xa90 = _per_90(current.xa, current.minutes)
    c_s90  = _per_90(current.shots, current.minutes)
    c_sot90 = _per_90(current.sot, current.minutes)

    p_g90  = _per_90(prior.goals, prior.minutes) if prior else 0.0
    p_a90  = _per_90(prior.assists, prior.minutes) if prior else 0.0
    p_xg90 = _per_90(prior.xg, prior.minutes) if prior else 0.0
    p_np90 = _per_90(prior.npxg if (prior and prior.npxg is not None)
                      else (prior.xg if prior else 0),
                      prior.minutes if prior else 0.0)
    p_xa90 = _per_90(prior.xa, prior.minutes) if prior else 0.0
    p_s90  = _per_90(prior.shots, prior.minutes) if prior else 0.0
    p_sot90 = _per_90(prior.sot, prior.minutes) if prior else 0.0

    # Weights — minutes-driven, capped at PRIOR_ANCHOR_MINUTES so a
    # single season of prior evidence cannot outweigh a full current
    # season.
    current_w = float(current.minutes or 0.0)
    prior_w = min(float(prior.minutes if prior else 0.0), PRIOR_ANCHOR_MINUTES)

    # Optional 2-seasons-ago at reduced weight.
    if second_prior and second_prior.minutes:
        sp_w = min(float(second_prior.minutes), PRIOR_ANCHOR_MINUTES) * SECOND_PRIOR_DECAY
        sp_xg90 = _per_90(second_prior.xg, second_prior.minutes)
        sp_xa90 = _per_90(second_prior.xa, second_prior.minutes)
        sp_g90  = _per_90(second_prior.goals, second_prior.minutes)
        sp_a90  = _per_90(second_prior.assists, second_prior.minutes)
        sp_s90  = _per_90(second_prior.shots, second_prior.minutes)
        sp_sot90 = _per_90(second_prior.sot, second_prior.minutes)
        # Fold into prior aggregates.
        if prior_w + sp_w > 0:
            p_xg90 = _blend(sp_xg90, p_xg90, sp_w, prior_w)
            p_xa90 = _blend(sp_xa90, p_xa90, sp_w, prior_w)
            p_g90  = _blend(sp_g90,  p_g90,  sp_w, prior_w)
            p_a90  = _blend(sp_a90,  p_a90,  sp_w, prior_w)
            p_s90  = _blend(sp_s90,  p_s90,  sp_w, prior_w)
            p_sot90 = _blend(sp_sot90, p_sot90, sp_w, prior_w)
        prior_w += sp_w

    # Environment-shift for club / league change.
    env_shift = 1.0
    if prior and current_team and prior.team and prior.team != current_team:
        env_shift *= ENV_SHIFT_TEAM_CHANGE
    if prior and current_league and prior.league and prior.league != current_league:
        env_shift *= ENV_SHIFT_LEAGUE_CHANGE

    # Blend per-90 estimates.  Environment shift only attenuates the
    # PRIOR contribution — current season already reflects the new
    # environment.
    eff_prior_w = prior_w * env_shift
    xg90  = _blend(p_xg90, c_xg90, eff_prior_w, current_w)
    xa90  = _blend(p_xa90, c_xa90, eff_prior_w, current_w)
    g90   = _blend(p_g90,  c_g90,  eff_prior_w, current_w)
    a90   = _blend(p_a90,  c_a90,  eff_prior_w, current_w)
    np90  = _blend(p_np90, c_np90, eff_prior_w, current_w)
    s90   = _blend(p_s90,  c_s90,  eff_prior_w, current_w)
    sot90 = _blend(p_sot90, c_sot90, eff_prior_w, current_w)

    minutes_total = (current.minutes or 0) + (prior.minutes if prior else 0)

    quality_profile = _classify_attack_profile(xg90, xa90, minutes_total)

    sources = ["multi_season_posterior_v1"]
    if prior and prior.minutes:
        sources.append(f"prior_season_minutes={int(prior.minutes)}")
    if current.minutes:
        sources.append(f"current_season_minutes={int(current.minutes)}")
    if env_shift < 1.0:
        sources.append(f"env_shift={env_shift:.2f}")

    return MultiSeasonPosterior(
        minutes_total=minutes_total,
        goals_per_90=round(g90, 4),
        assists_per_90=round(a90, 4),
        xg_per_90=round(xg90, 4),
        npxg_per_90=round(np90, 4),
        xa_per_90=round(xa90, 4),
        shots_per_90=round(s90, 4),
        sot_per_90=round(sot90, 4),
        penalty_share=(current.penalty_share
                       if current.penalty_share is not None else 0.0),
        setpiece_share=(current.setpiece_share
                        if current.setpiece_share is not None else 0.0),
        prior_weight=round(eff_prior_w, 2),
        current_weight=round(current_w, 2),
        quality_profile=quality_profile,
        env_shift=round(env_shift, 3),
        sources=sources,
    )


__all__ = [
    "SeasonSample",
    "MultiSeasonPosterior",
    "compute_multi_season_posterior",
    "PRIOR_ANCHOR_MINUTES",
    "ENV_SHIFT_TEAM_CHANGE",
    "ENV_SHIFT_LEAGUE_CHANGE",
]
