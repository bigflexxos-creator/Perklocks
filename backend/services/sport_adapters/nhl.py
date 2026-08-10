"""NHL sport adapter — ESPN athletes is the current-truth source."""
from __future__ import annotations

from typing import Any, Optional

from .base import (
    any_token, is_team_market_for_team_sport,
    dob_matches, provider_ids_conflict,
)

SPORT = "NHL"
SPORT_CLASS = "team"
ROSTER_SOURCE = "espn_nhl_athletes"
PROVIDER_IDS = ("espn", "nhl_stats", "odds_api")
IDENTITY_ATTRIBUTES = ("full_name", "dob")
DISAMBIGUATORS = ("provider_id", "dob")
HISTORY_FIELDS = ("sport", "date", "event_id", "market",
                    "team_at_time", "value")

PLAYER_MARKET_TOKENS = (
    "goals scored", "goalscorer", "goal scorer",
    "shots on goal", "sog",
    "assists", "points",
    "player_",
    "first goal", "last goal",
    "saves",
    "power play points",
)


def is_player_market(market: str) -> bool:
    if not market:
        return False
    if is_team_market_for_team_sport(market):
        return False
    return any_token(market, PLAYER_MARKET_TOKENS)


def canonical_current_team_key(pick: dict) -> Optional[str]:
    return pick.get("player_team") or pick.get("current_team")


def transfer_preserves_identity(before: dict, after: dict) -> bool:
    return True


def validate_identity_change(before: dict, after: dict) -> Optional[str]:
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
