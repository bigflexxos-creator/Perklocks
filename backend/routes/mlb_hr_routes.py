"""MLB Home-Run Tab routes.

Mounts `/api/mlb/hr-slate` — backing endpoint for the new HR tab in the
mobile app. Wraps `services.mlb_hr_intel.build_hr_slate` so the heavy
work (Statcast park factors × pitcher HR/9 × batter ISO/HR-PA × Open-Meteo
wind/temp × roof × H2H BvP) lives in one place.

Auth: requires a logged-in user (uses the same Bearer-token dependency
as the rest of /api).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

logger = logging.getLogger("lockscore.mlb_hr_routes")

# `deps.py` exposes `db` (the motor handle) and the auth dependency.
from deps import db, current_user, UserPublic   # type: ignore

router = APIRouter(prefix="/api/mlb", tags=["mlb-hr"])


@router.get("/hr-slate")
async def get_hr_slate(
    user: Annotated[UserPublic, Depends(current_user)],
    date: Optional[str] = Query(default=None,
        description="YYYY-MM-DD (defaults to today UTC)"),
    refresh: bool = Query(default=False,
        description="If true, bypass the 25-min cache layer"),
) -> dict[str, Any]:
    """Build / fetch the HR slate for the requested date.

    Response shape:
    ```
    {
      "date": "2026-06-30",
      "as_of": "<iso>",
      "games": [GameHRSlate, ...],     # at most one per game today
      "total_picks": int,
    }
    ```
    """
    try:
        from services.mlb_hr_intel import build_hr_slate
    except Exception as e:
        logger.exception("mlb_hr_intel import failed: %s", e)
        raise HTTPException(500, detail="HR intelligence engine unavailable")

    date_iso = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Optional cache bust.
    if refresh:
        try:
            await db.mlb_hr_slate.delete_one({"_id": date_iso})
        except Exception:
            pass

    try:
        slate = await build_hr_slate(db, date=date_iso)
    except Exception as e:
        logger.exception("HR slate build failed: %s", e)
        raise HTTPException(500, detail=f"HR slate build error: {e}")

    return {
        "date": date_iso,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "games": slate,
        "total_picks": sum(len(g.get("picks") or []) for g in slate),
    }
