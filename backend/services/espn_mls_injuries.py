"""ESPN MLS team injury / rest-day ingest.

Scrapes ESPN's team injury JSON for every MLS club and stores in
`espn_mls_injuries` MongoDB collection. Consumed by
`services/espn_mls_stats._espn_mls_scorer_picks` (via `is_player_available`)
to skip goalscorer / SoA picks when the player is listed
Out / Day-to-Day / Suspended / Questionable+injury.

User report 2026-07-22: "Messi on rest you should know this" — Messi
picks kept surfacing even when he was rested for Miami. This module
adds the missing awareness.

Endpoint (public, no auth):
  https://sports.core.api.espn.com/v2/sports/soccer/leagues/usa.1/
    teams/{team_id}/injuries?lang=en&region=us
"""
from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger("lockscore.espn_mls_injuries")

_TEAMS_URL = (
    "https://sports.core.api.espn.com/v2/sports/soccer/leagues/usa.1/"
    "seasons/{year}/teams?limit=100"
)
_TEAM_INJURIES_URL = (
    "https://sports.core.api.espn.com/v2/sports/soccer/leagues/usa.1/"
    "teams/{team_id}/injuries?lang=en&region=us"
)


def _norm(name: str) -> str:
    if not name:
        return ""
    nk = unicodedata.normalize("NFKD", name)
    return "".join(c for c in nk if not unicodedata.combining(c)).lower().strip()


async def _get_json(cx: httpx.AsyncClient, url: str) -> Optional[dict]:
    try:
        r = await cx.get(url)
        if r.status_code == 200:
            return r.json()
    except Exception:
        return None
    return None


def _extract_id(url: str) -> Optional[str]:
    m = re.search(r"/teams/(\d+)", url or "")
    return m.group(1) if m else None


async def refresh_mls_injuries(season: int = 2025) -> dict:
    """Pull injury list for every MLS team and upsert into
    `espn_mls_injuries` collection.

    Doc shape (per player, one row):
      { "_id": "<team_id>:<athlete_id>",
        "player_id": "45843", "name": "Lionel Messi",
        "name_norm": "lionel messi",
        "team_id": "20232", "team": "Inter Miami CF",
        "status": "Day-To-Day", "description": "Resting after ...",
        "date": "2026-07-22T14:00Z",
        "unavailable": True,
        "refreshed_at": ISO }
    """
    from deps import db

    async with httpx.AsyncClient(timeout=15) as cx:
        # Discover team IDs.
        teams_blob = await _get_json(cx, _TEAMS_URL.format(year=season))
        if not teams_blob:
            return {"ok": False, "reason": "teams_list_fetch_failed"}

        # Team list can be nested — items[] with $ref to each team.
        team_refs = []
        for item in teams_blob.get("items", []):
            ref = item.get("$ref") if isinstance(item, dict) else None
            tid = _extract_id(ref or "")
            if tid:
                team_refs.append(tid)
        if not team_refs:
            return {"ok": False, "reason": "no_team_ids"}

        sem = asyncio.Semaphore(6)

        async def _pull_team(tid: str) -> list[dict]:
            async with sem:
                inj_blob = await _get_json(
                    cx, _TEAM_INJURIES_URL.format(team_id=tid),
                )
                if not inj_blob:
                    return []
                rows = []
                for item in inj_blob.get("items", []):
                    if not isinstance(item, dict):
                        continue
                    ath_ref = (item.get("athlete") or {}).get("$ref") or ""
                    aid = re.search(r"/athletes/(\d+)", ath_ref)
                    if not aid:
                        continue
                    aid = aid.group(1)
                    # Athlete name — dereference (small overhead, ~1 call).
                    ath_data = await _get_json(cx, ath_ref) if ath_ref else None
                    name = ""
                    if ath_data:
                        name = (ath_data.get("fullName")
                                or ath_data.get("displayName")
                                or "")
                    status = item.get("status") or item.get("type") or ""
                    detail = item.get("shortComment") or item.get("longComment") or ""
                    date = item.get("date") or ""
                    unavailable = str(status).lower() in {
                        "out", "day-to-day", "suspended", "injured",
                        "doubtful",
                    } or "rest" in (detail or "").lower()
                    rows.append({
                        "_id": f"{tid}:{aid}",
                        "player_id": aid,
                        "name": name,
                        "name_norm": _norm(name),
                        "team_id": tid,
                        "status": status,
                        "description": detail,
                        "date": date,
                        "unavailable": unavailable,
                        "refreshed_at": datetime.now(timezone.utc).isoformat(),
                    })
                return rows

        results = await asyncio.gather(*[_pull_team(t) for t in team_refs])
        flat = [r for team_rows in results for r in team_rows]
    if not flat:
        return {"ok": True, "players": 0, "note": "no active injuries"}
    from pymongo import ReplaceOne
    await db.espn_mls_injuries.bulk_write(
        [ReplaceOne({"_id": d["_id"]}, d, upsert=True) for d in flat],
        ordered=False,
    )
    unavailable_count = sum(1 for r in flat if r["unavailable"])
    logger.info(
        "ESPN MLS injuries refresh: %d entries (%d unavailable)",
        len(flat), unavailable_count,
    )
    return {"ok": True, "players": len(flat), "unavailable": unavailable_count}


# In-memory cache for lookup in the sync pick pipeline.
_INJURY_INDEX: dict[str, dict] = {}


async def load_injury_snapshot() -> dict[str, dict]:
    """Return `{name_norm: {status, unavailable, description}}` map."""
    from deps import db
    docs = await db.espn_mls_injuries.find({}).to_list(length=1000)
    idx = {}
    for d in docs:
        idx[d.get("name_norm", "")] = {
            "status": d.get("status"),
            "unavailable": bool(d.get("unavailable")),
            "description": d.get("description"),
        }
    return idx


def apply_injury_snapshot(idx: dict[str, dict]) -> None:
    global _INJURY_INDEX
    _INJURY_INDEX = idx


def is_player_available(name: str) -> tuple[bool, str]:
    """True if the player is NOT flagged Out/D2D/Suspended/Rest."""
    n = _norm(name)
    rec = _INJURY_INDEX.get(n)
    if not rec:
        return True, "no_injury_flag"
    if rec.get("unavailable"):
        return False, rec.get("status") or "unavailable"
    return True, rec.get("status") or "active"
