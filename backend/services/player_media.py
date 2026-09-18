"""P3 — Player media authority (background ingest, cached lightweight URL).

canonical_player_id → player_media → { headshot_url, source, verified, team_logo }

Screen-time reads hit Mongo only (``player_media`` collection).  Discovery
(player_db lookup, ESPN/MLB headshot URL derivation) runs in the background
``ingest_player_media_for_picks`` job and is fail-open.  Fallback chain is
resolved by the client: verified headshot → provider headshot → team logo →
sport icon → initials.  Media failures are optional failures — never a
screen failure, never a Connection Hiccup, never a logout.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("lockscore.player_media")
COLLECTION = "player_media"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _media_key(pick: dict) -> Optional[str]:
    cid = pick.get("canonical_player_id")
    if cid:
        return str(cid)
    from services.player_meta_decorator import _pick_player_name, _canonical
    name = _pick_player_name(pick)
    if not name:
        return None
    return f"name:{(pick.get('sport') or '').lower()}:{_canonical(name)}"


async def get_player_media(db, key: str) -> Optional[dict]:
    return await db[COLLECTION].find_one({"media_key": key}, {"_id": 0})


async def ingest_player_media_for_picks(db, picks: list[dict], *, max_new: int = 200) -> dict:
    """Background job: resolve headshots for player-prop picks lacking a
    player_media row.  Mongo + in-process player_db only; no screen-time
    provider discovery."""
    from services.player_meta_decorator import (
        _pick_player_name, _pick_team_abbrev, _resolve_from_player_db, _looks_like_photo,
    )
    from services.espn_team_meta import lookup as team_lookup
    seen: set[str] = set()
    created = skipped = 0
    for p in picks:
        key = _media_key(p)
        if not key or key in seen:
            continue
        seen.add(key)
        if created >= max_new:
            break
        if await db[COLLECTION].find_one({"media_key": key}, {"_id": 1}):
            continue
        name = _pick_player_name(p)
        sport = p.get("sport") or ""
        if not name or not sport:
            continue
        try:
            row = await _resolve_from_player_db(db, sport, name, _pick_team_abbrev(p))
        except Exception:
            row = None
        photo = row.get("photo_url") if isinstance(row, dict) else None
        team_logo = None
        team_name = p.get("player_team") or p.get("player_team_name")
        if team_name:
            try:
                tm = await team_lookup(db, str(team_name), sport)
                team_logo = (tm or {}).get("logo")
            except Exception:
                team_logo = None
        doc = {
            "media_key": key,
            "canonical_player_id": p.get("canonical_player_id"),
            "player_name": name,
            "sport": sport,
            "headshot_url": photo if _looks_like_photo(photo) else None,
            "headshot_source": (row or {}).get("source") if isinstance(row, dict) else None,
            "headshot_verified": bool(photo and _looks_like_photo(photo) and isinstance(row, dict) and row.get("verified", True)),
            "team_logo": team_logo,
            "provenance": "background_ingest.player_db",
            "data_as_of": _now(),
        }
        if doc["headshot_url"] is None and team_logo is None:
            skipped += 1
        await db[COLLECTION].update_one({"media_key": key}, {"$set": doc}, upsert=True)
        created += 1
    return {"created": created, "no_media": skipped, "scanned": len(seen)}


async def overlay_player_media(db, picks: list[dict]) -> None:
    """Screen-time overlay: attach ``player_media`` from Mongo only."""
    keys = {k for k in (_media_key(p) for p in picks) if k}
    if not keys:
        return
    rows = {r["media_key"]: r async for r in db[COLLECTION].find(
        {"media_key": {"$in": sorted(keys)}},
        {"_id": 0, "media_key": 1, "headshot_url": 1, "headshot_verified": 1,
         "headshot_source": 1, "team_logo": 1})}
    for p in picks:
        k = _media_key(p)
        if k and k in rows:
            r = rows[k]
            p["player_media"] = {"headshot_url": r.get("headshot_url"),
                                 "verified": r.get("headshot_verified"),
                                 "source": r.get("headshot_source"),
                                 "team_logo": r.get("team_logo")}


__all__ = ["get_player_media", "ingest_player_media_for_picks", "overlay_player_media", "COLLECTION"]
