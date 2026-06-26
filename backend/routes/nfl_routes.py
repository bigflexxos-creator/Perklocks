"""HTTP routes for the NFL intelligence engines.

Exposes:
  • Safe-Bets engine  — `GET /api/nfl/safe-bets`
       Highest TRUE win-probability player-prop picks across rushing,
       receiving, receptions, passing, ATD. Filtered by ALT RULES
       (median ≥ line, floor p10 ≥ line, ≥10 attempts, ≥5 games, no
       single-game outlier inflation).

  • ATD Leaderboard   — `GET /api/nfl/atd/leaderboard`
       Per-player TRUE probability of scoring ≥ 1 TD, ranked by
       confidence. Neutral matchup unless caller supplies opponent.

  • ATD Predict       — `GET /api/nfl/atd/predict`
       Single-player matchup-adjusted prediction. Accepts optional
       `opponent` (team displayName) and `spread`.

Authentication: read-only public — no admin gate. Heavy aggregates
are bounded by MIN_GAMES_SAMPLE / volume floors so a noisy caller can't
DOS the DB.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from deps import db

router = APIRouter(prefix="/api/nfl")


@router.get("/safe-bets")
async def nfl_safe_bets(
    limit: int = Query(10, ge=1, le=50),
    min_probability: float = Query(0.78, ge=0.5, le=0.99),
):
    """Top NFL player-prop locks ranked by TRUE probability, not edge.

    Default `min_probability=0.78` (≈ -355 American odds). The user-locked
    "preferred" range is `≥ 0.857` (-600 American or shorter).
    """
    try:
        from nfl_safe_engine import compute_safe_bets
        return await compute_safe_bets(
            db, limit=limit, min_probability=min_probability,
        )
    except Exception as e:
        raise HTTPException(500, f"nfl safe-bets failed: {e}")


@router.get("/atd/leaderboard")
async def nfl_atd_leaderboard(
    limit: int = Query(20, ge=1, le=100),
    min_probability: float = Query(0.30, ge=0.05, le=0.95),
    min_opportunity_rating: str = Query("med", regex="^(low|med|high)$"),
):
    """Rank every eligible NFL player by P(TD ≥ 1) under a neutral matchup.

    `min_opportunity_rating` filters by L10 weighted touch volume:
      low: ≥0  ·  med: ≥7.0  ·  high: ≥12.0 touches/game.
    """
    try:
        from nfl_atd_engine import atd_leaderboard
        return await atd_leaderboard(
            db,
            limit=limit,
            min_probability=min_probability,
            min_opportunity_rating=min_opportunity_rating,
        )
    except Exception as e:
        raise HTTPException(500, f"nfl atd leaderboard failed: {e}")


@router.get("/atd/predict")
async def nfl_atd_predict(
    player_id: str = Query(..., min_length=2),
    opponent: Optional[str] = Query(None, description="Opponent team displayName, e.g. 'Dallas Cowboys'"),
    spread: Optional[float] = Query(None, description="Player's TEAM spread (negative = favored)"),
):
    """Single-player ATD prediction with optional matchup + game script."""
    try:
        from nfl_atd_engine import predict_player_atd
        out = await predict_player_atd(
            db, player_id=player_id, opponent=opponent, spread=spread,
        )
        if out.get("reject"):
            raise HTTPException(422, f"Rejected: {out['reject']} · {out}")
        return out
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"nfl atd predict failed: {e}")
