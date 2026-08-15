"""Soccer Scorer Bridge — Phase 2A.5 (2026-06).

Purpose
-------
Real-line Soccer scorer markets (Anytime, First, Score-or-Assist) used to
fall through to book-implied-only evidence:

    factors = {"Book Implied Probability": mp}

That contradicts the runtime contract: a real sportsbook line is NOT an
independent predictive model. Phase 2A.5 promotes the existing
`goal_scorer_engine_v2` from shadow-only to the authoritative scorer
intelligence for the real-line path.

Design
------
* Fully synchronous — the pick-emission loop in `sports_engine._props_picks_from_event`
  is sync and cannot await Mongo. Data is pre-loaded upstream and passed
  in as `form_row`.
* Sample-size-aware finishing shrinkage: strong scorers do not receive an
  extreme finishing multiplier off tiny samples; historically strong
  finishers do not lose their profile off a few scoreless matches.
* Dynamic player-quality classification — derived from evidence,
  never from a hardcoded name list.  Elite classification does NOT
  guarantee an elite Lock Score.  Bet quality is a separate concept
  (edge vs de-vig).
* Missing evidence increases uncertainty and returns None so the caller
  can emit MISSING_FEATURE_DATA rather than silently book-follow.

Contract
--------
Callers pass:
    player       : canonical player name (str)
    market_key   : "player_goal_scorer_anytime"
                 | "player_first_goal_scorer"
                 | "player_to_score_or_assist"
    book_implied : book-derived implied probability (0..1, post-vig)
    form_row     : soccer_player_form doc (or None)
    league       : e.g. "MLS", "Premier League"
    team_ctx     : optional dict {"team_xG": ..., "opponent_xGA": ...,
                                   "is_home": bool, "lineup_confidence": str}

We return:
    {
      "model_prob":       float,           # authoritative P(outcome)
      "factors":          dict[str, float],# 0..1 evidence factors
      "sources":          list[str],       # provenance
      "quality_profile":  str,             # ELITE_SCORER_PROFILE|
                                           # STRONG_SCORER_PROFILE|
                                           # AVERAGE_SCORER_PROFILE|
                                           # LIMITED_SCORER_PROFILE
      "uncertainty":      float,           # 0..1
      "engine_version":   str,
    }
or None if evidence is insufficient.
"""
from __future__ import annotations

import logging
import math
from typing import Any, Optional

logger = logging.getLogger("lockscore.soccer_scorer_bridge")

# ------------------------------------------------------------------ #
# Sample-size shrinkage constants
# ------------------------------------------------------------------ #
# Finishing quality is (goals / xG).  Prior K = 8 "phantom" xG worth of
# league-average finishing so a hot player with 3 goals off 1 xG is
# shrunk toward 1.0×, and a cold player with 0 goals off 5 xG is not
# thrown out entirely.
FINISHING_SHRINKAGE_K = 8.0

# Rate shrinkage — for xG/90 and shots/90 use K = 10 90-minute intervals
# worth of league-average.
RATE_SHRINKAGE_MATCHES = 10

# League-average per-90 baselines (attackers).  Very rough, refined later
# from soccer_player_form aggregates.
LEAGUE_AVG_XG_PER_90 = 0.35
LEAGUE_AVG_SOT_PER_90 = 1.10
LEAGUE_AVG_SHOTS_PER_90 = 2.20

# Anytime-scorer baseline for a typical starter (not a defender) — used
# as the "no evidence at all" prior when we have SOME form data but
# missing rate columns.
BASELINE_ANYTIME_PROB = 0.24


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #
def _shrink_rate(observed_rate: float, matches: int,
                 league_avg: float,
                 k: int = RATE_SHRINKAGE_MATCHES) -> float:
    """James-Stein-style shrinkage of an observed per-90 rate toward
    the league average, weighted by sample size."""
    if matches is None or matches <= 0:
        return league_avg
    w = matches / (matches + k)
    return w * observed_rate + (1.0 - w) * league_avg


def _shrink_finishing(goals: float, xg: float) -> float:
    """Return a finishing-quality multiplier bounded to ~[0.55, 1.65].

    Uses (goals + prior) / (xG + prior) where prior = FINISHING_SHRINKAGE_K
    worth of league-average (== 1.0×) finishing.
    """
    if xg is None or xg <= 0:
        return 1.0
    if goals is None or goals < 0:
        goals = 0.0
    num = goals + FINISHING_SHRINKAGE_K
    den = xg + FINISHING_SHRINKAGE_K
    fin = num / den
    return max(0.55, min(1.65, fin))


def _lineup_confidence_from_form(form_row: dict) -> str:
    """Best-effort lineup confidence from form_row.

    We don't have a live lineup feed inside this sync path, so we infer
    from starts / games:
        starts >= 0.7 * games  → "starting_xi"
        starts >= 0.4 * games  → "high_confidence"
        starts >= 0.1 * games  → "rotation"
        else                   → "bench_risk"
    """
    starts = int(form_row.get("starts") or form_row.get("games_started") or 0)
    games = int(form_row.get("games") or form_row.get("games_played") or 0)
    if games <= 0:
        return "unknown"
    ratio = starts / games if starts else 0.0
    # Fallback when only `games` is populated — assume starter if there
    # are enough appearances.
    if starts == 0 and games >= 10:
        return "high_confidence"
    if ratio >= 0.70:
        return "starting_xi"
    if ratio >= 0.40:
        return "high_confidence"
    if ratio >= 0.10:
        return "rotation"
    return "bench_risk"


def _classify_quality(model_prob: float,
                      xg_per_90_shrunk: float,
                      starts: int) -> str:
    """Dynamic player-quality classification.

    Derived from evidence (shrunk xG/90 + expected role) — NOT from a
    hardcoded name list.  This flag is *explainability only* — it never
    directly sets Lock Score.
    """
    if starts < 3:
        return "LIMITED_SCORER_PROFILE"
    if xg_per_90_shrunk >= 0.65 and model_prob >= 0.55:
        return "ELITE_SCORER_PROFILE"
    if xg_per_90_shrunk >= 0.40 and model_prob >= 0.35:
        return "STRONG_SCORER_PROFILE"
    if xg_per_90_shrunk >= 0.20 and model_prob >= 0.20:
        return "AVERAGE_SCORER_PROFILE"
    return "LIMITED_SCORER_PROFILE"


# ------------------------------------------------------------------ #
# Main API
# ------------------------------------------------------------------ #
def compute_soccer_scorer_factors_sync(
    *,
    player: str,
    market_key: str,
    book_implied: float,
    form_row: Optional[dict] = None,
    league: str = "",
    team_ctx: Optional[dict] = None,
) -> Optional[dict]:
    """Authoritative sync bridge into the Phase 2A.5 scorer intelligence.

    Returns None when evidence is insufficient (missing form entirely).
    """
    if not player or not market_key:
        return None
    if market_key not in (
        "player_goal_scorer_anytime",
        "player_first_goal_scorer",
        "player_to_score_or_assist",
    ):
        return None

    if not isinstance(form_row, dict) or not form_row:
        # No form data at all — caller should emit MISSING_FEATURE_DATA.
        return None

    # ── Extract features ─────────────────────────────────────────────
    xg = float(form_row.get("xg") or form_row.get("xG") or 0.0)
    xa = float(form_row.get("xa") or form_row.get("xA") or 0.0)
    goals = float(form_row.get("goals") or 0.0)
    minutes = int(form_row.get("minutes") or 0)
    games = int(form_row.get("games") or form_row.get("games_played") or 0)
    starts = int(form_row.get("starts") or form_row.get("games_started") or games)
    position = str(form_row.get("position") or "FW")
    form_score = float(form_row.get("form_score") or 50) / 100.0

    # Per-90 rates from raw totals when explicit "_per_90" fields aren't set.
    if minutes and minutes > 0:
        matches90 = minutes / 90.0
    else:
        matches90 = max(1, games)

    xg_per_90_raw = xg / matches90 if matches90 > 0 else 0.0
    shots_per_90_raw = float(form_row.get("shots_per_90") or 0.0)
    if not shots_per_90_raw and form_row.get("shots"):
        shots_per_90_raw = float(form_row["shots"]) / matches90
    sot_per_90_raw = float(form_row.get("sot_per_90") or 0.0)
    if not sot_per_90_raw and form_row.get("sot"):
        sot_per_90_raw = float(form_row["sot"]) / matches90

    # ── Sample-size shrinkage ────────────────────────────────────────
    xg_per_90 = _shrink_rate(xg_per_90_raw, games, LEAGUE_AVG_XG_PER_90)
    shots_per_90 = _shrink_rate(shots_per_90_raw, games, LEAGUE_AVG_SHOTS_PER_90)
    sot_per_90 = _shrink_rate(sot_per_90_raw, games, LEAGUE_AVG_SOT_PER_90)
    finishing = _shrink_finishing(goals, xg)

    # ── Team / opponent context ──────────────────────────────────────
    team_ctx = team_ctx or {}
    team_xG = float(team_ctx.get("team_xG") or 1.30)
    opp_xGA = float(team_ctx.get("opponent_xGA") or 1.30)
    lineup_conf = str(team_ctx.get("lineup_confidence") or
                      _lineup_confidence_from_form(form_row))
    # Convert lineup confidence → minutes multiplier.
    lineup_mult_map = {
        "starting_xi":     1.00, "high_confidence": 0.85,
        "rotation":        0.60, "bench_risk":      0.35,
        "unknown":         0.75,
    }
    lineup_mult = lineup_mult_map.get(lineup_conf, 0.75)

    # ── Delegate to Poisson engine ───────────────────────────────────
    try:
        from goal_scorer_engine_v2 import (
            PlayerFeatures, compute_probabilities,
        )
    except Exception as e:
        logger.debug("goal_scorer_engine_v2 import failed: %s", e)
        return None

    features = PlayerFeatures(
        player=player,
        team=(team_ctx.get("team") or form_row.get("team") or ""),
        opponent=(team_ctx.get("opponent") or ""),
        league=league,
        xG=xg,
        xA=xa,
        shot_volume=shots_per_90,
        shot_quality=(xg / max(1.0, xg + xa + 5.0)),
        minutes_played=minutes,
        games_played=games,
        starts=starts,
        position=position,
        minutes_projection=int(80 * lineup_mult),
        team_xG=team_xG,
        opponent_xGA=opp_xGA,
        lineup_confidence=lineup_conf,
        recent_form=form_score,
    )
    try:
        outputs = compute_probabilities(features, calibration_mult=1.0)
    except Exception as e:
        logger.debug("compute_probabilities failed for %s: %s", player, e)
        return None

    # Apply sample-size-aware finishing multiplier ON TOP of the engine
    # output so both concepts are visible: base xG shape + finishing
    # over/underperformance (shrunk).
    if market_key == "player_goal_scorer_anytime":
        raw_prob = outputs.p_anytime
    elif market_key == "player_first_goal_scorer":
        raw_prob = outputs.p_first
    else:  # score_or_assist
        raw_prob = outputs.p_score_or_assist

    # Finishing multiplier is applied to the LAMBDA in Poisson space so
    # probabilities stay in [0, 1].  Approximate: P' = 1 - (1-P)^finishing
    # for anytime-like; keep raw for first (tail already narrow).
    if market_key == "player_goal_scorer_anytime":
        model_prob = 1.0 - math.pow(max(1e-6, 1.0 - raw_prob), finishing)
    elif market_key == "player_to_score_or_assist":
        model_prob = 1.0 - math.pow(max(1e-6, 1.0 - raw_prob), finishing)
    else:
        model_prob = raw_prob * finishing / (1.0 + (finishing - 1.0) * raw_prob)

    model_prob = max(0.001, min(0.98, model_prob))

    # ── Build 0..1 factor dict for compute_lock_score ────────────────
    # Factors are per-name evidence signals — NOT boosted by name.
    factors: dict[str, float] = {
        "Scorer Model Probability":   round(model_prob, 4),
        "Expected Minutes":            round(lineup_mult, 3),
        "xG per 90 (shrunk)":          round(min(1.0, xg_per_90 / 0.90), 3),
        "Shots on Target per 90":      round(min(1.0, sot_per_90 / 2.5), 3),
        "Finishing Quality":           round((finishing - 0.5) / 1.15, 3),
        "Team Attack Environment":     round(min(1.0, team_xG / 2.5), 3),
        "Opponent Defensive Weakness": round(min(1.0, opp_xGA / 2.5), 3),
        "Recent Form":                 round(max(0.0, min(1.0, form_score)), 3),
    }
    # Clamp all factors to [0, 1] safely.
    for k in list(factors.keys()):
        v = factors[k]
        if not isinstance(v, (int, float)):
            factors.pop(k, None)
            continue
        factors[k] = max(0.0, min(1.0, float(v)))

    quality_profile = _classify_quality(model_prob, xg_per_90, starts)

    # Uncertainty grows with missing data and low sample.
    unc = 0.35 if games < 5 else (0.20 if games < 10 else 0.10)
    if lineup_conf in ("rotation", "bench_risk", "unknown"):
        unc += 0.10

    sources = [
        "soccer_scorer_bridge_v1",
        "goal_scorer_engine_v2",
    ]
    if form_row.get("source"):
        sources.append(str(form_row["source"]))

    return {
        "model_prob":       round(model_prob, 4),
        "factors":          factors,
        "sources":          sources,
        "quality_profile":  quality_profile,
        "uncertainty":      round(min(0.60, unc), 3),
        "engine_version":   "phase2a5_scorer_bridge_v1",
    }


__all__ = [
    "compute_soccer_scorer_factors_sync",
    "FINISHING_SHRINKAGE_K",
    "LEAGUE_AVG_XG_PER_90",
]
