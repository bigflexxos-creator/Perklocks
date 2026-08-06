"""MLB Team K% vs Handedness — statsapi.mlb.com integration.

For pitcher strikeout props, the SINGLE most predictive factor after
pitcher quality is: **how often does the opposing team strike out vs
this handedness?**

Real example (from SportsbookReview, 2026-07-21):
  Noah Schultz Over 4.5 K's (+114) — recommendation logic:
  "Rangers post MLB's 4th-worst K-rate vs left-handed pitching"

We previously had zero data on this — team K% splits weren't fetched.
This module fills the gap using MLB's FREE statsapi endpoint.

DATA SOURCE:
  statsapi.mlb.com/api/v1/teams/{teamId}/stats?stats=statSplits
    &group=hitting&sportIds=1&sitCodes=vl,vr&season={year}

  Response yields `splits[].stat.strikeOuts`, `splits[].stat.plateAppearances`.
  K% = strikeOuts / plateAppearances.

CACHING:
  - Team K% splits change slowly (across a season). Refresh once per day.
  - Stored in `mongo.mlb_team_k_splits` collection keyed by (team_id, season).

USAGE:
  from services.mlb_team_k_intel import get_team_k_pct_vs_hand
  k_pct = await get_team_k_pct_vs_hand(db, team_id=147, pitcher_hand="L")
  # returns: {"k_pct": 0.242, "pa": 890, "rank": 4}  (or None if unknown)
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx
from motor.motor_asyncio import AsyncIOMotorClient

logger = logging.getLogger("lockscore.mlb_team_k_intel")

MLB_BASE = "https://statsapi.mlb.com/api/v1"
COLLECTION = "mlb_team_k_splits"
CACHE_TTL_HOURS = 20  # refresh once per day

# Phase 3B — shared client owner (services/database.py) replaces the
# local lazy singleton.  Keeps the _get_db() name for callers.
_LAZY_DB = None


def _get_db():
    global _LAZY_DB
    if _LAZY_DB is not None:
        return _LAZY_DB
    try:
        from services.database import get_database
        _LAZY_DB = get_database()
    except Exception:
        return None
    return _LAZY_DB

# Human-readable team-id map for LOGS (statsapi tolerates ID look-ups).
_TEAM_NAMES = {}  # populated lazily


async def _fetch_all_teams(client: httpx.AsyncClient) -> list[dict]:
    """Fetch all active MLB team IDs."""
    r = await client.get(f"{MLB_BASE}/teams?sportId=1&activeStatus=Y")
    r.raise_for_status()
    return r.json().get("teams") or []


async def _fetch_team_splits(client: httpx.AsyncClient, team_id: int,
                             season: int) -> dict:
    """Fetch team hitting splits vs LHP (vl) and RHP (vr).

    Returns: {"L": {"k_pct":..., "pa":...}, "R": {...}} or {}.
    """
    url = (
        f"{MLB_BASE}/teams/{team_id}/stats"
        f"?stats=statSplits&group=hitting&sportIds=1"
        f"&sitCodes=vl,vr&season={season}"
    )
    try:
        r = await client.get(url, timeout=8.0)
        r.raise_for_status()
    except (httpx.HTTPError, httpx.TimeoutException) as e:
        logger.debug("statsapi splits fetch failed for team %s: %s", team_id, e)
        return {}

    out: dict[str, dict] = {}
    data = r.json()
    # Response shape: {"stats": [{"splits": [{"split":{"code":"vl"}, "stat":{"strikeOuts":.., "plateAppearances":..}}]}]}
    for stat_group in data.get("stats") or []:
        for split in stat_group.get("splits") or []:
            code = ((split.get("split") or {}).get("code") or "").lower()
            stat = split.get("stat") or {}
            k = stat.get("strikeOuts")
            pa = stat.get("plateAppearances")
            if code in ("vl", "vr") and k is not None and pa:
                try:
                    hand = "L" if code == "vl" else "R"
                    out[hand] = {
                        "k_pct": round(float(k) / float(pa), 4),
                        "k": int(k),
                        "pa": int(pa),
                    }
                except (TypeError, ValueError, ZeroDivisionError):
                    pass
    return out


async def refresh_all_teams(db) -> dict:
    """Refresh team K splits for all 30 MLB clubs. Called by a nightly job.

    Safe to call more than once per day — will no-op picks that are still fresh."""
    stats = {"refreshed": 0, "cached": 0, "failed": 0, "total": 0}
    season = datetime.now(timezone.utc).year
    cutoff = datetime.now(timezone.utc) - timedelta(hours=CACHE_TTL_HOURS)

    async with httpx.AsyncClient(timeout=15) as client:
        teams = await _fetch_all_teams(client)
        stats["total"] = len(teams)

        # Build ranked table AFTER fetching all so we can compute rank.
        by_team: dict[int, dict] = {}
        for team in teams:
            tid = team.get("id")
            tname = team.get("name") or ""
            if not tid:
                continue
            _TEAM_NAMES[tid] = tname

            # Cache check
            existing = await db[COLLECTION].find_one({"team_id": tid, "season": season})
            if existing and existing.get("updated_at"):
                try:
                    up = datetime.fromisoformat(existing["updated_at"].replace("Z", "+00:00"))
                    if up.tzinfo is None:
                        up = up.replace(tzinfo=timezone.utc)
                    if up > cutoff:
                        stats["cached"] += 1
                        by_team[tid] = existing
                        continue
                except Exception:
                    pass

            splits = await _fetch_team_splits(client, tid, season)
            if not splits:
                stats["failed"] += 1
                continue

            doc = {
                "team_id": tid,
                "team_name": tname,
                "season": season,
                "vs_L": splits.get("L"),
                "vs_R": splits.get("R"),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            await db[COLLECTION].update_one(
                {"team_id": tid, "season": season},
                {"$set": doc},
                upsert=True,
            )
            stats["refreshed"] += 1
            by_team[tid] = doc

    # Compute ranks (higher K% = worse for hitters, better for K props).
    for hand in ("L", "R"):
        key = f"vs_{hand}"
        rows = [(tid, (d.get(key) or {}).get("k_pct"))
                for tid, d in by_team.items()
                if (d.get(key) or {}).get("pa", 0) >= 100]  # min sample
        # Sort DESCENDING (worst K teams first — highest K% = MLB rank 1)
        rows.sort(key=lambda r: -(r[1] or 0))
        for rank, (tid, _) in enumerate(rows, start=1):
            await db[COLLECTION].update_one(
                {"team_id": tid, "season": season},
                {"$set": {f"rank_vs_{hand}": rank}},
            )
    return stats


async def get_team_k_pct_vs_hand(db, team_id: int,
                                 pitcher_hand: str,
                                 auto_refresh: bool = True) -> Optional[dict]:
    """Return dict with `k_pct`, `pa`, `rank` — or None if unknown.

    pitcher_hand: "L" or "R" (pitcher's throwing hand).
    If `db` is None, lazily initialises a motor client from env.
    """
    if pitcher_hand not in ("L", "R"):
        return None
    if db is None:
        db = _get_db()
        if db is None:
            return None
    season = datetime.now(timezone.utc).year
    row = await db[COLLECTION].find_one({"team_id": int(team_id), "season": season})

    if not row and auto_refresh:
        # Lazy-refresh on first use if collection is empty.
        empty = await db[COLLECTION].count_documents({"season": season}) == 0
        if empty:
            try:
                await refresh_all_teams(db)
                row = await db[COLLECTION].find_one({"team_id": int(team_id), "season": season})
            except Exception as e:
                logger.debug("lazy refresh failed for team %s: %s", team_id, e)

    if not row:
        return None
    key = "vs_L" if pitcher_hand == "L" else "vs_R"
    split = row.get(key)
    if not split or not split.get("k_pct"):
        return None
    return {
        "k_pct": split["k_pct"],
        "pa": split["pa"],
        "rank": row.get(f"rank_vs_{pitcher_hand}"),
        "team_name": row.get("team_name"),
        "season": season,
    }


__all__ = ["refresh_all_teams", "get_team_k_pct_vs_hand"]
