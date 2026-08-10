"""Soccer sport adapter — pure passthrough to the existing
P0-A..P0-E Soccer identity stack.

CRITICAL: nothing in this adapter is allowed to bypass or duplicate
``services.player_identity`` / ``services.player_team_fixture_validator``
— all Soccer identity + validation flows through those.
"""
from __future__ import annotations

from typing import Any, Optional

# NOTE: Soccer's market tokens live inside player_team_fixture_validator.
from services.player_team_fixture_validator import (
    _is_player_based_market as _soccer_is_player_market,
)

SPORT = "Soccer"
SPORT_CLASS = "team"
ROSTER_SOURCE = "espn_live_soccer_rosters+national_team+identity_ingest"
PROVIDER_IDS = ("espn", "sportdb", "apisports", "understat",
                "football_data", "odds_api")
IDENTITY_ATTRIBUTES = ("full_name", "dob", "nationality")
DISAMBIGUATORS = ("provider_id", "dob")
HISTORY_FIELDS = ("sport", "date", "event_id", "market",
                    "team_at_time", "value", "opponent")

# Populated indirectly via _soccer_is_player_market; kept here for
# adapter-registry introspection only.
PLAYER_MARKET_TOKENS = (
    "anytime goal scorer", "to score", "to score or assist",
    "first goal scorer", "last goal scorer", "player_",
)


def is_player_market(market: str) -> bool:
    """Delegate — never diverge from the Soccer contract."""
    return _soccer_is_player_market(market)


def canonical_current_team_key(pick: dict) -> Optional[str]:
    return pick.get("player_team") or pick.get("current_team")


def transfer_preserves_identity(before: dict, after: dict) -> bool:
    """Club transfer preserves identity (Messi PSG→Miami keeps id)."""
    return True


def validate_identity_change(before: dict, after: dict) -> Optional[str]:
    # Soccer's identity change rules already live in player_identity;
    # this adapter is passthrough for introspection only.
    return None


__all__ = [
    "SPORT", "SPORT_CLASS", "ROSTER_SOURCE", "PROVIDER_IDS",
    "IDENTITY_ATTRIBUTES", "DISAMBIGUATORS", "HISTORY_FIELDS",
    "PLAYER_MARKET_TOKENS",
    "is_player_market", "canonical_current_team_key",
    "transfer_preserves_identity", "validate_identity_change",
]
