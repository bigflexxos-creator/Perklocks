"""single_flight — Phase 2γ distributed request-owner election.

When multiple workers (or the same worker across restarts) fire an
identical Odds API request within a short window, only ONE process
should perform the upstream call.  Everyone else awaits the result
or serves the last-known stale cache.

Backed by a Mongo collection ``odds_request_flights`` with atomic
`find_one_and_update` — the same pattern used by JobCoordinator.
Ownership is scoped by a deterministic ``request_key`` derived from
the provider contract (provider, endpoint, sport, event, sorted
markets, regions, bookmakers, odds format, extra params).

Falls back to a short in-memory cache too — but the durable Mongo
record is the source of truth so rolling deployments don't overlap.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import socket
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger("lockscore.single_flight")

FLIGHT_COLL = "odds_request_flights"

STATUS_INFLIGHT = "inflight"
STATUS_DONE     = "done"
STATUS_FAILED   = "failed"

DEFAULT_TTL_SECONDS = 30
DEFAULT_WAIT_SECONDS = 4.0
DEFAULT_POLL_INTERVAL = 0.15


def _now() -> datetime:
    return datetime.now(timezone.utc)


def build_request_key(
    *, provider: str = "odds_api",
    endpoint: str,
    sport_key: Optional[str] = None,
    event_id: Optional[str] = None,
    markets: Optional[str] = None,
    regions: Optional[str] = None,
    bookmakers: Optional[str] = None,
    odds_format: Optional[str] = None,
    extra_params: Optional[dict] = None,
) -> str:
    """Return a deterministic ``request_key`` for the given contract.

    Two callers that build the same contract get the same key, so
    duplicate suppression works across processes.  Secrets and
    caller-specific noise (like ``apiKey``) MUST NOT be included."""
    markets_norm = ",".join(sorted(
        (markets or "").replace(" ", "").split(",")
    )) if markets else ""
    extra_norm: dict[str, Any] = {}
    if extra_params:
        for k, v in sorted(extra_params.items()):
            kl = k.lower()
            if kl in ("apikey", "api_key", "authorization", "token", "secret"):
                continue
            extra_norm[k] = v
    payload = json.dumps(
        {
            "p": provider,
            "e": endpoint,
            "s": sport_key,
            "ev": event_id,
            "m": markets_norm,
            "r": regions or "",
            "b": bookmakers or "",
            "of": odds_format or "",
            "x": extra_norm,
        },
        separators=(",", ":"), sort_keys=True,
    )
    h = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"{provider}:{endpoint.strip('/')}:{h}"


class SingleFlight:
    """Distributed single-flight for Odds API contracts.

    Usage::

        sf = SingleFlight(db)
        won, prev = await sf.acquire(request_key, ttl_seconds=30)
        if won:
            result = await do_upstream()
            await sf.complete(request_key, prev, result_summary={...})
        else:
            waited = await sf.wait_for_result(request_key)
    """

    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self.db = db

    async def ensure_indices(self) -> None:
        try:
            await self.db[FLIGHT_COLL].create_index(
                "request_key", name="request_key_uniq", unique=True)
            await self.db[FLIGHT_COLL].create_index(
                "expires_at", name="expires_at_idx")
            # Auto-delete completed rows after 5 min so the collection
            # doesn't grow.  ttl_at is stamped by complete() / fail().
            await self.db[FLIGHT_COLL].create_index(
                "ttl_at", name="flight_ttl_idx",
                expireAfterSeconds=0,
            )
        except Exception as e:  # pragma: no cover
            logger.debug("flight index create: %s", e)

    async def acquire(
        self, request_key: str, *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        owner: Optional[str] = None,
    ) -> tuple[bool, Optional[dict]]:
        """Try to become the owner of ``request_key``.

        Returns ``(won, current_doc)`` — ``won=True`` if the caller
        should perform the upstream fetch.  Otherwise the returned
        doc describes who currently owns it (may be None on race)."""
        now = _now()
        expires_at = now + timedelta(seconds=int(ttl_seconds))
        token = uuid.uuid4().hex
        owner_id = owner or f"{socket.gethostname()}:{os.getpid()}"
        filt = {
            "request_key": request_key,
            "$or": [
                {"status": {"$ne": STATUS_INFLIGHT}},
                {"expires_at": {"$lt": now}},
            ],
        }
        update = {
            "$set": {
                "request_key": request_key,
                "status":      STATUS_INFLIGHT,
                "owner":       owner_id,
                "owner_token": token,
                "acquired_at": now,
                "expires_at":  expires_at,
                "updated_at":  now,
            },
            "$setOnInsert": {"created_at": now, "waiter_count": 0},
        }
        try:
            doc = await self.db[FLIGHT_COLL].find_one_and_update(
                filt, update, upsert=True, return_document=True,
            )
        except Exception as e:
            if "E11000" in str(e) or "duplicate key" in str(e).lower():
                cur = await self.db[FLIGHT_COLL].find_one(
                    {"request_key": request_key}, {"_id": 0})
                return False, cur
            raise
        if doc and doc.get("owner_token") == token:
            return True, doc
        cur = await self.db[FLIGHT_COLL].find_one(
            {"request_key": request_key}, {"_id": 0})
        return False, cur

    async def complete(self, request_key: str, owner_token: str, *,
                        result_summary: Optional[dict] = None,
                        retention_seconds: int = 60) -> bool:
        now = _now()
        set_ = {
            "status":       STATUS_DONE,
            "completed_at": now,
            "ttl_at":       now + timedelta(seconds=retention_seconds),
            "updated_at":   now,
            "result_summary": result_summary or {},
        }
        res = await self.db[FLIGHT_COLL].update_one(
            {"request_key": request_key, "owner_token": owner_token},
            {"$set": set_},
        )
        return bool(res.modified_count)

    async def fail(self, request_key: str, owner_token: str, *,
                    error: str, retention_seconds: int = 60) -> bool:
        now = _now()
        res = await self.db[FLIGHT_COLL].update_one(
            {"request_key": request_key, "owner_token": owner_token},
            {"$set": {
                "status":     STATUS_FAILED,
                "failed_at":  now,
                "ttl_at":     now + timedelta(seconds=retention_seconds),
                "updated_at": now,
                "error":      str(error)[:1000],
            }},
        )
        return bool(res.modified_count)

    async def wait_for_result(self, request_key: str, *,
                               timeout: float = DEFAULT_WAIT_SECONDS,
                               poll_interval: float = DEFAULT_POLL_INTERVAL,
                               ) -> Optional[dict]:
        """Poll until the owner finishes or the timeout expires.
        Returns the final flight doc, or None on timeout."""
        deadline = time.monotonic() + max(0.1, float(timeout))
        # Bump waiter counter for observability.
        try:
            await self.db[FLIGHT_COLL].update_one(
                {"request_key": request_key},
                {"$inc": {"waiter_count": 1}},
            )
        except Exception:  # pragma: no cover
            pass
        while time.monotonic() < deadline:
            doc = await self.db[FLIGHT_COLL].find_one(
                {"request_key": request_key}, {"_id": 0})
            if not doc:
                return None
            if doc.get("status") in (STATUS_DONE, STATUS_FAILED):
                return doc
            await asyncio.sleep(poll_interval)
        return None


__all__ = [
    "SingleFlight", "FLIGHT_COLL", "build_request_key",
    "STATUS_INFLIGHT", "STATUS_DONE", "STATUS_FAILED",
]
