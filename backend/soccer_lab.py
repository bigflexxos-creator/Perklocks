"""Soccer League Discovery + Ranked Feed.

Dynamically discovers all ACTIVE soccer leagues from The Odds API instead of
relying on a hardcoded list. Caches the discovery for 24h so we don't burn
credits re-querying /sports.

Mounted at /api/soccer-lab/* in server.py.

Exposes:
  * GET /soccer-lab/leagues   — list of active soccer_* keys (cached)
  * GET /soccer-lab/feed      — ranked soccer picks (confidence-sorted)
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException

router = APIRouter(prefix="/soccer-lab", tags=["soccer-lab"])
logger = logging.getLogger("perkslocks.soccer_lab")


def _get_db():
    from server import db
    return db


def _require_auth():
    from server import current_user
    return current_user


_ODDS_API_BASE = "https://api.the-odds-api.com/v4"
_LEAGUE_CACHE_TTL_SECONDS = 24 * 3600  # 24h

# In-process cache for discovered league list
_league_cache: dict[str, Any] = {
    "leagues":     [],
    "fetched_at":  0,
    "source":      "uninitialised",
    "total_seen":  0,
}


async def _fetch_active_soccer_leagues() -> list[dict[str, Any]]:
    """Call /sports and return all ACTIVE leagues whose key starts with 'soccer_'."""
    api_key = os.environ.get("THE_ODDS_API_KEY")
    if not api_key:
        logger.warning("Soccer Lab: THE_ODDS_API_KEY missing — cannot discover")
        return []
    async with httpx.AsyncClient(timeout=15.0) as cx:
        try:
            r = await cx.get(f"{_ODDS_API_BASE}/sports", params={"apiKey": api_key})
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            logger.warning("Soccer Lab: /sports fetch failed: %s", e)
            return []
    out = []
    for s in data:
        key = s.get("key") or ""
        if not key.startswith("soccer_"):
            continue
        if not s.get("active"):
            continue
        out.append({
            "key":         key,
            "title":       s.get("title"),
            "group":       s.get("group"),
            "description": s.get("description"),
            "has_outrights": bool(s.get("has_outrights")),
        })
    return out


async def discover_soccer_leagues(force: bool = False) -> dict[str, Any]:
    """Return cached active soccer leagues; refresh on TTL miss or `force`."""
    now = time.time()
    age = now - _league_cache["fetched_at"]
    if not force and _league_cache["leagues"] and age < _LEAGUE_CACHE_TTL_SECONDS:
        return {
            "leagues":    _league_cache["leagues"],
            "count":      len(_league_cache["leagues"]),
            "age_sec":    round(age),
            "source":     "cache",
            "fetched_at": _league_cache["fetched_at"],
        }
    fresh = await _fetch_active_soccer_leagues()
    if fresh:
        _league_cache["leagues"]    = fresh
        _league_cache["fetched_at"] = now
        _league_cache["source"]     = "odds_api"
        _league_cache["total_seen"] = len(fresh)
    return {
        "leagues":    _league_cache["leagues"],
        "count":      len(_league_cache["leagues"]),
        "age_sec":    0,
        "source":     _league_cache["source"],
        "fetched_at": _league_cache["fetched_at"],
    }


# ---------------------------------------------------------------------------
# Endpoint: GET /soccer-lab/leagues — active leagues discovery
# ---------------------------------------------------------------------------
@router.get("/leagues")
async def soccer_lab_leagues(
    refresh: bool = False,
    user=Depends(_require_auth()),
):
    """Active soccer_* leagues discovered from The Odds API. 24h-cached."""
    return await discover_soccer_leagues(force=refresh)


# ---------------------------------------------------------------------------
# Endpoint: GET /soccer-lab/feed — ranked global soccer feed
# ---------------------------------------------------------------------------
def _decimal_odds(p: dict) -> float | None:
    """Convert pick's American book_odds to decimal."""
    a = p.get("book_odds")
    try:
        a = float(a)
    except Exception:
        return None
    if a == 0:
        return None
    return 1.0 + (a / 100.0) if a > 0 else 1.0 + (100.0 / -a)


def _confidence(p: dict) -> float:
    """Confidence = 1/decimal_odds × 100 (matches user's JS formula).

    Falls back to lock_score for any pick missing odds.
    """
    d = _decimal_odds(p)
    if d:
        return (1.0 / d) * 100.0
    return float(p.get("lock_score") or 0)


@router.get("/feed")
async def soccer_lab_feed(
    limit: int = 50,
    min_lock: float = 78.0,
    sport: str = "Soccer",
    user=Depends(_require_auth()),
):
    """Ranked global feed for `sport` — defaults to Soccer.

    * Excludes `no_bet=True` picks and `Pass` grades.
    * Pregame only.
    * Confidence = 1/decimal_odds × 100, falling back to lock_score.
    """
    if limit < 1 or limit > 200:
        raise HTTPException(400, "limit must be 1-200")
    db = _get_db()

    import datetime as _dt
    now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()

    cur = db.picks.find(
        {
            "sport":      sport,
            "no_bet":     {"$ne": True},
            "grade":      {"$ne": "Pass"},
            "event_time": {"$gt": now_iso},
            "lock_score": {"$gte": min_lock},
        },
        {
            "_id":             0,
            "id":              1,
            "league":          1,
            "event":           1,
            "event_time":      1,
            "market":          1,
            "selection":       1,
            "book_odds":       1,
            "win_probability": 1,
            "edge_percent":    1,
            "lock_score":      1,
            "grade":           1,
            "lock_score_v2":   1,
            "tier_v2":         1,
            "is_apex":         1,
            "implied_probability": 1,
        },
    )
    picks: list[dict] = []
    async for p in cur:
        p["confidence"] = round(_confidence(p), 2)
        picks.append(p)

    picks.sort(key=lambda x: x["confidence"], reverse=True)

    # Group league distribution for the UI chips
    league_counts: dict[str, int] = {}
    for p in picks:
        lg = p.get("league") or "?"
        league_counts[lg] = league_counts.get(lg, 0) + 1
    league_distribution = sorted(
        ({"league": k, "count": v} for k, v in league_counts.items()),
        key=lambda x: -x["count"],
    )

    return {
        "count":               len(picks),
        "picks":               picks[:limit],
        "total_returned":      min(limit, len(picks)),
        "min_lock":            min_lock,
        "league_distribution": league_distribution,
    }
