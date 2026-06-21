"""Historical Sports Intelligence Engine — low-cost edition.

Goal: give the Lock Engine historical memory and current-season totals
without burning the paid Odds API for stats data. Stats only come from
FREE / no-cost sources:

  • Soccer  → football-data.org   (fixtures, scorers, standings)
  • MLB     → statsapi.mlb.com    (free, no key)
  • NFL     → nflverse (R-language CSVs, no key)
  • NBA     → nba_api (unofficial endpoint, no key)
  • Tennis  → TheSportsDB (free key)

The paid Odds API is RESERVED for live odds only.

═════════════════════════════════════════════════════════════════════
DATA STORAGE (MongoDB collections — created on first write)
═════════════════════════════════════════════════════════════════════

players                 {player_id, sport, team, position, name}
games                   {game_id, sport, date, home, away, result, status}
player_form             {player_id, last3, last5, last10, season_total, consistency}
team_form               {team_id, last5_goals, last5_allowed, over15_rate, over25_rate, btts_rate}
season_totals           {player_id, season, games, goals, assists, shots, minutes}

soccer_team_model       {team_id, home_form, away_form, goal_rate, concede_rate,
                         clean_sheet_rate, over15_rate, over25_rate, btts_rate, ppm}
soccer_player_model     {player_id, goals_last5, shots_last5, minutes_last5,
                         goal_involvement, trend}

═════════════════════════════════════════════════════════════════════
API SAVER MODE — caching and lazy-loading policies
═════════════════════════════════════════════════════════════════════

Backfill: CURRENT SEASON ONLY on first run.
Incremental: only fetch games completed since `last_sync` timestamp.
Lazy: only pull player stats for players who appear in:
  • today's pick markets
  • probable starting lineups
  • top-N scorers in their league

Cache TTLs (enforced by `_cached_get` in each client):
  • fixtures        12h
  • standings       24h
  • season totals   24h
  • profiles        24h
  • live odds       60-120s   ← unchanged, lives in odds_api.py

═════════════════════════════════════════════════════════════════════
PROFILE GENERATION (auto-derived archetypes)
═════════════════════════════════════════════════════════════════════

shots high + goals low      → positive_regression
goals high + shots low      → regression_risk
consistent production       → high_floor
volatile outputs            → boom_bust

Never assign elite tier by player name. Profiles are computed from
stored historical data. `elite_players.py` will be deprecated once
the Lock Engine is wired to read from `player_form` instead.

═════════════════════════════════════════════════════════════════════
LOCK ENGINE INPUT WEIGHTS  (post-historical-engine ship)
═════════════════════════════════════════════════════════════════════

  40% season_totals
  25% recent_form
  15% matchup
  10% market
  10% lineups

Wire-up lives in `learning_system_v2.py` (to be patched in Phase 2).
"""

from __future__ import annotations

# Schema version — bumped any time the collection layout changes.
SCHEMA_VERSION = "2026.06.21-v1"

# Public exports — keep tight so consumers go through the engine
# orchestrator rather than poking individual clients.
from .orchestrator import (  # noqa: F401
    backfill_current_season,
    incremental_sync,
    refresh_player_form,
    refresh_team_form,
    generate_profiles,
)

__all__ = [
    "SCHEMA_VERSION",
    "backfill_current_season",
    "incremental_sync",
    "refresh_player_form",
    "refresh_team_form",
    "generate_profiles",
]
