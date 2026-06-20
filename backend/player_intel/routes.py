"""Player Intelligence — FastAPI routes.

Exposes:
  GET  /api/player-intel/profile?name=<X>&sport=<Y>   single profile (resolved)
  GET  /api/player-intel/list?sport=<Y>&q=<text>      browseable list
  POST /api/player-intel/refresh                       force rebuild (admin)
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .refresh_job import COLLECTION, refresh_player_profiles
from .resolver import resolve_player

router = APIRouter(tags=["player_intel"])


def _get_db():
    from server import db
    return db


def _require_auth():
    from server import current_user
    return current_user


@router.get("/player-intel/profile")
async def player_intel_profile(
    name: str,
    sport: str,
    user=Depends(_require_auth()),
):
    """Resolve a player by name (or alias / market string) and return the
    enriched profile. Always returns something — empty fields when unknown."""
    if not name or not sport:
        raise HTTPException(status_code=400, detail="name + sport required")
    db = _get_db()
    profile = resolve_player(name, sport)
    # Augment with the most-recent learned stats from Mongo if we have them.
    learned = await db[COLLECTION].find_one(
        {"sport": sport, "canonical_name": profile.get("canonical_name")},
        {"_id": 0},
    )
    if learned:
        # Merge — DB data wins for stats; seed data wins for archetype if
        # archetype_source==seed.
        for key, val in learned.items():
            if key == "archetype" and profile.get("archetype_source") == "seed":
                continue
            profile[key] = val
    return {"profile": profile}


@router.get("/player-intel/list")
async def player_intel_list(
    sport: str,
    q: str | None = None,
    limit: int = 50,
    user=Depends(_require_auth()),
):
    """Browse profiles — used by the future Player Profile UI."""
    db = _get_db()
    query: dict = {"sport": sport}
    if q:
        query["$or"] = [
            {"canonical_name": {"$regex": q, "$options": "i"}},
            {"aliases":        {"$regex": q, "$options": "i"}},
        ]
    out: list[dict] = []
    async for row in (
        db[COLLECTION]
        .find(query, {"_id": 0})
        .sort([("usage_intensity", -1), ("sample_size", -1)])
        .limit(min(max(limit, 1), 200))
    ):
        out.append(row)
    return {"sport": sport, "count": len(out), "players": out}


@router.post("/player-intel/refresh")
async def player_intel_refresh(user=Depends(_require_auth())):
    """Force-rebuild the catalog from settled picks. Idempotent."""
    db = _get_db()
    return await refresh_player_profiles(db)
