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
    """Phase 3C — delegate to central registry."""
    if db is None:
        return
    try:
        from services import index_registry as _ir
        await _ir.ensure_collection(db, COLLECTION)
    except Exception as e:
        logger.warning("bad_market_registry ensure_indices via registry: %s", e)


async def mark_bad(
    db, *,
    sport_key: str,
    markets: Iterable[str],
    ttl_hours: Optional[int] = None,
    reason: str = "422_unsupported",
    event_id: Optional[str] = None,
    scope: str = "event",
    event_commence_time: Optional[datetime] = None,
) -> None:
    """Persist bad-market records.

    Block 2C (2026-08) — event-specific keying (XCUT-2 fix).

    A single event-level 422 must NEVER suppress an unrelated event's
    prop family.  ``scope`` controls the write:

      "event"  (default): key = (sport_key, event_id, market)
                          — used for per-event prop bundle 422s.
      "global":           key = (sport_key, None,     market)
                          — used only when the market is proven
                          unsupported by the sport globally
                          (e.g. NHL player props).  Do NOT emit
                          "global" from a single event 422.

    Backwards compatibility: existing rows without ``event_id`` remain
    valid GLOBAL markers.  Filter reads honor both scopes.

    PERKLOCKS-MAIN 35 — DYNAMIC TIME-TO-EVENT PROVIDER BUDGET
    (2026-06-30).  ``event_commence_time`` (optional UTC datetime)
    enables adaptive TTL so late-appearing markets are discovered on
    the correct cadence without starving the daily provider budget:

        <  6h to kickoff  → 1h TTL (aggressive re-probe; NFL alt
                                     props / MLB late-game hitters
                                     land 2-6h before first pitch)
        6h ≤ Δ < 24h      → 6h TTL (medium cadence)
        ≥ 24h to kickoff  → 24h TTL (long cadence; do not burn
                                      credits polling a market on
                                      an event 5-days out)
        already started   → 24h TTL (past events won't re-post)

    Callers that pass an explicit ``ttl_hours`` override the adaptive
    calculation (used by out-of-band ops probes).  Default TTL is
    24h when neither an explicit ``ttl_hours`` nor a
    ``event_commence_time`` is supplied — matches the pre-fix
    behaviour so callers that don't yet plumb commence through
    remain compatible.
    """
    if db is None or not sport_key:
        return
    if scope not in ("event", "global"):
        raise ValueError(f"invalid bad-market scope: {scope!r}")
    if scope == "event" and not event_id:
        # Never widen an event-level failure into a global marker.
        logger.warning(
            "bad_market_registry mark_bad(scope=event) called without "
            "event_id — refusing to write a global marker for %s %s",
            sport_key, list(markets))
        return
    now = datetime.now(timezone.utc)
    # Adaptive TTL — only when caller didn't set an explicit value.
    if ttl_hours is None:
        ttl_hours = _adaptive_ttl_hours(now, event_commence_time)
    expires_at = now + timedelta(hours=ttl_hours)
    for m in markets:
        if not m:
            continue
        try:
            key_filter = {"sport_key": sport_key, "market": m,
                           "event_id": event_id if scope == "event" else None}
            await db[COLLECTION].update_one(
                key_filter,
                {"$set": {
                    "sport_key":  sport_key,
                    "market":     m,
                    "event_id":   event_id if scope == "event" else None,
                    "scope":      scope,
                    "marked_at":  now,
                    "expires_at": expires_at,
                    "reason":     reason,
                    "ttl_hours":  ttl_hours,
                }},
                upsert=True,
            )
            logger.info(
                "bad_market registered: sport=%s market=%s scope=%s "
                "event=%s ttl=%dh (%s)",
                sport_key, m, scope, event_id, ttl_hours, reason)
        except Exception as e:
            logger.warning("bad_market_registry write failed: %s", e)


def _adaptive_ttl_hours(
    now: datetime, commence: Optional[datetime],
) -> int:
    """Return the adaptive TTL in hours based on time-to-event.

    Near-event → short TTL (aggressive re-probe); far-event →
    long TTL (protect provider budget).  Returns the legacy
    ``DEFAULT_TTL_HOURS`` (24h) when ``commence`` is missing so
    the pre-PERKLOCKS behaviour is preserved for legacy callers.
    """
    if commence is None:
        return DEFAULT_TTL_HOURS
    try:
        delta = (commence - now).total_seconds()
    except Exception:
        return DEFAULT_TTL_HOURS
    if delta < 0:
        # Event already started — the market bundle isn't going to
        # re-open; long TTL keeps the row out of the way.
        return DEFAULT_TTL_HOURS
    if delta < 6 * 3600:
        return 1
    if delta < 24 * 3600:
        return 6
    return DEFAULT_TTL_HOURS


async def filter_markets(
    db, *,
    sport_key: str,
    markets: Iterable[str],
    event_id: Optional[str] = None,
) -> list[str]:
    """Return `markets` with any registered-bad ones removed.

    Block 2C — event-aware filtering.

    Filters out a market when EITHER:
      * a GLOBAL marker exists for (sport_key, market), OR
      * an EVENT marker exists for (sport_key, event_id, market)
        (only when ``event_id`` is provided by the caller).

    Callers doing a bundled per-event prop fetch MUST pass
    ``event_id`` so unrelated events are not suppressed.  Callers
    that don't have an event yet (e.g. slate-level probes) omit
    ``event_id`` and only receive GLOBAL suppressions.
    """
    if db is None or not sport_key:
        return list(markets)
    try:
        bad_global: set[str] = set()
        bad_event:  set[str] = set()
        now = datetime.now(timezone.utc)
        # GLOBAL bad markets (scope="global" OR legacy rows without
        # event_id).
        cursor = db[COLLECTION].find(
            {"sport_key": sport_key,
              "expires_at": {"$gt": now},
              "$or": [{"scope": "global"},
                      {"scope": {"$exists": False}}]},
            {"market": 1, "_id": 0},
        )
        async for doc in cursor:
            m = doc.get("market")
            if m:
                bad_global.add(m)
        # EVENT-scoped bad markets (only if caller supplied event_id).
        if event_id:
            cursor = db[COLLECTION].find(
                {"sport_key": sport_key,
                  "scope":     "event",
                  "event_id":  event_id,
                  "expires_at": {"$gt": now}},
                {"market": 1, "_id": 0},
            )
            async for doc in cursor:
                m = doc.get("market")
                if m:
                    bad_event.add(m)
        bad = bad_global | bad_event
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
    "_adaptive_ttl_hours",
]
