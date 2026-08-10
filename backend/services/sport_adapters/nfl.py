"""NFL sport adapter — ESPN roster/athletes is the current-truth source.

Rules:
  * Same canonical_player_id across team trades / free-agency moves.
  * historical_teams preserves every team a player was on.
  * current_team is authoritative only when observed_at is fresh.
  * Two players with identical names disambiguated by ESPN athlete_id
    (or DOB when provided) — never by name alone.
"""
from __future__ import annotations

from typing import Any, Optional

from .base import (
    norm, any_token, is_team_market_for_team_sport,
    dob_matches, provider_ids_conflict,
)

SPORT = "NFL"
SPORT_CLASS = "team"
ROSTER_SOURCE = "espn_nfl_athletes"
PROVIDER_IDS = ("espn", "sportradar_nfl", "pfr", "odds_api")
IDENTITY_ATTRIBUTES = ("full_name", "dob", "handedness")
DISAMBIGUATORS = ("provider_id", "dob")
HISTORY_FIELDS = ("sport", "date", "event_id", "market", "team_at_time", "value")

PLAYER_MARKET_TOKENS = (
    "passing yards", "pass yards", "passing tds", "pass tds",
    "passing completions", "passing attempts",
    "rushing yards", "rush yards", "rushing tds", "rush tds",
    "carries",
    "receiving yards", "rec yards", "receiving tds", "rec tds",
    "receptions", "targets",
    "anytime touchdown", "anytime td",
    "first touchdown", "last touchdown",
    "player_", "sacks", "interceptions thrown", "longest reception",
    "longest completion", "longest rush",
    "kicking points", "field goals made",
)


def is_player_market(market: str) -> bool:
    if not market:
        return False
    if is_team_market_for_team_sport(market):
        return False
    return any_token(market, PLAYER_MARKET_TOKENS)


def canonical_current_team_key(pick: dict) -> Optional[str]:
    """NFL uses the roster team name — same key downstream ingesters
    use in ``active_registry`` / ``player_identities.current_team``."""
    return pick.get("player_team") or pick.get("current_team")


def transfer_preserves_identity(before: dict, after: dict) -> bool:
    """Team changes NEVER mint new identity."""
    return True


def validate_identity_change(before: dict, after: dict) -> Optional[str]:
    """Return reason string if change is DISALLOWED, else None."""
    if not dob_matches(before.get("dob"), after.get("dob")):
        return "dob_mismatch"
    conflict = provider_ids_conflict(before, after)
    if conflict:
        return f"provider_id_conflict:{conflict}"
    return None


__all__ = [
    "SPORT", "SPORT_CLASS", "ROSTER_SOURCE", "PROVIDER_IDS",
    "IDENTITY_ATTRIBUTES", "DISAMBIGUATORS", "HISTORY_FIELDS",
    "PLAYER_MARKET_TOKENS",
    "is_player_market", "canonical_current_team_key",
    "transfer_preserves_identity", "validate_identity_change",
]
