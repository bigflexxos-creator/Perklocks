"""FastAPI router for the Survivability Engine.

Mounted under /api/picks/{pick_id}/coverage in server.py.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

router = APIRouter(prefix="/picks", tags=["survival"])


def _get_db():
    from server import db
    return db


def _require_auth():
    from server import current_user
    return current_user


@router.get("/{pick_id}/coverage")
async def coverage_for_pick(pick_id: str, user = Depends(_require_auth())):
    """Return conditional-hit coverage for an MLB hits prop.

    Pure insight — never modifies the underlying pick. For non-hits
    props (or non-MLB) we return an explanatory `note` with empty
    candidates so the UI can show a clean "Coverage not applicable"
    state.
    """
    db = _get_db()
    pick = await db.picks.find_one({"id": pick_id}, {"_id": 0})
    if not pick:
        raise HTTPException(status_code=404, detail="Pick not found")

    from .pipeline import compute_coverage_for_pick
    return await compute_coverage_for_pick(pick, db)
