"""HTTP routes for the Parlay-History (Save-on-Tap) feature.

Extracted from server.py during the 2026-06-24 monolith decomposition.
The data-layer module (`parlay_history.py`) is unchanged — these route
handlers just wire its functions onto FastAPI endpoints.

Mounted by `server.py` via `app.include_router(parlay_history_routes.router)`.
"""
from __future__ import annotations

from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import UserPublic
from deps import current_user, db, logger, strip_mongo

router = APIRouter(prefix="/api")


class SaveParlayRequest(BaseModel):
    legs: list[dict]                # the full pick objects from /api/picks/parlay
    mode: str = "standard"
    stake: float = 1.0


@router.post("/parlay/save")
async def parlay_save(
    req: SaveParlayRequest,
    user: Annotated[UserPublic, Depends(current_user)],
):
    try:
        from parlay_history import save_parlay
        doc = await save_parlay(
            db, user_id=user.id, legs=req.legs,
            mode=req.mode, stake=req.stake,
        )
        return strip_mongo(doc)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.exception("save_parlay failed")
        raise HTTPException(500, f"save failed: {e}")


@router.get("/parlay/history")
async def parlay_history_list(
    user: Annotated[UserPublic, Depends(current_user)],
    filter: Optional[str] = None,
    limit: int = 50,
):
    """List the user's saved parlays. `filter` = won | live | lost | all."""
    try:
        from parlay_history import list_history
        rows = await list_history(
            db, user_id=user.id, status_filter=filter, limit=limit,
        )
        return {"parlays": rows, "count": len(rows)}
    except Exception as e:
        raise HTTPException(500, f"list failed: {e}")


@router.get("/parlay/{parlay_id}")
async def parlay_detail(
    parlay_id: str,
    user: Annotated[UserPublic, Depends(current_user)],
):
    from parlay_history import get_parlay
    doc = await get_parlay(db, user_id=user.id, parlay_id=parlay_id)
    if not doc:
        raise HTTPException(404, "parlay not found")
    return doc


@router.delete("/parlay/{parlay_id}")
async def parlay_remove(
    parlay_id: str,
    user: Annotated[UserPublic, Depends(current_user)],
):
    from parlay_history import delete_parlay
    ok = await delete_parlay(db, user_id=user.id, parlay_id=parlay_id)
    return {"deleted": ok}


@router.post("/parlay/{parlay_id}/resettle")
async def parlay_resettle(
    parlay_id: str,
    user: Annotated[UserPublic, Depends(current_user)],
):
    """Force-run the leg-resolution chain (pick lookup → snapshot match
    → external adapter) for a single parlay. Returns the updated doc.

    Useful when the user sees a stuck 'pending' leg and doesn't want to
    wait for the periodic settler."""
    try:
        from parlay_history import resettle_parlay
        doc = await resettle_parlay(db, user_id=user.id, parlay_id=parlay_id)
        return strip_mongo(doc)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        logger.exception("resettle_parlay failed")
        raise HTTPException(500, f"resettle failed: {e}")
