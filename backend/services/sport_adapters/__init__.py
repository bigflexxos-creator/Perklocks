"""Phase 5 (2026-08-11) — Sport-specific identity adapters.

Every adapter is a stateless module exposing:

    SPORT: str                              # "NFL" / "NBA" / ...
    SPORT_CLASS: {"team", "individual"}
    IDENTITY_ATTRIBUTES: tuple[str, ...]    # attributes that never mint new id
    DISAMBIGUATORS: tuple[str, ...]         # attributes that DO mint new id
    PROVIDER_IDS: tuple[str, ...]           # supported provider id namespaces
    PLAYER_MARKET_TOKENS: tuple[str, ...]   # markets that reference a player
    ROSTER_SOURCE: str                      # documented roster source id
    HISTORY_FIELDS: tuple[str, ...]         # required history-row keys

    def is_player_market(market: str) -> bool
    def canonical_current_team_key(pick: dict) -> Optional[str]
    def transfer_preserves_identity(before: dict, after: dict) -> bool
    def validate_identity_change(before: dict, after: dict) -> Optional[str]

`transfer_preserves_identity` returns True when the *type* of change
(team change, role change, weight class change, transfer school, etc.)
is one that MUST NOT mint a new canonical_player_id.  Returning False
means the change fundamentally identifies a different person and the
caller should mint a new id.

`validate_identity_change` returns None when the transition is
acceptable, or a short reason string if the transition is disallowed
(e.g. dob mismatch, provider id conflict).
"""
from __future__ import annotations

from typing import Any, Optional

from . import nfl as _nfl
from . import nba as _nba
from . import mlb as _mlb
from . import nhl as _nhl
from . import cfb as _cfb
from . import soccer as _soccer
from . import tennis as _tennis
from . import ufc as _ufc

_REGISTRY: dict[str, Any] = {
    "NFL": _nfl,
    "NBA": _nba,
    "MLB": _mlb,
    "NHL": _nhl,
    "CFB": _cfb,
    "Soccer": _soccer,
    "Tennis": _tennis,
    "UFC": _ufc,
}


def get_adapter(sport: str) -> Optional[Any]:
    return _REGISTRY.get(sport)


def enabled_sports() -> tuple[str, ...]:
    return tuple(_REGISTRY.keys())


__all__ = ["get_adapter", "enabled_sports"]
