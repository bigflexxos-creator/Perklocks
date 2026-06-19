"""FastAPI router for the new soccer module.

Mounted under /api/sports/soccer in server.py. None of these endpoints
disturb the existing pick generator — they live in a fully isolated
namespace.
"""
from __future__ import annotations

from datetime import date as date_type
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from .cache import cache
from .client import SoccerAPIError, client, get_quota_snapshot
from .pipeline import run_prediction_pipeline

router = APIRouter(prefix="/sports/soccer", tags=["soccer-api"])


def _get_db():
    # Late import so this module never pulls the rest of server.py at
    # import time — avoids the circular dep that bit us with the
    # MLB module earlier.
    from server import db
    return db


def _require_auth():
    from server import current_user, UserPublic  # noqa: F401
    return current_user


@router.get("/health")
async def soccer_health(user = Depends(_require_auth())):
    """Status + last-seen quota + cache hit rate. Costs 0 upstream calls."""
    return {
        "module":      "soccer.v1.0.0-mvp",
        "rate_limit":  get_quota_snapshot(),
        "cache":       cache.stats(),
    }


@router.get("/fixtures")
async def soccer_fixtures(
    user = Depends(_require_auth()),
    target_date: str | None = None,
    timezone: str = "UTC",
):
    """Today's soccer fixtures (or any date via `target_date=YYYY-MM-DD`).
    Cached for 15 min upstream so spamming this is free."""
    d = date_type.today() if not target_date else date_type.fromisoformat(target_date)
    try:
        data = await client.fixtures_by_date(d, timezone=timezone)
    except SoccerAPIError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return data


@router.get("/standings/{league_id}/{season}")
async def soccer_standings(league_id: int, season: int, user = Depends(_require_auth())):
    try:
        return await client.standings(league_id, season)
    except SoccerAPIError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/lineups/{fixture_id}")
async def soccer_lineups(fixture_id: int, user = Depends(_require_auth())):
    try:
        return await client.lineups(fixture_id)
    except SoccerAPIError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/predictions")
async def soccer_predictions_list(
    user = Depends(_require_auth()),
    min_confidence: float = 0.0,
    limit: int = 200,
):
    """Read-only view of this module's predictions — the canonical
    `soccer_predictions` collection. The same picks also flow into the
    main `picks` collection (when conf >= 85) so they show up in the
    Locks/Killer/Rollover tabs; this endpoint just exposes the raw
    soccer-specific output for debugging + future Soccer-AI UI."""
    db = _get_db()
    today = date_type.today().isoformat()
    cursor = db.soccer_predictions.find(
        {"confidence": {"$gte": min_confidence},
         "created_at": {"$gte": today}},     # only today’s
        {"_id": 0},
    ).sort("confidence", -1).limit(min(limit, 500))
    rows = await cursor.to_list(length=limit)
    return {"count": len(rows), "predictions": rows}


@router.post("/refresh")
async def soccer_refresh(user = Depends(_require_auth())):
    """Manually trigger the pipeline — fire-and-forget so the HTTP call
    returns immediately while the pipeline (which takes ~90s due to
    rate-limit pacing) runs in the background.

    Honours the 15-min cache TTL on individual football-data.org calls
    so button-mashing is harmless. Read predictions back via
    `GET /predictions` once the run completes (logged in the backend).
    """
    import asyncio as _asyncio
    db = _get_db()
    _asyncio.create_task(run_prediction_pipeline(db))
    return {
        "status": "started",
        "note": "Pipeline runs in background (~90s). Check /predictions for results.",
    }


@router.get("/accuracy")
async def soccer_accuracy(user = Depends(_require_auth())):
    """Return the latest rollup of soccer prediction accuracy.

    Populated by the daily backfill loop. Costs 0 upstream calls —
    reads the precomputed `soccer_accuracy` rollup doc."""
    db = _get_db()
    doc = await db.soccer_accuracy.find_one({"_id": "rollup"}, {"_id": 0})
    if not doc:
        return {
            "total": 0,
            "correct": 0,
            "accuracy": 0.0,
            "by_model_version": [],
            "by_pick_side": [],
            "by_league": [],
            "by_confidence_bucket": [],
            "note": "Backfill hasn't run yet — accuracy populates after the first graded match.",
        }
    return doc


@router.post("/backfill")
async def soccer_backfill(user = Depends(_require_auth())):
    """Manually trigger the historical backfill — grades any
    predictions whose match has finished in the lookback window."""
    from .backfill import run_backfill
    db = _get_db()
    return await run_backfill(db)
