"""settlement_scope — Phase 2δ scoped-settlement helper.

Only settle events that have PUBLISHED PICKS attached.  Skip leagues
and sport keys with zero active picks so we don't spend paid scores
credits on games the app never priced.

Consumers
─────────
- ``settle_due_picks`` (services.settlement_service) reads the
  ``active_sport_keys`` list before requesting per-league score
  feeds.
- Admin observability endpoint ``/api/admin/ops/settlement/scope``
  returns the same list for operators.

Rules
─────
1. A pick_date within the last ``lookback_days`` (default 14) is
   considered "active" — matches the settlement window in
   ``_settlement_loop``.
2. Only picks with a live/pending status are counted (no fully
   graded ones — those are already settled).
3. The result is a **stable, sorted, deduplicated** list of
   ``sport_key`` values so the caller iterates deterministically.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger("lockscore.settlement_scope")


DEFAULT_LOOKBACK_DAYS = 14

# Picks in these statuses are still eligible for settlement.
PENDING_STATUSES = {"pending", "live", None, ""}


async def active_sport_keys(
    db: AsyncIOMotorDatabase, *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> list[str]:
    """Return the sorted deduplicated list of ``sport_key`` values
    that currently have unsettled published picks."""
    since = (datetime.now(timezone.utc)
              - timedelta(days=int(lookback_days))).strftime("%Y-%m-%d")
    pipeline = [
        {"$match": {
            "pick_date":  {"$gte": since},
            "sport_key":  {"$exists": True, "$ne": None, "$ne": ""},
            "status":     {"$in": list(PENDING_STATUSES)},
        }},
        {"$group": {"_id": "$sport_key",
                     "count": {"$sum": 1}}},
        {"$sort": {"_id": 1}},
    ]
    try:
        rows = await db.picks.aggregate(pipeline).to_list(500)
    except Exception as e:  # pragma: no cover
        logger.warning("active_sport_keys aggregate failed: %s", e)
        return []
    return [r["_id"] for r in rows if r.get("_id")]


async def is_sport_key_active(
    db: AsyncIOMotorDatabase, *,
    sport_key: str,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> bool:
    keys = await active_sport_keys(db, lookback_days=lookback_days)
    return sport_key in set(keys)


async def scope_summary(
    db: AsyncIOMotorDatabase, *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> dict:
    """Return a human-readable summary of the current settlement scope.
    Used by the /api/admin/ops/settlement/scope endpoint."""
    since = (datetime.now(timezone.utc)
              - timedelta(days=int(lookback_days))).strftime("%Y-%m-%d")
    try:
        rows = await db.picks.aggregate([
            {"$match": {
                "pick_date":  {"$gte": since},
                "sport_key":  {"$exists": True, "$ne": None, "$ne": ""},
                "status":     {"$in": list(PENDING_STATUSES)},
            }},
            {"$group": {
                "_id": {"sport_key": "$sport_key",
                          "sport": "$sport"},
                "count": {"$sum": 1},
                "oldest": {"$min": "$pick_date"},
                "newest": {"$max": "$pick_date"},
            }},
            {"$sort": {"count": -1}},
        ]).to_list(500)
    except Exception as e:  # pragma: no cover
        logger.warning("scope_summary aggregate failed: %s", e)
        rows = []
    total_pending = sum(int(r.get("count", 0)) for r in rows)
    return {
        "as_of":            datetime.now(timezone.utc).isoformat(),
        "lookback_days":    int(lookback_days),
        "since":            since,
        "distinct_leagues": len(rows),
        "total_pending":    total_pending,
        "sport_keys":       [
            {"sport_key": r["_id"].get("sport_key"),
             "sport":     r["_id"].get("sport"),
             "pending":   int(r.get("count", 0)),
             "oldest":    r.get("oldest"),
             "newest":    r.get("newest")}
            for r in rows
        ],
    }


__all__ = [
    "active_sport_keys",
    "is_sport_key_active",
    "scope_summary",
    "DEFAULT_LOOKBACK_DAYS",
    "PENDING_STATUSES",
]
