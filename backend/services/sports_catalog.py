"""sports_catalog — Phase 2γ closeout, snapshot-scoped /sports reuse.

Within a single coordinated snapshot execution, the Odds API
``/v4/sports`` catalog is fetched **at most once** upstream.  All
subsequent consumers (tennis discovery, soccer discovery, event-list
callers, etc.) receive the exact same payload.

Design
──────
* One Mongo doc per ``snapshot_run_id`` in ``sports_catalog_snapshots``.
* The first caller in a run acquires a distributed single-flight
  slot keyed by the run_id + a stable "sports_list" endpoint tag.
  Subsequent callers hit the DB directly.
* If ``snapshot_run_id`` is omitted the module uses a per-UTC-day
  fallback key so ad-hoc callers still share.
* A helper ``current_run_id()`` returns the current 10-min window
  identifier so simultaneous startup schedulers coordinate.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger("lockscore.sports_catalog")

COLLECTION = "sports_catalog_snapshots"
DEFAULT_TTL_SECONDS = 3600            # keep last hour of snapshot rows


def _now() -> datetime:
    return datetime.now(timezone.utc)


def current_run_id() -> str:
    """Return a 10-minute-bucket UTC run identifier suitable for
    coordinating parallel discovery consumers."""
    dt = _now()
    bucket = (dt.minute // 10) * 10
    return dt.strftime(f"%Y%m%dT%H{bucket:02d}Z")


async def ensure_indices(db: AsyncIOMotorDatabase) -> None:
    """Phase 3C — delegate to central registry."""
    try:
        from services import index_registry as _ir
        await _ir.ensure_collection(db, COLLECTION)
    except Exception as e:  # pragma: no cover
        logger.debug("sports_catalog ensure_indices via registry: %s", e)


async def get_catalog(
    db: AsyncIOMotorDatabase, *,
    run_id: Optional[str] = None,
    caller: str = "unknown",
    reason: str = "discovery",
) -> dict:
    """Return ``{"data": [...sports...], "cache_hit": bool,
    "run_id": ..., "upstream_called": bool}``.

    * If a row exists for the run_id, returns it immediately
      (``cache_hit=True``, ``upstream_called=False``).  Never spends
      credits on this branch.
    * If no row exists, acquires an ``OddsApiGateway`` single-flight
      lock (via ``fetch``) that ensures only ONE upstream request
      per run.  All concurrent waiters serve from the resulting
      cached row.
    """
    rid = run_id or current_run_id()
    existing = await db[COLLECTION].find_one({"run_id": rid}, {"_id": 0})
    if existing and existing.get("data") is not None:
        return {
            "data":              existing["data"],
            "cache_hit":         True,
            "upstream_called":   False,
            "run_id":            rid,
            "fetched_at":        existing.get("fetched_at"),
        }
    # Cache miss — go through the gateway.  The gateway's own
    # single-flight collapses concurrent callers.
    from services.odds_api_gateway import OddsApiGateway, ODDS_API_BASE
    gw = OddsApiGateway(db)
    result = await gw.fetch(
        f"{ODDS_API_BASE}/sports",
        params={},
        caller=caller,
        reason=f"catalog_reuse:{reason}",
        job_name="sports_catalog_snapshot",
        emergency_requested=False,
        cache_policy="normal",
    )
    if not result or result.data is None:
        # Serve any prior day's data as fallback rather than fail.
        prev = await db[COLLECTION].find(
            {}, {"_id": 0},
        ).sort("fetched_at", -1).limit(1).to_list(1)
        if prev and prev[0].get("data") is not None:
            return {
                "data":            prev[0]["data"],
                "cache_hit":       True,
                "upstream_called": False,
                "run_id":          rid,
                "fetched_at":      prev[0].get("fetched_at"),
                "stale":           True,
            }
        return {
            "data":            [],
            "cache_hit":       False,
            "upstream_called": True,
            "run_id":          rid,
            "error":           result.get("reason", "upstream_failed"),
        }
    # Persist the snapshot row keyed by run_id so subsequent
    # consumers hit it directly.
    now = _now()
    try:
        await db[COLLECTION].update_one(
            {"run_id": rid},
            {"$set": {
                "run_id":       rid,
                "data":         result.data,
                "fetched_at":   now,
                "ttl_at":       now + timedelta(seconds=DEFAULT_TTL_SECONDS),
                "caller":       caller,
                "reason":       reason,
                "size":         len(result.data)
                                    if isinstance(result.data, list) else 0,
            }},
            upsert=True,
        )
    except Exception as e:
        logger.debug("catalog persist err: %s", e)
    return {
        "data":            result.data,
        "cache_hit":       False,
        "upstream_called": True,
        "run_id":          rid,
        "fetched_at":      now,
    }


__all__ = [
    "get_catalog", "current_run_id", "ensure_indices",
    "COLLECTION", "DEFAULT_TTL_SECONDS",
]
