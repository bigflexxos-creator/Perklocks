"""Soccer Game Model — Phase 2A.5B (2026-08).

DELTA CLOSURE — independent Soccer team/game probability core.

Purpose
-------
Before Phase 2A.5B, ``sports_engine`` line ~1335 executed:

    home_model = home_implied

for every Soccer match — the "model" was the sportsbook implied
probability.  That contradicts Phase 1B's independent-model contract.
This module replaces the probability CORE only, reusing existing
``home_form`` / ``away_form`` / ``home_xg_rolling`` / ``away_xg_rolling``
context populated upstream by ``services.game_context``.

Architecture
------------
1. ATTACK / DEFENSE STRENGTH derived from either:
   * real xG rolling (``source != "form_proxy"``) — TIER_A
   * form-derived GF / GA (labeled as GF/GA not xG) — TIER_B
   * only one side available — TIER_C (higher uncertainty)
   * insufficient — TIER_D → MODEL_UNAVAILABLE

2. Home advantage constant: ``+0.20 goals`` (approx league average).
   Regularized toward the league mean of 2.6 goals/match with a small
   K-prior when sample size is thin.

3. Poisson score matrix 0..7 × 0..7 with Dixon-Coles low-score correction
   for (0,0), (0,1), (1,0), (1,1) cells.

4. 1X2 / totals / BTTS / DC probabilities derived from the same matrix —
   downstream markets reuse this ONE distribution instead of each
   inventing their own probability.

Contracts
---------
* Sportsbook odds are NEVER read into λ. Book is MARKET INFORMATION only.
* GF / GA are NEVER labeled xG. When only form_proxy is available,
  ``sources`` carries ``TEAM_STRENGTH`` (not ``EXPECTED_GOALS``).
* Missing xG does not automatically MODEL_UNAVAILABLE — form-derived
  team strength is a legitimate tier.
* Same input → same output (deterministic).
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("lockscore.soccer_game_model")

# ------------------------------------------------------------------ #
# Constants
# ------------------------------------------------------------------ #
LEAGUE_AVG_GOALS_PER_MATCH = 2.65
LEAGUE_AVG_TEAM_GOALS       = LEAGUE_AVG_GOALS_PER_MATCH / 2.0  # ~1.325 / side
HOME_ADVANTAGE_GOALS        = 0.20     # +0.20 goals extra for home
DIXON_COLES_RHO             = -0.10    # low-score correlation

# Shrinkage priors (regularise thin-sample rate estimates).
STRENGTH_PRIOR_MATCHES      = 6        # phantom matches worth of league avg

# Score matrix bounds.
MAX_GOALS                   = 7

# Evidence categories — used to prevent correlated features from being
# counted as multiple independent confirmations by downstream systems.
EV_TEAM_STRENGTH   = "TEAM_STRENGTH"
EV_EXPECTED_GOALS  = "EXPECTED_GOALS"
EV_RECENT_FORM     = "RECENT_FORM"
EV_LINEUP          = "LINEUP_AVAILABILITY"
EV_REST            = "REST_SCHEDULE"
EV_H2H             = "H2H"
EV_SCORE_MODEL     = "SCORE_MODEL"


# ------------------------------------------------------------------ #
# Types
# ------------------------------------------------------------------ #
@dataclass
class SoccerGameOutputs:
    available: bool
    reason: Optional[str] = None
    tier: str = "D"
    p_home: float = 0.0
    p_draw: float = 0.0
    p_away: float = 0.0
    lambda_home: float = 0.0
    lambda_away: float = 0.0
    uncertainty: float = 0.5
    sources: list[str] = field(default_factory=list)
    evidence_categories: list[str] = field(default_factory=list)
    xg_available: bool = False
    home_strength: dict[str, float] = field(default_factory=dict)
    away_strength: dict[str, float] = field(default_factory=dict)
    # Full 8x8 Poisson matrix — downstream markets consume from here.
    score_matrix: list[list[float]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "reason": self.reason,
            "tier": self.tier,
            "p_home": self.p_home,
            "p_draw": self.p_draw,
            "p_away": self.p_away,
            "lambda_home": self.lambda_home,
            "lambda_away": self.lambda_away,
            "uncertainty": self.uncertainty,
            "sources": list(self.sources),
            "evidence_categories": list(self.evidence_categories),
            "xg_available": self.xg_available,
            "home_strength": dict(self.home_strength),
            "away_strength": dict(self.away_strength),
            "score_matrix_shape": [len(self.score_matrix),
                                   len(self.score_matrix[0]) if self.score_matrix else 0],
        }


# ------------------------------------------------------------------ #
# Team-strength extraction
# ------------------------------------------------------------------ #
def _extract_strength(side: str, ctx: dict) -> dict[str, Any]:
    """Return {'gf':, 'ga':, 'matches':, 'xg_source':, 'has_real_xg': bool}.

    * `side` is "home" or "away".
    * Prefer real xG (from `<side>_xg_rolling` when source != form_proxy).
    * Otherwise fall back to `<side>_form` GF/GA — TAG AS GF/GA, NOT xG.
    """
    xg_key = f"{side}_xg_rolling"
    form_key = f"{side}_form"
    xg_doc = ctx.get(xg_key) or {}
    form_doc = ctx.get(form_key) or {}
    xg_source = str(xg_doc.get("source") or "")
    has_real_xg = bool(xg_doc) and xg_source != "form_proxy" and (
        xg_doc.get("xg_avg") is not None
    )

    if has_real_xg:
        return {
            "gf":       float(xg_doc.get("xg_avg") or 0.0),
            "ga":       float(xg_doc.get("xga_avg") or 0.0),
            "matches":  int(xg_doc.get("matches") or 0),
            "xg_source": xg_source or "real_xg",
            "has_real_xg": True,
            "provenance": EV_EXPECTED_GOALS,
        }
    # ── form_proxy xg_rolling — this IS team strength, NOT xG ────────
    # `services.game_context` populates `<side>_xg_rolling` with
    # `source=form_proxy` when only GF/GA form data is available.  The
    # `xg_avg` / `xga_avg` keys are aliases of `gf_avg` / `ga_avg` in
    # that case — clearly labeled by `xg_available=False`.  We reuse
    # the numeric values but categorise the evidence as TEAM_STRENGTH.
    if (xg_doc and xg_doc.get("xg_avg") is not None
            and xg_doc.get("xga_avg") is not None):
        return {
            "gf":       float(xg_doc.get("gf_avg", xg_doc.get("xg_avg")) or 0.0),
            "ga":       float(xg_doc.get("ga_avg", xg_doc.get("xga_avg")) or 0.0),
            "matches":  int(xg_doc.get("matches") or 0),
            "xg_source": "form_proxy",
            "has_real_xg": False,
            "provenance": EV_TEAM_STRENGTH,
        }
    # Fall back to form-derived GF/GA — clearly labeled as GF/GA.
    if form_doc:
        gf = form_doc.get("gf_avg")
        ga = form_doc.get("ga_avg")
        if isinstance(gf, (int, float)) and isinstance(ga, (int, float)):
            return {
                "gf":       float(gf),
                "ga":       float(ga),
                "matches":  int(form_doc.get("n_matches") or 0),
                "xg_source": "form_gf_ga",
                "has_real_xg": False,
                "provenance": EV_TEAM_STRENGTH,
            }
    return {
        "gf": None, "ga": None, "matches": 0,
        "xg_source": "unavailable",
        "has_real_xg": False,
        "provenance": None,
    }


def _shrink_rate(observed: float, matches: int, prior: float,
                 k: int = STRENGTH_PRIOR_MATCHES) -> float:
    """Blend observed per-match rate toward the league prior."""
    if matches is None or matches <= 0:
        return prior
    w = matches / (matches + k)
    return w * observed + (1.0 - w) * prior


def _dixon_coles_tau(x: int, y: int, lambda_h: float, lambda_a: float,
                     rho: float = DIXON_COLES_RHO) -> float:
    """Dixon-Coles low-score correlation factor."""
    if x == 0 and y == 0:
        return 1.0 - lambda_h * lambda_a * rho
    if x == 0 and y == 1:
        return 1.0 + lambda_h * rho
    if x == 1 and y == 0:
        return 1.0 + lambda_a * rho
    if x == 1 and y == 1:
        return 1.0 - rho
    return 1.0


def _poisson_pmf(k: int, lam: float) -> float:
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def _build_score_matrix(lambda_h: float, lambda_a: float,
                        max_goals: int = MAX_GOALS,
                        rho: float = DIXON_COLES_RHO) -> list[list[float]]:
    """8×8 Poisson score matrix with Dixon-Coles low-score correction."""
    mat: list[list[float]] = []
    total = 0.0
    for x in range(max_goals + 1):
        row = []
        for y in range(max_goals + 1):
            base = _poisson_pmf(x, lambda_h) * _poisson_pmf(y, lambda_a)
            tau  = _dixon_coles_tau(x, y, lambda_h, lambda_a, rho)
            cell = max(0.0, base * tau)
            row.append(cell)
            total += cell
        mat.append(row)
    # Normalise so probabilities sum to exactly 1.
    if total > 0:
        for x in range(max_goals + 1):
            for y in range(max_goals + 1):
                mat[x][y] /= total
    return mat


# ------------------------------------------------------------------ #
# Public API
# ------------------------------------------------------------------ #
def estimate_soccer_game_probabilities(
    ctx: Optional[dict],
    home: str,
    away: str,
    *,
    home_advantage: float = HOME_ADVANTAGE_GOALS,
    league_avg_team_goals: float = LEAGUE_AVG_TEAM_GOALS,
) -> SoccerGameOutputs:
    """Return authoritative independent Soccer game probabilities.

    Never reads sportsbook odds.  Never fabricates xG from GF/GA.
    """
    ctx = ctx or {}
    h = _extract_strength("home", ctx)
    a = _extract_strength("away", ctx)

    # ── Tier determination ─────────────────────────────────────────
    both_missing = (h["gf"] is None and a["gf"] is None)
    one_missing  = (h["gf"] is None) ^ (a["gf"] is None)
    if both_missing:
        return SoccerGameOutputs(
            available=False, reason="INSUFFICIENT_HISTORY", tier="D",
            uncertainty=0.90,
        )

    if h["has_real_xg"] and a["has_real_xg"]:
        tier = "A"
        uncertainty_base = 0.10
    elif (h["has_real_xg"] or a["has_real_xg"]) and not one_missing:
        tier = "B"
        uncertainty_base = 0.20
    elif not one_missing:
        tier = "B"
        uncertainty_base = 0.22
    else:
        tier = "C"
        uncertainty_base = 0.35

    # ── Fill missing side with league prior when only one side has data.
    for row in (h, a):
        if row["gf"] is None:
            row["gf"] = league_avg_team_goals
            row["ga"] = league_avg_team_goals
            row["matches"] = 0

    # ── Sample-size shrinkage ─────────────────────────────────────
    h_gf = _shrink_rate(h["gf"], h["matches"], league_avg_team_goals)
    h_ga = _shrink_rate(h["ga"], h["matches"], league_avg_team_goals)
    a_gf = _shrink_rate(a["gf"], a["matches"], league_avg_team_goals)
    a_ga = _shrink_rate(a["ga"], a["matches"], league_avg_team_goals)

    # ── Attack / defense multipliers (relative to league average) ─
    ha_attack = h_gf / max(0.20, league_avg_team_goals)
    ha_def    = h_ga / max(0.20, league_avg_team_goals)
    aw_attack = a_gf / max(0.20, league_avg_team_goals)
    aw_def    = a_ga / max(0.20, league_avg_team_goals)

    # λ_home = league_avg * home_attack * away_defensive_weakness + home_advantage
    lambda_home = league_avg_team_goals * ha_attack * aw_def + home_advantage
    lambda_away = league_avg_team_goals * aw_attack * ha_def
    lambda_home = max(0.05, min(6.0, lambda_home))
    lambda_away = max(0.05, min(6.0, lambda_away))

    # ── Score matrix ─────────────────────────────────────────────
    mat = _build_score_matrix(lambda_home, lambda_away)

    # ── 1X2 probabilities from the matrix ────────────────────────
    p_home = 0.0
    p_away = 0.0
    p_draw = 0.0
    for x in range(len(mat)):
        for y in range(len(mat[x])):
            cell = mat[x][y]
            if x > y:  p_home += cell
            elif x < y: p_away += cell
            else:       p_draw += cell
    # Normalise (should already be 1.0 by construction).
    total = p_home + p_draw + p_away
    if total > 0:
        p_home /= total; p_draw /= total; p_away /= total

    # ── Provenance / evidence categories ─────────────────────────
    ev_cats: set[str] = {EV_SCORE_MODEL}
    sources: list[str] = ["soccer_game_model_v1"]
    if h["has_real_xg"] or a["has_real_xg"]:
        ev_cats.add(EV_EXPECTED_GOALS)
        sources.append(f"xg_source:home={h['xg_source']}")
        sources.append(f"xg_source:away={a['xg_source']}")
    else:
        ev_cats.add(EV_TEAM_STRENGTH)
        sources.append("gf_ga_form_proxy")

    # Uncertainty grows with sample size gap.
    matches_gap = abs((h["matches"] or 0) - (a["matches"] or 0))
    unc_bonus = 0.10 if matches_gap >= 10 else (0.05 if matches_gap >= 5 else 0.0)

    return SoccerGameOutputs(
        available=True,
        tier=tier,
        p_home=round(p_home, 4),
        p_draw=round(p_draw, 4),
        p_away=round(p_away, 4),
        lambda_home=round(lambda_home, 4),
        lambda_away=round(lambda_away, 4),
        uncertainty=round(min(0.75, uncertainty_base + unc_bonus), 3),
        sources=sources,
        evidence_categories=sorted(ev_cats),
        xg_available=(h["has_real_xg"] or a["has_real_xg"]),
        home_strength={
            "gf": round(h_gf, 3), "ga": round(h_ga, 3),
            "matches": h["matches"],
        },
        away_strength={
            "gf": round(a_gf, 3), "ga": round(a_ga, 3),
            "matches": a["matches"],
        },
        score_matrix=mat,
    )


# ------------------------------------------------------------------ #
# Derived market probabilities (reuse the ONE distribution)
# ------------------------------------------------------------------ #
def totals_from_matrix(mat: list[list[float]], line: float) -> tuple[float, float]:
    """Return (P(Over line), P(Under line)) from score matrix.
    Push (X + Y == line) is treated as neutral (0.5 each) for integer
    lines, but Soccer lines are always half-integer in practice.
    """
    p_over = 0.0
    p_under = 0.0
    p_push = 0.0
    for x in range(len(mat)):
        for y in range(len(mat[x])):
            tot = x + y
            cell = mat[x][y]
            if tot > line:      p_over += cell
            elif tot < line:    p_under += cell
            else:               p_push += cell
    if p_push > 0:
        p_over  += p_push * 0.5
        p_under += p_push * 0.5
    return round(p_over, 4), round(p_under, 4)


def btts_from_matrix(mat: list[list[float]]) -> tuple[float, float]:
    """Return (P(BTTS Yes), P(BTTS No))."""
    p_yes = 0.0
    for x in range(1, len(mat)):
        for y in range(1, len(mat[x])):
            p_yes += mat[x][y]
    p_yes = max(0.0, min(1.0, p_yes))
    return round(p_yes, 4), round(1.0 - p_yes, 4)


def double_chance_from_1x2(p_home: float, p_draw: float,
                            p_away: float) -> dict[str, float]:
    """Return P(1X), P(X2), P(12) from 1X2 probabilities."""
    return {
        "1X": round(p_home + p_draw, 4),
        "X2": round(p_draw + p_away, 4),
        "12": round(p_home + p_away, 4),
    }


__all__ = [
    "estimate_soccer_game_probabilities",
    "totals_from_matrix",
    "btts_from_matrix",
    "double_chance_from_1x2",
    "SoccerGameOutputs",
    "EV_TEAM_STRENGTH",
    "EV_EXPECTED_GOALS",
    "EV_SCORE_MODEL",
    "EV_RECENT_FORM",
    "EV_H2H",
]
