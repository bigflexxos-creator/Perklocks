"""Picks routes — Phase 1 extraction from server.py (2026-06-25).

This is the first slice of an incremental decomposition of the ~4,600-line
server.py monolith. Phase 1 extracts the simplest, lowest-coupling picks
endpoints into this dedicated router:

  • GET /api/picks/all              — full slate for the day (no filters)
  • GET /api/picks/nrfi-yrfi        — dedicated MLB 1st-inning feed
  • GET /api/picks/markets/{sport}  — markets + league counts for a sport
  • GET /api/picks/refresh-status   — cooldown payload for the refresh button

Phase 2 (next) will extract /picks/under-of-the-day, /picks/rollover,
/picks/history, /picks/{pick_id} + nested enrichment endpoints (~700 lines).

Phase 3 (last) extracts the monsters /picks/today and /picks/parlay.

Helpers from server.py (`_today_str`, `_filter_in_play_window`,
`_canonicalize_picks`, `_canonicalize_lock_score`, `_ensure_today_picks`,
`_cooldown_payload`, `SPORT_MARKETS`) are imported lazily inside each
handler to avoid the circular-import problem (server.py already imports
this module via api.include_router(router) at the bottom of its file).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends

from auth import UserPublic
from deps import current_user, db

router = APIRouter(prefix="/picks", tags=["picks"])


@router.get("/all")
async def picks_all(
    user: Annotated[UserPublic, Depends(current_user)],
    sport: Optional[str] = None,
):
    from server import _ensure_today_picks, _today_str, _canonicalize_picks  # lazy: avoid circular
    await _ensure_today_picks()
    q: dict = {"pick_date": _today_str()}
    if sport and sport.lower() != "all":
        q["sport"] = sport
    cursor = db.picks.find(q, {"_id": 0}).sort("lock_score", -1).limit(200)
    return {"picks": _canonicalize_picks(await cursor.to_list(length=200))}


@router.get("/nrfi-yrfi")
async def picks_nrfi_yrfi(user: Annotated[UserPublic, Depends(current_user)]):
    """Dedicated MLB NRFI/YRFI feed — these picks are intentionally
    excluded from the main /picks/today board (`hide_from_main_board`
    flag). Returns today's slate with full model audit-trail so the UI
    can show λ₁, pitcher/lineup/park factors per pick."""
    from server import _today_str, _filter_in_play_window, _canonicalize_lock_score  # lazy
    q = {
        "pick_date": _today_str(),
        "category": "nrfi_yrfi",
    }
    cursor = db.picks.find(q, {"_id": 0}).sort("lock_score", -1).limit(50)
    rows = await cursor.to_list(length=50)
    rows = _filter_in_play_window(rows)
    rows = [_canonicalize_lock_score(r) for r in rows]
    return {"picks": rows, "count": len(rows), "category": "nrfi_yrfi"}


@router.get("/markets/{sport}")
async def markets_for_sport(
    user: Annotated[UserPublic, Depends(current_user)],
    sport: str,
):
    """Return the dynamic market list + active leagues for a given sport.
    Used by the Locks tab to populate the MarketSelector + League pills.

    Critically, the league `count` MUST be computed from the SAME pick
    universe that `/picks/today` serves — i.e. after `_filter_in_play_window`
    drops games that have already started.
    """
    from server import SPORT_MARKETS, _today_str, _filter_in_play_window  # lazy
    markets = SPORT_MARKETS.get(sport, [])
    raw = await db.picks.find(
        {"sport": sport, "pick_date": _today_str()},
        {"_id": 0, "league": 1, "event_time": 1, "lock_score": 1,
         "is_under_lock": 1, "no_bet": 1, "edge_percent": 1,
         "elite_player": 1},
    ).to_list(length=1000)

    def _qualifies(p: dict) -> bool:
        if p.get("no_bet") is True:
            return False
        elite = bool(p.get("elite_player"))
        lock = float(p.get("lock_score") or 0)
        edge = float(p.get("edge_percent") or 0)
        if elite:
            return True
        return lock >= 85 and edge >= 0

    raw = [p for p in raw if _qualifies(p)]
    raw = _filter_in_play_window(raw)
    counts: dict[str, int] = {}
    for p in raw:
        lg = p.get("league")
        if not lg:
            continue
        counts[lg] = counts.get(lg, 0) + 1
    leagues = [{"name": name, "count": c}
               for name, c in sorted(counts.items(), key=lambda kv: -kv[1])]
    return {"sport": sport, "markets": markets, "leagues": leagues}


@router.get("/refresh-status")
async def refresh_status_pre(user: Annotated[UserPublic, Depends(current_user)]):
    """Return the user's current refresh cooldown WITHOUT triggering a
    refresh (zero Odds API cost). Declared BEFORE /picks/{pick_id} so
    FastAPI's route matching doesn't capture the literal segment as an
    ID. Logic lives in _cooldown_payload() in server.py."""
    from server import _cooldown_payload  # lazy
    now = datetime.now(timezone.utc)
    user_doc = await db.users.find_one(
        {"id": user.id}, {"_id": 0, "last_refresh_at": 1},
    )
    last_iso = (user_doc or {}).get("last_refresh_at")
    return _cooldown_payload(last_iso, now)
