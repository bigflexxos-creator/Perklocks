"""Player Prop Intelligence System — Phase 2.

USER MANDATE (2026-07-22): Build a complete "Player Prop Intelligence
System" for soccer with distinct models for:
   • Anytime Goalscorer
   • Anytime Assist
   • Goal Involvement (G+A either)

Architecture:
   stats_aggregator  → unified PlayerStats across all sources
                       (espn_mls_stats, soccer_player_form,
                        wiki_top_scorers, matchup_history)
   archetype_engine  → 5-way classifier
                       (Goal Scorer / Creator / Dual Threat /
                        Playmaker / Low Involvement)
   goalscorer_model  → P(anytime goal)  per match, per opponent
   assist_model      → P(anytime assist) per match, per opponent
   goal_involvement_model → P(G or A) per match, per opponent

All three models use REAL DATA ONLY. Zero randomness, zero fabricated
factors. If underlying stats are missing → returns confidence=0 and
`data_ok=False` so caller can skip the pick.
"""
from .models import (
    Archetype,
    PlayerStats,
    MatchupSplit,
    PickRecommendation,
)
from .stats_aggregator import get_player_stats, get_matchup_split
from .archetype_engine import classify_archetype, archetype_multiplier
from .goalscorer_model import predict_goal
from .assist_model import predict_assist
from .goal_involvement_model import predict_goal_involvement

__all__ = [
    # types
    "Archetype",
    "PlayerStats",
    "MatchupSplit",
    "PickRecommendation",
    # stats
    "get_player_stats",
    "get_matchup_split",
    # archetype
    "classify_archetype",
    "archetype_multiplier",
    # models
    "predict_goal",
    "predict_assist",
    "predict_goal_involvement",
]
