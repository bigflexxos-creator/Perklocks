"""Phase 4C finalization — Admin-only MLB rejection-counter diagnostics.

Mounts under ``/api/admin/mlb/rejections`` and returns the current
snapshot of the structured rejection counter registered in
:mod:`services.mlb_gates`.  Zero provider secrets are exposed.  No
production writes.
"""
from __future__ import annotations

from typing import Annotated, Optional

from fastapi import APIRouter, Depends

from auth import UserPublic, require_admin_user, oauth2_scheme
from deps import db
from services import mlb_gates

router = APIRouter(prefix="/api/admin/mlb", tags=["admin"])


async def _current_admin(
    token: Annotated[Optional[str], Depends(oauth2_scheme)],
) -> UserPublic:
    return await require_admin_user(db, token)


@router.get("/rejections")
async def get_mlb_rejections(
    _user: Annotated[UserPublic, Depends(_current_admin)],
) -> dict:
    """Admin-only snapshot of MLB rejection counters."""
    return mlb_gates.snapshot()


@router.post("/rejections/reset")
async def reset_mlb_rejections(
    _user: Annotated[UserPublic, Depends(_current_admin)],
) -> dict:
    """Reset MLB rejection counters — admin only."""
    mlb_gates.reset()
    return {"ok": True, "reset_at": mlb_gates.snapshot()["since"]}


__all__ = ["router"]
