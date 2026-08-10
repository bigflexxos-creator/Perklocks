"""MLB sport adapter — statsapi.mlb.com is the current-truth source
(same as ``services.mlb_hitter_intel`` / ``mlb_matchup_resolver``).

Rules:
  * Switch-hitter status is an ATTRIBUTE (``handedness="switch"``)
    NOT a separate identity.
  * Position change (pitcher ↔ position player — rare, Ohtani-like)
    is an attribute change, NEVER a new canonical id.
  * Team change preserves canonical id.
  * Historical teams tracked in ``historical_teams``.
  * DOB is the strongest disambiguator when two names collide.
"""
from __future__ import annotations

from typing import Any, Optional

from .base import (
    any_token, is_team_market_for_team_sport,
    dob_matches, provider_ids_conflict,
)

SPORT = "MLB"
SPORT_CLASS = "team"
ROSTER_SOURCE = "statsapi_mlb"
PROVIDER_IDS = ("mlb_stats", "espn", "fangraphs", "odds_api")
IDENTITY_ATTRIBUTES = ("full_name", "dob")
# switch-hitter is an attribute; role is an attribute; DOB is
# the tie-breaker between identically named players.
ATTRIBUTES_NOT_IDENTITY = ("handedness", "role", "position",
                           "throws", "bats")
DISAMBIGUATORS = ("provider_id", "dob")
HISTORY_FIELDS = ("sport", "date", "event_id", "market",
                    "team_at_time", "value", "pitcher_faced",
                    "handedness_used")

PLAYER_MARKET_TOKENS = (
    "hits", "home run", "hr", "total bases", "rbi",
    "runs scored", "singles", "doubles", "triples",
    "stolen bases",
    "strikeouts", "walks", "pitches thrown",
    "innings pitched", "earned runs",
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
    """Team change, position change (P→OF), and handedness change
    (switch-hitting) all preserve the canonical id."""
    return True


def validate_identity_change(before: dict, after: dict) -> Optional[str]:
    if not dob_matches(before.get("dob"), after.get("dob")):
        return "dob_mismatch"
    conflict = provider_ids_conflict(before, after)
    if conflict:
        return f"provider_id_conflict:{conflict}"
    return None


def is_attribute_change_not_identity(field: str) -> bool:
    """True iff a change to `field` is an attribute update, NEVER a
    new identity — used by test harnesses to assert switch-hitter
    upgrades don't split identities."""
    return field in ATTRIBUTES_NOT_IDENTITY


__all__ = [
    "SPORT", "SPORT_CLASS", "ROSTER_SOURCE", "PROVIDER_IDS",
    "IDENTITY_ATTRIBUTES", "ATTRIBUTES_NOT_IDENTITY",
    "DISAMBIGUATORS", "HISTORY_FIELDS", "PLAYER_MARKET_TOKENS",
    "is_player_market", "canonical_current_team_key",
    "transfer_preserves_identity", "validate_identity_change",
    "is_attribute_change_not_identity",
]
