"""NHL 2.0 — Market-specific challenger models.

2026-10-02 — V2 CHALLENGER scaffold.  V1 (brain_sim_nhl) remains
production champion; these models are published as CHALLENGER with
full provenance (model_family, model_version, calibrator_version,
feature_contract_version) but promotion gated by walk-forward
evaluation that occurs OUTSIDE this request (requires scoring
historical events — a dedicated batch job).

Every model:
  * Consumes MatchupContext from services/nhl_v2/features.py
  * Produces an INDEPENDENT probability (no sportsbook seeding)
  * Degrades gracefully when source features are UNAVAILABLE
  * Stamps full provenance
"""
from __future__ import annotations

import logging
import math
from typing import Optional

from services.nhl_v2.features import (
    FEATURE_CONTRACT_VERSION, MODEL_FAMILY,
    MatchupContext, PlayerContext,
)

logger = logging.getLogger("lockscore.nhl_v2.models")

MODEL_VERSION      = "nhl_v2.0.0-challenger"
CALIBRATOR_VERSION = "nhl_v2.identity.v0"


def _stamp(prob: Optional[float], market: str, availability: dict) -> dict:
    """Return the canonical probability block consumed downstream."""
    return {
        "independent_model_probability": None if prob is None else round(float(prob), 6),
        "probability_source":            "nhl_v2",
        "model_family":                  MODEL_FAMILY,
        "model_version":                 MODEL_VERSION,
        "calibrator_version":            CALIBRATOR_VERSION,
        "feature_contract_version":      FEATURE_CONTRACT_VERSION,
        "market":                        market,
        "feature_availability":          availability,
    }


def _poisson_tail_ge(lam: float, k: int) -> float:
    """P(X >= k) for X ~ Poisson(lam).  Stable for lam up to ~50."""
    if lam <= 0 or k <= 0:
        return 1.0 if k <= 0 else 0.0
    # 1 - CDF(k-1)
    s = 0.0
    log_lam = math.log(lam)
    log_term = -lam  # i=0
    s += math.exp(log_term)
    for i in range(1, k):
        log_term = log_term + log_lam - math.log(i)
        s += math.exp(log_term)
    return max(0.0, min(1.0, 1.0 - s))


# ───────────────────── GAME MARKETS ─────────────────────

def predict_ml(ctx: MatchupContext) -> dict:
    """Independent moneyline probability for the HOME team."""
    home_rate = ctx.home.goals_for_per60
    away_rate = ctx.away.goals_for_per60
    home_def  = ctx.home.goals_against_per60
    away_def  = ctx.away.goals_against_per60
    availability = {
        "home_gf": bool(home_rate), "away_gf": bool(away_rate),
        "home_ga": bool(home_def),  "away_ga": bool(away_def),
    }
    if not (home_rate and away_rate and home_def and away_def):
        return _stamp(None, "ml", availability | {"reason": "insufficient team scoring evidence"})
    # Expected goals: average of own GF and opponent GA
    exp_home = (float(home_rate) + float(away_def)) / 2.0
    exp_away = (float(away_rate) + float(home_def)) / 2.0
    # Home ice bump
    exp_home *= 1.04
    exp_away *= 0.96
    # Convert to ML via Poisson difference approximation (skellam approximation).
    # P(home wins) = sum_{k=1..∞} P(home_margin == k).  Approximated via
    # a lightweight simulation-free integral: logistic over expected-goal diff.
    diff = exp_home - exp_away
    # Slope calibrated loosely against NHL margins (~1.2 goals ≈ 55-60% win).
    p = 1.0 / (1.0 + math.exp(-diff * 0.75))
    return _stamp(p, "ml", availability | {"exp_home": round(exp_home,2),
                                            "exp_away": round(exp_away,2)})


def predict_puck_line(ctx: MatchupContext, line: float = -1.5) -> dict:
    """P(home covers -1.5)."""
    out = predict_ml(ctx)
    if out.get("independent_model_probability") is None:
        return _stamp(None, "puck_line", out.get("feature_availability") or {})
    # Puck-line cover is roughly ~45% of home-win probability for -1.5.
    p = float(out["independent_model_probability"]) * 0.60
    return _stamp(p, "puck_line", out.get("feature_availability") or {})


def predict_total(ctx: MatchupContext, line: float = 6.0) -> dict:
    """P(total > line)."""
    exp_home_gf = ctx.home.goals_for_per60
    exp_away_gf = ctx.away.goals_for_per60
    availability = {"home_gf": bool(exp_home_gf), "away_gf": bool(exp_away_gf)}
    if not (exp_home_gf and exp_away_gf):
        return _stamp(None, "total", availability | {"reason": "insufficient scoring evidence"})
    lam = float(exp_home_gf) + float(exp_away_gf)
    k = int(math.floor(line)) + 1
    p_over = _poisson_tail_ge(lam, k)
    return _stamp(p_over, "total", availability | {"lambda_total": round(lam, 2),
                                                    "line": line})


# ───────────────────── PLAYER MARKETS ─────────────────────

def predict_sog(pctx: PlayerContext, opp_shots_against_per60: Optional[float],
                 line: float = 2.5) -> dict:
    """P(player shots-on-goal > line).

    Uses opponent direct SOG defense as primary (replacing GA proxy).
    """
    availability = {
        "player_shots_rate": bool(pctx.shots_per_game_last10),
        "opp_sog_defense":   bool(opp_shots_against_per60),
    }
    if not pctx.shots_per_game_last10:
        return _stamp(None, "sog", availability | {"reason": "no player shot rate"})
    rate = float(pctx.shots_per_game_last10)
    # Opp SOG adjustment — league median ≈ 29 SOG/game.
    if opp_shots_against_per60:
        rate *= (float(opp_shots_against_per60) / 29.0)
        availability["opp_sog_defense_quality"] = "DIRECT"
    else:
        availability["opp_sog_defense_quality"] = "PROXY_OR_UNAVAILABLE"
    # TOI scaling
    if pctx.projected_total_toi and pctx.toi_last5:
        toi_scale = float(pctx.projected_total_toi) / max(float(pctx.toi_last5), 1.0)
        rate *= toi_scale
    lam = max(0.05, rate)
    k = int(math.floor(line)) + 1
    p = _poisson_tail_ge(lam, k)
    return _stamp(p, "sog", availability | {"lambda_shots": round(lam, 3), "line": line})


def predict_goals(pctx: PlayerContext, opp_ga_per60: Optional[float],
                   line: float = 0.5) -> dict:
    availability = {
        "player_goals_rate": bool(pctx.goals_per_game_last10),
        "opp_ga":            bool(opp_ga_per60),
    }
    if not pctx.goals_per_game_last10:
        return _stamp(None, "goals", availability | {"reason": "no player goal rate"})
    rate = float(pctx.goals_per_game_last10)
    if opp_ga_per60:
        rate *= (float(opp_ga_per60) / 3.0)
    if pctx.projected_total_toi and pctx.toi_last5:
        rate *= float(pctx.projected_total_toi) / max(float(pctx.toi_last5), 1.0)
    lam = max(0.01, rate)
    k = int(math.floor(line)) + 1
    p = _poisson_tail_ge(lam, k)
    return _stamp(p, "goals", availability | {"lambda_goals": round(lam, 4), "line": line})


def predict_assists(pctx: PlayerContext, opp_ga_per60: Optional[float],
                     line: float = 0.5) -> dict:
    availability = {
        "player_assists_rate": bool(pctx.assists_per_game_last10),
        "opp_ga":              bool(opp_ga_per60),
    }
    if not pctx.assists_per_game_last10:
        return _stamp(None, "assists", availability | {"reason": "no player assist rate"})
    rate = float(pctx.assists_per_game_last10)
    if opp_ga_per60:
        rate *= (float(opp_ga_per60) / 3.0)
    if pctx.projected_total_toi and pctx.toi_last5:
        rate *= float(pctx.projected_total_toi) / max(float(pctx.toi_last5), 1.0)
    lam = max(0.01, rate)
    k = int(math.floor(line)) + 1
    p = _poisson_tail_ge(lam, k)
    return _stamp(p, "assists", availability | {"lambda_assists": round(lam, 4), "line": line})


def predict_points(pctx: PlayerContext, opp_ga_per60: Optional[float],
                    line: float = 0.5) -> dict:
    """Points = Goals + Assists.  Joint Poisson approximation."""
    g = predict_goals(pctx, opp_ga_per60, line=line)
    a = predict_assists(pctx, opp_ga_per60, line=line)
    availability = {"goals_available":    g.get("independent_model_probability") is not None,
                    "assists_available":  a.get("independent_model_probability") is not None}
    gp = g.get("independent_model_probability")
    ap = a.get("independent_model_probability")
    if gp is None and ap is None:
        return _stamp(None, "points", availability | {"reason": "neither goals nor assists available"})
    lam_g = (g.get("feature_availability") or {}).get("lambda_goals") or 0.0
    lam_a = (a.get("feature_availability") or {}).get("lambda_assists") or 0.0
    lam = max(0.01, float(lam_g) + float(lam_a))
    k = int(math.floor(line)) + 1
    p = _poisson_tail_ge(lam, k)
    return _stamp(p, "points", availability | {"lambda_points": round(lam, 4), "line": line})


__all__ = [
    "MODEL_FAMILY", "MODEL_VERSION", "CALIBRATOR_VERSION",
    "predict_ml", "predict_puck_line", "predict_total",
    "predict_sog", "predict_goals", "predict_assists", "predict_points",
]
