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
    prior_form_row: Optional[dict] = None,   # Phase 2A.5D AMENDMENT
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
    # 2026-08-23 CHEAP SURGICAL — Soccer xG field normalization.
    # Different upstream sources use different aliases for the same
    # field.  Normalize them once here so the Soccer scorer model
    # receives xG / xA whatever the upstream calls it.  No model
    # change, no invented values.
    def _xg_alias(row: dict) -> float:
        for _k in ("xg", "xG", "expected_goals", "xgoals",
                    "npxg", "npxG", "non_penalty_xg"):
            _v = row.get(_k)
            if _v is not None:
                try:
                    return float(_v)
                except (TypeError, ValueError):
                    continue
        return 0.0
    def _xa_alias(row: dict) -> float:
        for _k in ("xa", "xA", "expected_assists", "xassists",
                    "primary_xa", "xag"):
            _v = row.get(_k)
            if _v is not None:
                try:
                    return float(_v)
                except (TypeError, ValueError):
                    continue
        return 0.0
    xg = _xg_alias(form_row)
    xa = _xa_alias(form_row)
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

    # ── Phase 2A.5D AMENDMENT — multi-season posterior override ──────
    # When a prior_form_row is supplied AND has meaningful minutes,
    # blend prior-season attacking rates into the per-90 estimates via
    # empirical-Bayes weighting.  Small current-season sample → prior
    # dominates.  Large sample → current dominates.  No hard cutoff.
    # Falls back to current-season-only behaviour when no prior data.
    multi_season_profile: Optional[str] = None
    if (prior_form_row and isinstance(prior_form_row, dict)
            and int(prior_form_row.get("minutes") or 0) >= 400):
        try:
            from services.soccer_scorer_multi_season import (
                SeasonSample, compute_multi_season_posterior,
            )
            _cur = SeasonSample(
                minutes=float(minutes or 0), games=games, starts=starts,
                goals=goals, xg=xg, xa=xa,
                shots=float(form_row.get("shots") or 0),
                sot=float(form_row.get("sot") or 0),
                team=str(form_row.get("team") or ""), league=league,
            )
            _pri = SeasonSample(
                minutes=float(prior_form_row.get("minutes") or 0),
                games=int(prior_form_row.get("games") or 0),
                starts=int(prior_form_row.get("starts")
                           or prior_form_row.get("games_started") or 0),
                goals=float(prior_form_row.get("goals") or 0),
                xg=_xg_alias(prior_form_row),
                xa=_xa_alias(prior_form_row),
                shots=float(prior_form_row.get("shots") or 0),
                sot=float(prior_form_row.get("sot") or 0),
                team=str(prior_form_row.get("team") or ""),
                league=str(prior_form_row.get("league") or ""),
            )
            _post = compute_multi_season_posterior(
                current=_cur, prior=_pri,
                availability="active",
                current_team=(team_ctx or {}).get("team")
                            if isinstance(team_ctx, dict) else None,
                current_league=league,
            )
            # Override per-90 estimates with the posterior blend.  These
            # remain shrunk (via posterior weight math) so tiny current-
            # season hot streaks cannot override strong priors.
            xg_per_90    = float(_post.xg_per_90)
            shots_per_90 = float(_post.shots_per_90) or shots_per_90
            sot_per_90   = float(_post.sot_per_90) or sot_per_90
            multi_season_profile = _post.quality_profile
        except Exception as _ms_err:
            logger.debug("multi-season posterior skipped: %s", _ms_err)

    # ── Team / opponent context ──────────────────────────────────────
    team_ctx = team_ctx or {}
    team_xG = float(team_ctx.get("team_xG") or 1.30)
    opp_xGA = float(team_ctx.get("opponent_xGA") or 1.30)
    is_home_flag = bool(team_ctx.get("is_home"))
    lineup_conf = str(team_ctx.get("lineup_confidence") or
                      _lineup_confidence_from_form(form_row))
    # Convert lineup confidence → minutes multiplier.
    lineup_mult_map = {
        "starting_xi":     1.00, "high_confidence": 0.85,
        "rotation":        0.60, "bench_risk":      0.35,
        "unknown":         0.75,
    }
    lineup_mult = lineup_mult_map.get(lineup_conf, 0.75)

    # ── 2026-06 · P0 SOCCER GOALSCORER INTELLIGENCE ──────────────────
    # Extract optional per-player evidence that the Lock Score should
    # know about but the Poisson scorer engine does not re-model:
    #   • H2H vs tonight's opponent (from preloaded h2h row)
    #   • Home / away (passed in via team_ctx)
    #   • Penalty-taker role (form_row.pens / penalty_role field)
    #   • L5 / L10 goals from the form_row recent window if present
    h2h_signal: Optional[float] = None
    h2h_games: int = 0
    _h2h_row = (team_ctx or {}).get("h2h")
    if isinstance(_h2h_row, dict):
        try:
            _h2h_g   = float(_h2h_row.get("goals") or 0.0)
            _h2h_app = float(_h2h_row.get("appearances")
                             or _h2h_row.get("games") or 0.0)
            if _h2h_app > 0:
                h2h_games = int(_h2h_app)
                # Scorer-rate per appearance vs this opponent (0..1).
                h2h_signal = min(1.0, _h2h_g / max(1.0, _h2h_app))
        except Exception:
            pass

    # Penalty-taker role: form_row.penalty_taker or form_row.pens.
    is_pen_taker: bool = False
    pens_taken = float(form_row.get("pens_taken")
                        or form_row.get("penalties_taken") or 0.0)
    pens_scored = float(form_row.get("pens_scored")
                         or form_row.get("penalties_scored") or 0.0)
    if (bool(form_row.get("penalty_taker"))
            or bool(form_row.get("is_penalty_taker"))
            or pens_taken >= 1):
        is_pen_taker = True

    # L5 / L10 goals from the form_row (optional — some ingesters store
    # these; others only store season totals).
    goals_l5 = form_row.get("goals_l5")
    goals_l10 = form_row.get("goals_l10")
    try:
        goals_l5 = float(goals_l5) if goals_l5 is not None else None
    except Exception:
        goals_l5 = None
    try:
        goals_l10 = float(goals_l10) if goals_l10 is not None else None
    except Exception:
        goals_l10 = None

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
    # Additive evidence factors — only emitted when real data supports.
    if h2h_signal is not None:
        factors["H2H vs Opponent"] = round(min(1.0, h2h_signal), 3)
    if is_home_flag:
        factors["Home Advantage"] = 0.55
    else:
        # Explicit away flag so Lock Score sees we modeled it.
        factors["Home Advantage"] = 0.45
    if is_pen_taker:
        # Being on penalty duty is a real anytime-scorer uplift signal
        # for the team's primary attacker.  Capped at a modest level —
        # most pens don't convert into non-penalty goals the same game.
        factors["Penalty Taker Role"] = 0.70
    if goals_l5 is not None:
        factors["L5 Goals"] = round(min(1.0, goals_l5 / 3.0), 3)
    if goals_l10 is not None:
        factors["L10 Goals"] = round(min(1.0, goals_l10 / 5.0), 3)
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

    # ── 2026-06 P0 — explicit factor_sources + canonical identity.
    # Mirrors the NHL contract: every independent signal class gets
    # its own token so compute_lock_score's data-quality component
    # can tally evidence breadth, not just evidence magnitude.
    sources = [
        "soccer_scorer_bridge_v1",
        "goal_scorer_engine_v2",
        f"soccer_form:{int(games)}games",
    ]
    if form_row.get("source"):
        sources.append(str(form_row["source"]))
    if prior_form_row:
        sources.append("soccer_prior_season")
    if h2h_signal is not None and h2h_games:
        sources.append(f"soccer_h2h:{h2h_games}")
    if is_pen_taker:
        sources.append("soccer_penalty_role")
    if goals_l10 is not None:
        sources.append("soccer_l10_goals")
    elif goals_l5 is not None:
        sources.append("soccer_l5_goals")
    # Evidence count mirrors the NHL contract.
    evidence_count = 0
    if games >= 5: evidence_count += 1
    if games >= 15: evidence_count += 1
    if xg > 0: evidence_count += 1
    if h2h_games >= 2: evidence_count += 1
    if is_pen_taker: evidence_count += 1
    if prior_form_row: evidence_count += 1
    if lineup_conf == "starting_xi": evidence_count += 1

    # Canonical identity (passed through from form_row when upstream
    # soccer_identity_ingest resolved it).  Never fabricate.
    canonical_player_id = (
        form_row.get("canonical_player_id")
        or form_row.get("player_id")
        or (team_ctx or {}).get("canonical_player_id")
        or None
    )
    identity_source = ("canonical_player_id"
                       if canonical_player_id else "name_only")

    return {
        "model_prob":       round(model_prob, 4),
        "factors":          factors,
        "sources":          sources,
        "factor_sources":   sources,
        "evidence_count":   int(evidence_count),
        "canonical_player_id": canonical_player_id,
        "player_identity_source": identity_source,
        "player_identity_stable": bool(canonical_player_id),
        "simulator_provenance":  "EMPIRICAL_INDEPENDENT",
        "h2h_games":        int(h2h_games),
        "is_home":          bool(is_home_flag),
        "quality_profile":  quality_profile,
        # Phase 2A.5D AMENDMENT — multi-season attacking profile, when
        # a prior_form_row was supplied.  Descriptive only; never sets
        # Lock Score.
        "multi_season_profile": multi_season_profile,
        "uncertainty":      round(min(0.60, unc), 3),
        "engine_version":   "phase2a5_scorer_bridge_v2_p0_intel" + (
            "+multi_season" if multi_season_profile else ""),
    }


__all__ = [
    "compute_soccer_scorer_factors_sync",
    "FINISHING_SHRINKAGE_K",
    "LEAGUE_AVG_XG_PER_90",
]
