"""NFL Player Props 2.0 — admin discovery endpoint.

Read-only endpoint that runs the universal engine over the CURRENT
slate and returns the full trace.  Does NOT mutate any board,
publication, or Locks state.  Safe to call ad-hoc from admin tools.
"""
from __future__ import annotations

from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Query

from deps import current_admin, db, logger
from auth import UserPublic
from services.nfl_props_v2.slate_orchestrator import discover_slate_best_bets
from services.nfl_props_v2.engine import evaluate_player_across_markets


router = APIRouter(prefix="/api/admin/nfl-props-v2", tags=["admin-nfl-props-v2"])


@router.get("/discover")
async def nfl_props_v2_discover(
    user: Annotated[UserPublic, Depends(current_admin)],
    pick_date: Optional[str] = Query(default=None),
    max_players_per_position: int = Query(default=3, ge=1, le=8),
):
    """Slate-wide discovery.  Returns the compact acceptance-test
    payload: 2 QB / 2 RB / 2 WR / 2 TE evaluations by default."""
    return await discover_slate_best_bets(
        db, pick_date=pick_date,
        max_players_per_position=max_players_per_position,
    )


@router.get("/evaluate")
async def nfl_props_v2_evaluate(
    user: Annotated[UserPublic, Depends(current_admin)],
    player_name: str,
    team: Optional[str] = None,
    opponent: Optional[str] = None,
    home_team: Optional[str] = None,
    away_team: Optional[str] = None,
    canonical_player_id: Optional[str] = None,
    position: Optional[str] = None,
):
    """One-player deep evaluation."""
    game = {"home_team": home_team, "away_team": away_team}
    ev = await evaluate_player_across_markets(
        db, player_name=player_name,
        canonical_player_id=canonical_player_id,
        position=position, team=team, opponent=opponent,
        game=game,
    )
    return ev.as_dict()
