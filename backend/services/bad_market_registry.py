"""Bad-market registry (Phase A — Odds API burn reduction).

The Odds API returns HTTP 422 when a sport does not carry a market
(e.g. `player_goal_scorer_anytime` in China Super League, or
`alternate_totals_games` in a lower-tier WTA 250 event).  Historically
`alt_lines_feed` historically retried EACH
market individually — burning ~4,000 credits/day on markets we know
don't exist.

This registry persists (sport_key, market) combinations that have
returned "market does not exist / not supported" errors, so we skip
them on subsequent fetch cycles.  Entries auto-expire after 24 h so
that if The Odds API starts carrying a market, we pick it up within
a day.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

logger = logging.getLogger("lockscore.bad_market_registry")

COLLECTION = "odds_bad_market_registry"
DEFAULT_TTL_HOURS = 24


async def ensure_indices(db) -> None:
    if db is None:
        return
    try:
        await db[COLLECTION].create_index(
            [("sport_key", 1), ("market", 1)],
            name="sport_market_uniq",
            unique=True,
        )
        await db[COLLECTION].create_index(
            "expires_at", name="expires_at_ttl", expireAfterSeconds=0,
        )
    except Exception as e:
        logger.warning("bad_market_registry index create failed: %s", e)


async def mark_bad(
    db, *,
    sport_key: str,
    markets: Iterable[str],
    ttl_hours: int = DEFAULT_TTL_HOURS,
    reason: str = "422_unsupported",
) -> None:
    """Persist all (sport_key, market) tuples as bad for `ttl_hours`."""
    if db is None or not sport_key:
        return
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=ttl_hours)
    for m in markets:
        if not m:
            continue
        try:
            await db[COLLECTION].update_one(
                {"sport_key": sport_key, "market": m},
                {"$set": {
                    "sport_key": sport_key,
                    "market": m,
                    "marked_at": now,
                    "expires_at": expires_at,
                    "reason": reason,
                }},
                upsert=True,
            )
            logger.info("bad_market registered: sport=%s market=%s (%s)",
                        sport_key, m, reason)
        except Exception as e:
            logger.warning("bad_market_registry write failed: %s", e)


async def filter_markets(
    db, *,
    sport_key: str,
    markets: Iterable[str],
) -> list[str]:
    """Return `markets` with any registered-bad ones removed."""
    if db is None or not sport_key:
        return list(markets)
    try:
        bad = set()
        now = datetime.now(timezone.utc)
        cursor = db[COLLECTION].find(
            {"sport_key": sport_key, "expires_at": {"$gt": now}},
            {"market": 1, "_id": 0},
        )
        async for doc in cursor:
            m = doc.get("market")
            if m:
                bad.add(m)
        if not bad:
            return list(markets)
        return [m for m in markets if m not in bad]
    except Exception as e:
        logger.debug("bad_market_registry filter err: %s", e)
        return list(markets)


async def is_all_bad(db, *, sport_key: str,
                      markets: Iterable[str]) -> bool:
    remaining = await filter_markets(db, sport_key=sport_key, markets=markets)
    return len(remaining) == 0


async def stats(db) -> dict:
    if db is None:
        return {}
    now = datetime.now(timezone.utc)
    active = await db[COLLECTION].count_documents(
        {"expires_at": {"$gt": now}})
    by_sport: dict[str, int] = {}
    cursor = db[COLLECTION].aggregate([
        {"$match": {"expires_at": {"$gt": now}}},
        {"$group": {"_id": "$sport_key", "n": {"$sum": 1}}},
        {"$sort": {"n": -1}},
    ])
    async for r in cursor:
        by_sport[r["_id"] or "-"] = r.get("n", 0)
    return {"active_entries": active, "by_sport": by_sport}


__all__ = [
    "ensure_indices",
    "mark_bad",
    "filter_markets",
    "is_all_bad",
    "stats",
    "COLLECTION",
]
