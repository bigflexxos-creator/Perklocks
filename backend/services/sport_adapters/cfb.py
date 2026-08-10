"""CFB sport adapter — ESPN athletes is the current-truth source for
player rosters; ``services.cfb_ingest`` provides team-level metadata.

Rules:
  * Transfer to a different school PRESERVES canonical id (portal
    entries are just a team change on the same identity).
  * Historical schools tracked in ``historical_teams``.
  * DOB is the strongest disambiguator between identically named
    college players.
"""
from __future__ import annotations

from typing import Any, Optional

from .base import (
    any_token, is_team_market_for_team_sport,
    dob_matches, provider_ids_conflict,
)

SPORT = "CFB"
SPORT_CLASS = "team"
ROSTER_SOURCE = "espn_college_football_athletes"
PROVIDER_IDS = ("espn", "cfbd", "odds_api")
IDENTITY_ATTRIBUTES = ("full_name", "dob")
DISAMBIGUATORS = ("provider_id", "dob")
HISTORY_FIELDS = ("sport", "date", "event_id", "market",
                    "team_at_time", "value")

PLAYER_MARKET_TOKENS = (
    "passing yards", "pass yards", "passing tds", "pass tds",
    "rushing yards", "rush yards", "rushing tds", "rush tds",
    "receiving yards", "rec yards", "receiving tds", "rec tds",
    "receptions",
    "anytime touchdown", "anytime td",
    "player_",
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
    """Portal transfer preserves identity."""
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
