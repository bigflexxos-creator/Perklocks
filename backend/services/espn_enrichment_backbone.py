"""Shared ESPN / provider enrichment backbone (Blocks 8-10, 29, 34).

ESPN is **not** sportsbook truth and **not** independent model
probability.  It is a shared schedules / rosters / history / box-score
hydrator.  Live scoring MUST NEVER depend on synchronous ESPN calls —
the correct lifecycle is:

    ESPN / provider
        → normalize
        → canonical store
        → model reads canonical store

This module is a dispatcher: given a sport, it returns the normalized
hydration shape so each sport-specific ingestor can plug in without
re-implementing the dispatch logic.

The actual HTTP calls live in the sport-specific ESPN modules
(``espn_soccer_fixtures.py``, ``espn_signal_engine.py``, …).  This
backbone wires their outputs into the shared canonical schema used by
``historical_intelligence_projectors.py``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

# Canonical fields ESPN may contribute per sport.  Advanced fields are
# never fabricated — if ESPN doesn't expose them for the sport, they
# stay absent and downstream models must consult specialized providers.
ESPN_FIELDS_BY_SPORT: dict[str, dict] = {
    "nfl": {
        "schedules": True, "completed_games": True, "scores": True,
        "teams": True, "rosters": True,
        "player_game_logs": True, "box_score_stats": True,
        "opponent": True, "home_away": True,
        "snaps": True, "starters": True, "status_injury": True,
        "season_metadata": True,
    },
    "mlb": {
        "schedules": True, "completed_games": True, "scores": True,
        "teams": True, "rosters": True,
        "player_game_logs": True, "box_score_stats": True,
        "opponent": True, "home_away": True,
        "starters": True, "status_injury": True,
        "season_metadata": True,
    },
    "nba": {
        "schedules": True, "completed_games": True, "scores": True,
        "teams": True, "rosters": True,
        "player_game_logs": True, "box_score_stats": True,
        "opponent": True, "home_away": True,
        "minutes": True, "starters": True, "status_injury": True,
        "season_metadata": True,
    },
    "nhl": {
        "schedules": True, "completed_games": True, "scores": True,
        "teams": True, "rosters": True,
        "player_game_logs": True, "box_score_stats": True,
        "opponent": True, "home_away": True,
        "toi": True, "starters": True, "status_injury": True,
        "season_metadata": True,
    },
    "cfb": {
        "schedules": True, "completed_games": True, "scores": True,
        "teams": True, "rosters": True,
        "opponent": True, "home_away": True,
        "season_metadata": True,
    },
    "soccer": {
        "schedules": True, "completed_games": True, "scores": True,
        "teams": True, "rosters": True,
        "player_game_logs": True,
        "starters": True, "status_injury": True,
        "season_metadata": True,
    },
    # Tennis uses specialized sources as primary.  ESPN may supplement
    # only where useful; declared empty here to prevent accidental
    # over-reliance on ESPN as the Tennis authority.
    "tennis": {
        "schedules": True, "completed_games": True,
        "status_injury": False,  # ESPN tennis coverage is spotty.
        "player_game_logs": False,
    },
}


def espn_contract_for_sport(sport: str) -> dict:
    """Return the ESPN enrichment contract for a sport (what ESPN
    MAY contribute).  Unlisted sports return an empty contract."""
    s = (sport or "").strip().lower()
    return dict(ESPN_FIELDS_BY_SPORT.get(s, {}))


def espn_is_authoritative_for(field: str) -> bool:
    """ESPN is never sportsbook truth or model probability.  This
    helper is deliberate verbosity to prevent accidental misuse."""
    return False


def normalize_espn_completed_game(sport: str, raw: dict) -> dict:
    """Normalize an ESPN completed-game raw payload into the canonical
    schema the HI projectors expect in ``games``.

    Expected raw keys (varies by sport): ``event_id`` / ``id``,
    ``home_team``, ``away_team``, ``home_score``, ``away_score``,
    ``game_date`` / ``start_date`` / ``competitions[0].date``.
    """
    s = (sport or "").strip().lower()
    out = {
        "sport": s,
        "event_id": raw.get("event_id") or raw.get("id") or raw.get("espn_id"),
        "home_team": raw.get("home_team") or raw.get("home"),
        "away_team": raw.get("away_team") or raw.get("away"),
        "home_score": raw.get("home_score"),
        "away_score": raw.get("away_score"),
        "game_date":  raw.get("game_date") or raw.get("start_date"),
        "status": "Final" if (raw.get("completed") or raw.get("status") in
                               ("final", "Final", "FINAL", "post")) else raw.get("status"),
        "provenance": {
            "provider": "espn",
            "authority": 70,
            "retrieved_at": datetime.now(timezone.utc).isoformat()
                              .replace("+00:00", "Z"),
        },
    }
    return out


__all__ = [
    "ESPN_FIELDS_BY_SPORT", "espn_contract_for_sport",
    "espn_is_authoritative_for", "normalize_espn_completed_game",
]
