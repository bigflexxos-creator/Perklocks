"""HTTP routes for admin / ops endpoints.

Covers:
  • Soccer player form refresh trigger (Understat scrape)
  • Tennis Elo ledger backfill
  • Historical Sports Intelligence Engine (backfill / status / lookup)

Extracted from server.py during the 2026-06-24 monolith decomposition.
No behavior change — only relocation. Mounted by `server.py` via
`app.include_router(admin_routes.router)`.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import UserPublic
from deps import current_user, db

router = APIRouter(prefix="/api")


# ────────────────────── Soccer / Tennis ops ──────────────────────
@router.post("/admin/refresh-soccer-player-form")
async def admin_refresh_soccer_player_form(
    user: Annotated[UserPublic, Depends(current_user)],
):
    """Manually kick the Understat scrape job. Used by ops + initial
    seed. Returns the same summary dict the background loop logs every
    12h. Guarded by auth — any logged-in user can trigger because the
    cost is bounded (5 Understat POSTs).
    """
    from soccer_player_form import refresh_soccer_player_form
    return await refresh_soccer_player_form(db)


@router.post("/admin/backfill-tennis-elo")
async def admin_backfill_tennis_elo(
    user: Annotated[UserPublic, Depends(current_user)],
    days_back: int = 30,
):
    """One-shot ops tool to seed the tennis_extra Elo + form ledger
    from the last `days_back` days of ESPN ATP/WTA results.

    SAFE TO RE-RUN ONCE on a fresh DB. Re-running on a populated DB
    will double-count form W/L (since `update_after_match` is
    `$inc`-based) — only re-trigger if you've reset the
    `tennis_players` collection first.
    """
    from espn_settlement import backfill_tennis_elo
    return await backfill_tennis_elo(db, days_back=max(1, min(60, days_back)))


# ────────────────────── Historical Sports Intelligence Engine ──────────────────────
class HistoricalBackfillRequest(BaseModel):
    sports: list[str] | None = None  # default: all 5 sports
    mode: str = "backfill"           # "backfill" | "incremental"
    days: int | None = None          # incremental: how many days back (default 3)


@router.post("/admin/historical/backfill")
async def historical_backfill(
    req: HistoricalBackfillRequest,
    user: Annotated[UserPublic, Depends(current_user)] = None,
):
    """Trigger a current-season backfill (or incremental sync) for one
    or more sports. Returns per-sport summary.

    Note: Soccer (football-data.org) is paced at 6.5s/req due to the
    strict 10 req/min free-tier limit — a full backfill of 8
    competitions can take ~3-5 minutes. Other sports complete much
    faster.
    """
    try:
        from historical.orchestrator import backfill_current_season, incremental_sync
    except Exception as e:
        raise HTTPException(500, f"Historical engine not loaded: {e}")
    sports = req.sports or ["mlb", "nba", "nfl", "nhl", "soccer"]
    if req.mode == "incremental":
        since_override = None
        if req.days and req.days > 0:
            since_override = datetime.now(timezone.utc) - timedelta(days=int(req.days))
        out = await incremental_sync(sports=sports, since_override=since_override)
    else:
        out = await backfill_current_season(sports=sports)
    return {"mode": req.mode, "sports": sports, "results": out}


@router.get("/admin/historical/status")
async def historical_status(
    user: Annotated[UserPublic, Depends(current_user)] = None,
):
    """Quick summary of what's stored in the historical engine."""
    counts = {}
    for col in ("players", "games", "player_game_logs", "season_totals", "team_form"):
        try:
            counts[col] = await db[col].estimated_document_count()
        except Exception:
            counts[col] = -1
    last_syncs = {}
    async for doc in db.historical_meta.find({}):
        last_syncs[doc.get("_id")] = doc.get("last_sync")
    return {"collections": counts, "last_syncs": last_syncs}


@router.get("/admin/historical/player-form")
async def historical_player_form(
    sport: str,
    name: str,
    market: Optional[str] = None,
    user: Annotated[UserPublic, Depends(current_user)] = None,
):
    """Look up the stored form summary for a player (debug + transparency)."""
    try:
        from historical.lookup import get_player_form
        out = await get_player_form(sport, name, market_hint=market)
        return out or {"found": False, "sport": sport, "name": name}
    except Exception as e:
        raise HTTPException(500, f"lookup failed: {e}")
