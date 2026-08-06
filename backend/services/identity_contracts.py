"""identity_contracts — Phase 3D typed identity contracts.

Immutable dataclasses that describe stable identity for every entity
we persist.  These contracts are compatibility-first: they do NOT
replace existing fields, and Phase 3D uses them in DRY-RUN mode only.

Six identities:
  * EventIdentity            — one canonical id per game/match
  * TeamIdentity             — one canonical id per club/franchise
  * PlayerIdentity           — one canonical id per athlete
  * MarketContractIdentity   — one canonical id per (event, market, side, line, book)
  * PredictionIdentity       — one canonical id per published prediction
  * BetLegIdentity           — one canonical id per user parlay leg

Every contract carries an ``identity_quality`` marker so downstream
consumers can distinguish "provider-verified" from "name-based
fallback" identity without silent trust.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional


IdentityQuality = Literal["provider", "canonical", "fallback", "ambiguous", "unresolved"]


@dataclass(frozen=True)
class EventIdentity:
    canonical_event_id:  str
    provider:            Optional[str]  = None
    provider_event_id:   Optional[str]  = None
    sport_key:           Optional[str]  = None
    commence_time:       Optional[str]  = None
    home_team_id:        Optional[str]  = None
    away_team_id:        Optional[str]  = None
    identity_quality:    IdentityQuality = "canonical"
    notes:               str            = ""


@dataclass(frozen=True)
class TeamIdentity:
    canonical_team_id:   str
    provider:            Optional[str]  = None
    provider_team_id:    Optional[str]  = None
    display_name:        Optional[str]  = None
    aliases:             tuple[str, ...] = field(default_factory=tuple)
    sport:               Optional[str]  = None
    identity_quality:    IdentityQuality = "canonical"
    notes:               str            = ""


@dataclass(frozen=True)
class PlayerIdentity:
    canonical_player_id: str
    provider:            Optional[str]  = None
    provider_player_id:  Optional[str]  = None
    display_name:        Optional[str]  = None
    team_id:             Optional[str]  = None
    sport:               Optional[str]  = None
    position:            Optional[str]  = None
    identity_quality:    IdentityQuality = "canonical"
    notes:               str            = ""


@dataclass(frozen=True)
class MarketContractIdentity:
    """A market contract must be unique across event × market × side ×
    line × bookmaker.  Missing any of these fields degrades identity."""
    canonical_market_contract_id: str
    canonical_event_id:  str
    market_key:          str
    participant_id:      Optional[str]  = None   # canonical_player_id or canonical_team_id
    side:                Optional[str]  = None   # over/under/yes/no/home/away
    line:                Optional[float] = None
    bookmaker:           Optional[str]  = None
    provider_market_key: Optional[str]  = None
    odds_timestamp:      Optional[str]  = None
    identity_quality:    IdentityQuality = "canonical"
    notes:               str            = ""


@dataclass(frozen=True)
class PredictionIdentity:
    prediction_id:                str
    snapshot_id:                  Optional[str] = None
    canonical_event_id:           Optional[str] = None
    canonical_market_contract_id: Optional[str] = None
    publication_version:          Optional[int] = None
    identity_quality:             IdentityQuality = "canonical"


@dataclass(frozen=True)
class BetLegIdentity:
    user_bet_id:                  str
    leg_id:                       str
    prediction_id:                Optional[str] = None
    snapshot_id:                  Optional[str] = None
    canonical_market_contract_id: Optional[str] = None
    identity_quality:             IdentityQuality = "canonical"


__all__ = [
    "IdentityQuality",
    "EventIdentity",
    "TeamIdentity",
    "PlayerIdentity",
    "MarketContractIdentity",
    "PredictionIdentity",
    "BetLegIdentity",
]
