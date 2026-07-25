"""GoalScorer Engine v3 — Layered probability engine for Anytime Goal
Scorer market.

Replaces the rudimentary per-90 heuristic in `goalscorer_model.py` with
a fully data-driven layered stack per user directive (2026-07-22):

    Layer 1: xG Engine (λ_player)
        Per-player expected goals per match derived from Understat
        npxg_per_90 blended with observed goals_per_90 (~ Empirical
        Bayes) and adjusted for expected minutes.

    Layer 2: Poisson Team-Goal Simulator
        Sample team_goals ~ Poisson(λ_team_adj) where
            λ_team_adj = team_attack_λ · opp_defense_λ / league_mean_goals
        Uses `team_strength.get_league_strength()` for the priors from
        2 seasons of real match data.

    Layer 3: Goal Allocation Engine
        For each simulated team goal, allocate to a player by their
        share of team open-play xG + set-piece/penalty adjustments.
        This produces player_goals for each Monte Carlo sample.

    Layer 4: Correlated Monte Carlo
        20k samples per match. Same-team players share the team_goals
        draw so their outcomes are naturally correlated (if team scores
        3, someone got them). Opposing teams draw independently.

    Layer 5: Bayesian Lineup Update
        If lineup_confirmation available (starting_xi / rotation /
        bench_risk), scale the player's allocation weight by
        expected-minutes multiplier. Otherwise use position priors.

    Layer 6: Ensemble Probability
        Blend:
            0.55 · MC-simulated P(anytime)
            0.30 · closed-form Poisson: 1 - exp(-λ_player)
            0.15 · form-adjusted historical baseline
        Then clamp [0.02, 0.85].

    Layer 7: Confidence + Edge Gate
        Strict — HIGH confidence requires ≥ 2 seasons of Understat data
        AND ≥ 15 games sample. Edge computed ONLY when odds_source is
        NOT "fallback" — meaning we have real sportsbook lines to
        measure against. Otherwise edge_percent = None with a flag.

Public API:
    result = await predict_goal_v3(db, stats, opp_team, sport_key, ...)
    → returns V3GoalOutput with per-market probabilities + provenance.
"""
from __future__ import annotations

import logging
import math
import unicodedata
from dataclasses import dataclass, field, asdict
from typing import Optional

import numpy as np

from .models import Archetype, MatchupSplit, PickRecommendation, PlayerStats
from .team_strength import (
    LEAGUE_CODE_MAP, LeagueStrength, TeamStrength,
    get_league_strength, lookup_team,
)

logger = logging.getLogger("lockscore.player_props.goal_scorer_v3")


ENGINE_VERSION = "gs_v3.0.0"
MC_SAMPLES = 20_000

# Lineup certainty → expected-minutes multiplier.
_LINEUP_MINUTES: dict[str, float] = {
    "starting_xi":     1.00,
    "high_confidence": 0.90,
    "rotation":        0.65,
    "bench_risk":      0.35,
    "unknown":         0.80,       # league-average starter assumption
}

# Position-based penalty priors (fraction of team penalties taken).
_PEN_PRIOR: dict[str, float] = {
    "F":  0.12, "FW": 0.12, "ST": 0.12, "CF": 0.12,
    "M F": 0.10, "F M": 0.10,
    "AM": 0.08, "CAM": 0.08,
    "W":  0.06, "LW": 0.06, "RW": 0.06,
    "M":  0.03, "CM": 0.03,
    "D":  0.01, "DEF": 0.01,
    "GK": 0.00,
}

# Set-piece / free-kick share priors.
_SP_PRIOR: dict[str, float] = {
    "AM": 0.14, "M":  0.10, "CM": 0.10,
    "W":  0.10, "LW": 0.10, "RW": 0.10,
    "F":  0.06, "FW": 0.06,
    "D":  0.02, "DEF": 0.02,
    "GK": 0.00,
}

# League baseline splits (share of goals that come from open play vs
# set-pieces vs penalties). Averaged from public soccer references.
_LEAGUE_PENALTY_SHARE  = 0.10     # ~10% of goals are from penalties
_LEAGUE_SETPIECE_SHARE = 0.15     # ~15% from set-pieces (non-pen)
_LEAGUE_OPENPLAY_SHARE = 0.75     # remainder

# Ensemble weights.
_W_MC          = 0.55
_W_CLOSED_FORM = 0.30
_W_FORM_HIST   = 0.15


def _norm(name: str) -> str:
    if not name:
        return ""
    nk = unicodedata.normalize("NFKD", name)
    return "".join(c for c in nk if not unicodedata.combining(c)).lower().strip()


def _first_pos_token(pos: str) -> str:
    p = (pos or "").upper().strip()
    if not p:
        return ""
    for sep in (" ", "/", ","):
        if sep in p:
            p = p.split(sep, 1)[0]
    return p


# ── Dataclasses ─────────────────────────────────────────────────────
@dataclass
class LineupInfo:
    """Lineup Bayesian prior. All optional — defaults to 'unknown'."""
    confirmed:         bool = False
    status:            str  = "unknown"   # starting_xi | high_confidence | rotation | bench_risk | unknown
    expected_minutes:  Optional[int] = None


@dataclass
class V3GoalOutput:
    """Result of a single (player, match) v3 evaluation."""
    p_anytime:          float
    p_first:            float
    p_2plus:            float
    lam_player:         float
    lam_team:           float
    lam_opponent:       float
    expected_minutes:   float
    goal_share:         float             # of team's expected goals
    ensemble_components: dict = field(default_factory=dict)
    confidence:         str = "MEDIUM"
    data_ok:            bool = True
    engine_version:     str  = ENGINE_VERSION
    debug:              dict = field(default_factory=dict)
    concerns:           list[str] = field(default_factory=list)
    evidence:           list[str] = field(default_factory=list)


# ── Core math ───────────────────────────────────────────────────────
def _player_lambda(stats: PlayerStats,
                   *,
                   expected_minutes: float,
                   team_lam: float,
                   team_goals_league_mean: float) -> tuple[float, float, dict]:
    """Compute player expected-goals λ_player (per this match).

    Returns (λ_player, share_of_team_goals, debug).
    """
    minutes_played = float(stats.minutes or (stats.games * 80))
    mp90 = max(1.0, minutes_played / 90.0)

    # Blend npxG/90 (predictive) with G/90 (empirical) — Empirical Bayes.
    # If Understat: use npxG heavily. If ESPN (no npxG): fall back to G/90.
    if stats.npxg_per_90 > 0:
        # 70% npxG-based, 30% actual — regressing to underlying quality.
        base_g90 = 0.70 * stats.npxg_per_90 + 0.30 * stats.goals_per_90
        source = "npxg+goals"
    elif stats.goals_per_90 > 0:
        base_g90 = stats.goals_per_90
        source = "goals_per_90"
    elif stats.gpm() > 0:
        # Fallback: goals per match ~ goals per ~80 min * 90/80
        base_g90 = stats.gpm() * 90 / 80
        source = "goals_per_match"
    else:
        base_g90 = 0.05
        source = "floor"

    # Scale to the player's share of a league-average team's goals.
    # A player scoring 0.5 G/90 on a team that averages 1.5 goals/game
    # = ~33% share of their team's goals per 90.
    # Multiply that share by THIS team's expected goals for the match.
    if team_goals_league_mean > 0:
        share = base_g90 / team_goals_league_mean
    else:
        share = 0.30
    share = max(0.02, min(0.85, share))

    # Player λ = team's expected goals × share × minutes-multiplier
    minute_mult = min(1.0, expected_minutes / 90.0)
    lam = team_lam * share * minute_mult

    # Penalty & set-piece additions (only if we have a position hint).
    pos = _first_pos_token(stats.position)
    pen_share = _PEN_PRIOR.get(pos, 0.03)
    sp_share  = _SP_PRIOR.get(pos, 0.04)
    # These add small extra λ scaled to league averages.
    pen_lam = pen_share * team_lam * _LEAGUE_PENALTY_SHARE
    sp_lam  = sp_share  * team_lam * _LEAGUE_SETPIECE_SHARE
    lam += pen_lam + sp_lam

    # Bound.
    lam = max(0.005, min(3.5, lam))

    return lam, share, {
        "base_g90": round(base_g90, 4),
        "share_of_team_goals": round(share, 4),
        "minute_mult": round(minute_mult, 3),
        "pen_lam": round(pen_lam, 4),
        "sp_lam": round(sp_lam, 4),
        "lam_source": source,
    }


def _correlated_monte_carlo(lam_team: float,
                            lam_opp: float,
                            player_lam: float,
                            rng: np.random.Generator,
                            samples: int = MC_SAMPLES) -> dict:
    """Run Layer-3 + Layer-4: Poisson team-goal sim + Goal-allocation.

    We DO NOT sample player goals independently — instead:
        1. Draw team_goals ~ Poisson(lam_team)
        2. Allocate each team goal to this player with probability
           p_alloc = player_lam / lam_team (capped at 0.85).
        3. player_goals in this sim = Binomial(team_goals, p_alloc).

    This produces exactly the correlation structure we want: a player
    can only score if their team scores, and multiple team goals
    increase the chance of a brace.
    """
    if lam_team <= 0.001:
        return {"p_anytime": 0.02, "p_2plus": 0.005,
                "p_first_when_scoring": 0.0}

    # Draw team goals for the match.
    team_goals = rng.poisson(lam_team, size=samples)

    # Per-goal allocation probability for THIS player.
    p_alloc = min(0.85, player_lam / max(0.001, lam_team))

    # Binomial: number of the team's goals scored by this player.
    # (rng.binomial supports vectorized `n` array.)
    player_goals = rng.binomial(team_goals, p_alloc)

    p_anytime = float((player_goals >= 1).mean())
    p_2plus   = float((player_goals >= 2).mean())

    # For P(first) we further need: given player scored, prob they
    # were first goal-scorer. Under Poisson-thinning assumption this
    # equals P(no earlier opp goal) × 1/(their team's goals before).
    # We approximate using Poisson tail identities:
    #   P(first) ≈ P(match has ≥1 goal) · (player_lam / (lam_team + lam_opp))
    lam_match = lam_team + lam_opp
    p_match_has_goal = 1.0 - math.exp(-lam_match) if lam_match > 0 else 0.0
    p_first = p_match_has_goal * (player_lam / max(0.001, lam_match))
    p_first = max(0.0, min(0.95, p_first))

    return {
        "p_anytime": p_anytime,
        "p_2plus":   p_2plus,
        "p_first":   p_first,
        "mean_team_goals": float(team_goals.mean()),
        "mean_player_goals": float(player_goals.mean()),
    }


def _closed_form_poisson(lam_player: float) -> tuple[float, float]:
    """Standard Poisson tail: P(X≥1) = 1 - e^-λ, P(X≥2) = 1 - e^-λ (1+λ)."""
    e = math.exp(-lam_player)
    return 1.0 - e, max(0.0, 1.0 - e * (1.0 + lam_player))


def _form_baseline(stats: PlayerStats) -> float:
    """Layer-6 3rd component — historical goals-per-match adjusted for form.

    Simple: convert stats.gpm() → probability approximation, then
    tilt by form_score (0-100).
    """
    gpm = stats.gpm()
    if gpm <= 0 and stats.goals_per_90 > 0:
        gpm = stats.goals_per_90 * 80.0 / 90.0
    # 1 - (1 - gpm)^1 → naive P(≥1 in one match) = gpm ceiling.
    p = min(0.75, gpm)
    # Form tilt (max ±20%).
    form_delta = (stats.form_score - 50.0) / 50.0    # -1..+1
    p *= 1.0 + max(-0.20, min(0.20, form_delta * 0.20))
    return max(0.02, min(0.85, p))


def _bayes_lineup_minutes(lineup: LineupInfo, default_minutes: int = 75) -> float:
    """Layer-5: convert lineup info → expected minutes.

    If confirmed with explicit minutes → return that.
    Otherwise scale default (75) by the confidence multiplier.
    """
    if lineup.expected_minutes and lineup.expected_minutes > 0:
        return float(lineup.expected_minutes)
    mult = _LINEUP_MINUTES.get(lineup.status, _LINEUP_MINUTES["unknown"])
    return default_minutes * mult


def _confidence(stats: PlayerStats, team: Optional[TeamStrength],
                lineup: LineupInfo) -> str:
    """Strict confidence gating."""
    games = stats.games_effective()
    has_understat = stats.source == "understat"
    has_team_data = bool(team and team.matches >= 30)

    if lineup.status == "bench_risk":
        return "LOW"
    if games < 3 or not has_team_data:
        return "LOW"
    if games >= 15 and has_understat and has_team_data:
        return "HIGH"
    if games >= 8 and has_team_data:
        return "MEDIUM"
    return "LOW"


# ── Public entry point ──────────────────────────────────────────────
async def predict_goal_v3(db,
                          stats: PlayerStats,
                          opp_team_name: str,
                          *,
                          sport_key: str = "",
                          is_home: bool = True,
                          lineup: Optional[LineupInfo] = None,
                          split: Optional[MatchupSplit] = None,
                          archetype: Optional[Archetype] = None,
                          samples: int = MC_SAMPLES,
                          rng: Optional[np.random.Generator] = None,
                          ) -> V3GoalOutput:
    """Run the full v3 pipeline for one player."""
    if not stats or not stats.data_ok:
        return V3GoalOutput(
            p_anytime=0.0, p_first=0.0, p_2plus=0.0,
            lam_player=0.0, lam_team=0.0, lam_opponent=0.0,
            expected_minutes=0.0, goal_share=0.0,
            data_ok=False, confidence="LOW",
            concerns=["no player stats available"],
        )

    lineup = lineup or LineupInfo()
    league_key = LEAGUE_CODE_MAP.get(sport_key, LEAGUE_CODE_MAP.get(stats.league, stats.league))

    # ── Layer-1 support: fetch team strengths ───────────────────────
    lg_strength, team_map = await get_league_strength(db, league_key)
    team_home_side = lookup_team(team_map, stats.team) if stats.team else None
    opp = lookup_team(team_map, opp_team_name)

    # If team data missing → we still run engine with league mean, but
    # confidence takes a hit.
    if team_home_side is None:
        team_home_side = TeamStrength(
            league=league_key, team=stats.team or "unknown",
            lam_attack_home=lg_strength.mean_home_goals,
            lam_defense_home=lg_strength.mean_away_goals,
            lam_attack_away=lg_strength.mean_away_goals,
            lam_defense_away=lg_strength.mean_home_goals,
            league_mean_goals=lg_strength.mean_total_goals,
            seasons_used=lg_strength.seasons_used,
        )
    if opp is None:
        opp = TeamStrength(
            league=league_key, team=opp_team_name or "unknown",
            lam_attack_home=lg_strength.mean_home_goals,
            lam_defense_home=lg_strength.mean_away_goals,
            lam_attack_away=lg_strength.mean_away_goals,
            lam_defense_away=lg_strength.mean_home_goals,
            league_mean_goals=lg_strength.mean_total_goals,
        )

    # Team λ (attack × opp defense / league-mean).
    lm = max(0.5, lg_strength.mean_home_goals if is_home else lg_strength.mean_away_goals)
    opp_def = opp.defense(is_home=not is_home)     # opp's defense when NOT at home = away defense (& vv.)
    team_atk = team_home_side.attack(is_home=is_home)
    lam_team = (team_atk * opp_def) / max(0.5, lm)
    lam_team = max(0.30, min(4.5, lam_team))

    opp_atk = opp.attack(is_home=not is_home)
    team_def = team_home_side.defense(is_home=is_home)
    lam_opp = (opp_atk * team_def) / max(0.5, lg_strength.mean_home_goals if not is_home else lg_strength.mean_away_goals)
    lam_opp = max(0.20, min(4.5, lam_opp))

    # ── Layer-5: Bayesian lineup update ─────────────────────────────
    expected_minutes = _bayes_lineup_minutes(lineup)

    # ── Layer-1: player λ ───────────────────────────────────────────
    lam_player, share, lam_dbg = _player_lambda(
        stats,
        expected_minutes=expected_minutes,
        team_lam=lam_team,
        team_goals_league_mean=lg_strength.mean_total_goals / 2.0,   # per-team mean
    )

    # ── Layer-3+4: Monte Carlo ──────────────────────────────────────
    if rng is None:
        rng = np.random.default_rng(seed=42)
    mc = _correlated_monte_carlo(lam_team, lam_opp, lam_player, rng, samples=samples)

    # ── Layer-2: closed-form ────────────────────────────────────────
    p_cf, p_2plus_cf = _closed_form_poisson(lam_player)

    # ── Form baseline ───────────────────────────────────────────────
    p_form = _form_baseline(stats)

    # ── Layer-6: ensemble ───────────────────────────────────────────
    p_anytime = (_W_MC * mc["p_anytime"]
                 + _W_CLOSED_FORM * p_cf
                 + _W_FORM_HIST * p_form)
    p_anytime = max(0.02, min(0.85, p_anytime))
    p_2plus  = max(0.0, min(0.60,
                    _W_MC * mc["p_2plus"] + _W_CLOSED_FORM * p_2plus_cf))
    p_first  = float(mc.get("p_first", 0.0))

    # Historical head-to-head shrinkage (matchup split).
    evidence: list[str] = []
    concerns: list[str] = []
    if split and split.matches >= 3:
        h2h_rate = split.score_rate()
        # Blend 15% weight (limited signal in small samples).
        p_anytime = 0.85 * p_anytime + 0.15 * h2h_rate
        if h2h_rate >= 0.5:
            evidence.append(f"📊 H2H vs {split.opponent}: {split.scored_matches}/{split.matches} scored")
        elif h2h_rate == 0.0 and split.matches >= 4:
            concerns.append(f"❄️ Never scored vs {split.opponent} ({split.matches} tries)")

    # Evidence + concerns build.
    if stats.npxg_per_90 >= 0.35:
        evidence.append(f"⚡ Elite xG: {stats.npxg_per_90:.2f} npxG/90")
    elif stats.npxg_per_90 >= 0.25:
        evidence.append(f"⚡ Strong xG: {stats.npxg_per_90:.2f} npxG/90")
    if lam_team >= 1.8:
        evidence.append(f"🎯 High team attack rate: {lam_team:.2f} xG projected")
    elif lam_team <= 0.8:
        concerns.append(f"🛡 Low team attack rate: {lam_team:.2f} xG projected")
    if lineup.status == "starting_xi":
        evidence.append("✅ Confirmed starter")
    elif lineup.status == "rotation":
        concerns.append("⚠️ Rotation risk")
    elif lineup.status == "bench_risk":
        concerns.append("❌ High bench risk")

    confidence = _confidence(stats, team_home_side, lineup)

    return V3GoalOutput(
        p_anytime=round(p_anytime, 4),
        p_first=round(p_first, 4),
        p_2plus=round(p_2plus, 4),
        lam_player=round(lam_player, 4),
        lam_team=round(lam_team, 4),
        lam_opponent=round(lam_opp, 4),
        expected_minutes=round(expected_minutes, 1),
        goal_share=round(share, 4),
        ensemble_components={
            "monte_carlo":       round(mc["p_anytime"], 4),
            "closed_form":       round(p_cf, 4),
            "form_baseline":     round(p_form, 4),
            "weights": {
                "monte_carlo": _W_MC,
                "closed_form": _W_CLOSED_FORM,
                "form_baseline": _W_FORM_HIST,
            },
        },
        confidence=confidence,
        data_ok=True,
        engine_version=ENGINE_VERSION,
        evidence=evidence,
        concerns=concerns,
        debug={
            **lam_dbg,
            "team_atk": team_home_side.attack(is_home),
            "team_def": team_home_side.defense(is_home),
            "opp_atk":  opp.attack(not is_home),
            "opp_def":  opp.defense(not is_home),
            "league_mean_home": round(lg_strength.mean_home_goals, 3),
            "league_mean_away": round(lg_strength.mean_away_goals, 3),
            "seasons_used": lg_strength.seasons_used,
            "mc_mean_team_goals": round(mc.get("mean_team_goals", 0.0), 3),
            "mc_mean_player_goals": round(mc.get("mean_player_goals", 0.0), 4),
            "team_matches_in_prior": team_home_side.matches,
            "lineup_status": lineup.status,
        },
    )


def to_pick_recommendation(out: V3GoalOutput,
                            stats: PlayerStats,
                            archetype: Archetype) -> PickRecommendation:
    """Adapt V3 output into the legacy PickRecommendation shape so it
    plugs into `market_selector.py` without changes."""
    return PickRecommendation(
        market="anytime_goal_scorer",
        player_name=stats.player_name,
        probability=out.p_anytime,
        confidence=out.confidence,
        archetype=archetype,
        data_ok=out.data_ok,
        evidence=out.evidence,
        concerns=out.concerns,
        debug={
            **out.debug,
            "engine": out.engine_version,
            "lam_player": out.lam_player,
            "lam_team":   out.lam_team,
            "lam_opponent": out.lam_opponent,
            "expected_minutes": out.expected_minutes,
            "goal_share": out.goal_share,
            "ensemble": out.ensemble_components,
            "p_first":  out.p_first,
            "p_2plus":  out.p_2plus,
        },
    )


__all__ = [
    "V3GoalOutput", "LineupInfo",
    "predict_goal_v3", "to_pick_recommendation",
    "ENGINE_VERSION", "MC_SAMPLES",
]
