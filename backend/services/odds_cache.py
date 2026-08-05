"""Centralized SWR (stale-while-revalidate) cache for The Odds API.

Goal — reduce Odds API credit burn by 90%+ without changing app
functionality or prediction logic. Provides:

  • MongoDB-backed persistent cache (survives container restarts).
  • TTL per endpoint type — bulk odds refresh far more often than
    static endpoints like `/sports`.
  • Stale-while-revalidate — serve cached data instantly even when
    stale, then trigger a background refresh so the next request is
    up-to-date.
  • Single-flight deduplication — if 50 concurrent callers ask for
    the same URL+params, exactly ONE upstream request is made and
    all callers receive the same JSON result.
  • Request logging — every real upstream call is written to
    `odds_api_request_log` with timestamp / endpoint / sport / market
    / caller / cache_status so an admin dashboard can compute the
    savings ratio in real time.
  • Skip-completed guard — bulk-odds callers can pass a
    `skip_completed=True` flag so the cache automatically drops
    games with `commence_time < now - 4h` before returning.
  • Diff-check — new payloads are hashed; if the hash matches the
    last cached hash we DON'T rewrite the document (avoids Mongo
    churn) but still update the `refreshed_at` timestamp.

Public API
──────────
    from services.odds_cache import cached_odds_get, get_odds_usage_report

    data = await cached_odds_get(
        url="https://api.the-odds-api.com/v4/sports/basketball_nba/odds",
        params={"regions": "us", "markets": "h2h,spreads,totals",
                 "oddsFormat": "american"},
        endpoint_type="bulk_odds",   # ← controls TTL
        caller="sports_engine._fetch_odds_for",
        sport_key="basketball_nba",
        markets="h2h,spreads,totals",
        upstream_fetch=_do_real_fetch,   # async callable, only called on MISS/STALE
        skip_completed=True,
    )

Every module that currently calls `httpx.get(api.the-odds-api.com/…)`
directly can switch to this helper — with zero change to their
business logic or return-value shape.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Awaitable, Callable, Optional

from motor.motor_asyncio import AsyncIOMotorClient

logger = logging.getLogger("lockscore.odds_cache")


# ═════════════════════════════════════════════════════════════════════
# TTL policy — how long a cached response is considered FRESH vs STALE
# ═════════════════════════════════════════════════════════════════════
# Endpoint-type → (fresh_ttl_sec, stale_ttl_sec)
# - fresh_ttl: within this window, serve cache without touching upstream
# - stale_ttl: within this window, serve cache AND kick off background
#              revalidation. Beyond stale_ttl, treat as hard miss (block).
_TTL_POLICY: dict[str, tuple[int, int]] = {
    # Bulk odds move continuously — 5 min fresh, 30 min stale gives a
    # reasonable balance between freshness and credit burn.
    "bulk_odds":       (5 * 60,   30 * 60),
    # Per-event odds move slower than the bulk feed (individual games
    # are only steamed close to first-pitch). 15 min fresh / 2 h stale
    # cuts alt-line credit burn massively without losing sharp signal.
    "event_odds":      (15 * 60,  2 * 3600),
    # Alt-line markets (player props, first-inning specials) barely
    # move once posted — 30 min fresh / 6 h stale is safe.
    "event_alt_lines": (30 * 60,  6 * 3600),
    # Fixtures list is stable — refresh 4 × / day.
    "events_list":     (60 * 60,  6 * 3600),
    # /sports catalog changes only when the API adds a league.
    "sports_list":     (24 * 3600, 7 * 24 * 3600),
    "generic":         (10 * 60,  60 * 60),
}


# ═════════════════════════════════════════════════════════════════════
# Mongo helpers (lazy, so tests can inject a fake db)
# ═════════════════════════════════════════════════════════════════════
_DB_CACHE: dict[str, Any] = {"client": None, "db": None, "inited": False}


def _reset_db_cache() -> None:
    """Test helper — force the next `_get_db()` call to build a fresh
    Motor client bound to the CURRENT running event loop."""
    _DB_CACHE["client"] = None
    _DB_CACHE["db"] = None
    _DB_CACHE["inited"] = False


def _get_db():
    """Lazy MongoDB client bound to the CURRENT running event loop.

    Motor's AsyncIOMotorClient is tied to the loop it was created on,
    so `asyncio.run()`-based unit tests (each run in a fresh loop)
    fail with "attached to a different loop" errors unless we rebuild
    the client on every loop change. We check the running loop's id
    against the cached loop id and rebuild if it changed.
    """
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None
    cached_loop = _DB_CACHE.get("loop")
    if _DB_CACHE.get("db") is not None and current_loop is cached_loop:
        return _DB_CACHE["db"]
    mongo_url = os.getenv("MONGO_URL")
    if not mongo_url:
        return None
    client = AsyncIOMotorClient(mongo_url)
    db = client["lockscore_db"]
    _DB_CACHE["client"] = client
    _DB_CACHE["db"] = db
    _DB_CACHE["loop"] = current_loop
    _DB_CACHE["inited"] = False   # re-run index setup on the new loop
    return db


async def _ensure_indexes(db) -> None:
    if _DB_CACHE["inited"]:
        return
    try:
        await db.odds_api_cache.create_index("cache_key", unique=True,
                                              name="uniq_cache_key")
        await db.odds_api_cache.create_index("refreshed_at",
                                              name="refreshed_at")
        await db.odds_api_request_log.create_index("ts", name="ts")
        await db.odds_api_request_log.create_index(
            [("sport_key", 1), ("ts", -1)], name="sport_ts")
        _DB_CACHE["inited"] = True
    except Exception as e:
        logger.warning("odds_cache index init failed: %s", e)


# ═════════════════════════════════════════════════════════════════════
# Cache key construction
# ═════════════════════════════════════════════════════════════════════
def _cache_key(url: str, params: dict) -> str:
    """Deterministic key = URL + normalized query params (apiKey stripped)."""
    stripped = {k: v for k, v in (params or {}).items()
                 if k.lower() not in ("apikey", "api_key")}
    payload = json.dumps({"u": url, "p": stripped}, sort_keys=True,
                          default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def _hash_body(body: Any) -> str:
    try:
        return hashlib.sha256(
            json.dumps(body, sort_keys=True, default=str).encode()
        ).hexdigest()
    except Exception:
        return ""


# ═════════════════════════════════════════════════════════════════════
# Single-flight — collapse duplicate concurrent requests
# ═════════════════════════════════════════════════════════════════════
_INFLIGHT: dict[str, asyncio.Future] = {}
_INFLIGHT_LOCK = asyncio.Lock()


async def _dedupe_fetch(
    cache_key: str,
    upstream_fetch: Callable[[], Awaitable[Any]],
) -> Any:
    """If a fetch for the same cache_key is already in flight, await it
    and return the same result. Otherwise create a new future."""
    async with _INFLIGHT_LOCK:
        existing = _INFLIGHT.get(cache_key)
        if existing is not None:
            fut = existing
            piggy = True
        else:
            fut = asyncio.get_event_loop().create_future()
            _INFLIGHT[cache_key] = fut
            piggy = False
    if piggy:
        try:
            return await fut
        except Exception:
            return None
    try:
        result = await upstream_fetch()
        if not fut.done():
            fut.set_result(result)
        return result
    except Exception as e:
        if not fut.done():
            fut.set_exception(e)
        raise
    finally:
        async with _INFLIGHT_LOCK:
            _INFLIGHT.pop(cache_key, None)


# ═════════════════════════════════════════════════════════════════════
# Completed-game filter
# ═════════════════════════════════════════════════════════════════════
def _drop_completed_games(payload: Any, cutoff_minutes: int = 240) -> Any:
    """For bulk-odds / events payloads, remove games whose
    commence_time is more than `cutoff_minutes` in the past. Games
    listed by The Odds API without a start time are kept as-is.
    """
    if not isinstance(payload, list):
        return payload
    now_utc = datetime.now(timezone.utc)
    cutoff = now_utc - timedelta(minutes=cutoff_minutes)
    kept = []
    for g in payload:
        if not isinstance(g, dict):
            kept.append(g)
            continue
        ct = g.get("commence_time")
        if not ct:
            kept.append(g)
            continue
        try:
            ct_dt = datetime.fromisoformat(str(ct).replace("Z", "+00:00"))
            if ct_dt.tzinfo is None:
                ct_dt = ct_dt.replace(tzinfo=timezone.utc)
            if ct_dt < cutoff:
                # Skip completed
                continue
        except Exception:
            pass
        kept.append(g)
    return kept


# ═════════════════════════════════════════════════════════════════════
# Request log writer
# ═════════════════════════════════════════════════════════════════════
async def _write_request_log(
    db, *,
    url: str,
    params: dict,
    sport_key: Optional[str],
    markets: Optional[str],
    caller: Optional[str],
    cache_status: str,   # "hit" | "stale_hit" | "miss" | "background_refresh"
    upstream_called: bool,
    upstream_status: Optional[int] = None,
    upstream_bytes: Optional[int] = None,
    reason: str = "",
) -> None:
    try:
        await db.odds_api_request_log.insert_one({
            "ts":              datetime.now(timezone.utc).isoformat(),
            "url":             url,
            "endpoint_path":   url.replace("https://api.the-odds-api.com", ""),
            "params":          {k: v for k, v in (params or {}).items()
                                  if k.lower() not in ("apikey", "api_key")},
            "sport_key":       sport_key,
            "markets":         markets,
            "caller":          caller,
            "cache_status":    cache_status,
            "upstream_called": bool(upstream_called),
            "upstream_status": upstream_status,
            "upstream_bytes":  upstream_bytes,
            "reason":          reason,
        })
    except Exception as e:
        # Log at WARNING (not debug) so silent event-loop / connection
        # failures don't leave the request log incomplete without any
        # trail. Analytics tests + admin dashboards rely on this table.
        logger.warning("odds_cache log write failed for %s: %s", url, e)


# ═════════════════════════════════════════════════════════════════════
# Public cached-fetch entry point
# ═════════════════════════════════════════════════════════════════════
async def cached_odds_get(
    *,
    url: str,
    params: Optional[dict] = None,
    endpoint_type: str = "generic",
    caller: str = "unknown",
    sport_key: Optional[str] = None,
    markets: Optional[str] = None,
    upstream_fetch: Callable[[], Awaitable[Any]],
    skip_completed: bool = False,
    force_refresh: bool = False,
) -> Any:
    """Serve The Odds API responses through the SWR cache.

    Return-value contract:
      - MISS  → run `upstream_fetch`, persist, return payload.
      - HIT (fresh) → return cached payload immediately (no upstream call).
      - HIT (stale) → return cached payload immediately AND fire an
        asyncio background task to revalidate.
      - HARD MISS (stale-ttl exceeded) → block on `upstream_fetch`,
        persist, return payload.
      - `force_refresh=True` → bypass cache and always call upstream.
    """
    db = _get_db()
    params = params or {}
    fresh_ttl, stale_ttl = _TTL_POLICY.get(endpoint_type,
                                            _TTL_POLICY["generic"])
    key = _cache_key(url, params)
    now = time.time()

    async def _do_upstream_and_persist(reason: str,
                                        cache_status: str) -> Any:
        async def _inner():
            t0 = time.monotonic()
            payload = await upstream_fetch()
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            if payload is None:
                # Upstream returned None (401 quota exhausted, 429, 5xx,
                # network err). Still log the attempt so we can see the
                # burn — but DON'T overwrite an existing cached body
                # with `None`, keep serving stale.
                if db is not None:
                    await _write_request_log(
                        db,
                        url=url, params=params, sport_key=sport_key,
                        markets=markets, caller=caller,
                        cache_status=cache_status,
                        upstream_called=True,
                        upstream_status=None,
                        upstream_bytes=0,
                        reason=f"{reason} + upstream returned None",
                    )
                return payload
            if db is not None:
                await _ensure_indexes(db)
                body_hash = _hash_body(payload)
                existing = await db.odds_api_cache.find_one(
                    {"cache_key": key}, {"body_hash": 1})
                unchanged = (existing
                              and existing.get("body_hash") == body_hash)
                doc = {
                    "cache_key":     key,
                    "url":           url,
                    "params":        {k: v for k, v in params.items()
                                        if k.lower() not in
                                        ("apikey", "api_key")},
                    "endpoint_type": endpoint_type,
                    "sport_key":     sport_key,
                    "markets":       markets,
                    "body_hash":     body_hash,
                    "refreshed_at":  now,
                    "refreshed_iso": datetime.now(timezone.utc).isoformat(),
                    "elapsed_ms":    elapsed_ms,
                }
                if not unchanged:
                    doc["body"] = payload
                await db.odds_api_cache.update_one(
                    {"cache_key": key}, {"$set": doc}, upsert=True,
                )
                await _write_request_log(
                    db,
                    url=url, params=params, sport_key=sport_key,
                    markets=markets, caller=caller,
                    cache_status=cache_status,
                    upstream_called=True,
                    upstream_status=200 if payload is not None else None,
                    upstream_bytes=len(json.dumps(payload,
                                                   default=str))
                    if payload is not None else 0,
                    reason=reason,
                )
            return payload

        return await _dedupe_fetch(key, _inner)

    # ── Force refresh path ─────────────────────────────────────────
    if force_refresh:
        return await _do_upstream_and_persist("force_refresh", "miss")

    # ── Cache lookup ───────────────────────────────────────────────
    cached = None
    if db is not None:
        try:
            await _ensure_indexes(db)
            cached = await db.odds_api_cache.find_one({"cache_key": key})
        except Exception as e:
            logger.debug("odds_cache lookup failed: %s", e)

    if cached and cached.get("body") is not None:
        age = now - float(cached.get("refreshed_at") or 0)
        payload = cached["body"]

        if age <= fresh_ttl:
            # HIT — fresh, no upstream needed.
            if db is not None:
                await _write_request_log(
                    db, url=url, params=params, sport_key=sport_key,
                    markets=markets, caller=caller,
                    cache_status="hit", upstream_called=False,
                    reason=f"fresh (age={int(age)}s < ttl={fresh_ttl}s)",
                )
            return (_drop_completed_games(payload)
                    if skip_completed else payload)

        if age <= stale_ttl:
            # STALE HIT — return cached payload, kick background refresh.
            if db is not None:
                await _write_request_log(
                    db, url=url, params=params, sport_key=sport_key,
                    markets=markets, caller=caller,
                    cache_status="stale_hit", upstream_called=False,
                    reason=f"stale (age={int(age)}s < stale_ttl={stale_ttl}s)",
                )
            # Fire and forget.
            asyncio.create_task(
                _do_upstream_and_persist("background_revalidate",
                                          "background_refresh")
            )
            return (_drop_completed_games(payload)
                    if skip_completed else payload)

    # ── HARD MISS — must fetch upstream synchronously ──────────────
    payload = await _do_upstream_and_persist(
        "hard_miss" if not cached else "stale_ttl_exceeded",
        "miss",
    )
    return (_drop_completed_games(payload)
            if skip_completed and payload is not None else payload)


# ═════════════════════════════════════════════════════════════════════
# Reporting — for the /api/admin/odds-usage endpoint
# ═════════════════════════════════════════════════════════════════════
async def get_odds_usage_report(hours: int = 24) -> dict:
    """Aggregate request-log stats over the last `hours` hours.

    Fields:
      total_requests, upstream_requests, cache_hits, cache_hit_rate,
      by_endpoint, by_sport, top_wasteful, estimated_credits_used,
      projected_monthly_at_current_rate, projected_monthly_10x_users.
    """
    db = _get_db()
    if db is None:
        return {"error": "db unavailable"}
    since = (datetime.now(timezone.utc) -
              timedelta(hours=hours)).isoformat()
    total = await db.odds_api_request_log.count_documents(
        {"ts": {"$gte": since}})
    upstream = await db.odds_api_request_log.count_documents(
        {"ts": {"$gte": since}, "upstream_called": True})
    hits = total - upstream
    hit_rate = round((hits / total) * 100.0, 2) if total else 0.0

    def _pipe_group(field: str, limit: int = 15) -> list:
        return [
            {"$match": {"ts": {"$gte": since}}},
            {"$group": {"_id": f"${field}",
                         "total": {"$sum": 1},
                         "upstream": {"$sum":
                                       {"$cond":
                                        ["$upstream_called", 1, 0]}}}},
            {"$sort": {"upstream": -1}},
            {"$limit": limit},
        ]

    by_endpoint = [d async for d in
                    db.odds_api_request_log.aggregate(
                        _pipe_group("endpoint_path"))]
    by_sport = [d async for d in
                 db.odds_api_request_log.aggregate(
                     _pipe_group("sport_key"))]
    by_caller = [d async for d in
                  db.odds_api_request_log.aggregate(
                      _pipe_group("caller", limit=10))]

    # Estimate credits — bulk odds cost = markets count, event_odds = markets,
    # events_list = 1, sports_list = 1 (per The Odds API v4 doc).
    total_credits = 0
    async for r in db.odds_api_request_log.aggregate([
        {"$match": {"ts": {"$gte": since}, "upstream_called": True}},
        {"$project": {
            "credits": {
                "$cond": [
                    {"$in": ["$endpoint_path.endpoint_type", ["sports_list"]]},
                    1,
                    {"$max": [1,
                              {"$size":
                                {"$split": [
                                    {"$ifNull": ["$markets", "h2h"]},
                                    ","]}}]},
                ]},
        }},
        {"$group": {"_id": None, "total": {"$sum": "$credits"}}},
    ]):
        total_credits = r.get("total") or 0

    # Projection: (credits in window) × (hours in month / hours in window).
    hours_in_month = 24 * 30
    projected_monthly = int(total_credits * (hours_in_month / max(1, hours)))
    projected_monthly_10x = projected_monthly * 10

    return {
        "window_hours":              hours,
        "total_requests":            total,
        "upstream_requests":         upstream,
        "cache_hits":                hits,
        "cache_hit_rate_percent":    hit_rate,
        "estimated_credits_used":    total_credits,
        "projected_monthly_credits": projected_monthly,
        "projected_monthly_at_10x":  projected_monthly_10x,
        "by_endpoint":               by_endpoint,
        "by_sport":                  by_sport,
        "top_callers":               by_caller,
    }


__all__ = [
    "cached_odds_get",
    "cached_httpx_get",
    "get_odds_usage_report",
    "_cache_key",
    "_TTL_POLICY",
    "_drop_completed_games",
]


# ═════════════════════════════════════════════════════════════════════
# Convenience wrapper for direct call-sites (drop-in httpx replacement)
# ═════════════════════════════════════════════════════════════════════
async def cached_httpx_get(
    url: str,
    params: Optional[dict] = None,
    *,
    api_key: Optional[str] = None,
    api_key_param: str = "apiKey",
    endpoint_type: Optional[str] = None,
    caller: str = "unknown",
    sport_key: Optional[str] = None,
    markets: Optional[str] = None,
    timeout: float = 15.0,
    skip_completed: bool = False,
    force_refresh: bool = False,
) -> Any:
    """One-line drop-in replacement for `httpx.get(...).json()` for
    Odds API call sites.

    Usage:
        data = await cached_httpx_get(
            f"{_ODDS_API_BASE}/sports/{sport_key}/events",
            {"regions": "us"},
            api_key=api_key,
            endpoint_type="events_list",
            caller="alt_lines_feed._fetch_events",
            sport_key=sport_key,
        )
    Returns parsed JSON on success, None on failure (matches the
    existing 401/429/5xx guard semantics).
    """
    import httpx
    params = dict(params or {})

    # Auto-infer endpoint_type from URL if not provided.
    ep = endpoint_type
    if ep is None:
        if url.endswith("/sports"):
            ep = "sports_list"
        elif "/events/" in url and url.endswith("/odds"):
            ep = "event_odds"
        elif url.endswith("/events"):
            ep = "events_list"
        elif url.endswith("/odds"):
            ep = "bulk_odds"
        else:
            ep = "generic"

    async def _upstream():
        full_params = {**params}
        if api_key:
            full_params[api_key_param] = api_key
        try:
            async with httpx.AsyncClient(timeout=timeout) as cx:
                r = await cx.get(url, params=full_params)
                if r.status_code == 200:
                    try:
                        return r.json()
                    except ValueError:
                        logger.warning("odds_cache upstream JSON error %s",
                                        url)
                        return None
                logger.warning("odds_cache upstream %s → HTTP %s",
                                url, r.status_code)
                return None
        except Exception as e:
            logger.warning("odds_cache upstream err %s: %s", url, e)
            return None

    return await cached_odds_get(
        url=url,
        params=params,
        endpoint_type=ep,
        caller=caller,
        sport_key=sport_key,
        markets=markets or params.get("markets"),
        upstream_fetch=_upstream,
        skip_completed=skip_completed,
        force_refresh=force_refresh,
    )
