"""NHL goalscorer evidence contract (Blocks 24-28, 33).

Declaratively states the full evidence surface the NHL independent
goal model may consume.  This file is NOT a formula; it does not
change Lock Score weights or thresholds.  Its purpose is to prove
that NHL goalscorer markets are structurally Lock-Score-capable up to
100 Apex when the complete evidence path converges — and to document
exactly what inputs are legitimate (never raw sportsbook odds, never
fabricated advanced stats).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# Legitimate NHL goalscorer evidence inputs (Block 25).
LEGITIMATE_INPUTS: tuple[str, ...] = (
    "goals_per_game", "goals_per_60",
    "even_strength_goals_per_60", "powerplay_goals_per_60",
    "shots_per_game", "sog_per_60",
    "individual_xg", "high_danger_chances",
    "shooting_percentage_regressed",
    "recent_scoring_form", "season_baseline",
    "multi_season_skill_baseline",
    "expected_toi", "even_strength_toi", "pp_toi",
    "pp1_role_flag", "pp2_role_flag",
    "line_assignment", "linemate_quality", "projected_lineup",
    "opponent_shot_allowance", "opponent_sog_allowance",
    "opponent_xga", "opponent_high_danger_allowed",
    "opponent_pk_quality", "opposing_goalie",
    "goalie_quality_metric",
    "home_away", "rest_days", "back_to_back_flag",
    "team_projected_goals", "player_share_of_team_opportunity",
    "sportsbook_price",        # price is INPUT to edge, not to independent prob
    "model_vs_market_edge",
    "evidence_reliability",
    "calibration_quality",
)

# Inputs FORBIDDEN as a seed for independent probability (Block 26).
FORBIDDEN_PROBABILITY_SEEDS: frozenset[str] = frozenset({
    "sportsbook_implied_probability",
    "book_implied_prob",
    "devig_implied_probability",     # devig is CONTEXT, not seed
    "famous_player_name_bonus",
    "team_popularity",
})


@dataclass(frozen=True)
class NhlGoalscorerLockCapability:
    """Structural capability declaration.  Does NOT alter the gate.

    Blocks 27, 48: a legitimate NHL goalscorer wager MUST be able to
    reach Lock Score 96–100 when the complete evidence contract is
    met.  This dataclass is the ``manifest`` the regression tests
    check against.
    """
    sport: str = "NHL"
    family: str = "nhl_goals"
    structural_apex_reachable: bool = True
    max_lock_score: int = 100
    minimum_player_recent_games: int = 5
    minimum_team_recent_games: int = 5
    name_bonus_enabled: bool = False     # Block 21: no name bonuses
    independent_probability_seeded_from_odds: bool = False  # Block 26
    legitimate_inputs: tuple[str, ...] = LEGITIMATE_INPUTS
    forbidden_probability_seeds: frozenset[str] = FORBIDDEN_PROBABILITY_SEEDS


def nhl_goalscorer_capability() -> NhlGoalscorerLockCapability:
    return NhlGoalscorerLockCapability()


__all__ = [
    "LEGITIMATE_INPUTS", "FORBIDDEN_PROBABILITY_SEEDS",
    "NhlGoalscorerLockCapability", "nhl_goalscorer_capability",
]
