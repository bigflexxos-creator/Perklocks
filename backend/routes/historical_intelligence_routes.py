"""GET /api/picks/{canonical_pick_id}/historical-intelligence

Session 4 · Universal Historical Intelligence on-demand endpoint.
This route lives OUTSIDE the mobile Locks lite hot path — Pick
Breakdown loads it lazily after the card opens.  Locks stays
lightweight; deep history stays reachable.
"""
from __future__ import annotations

from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from deps import current_user
from auth import UserPublic
from services.historical_intelligence import (
    HistoricalQuery,
    query_historical,
    _nfl_market_family_from_market,
)


def _get_db():
    from server import db as _db
    return _db

router = APIRouter(prefix="/api/picks", tags=["historical-intelligence"])


def _resolve_market_family(sport: str, market: str) -> Optional[str]:
    if not market:
        return None
    if sport == "NFL":
        return _nfl_market_family_from_market(market)
    # Other-sport wiring stays a Session 5 item; the endpoint still
    # dispatches so the caller receives a shaped (empty) response.
    m = (market or "").lower()
    # Cheap heuristics so MLB/Soccer/Tennis/CFB return something
    # semantically meaningful even before their adapters land.
    if sport == "MLB":
        if "hit" in m and "run" not in m: return "hits"
        if "total base" in m:              return "total_bases"
        if "home run" in m or "hr" in m:   return "home_runs"
        if "rbi" in m:                     return "rbi"
        if "strikeout" in m or " k" in m:  return "strikeouts"
        if "run line" in m:                return "run_line"
        if "run" in m:                     return "runs"
    if sport == "Soccer":
        if "goal" in m:      return "goals"
        if "shot on" in m:   return "sot"
        if "shot" in m:      return "shots"
        if "assist" in m:    return "assists"
    if sport == "Tennis":
        if "spread" in m:    return "game_spread"
        if "total" in m:     return "game_total"
        return "moneyline"
    if sport == "CFB":
        if "spread" in m: return "spread"
        if "total" in m:  return "total"
        return "moneyline"
    return None


def _resolve_side(market: str) -> str:
    m = (market or "").lower()
    if "under" in m: return "under"
    if "over" in m:  return "over"
    return "over"


def _resolve_threshold(pick: dict) -> Optional[float]:
    for key in ("line", "point", "spread", "total"):
        v = pick.get(key)
        if v is None:
            continue
        try:
            return float(v)
        except Exception:
            continue
    # Extract from market string, e.g. "Joe Burrow Over 225.5 Passing Yards"
    import re
    market = pick.get("market") or ""
    m = re.search(r"[-+]?\d+(?:\.\d+)?", market)
    if m:
        try:
            return float(m.group(0))
        except Exception:
            return None
    return None


@router.get("/{pick_id}/historical-intelligence")
async def historical_intelligence(
    user: Annotated[UserPublic, Depends(current_user)],
    pick_id: str,
    sample_scope: str = Query("L10", pattern="^(L5|L10|L20|SEASON|ALL)$"),
    venue_scope:  str = Query("ALL", pattern="^(ALL|HOME|AWAY)$"),
    context_scope: Optional[str] = None,
):
    pick = await _get_db().picks.find_one({"id": pick_id})
    if not pick:
        raise HTTPException(404, "canonical pick not found")

    sport         = pick.get("sport")
    market        = pick.get("market") or ""
    entity_type   = "player" if pick.get("canonical_player_id") or pick.get("player_name") else "team"
    entity_id     = (
        pick.get("canonical_player_id")
        or pick.get("player_id")
        or pick.get("canonical_team_id")
        or pick.get("team")
        or ""
    )
    entity_name   = pick.get("player_name") or pick.get("team")
    opponent_id   = pick.get("canonical_opponent_id")
    opponent_name = pick.get("opponent")

    q = HistoricalQuery(
        sport=sport,
        entity_type=entity_type,
        entity_id=entity_id,
        entity_name=entity_name,
        opponent_id=opponent_id,
        opponent_name=opponent_name,
        market_family=_resolve_market_family(sport, market) or "",
        current_threshold=_resolve_threshold(pick),
        sample_scope=sample_scope,
        venue_scope=venue_scope,
        context_scope=context_scope,
        side=_resolve_side(market),
    )
    resp = await query_historical(_get_db(), q)
    return resp.to_dict()
