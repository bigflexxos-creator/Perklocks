"""Soccer settlement bridge (PitchAPI primary, Big Balls fallback).

Scaffold-only glue that the existing canonical settlement service can
opt in to per-market via ``wire=True``. This module does NOT create a
parallel settlement path — it only fetches a single authoritative
actual for a completed Soccer fixture and hands it back for the
existing canonical settler to consume.

Flow (per P3):
  published Soccer pick
    → completed fixture
    → PitchAPI (primary)
    → Big Balls (fallback when PitchAPI returns DATA_UNAVAILABLE /
       MARKET_UNSUPPORTED / PROVIDER_ERROR / AUTH_FAIL)
    → canonical actual → EXISTING canonical settler → History

Never averages conflicting provider values. If both providers return
OK with different values → DATA_CONFLICT (never settle).

Never guesses WIN/LOSS/PUSH. Only returns the authoritative actual.
"""
from __future__ import annotations

from typing import Optional

from services.providers.pitchapi import (
    get_completed_actual as pitchapi_actual,
    SUPPORTED_MARKETS as PITCHAPI_MARKETS,
    ProviderResult,
)
from services.providers.bigballs import (
    get_completed_actual as bigballs_actual,
    SUPPORTED_MARKETS_CROSS_SPORT as BIGBALLS_MARKETS,
)

TERMINAL_MISS_STATUSES = frozenset({
    "DATA_UNAVAILABLE", "MARKET_UNSUPPORTED",
    "AUTH_FAIL", "PROVIDER_ERROR",
})


async def resolve_completed_actual(
    db, *, sport: str, canonical_event_id: str,
    market_family: str, canonical_player_id: Optional[str] = None,
    player_name: Optional[str] = None,
    force_refresh: bool = False,
) -> ProviderResult:
    """Return the authoritative completed actual, or a MISS.

    Reads PitchAPI first (Soccer only). Falls back to Big Balls if
    PitchAPI does not cover the family or returns a terminal miss.
    Cache is respected on both providers.

    `canonical_event_id` MUST be the PROVIDER match id (``m_<slug>``
    for PitchAPI, or Big Balls match id).  Callers use
    ``soccer_fixture_resolver.resolve_fixture`` to obtain both IDs.
    """
    sport_l = (sport or "").lower()
    primary: Optional[ProviderResult] = None
    if sport_l == "soccer" and market_family in PITCHAPI_MARKETS:
        primary = await pitchapi_actual(
            db, sport=sport, canonical_event_id=canonical_event_id,
            market_family=market_family,
            canonical_player_id=canonical_player_id,
            player_name=player_name,
            force_refresh=force_refresh,
        )
        if primary.status == "OK":
            return primary
        if primary.status not in TERMINAL_MISS_STATUSES:
            return primary
    # PitchAPI miss (or non-Soccer / unsupported family) → Big Balls fallback.
    if market_family in BIGBALLS_MARKETS:
        secondary = await bigballs_actual(
            db, sport=sport, canonical_event_id=canonical_event_id,
            market_family=market_family,
            canonical_player_id=canonical_player_id,
            force_refresh=force_refresh,
        )
        # Attach primary provenance so downstream can see the cascade.
        if primary is not None:
            secondary.provenance = dict(secondary.provenance or {})
            secondary.provenance["primary_miss"] = {
                "provider": primary.provider,
                "status":   primary.status,
                "detail":   primary.error_detail,
            }
        return secondary
    # Neither provider covers this market — the settlement gate must
    # keep the market SETTLEMENT_UNSUPPORTED.
    return ProviderResult(
        status="MARKET_UNSUPPORTED",
        provider="cascade",
        canonical_event_id=canonical_event_id,
        canonical_player_id=canonical_player_id,
        error_detail=f"neither PitchAPI nor Big Balls covers {market_family!r}",
    )


__all__ = ["resolve_completed_actual", "ProviderResult"]
