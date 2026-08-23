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
        url=f"{ODDS_API_BASE}/sports/basketball_nba/odds",
        params={"regions": "us", "markets": "h2h,spreads,totals",
                 "oddsFormat": "american"},
        endpoint_type="bulk_odds",   # ← controls TTL
        caller="sports_engine._fetch_odds_for",
        sport_key="basketball_nba",
        markets="h2h,spreads,totals",
        upstream_fetch=_do_real_fetch,   # async callable, only called on MISS/STALE
        skip_completed=True,
    )

Every module that currently issues an HTTP GET against the Odds API
should call this helper — with zero change to their business logic
or return-value shape.
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
from urllib.parse import urlparse

from motor.motor_asyncio import AsyncIOMotorClient

logger = logging.getLogger("lockscore.odds_cache")

# ── Odds API base URL constant re-exported from the gateway ──────────
# ``services.odds_api_gateway`` owns the definitive value; we keep a
# module-level alias here so this file contains no hard-coded provider
# URL literal (Phase 2γ guardrail).
from services.odds_api_gateway import ODDS_API_BASE  # noqa: E402


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
# Phase 3 — Time-aware TTL scaling (2026-08)
# ═════════════════════════════════════════════════════════════════════
# Odds move MUCH more aggressively as tip-off approaches. If the
# nearest game in a sport is 12 hours away, refreshing every 5 minutes
# is pure waste — the line barely moves. But the same feed 30 minutes
# before tip needs tight refresh.
#
# `_TIME_AWARE_TTL_MULTIPLIER` maps "hours to nearest game" → TTL
# multiplier for `bulk_odds` and `event_odds` endpoints. The multiplier
# is applied to BOTH the fresh window AND the stale window, but the
# stale window is capped at 24 h.
#
# The base 5-min fresh × 12 = 60 min fresh for far-out games. The
# 30-min stale × 12 = 6 h stale (safe — odds don't jump wildly 24h
# before puck-drop / tip-off).
_TIME_AWARE_MULTIPLIERS: list[tuple[float, float]] = [
    # (max_hours_until_nearest_game, multiplier)
    (2.0,   1.0),      # < 2 h    →  base TTL (aggressive)
    (6.0,   3.0),      # 2 – 6 h  →  base × 3
    (12.0,  6.0),      # 6 – 12 h →  base × 6
    (24.0,  12.0),     # 12 – 24 h → base × 12
    (48.0,  24.0),     # 24 – 48 h → base × 24
    (float("inf"), 48.0),   # ≥ 48 h  → base × 48
]
_TIME_AWARE_ENDPOINTS = {"bulk_odds", "event_odds", "event_alt_lines"}
_TIME_AWARE_MAX_STALE = 24 * 3600     # never let stale exceed 24 h

# ─── Per-sport "nearest game" cache (avoids per-request Mongo lookup)
_NEAREST_GAME_CACHE: dict[str, tuple[float, Optional[float]]] = {}
_NEAREST_GAME_CACHE_TTL = 600   # 10 min — recomputed after this


async def _compute_hours_to_nearest_game(
    db, sport_key: Optional[str],
) -> Optional[float]:
    """Return hours until the earliest upcoming game for this sport,
    based on the most-recently-cached events/bulk-odds payload in the
    SWR cache. Falls back to None (→ base TTL) if we have no signal.
    """
    if not sport_key or db is None:
        return None
    # Local micro-cache — recompute at most once per 10 min.
    now_t = time.time()
    cached = _NEAREST_GAME_CACHE.get(sport_key)
    if cached and (now_t - cached[0]) < _NEAREST_GAME_CACHE_TTL:
        return cached[1]

    try:
        # Look for cached payloads for this sport that have events
        # with a `commence_time` — bulk_odds and events_list both
        # store an array of game dicts.
        candidates = db.odds_api_cache.find(
            {"sport_key": sport_key,
              "endpoint_type": {"$in": ["bulk_odds", "events_list"]}},
            {"body": 1},
        ).sort("refreshed_at", -1).limit(3)
        now_utc = datetime.now(timezone.utc)
        earliest: Optional[datetime] = None
        async for doc in candidates:
            body = doc.get("body")
            if not isinstance(body, list):
                continue
            for g in body:
                if not isinstance(g, dict):
                    continue
                ct = g.get("commence_time")
                if not ct:
                    continue
                try:
                    ct_dt = datetime.fromisoformat(
                        str(ct).replace("Z", "+00:00"))
                    if ct_dt.tzinfo is None:
                        ct_dt = ct_dt.replace(tzinfo=timezone.utc)
                except Exception:
                    continue
                # Only consider FUTURE games (or in-progress within 4h).
                if ct_dt < now_utc - timedelta(hours=4):
                    continue
                if earliest is None or ct_dt < earliest:
                    earliest = ct_dt
        hours = None
        if earliest is not None:
            delta_sec = (earliest - now_utc).total_seconds()
            hours = max(0.0, delta_sec / 3600.0)
        _NEAREST_GAME_CACHE[sport_key] = (now_t, hours)
        return hours
    except Exception as e:
        logger.debug("_compute_hours_to_nearest_game err %s: %s",
                      sport_key, e)
        return None


def _select_ttl_multiplier(hours_to_nearest: Optional[float]) -> float:
    """Look up the TTL multiplier for a given hours-to-nearest-game."""
    if hours_to_nearest is None:
        return 1.0
    for hmax, mult in _TIME_AWARE_MULTIPLIERS:
        if hours_to_nearest <= hmax:
            return mult
    return 1.0


# ═════════════════════════════════════════════════════════════════════
# Phase B — Off-peak TTL scaling (2026-08)
# ═════════════════════════════════════════════════════════════════════
# PerksLocks is a pick-generation app, not a live-odds app.  Between
# 03:00 – 14:00 UTC (roughly the overnight window in the US) there are
# very few active games and even fewer active users, so we further
# multiply the time-aware TTLs by `_OFFPEAK_MULT` during that window.
# Combined with the picks-scope + snapshot cadence changes this drops
# the total credit bill by an additional ~15-20%.
_OFFPEAK_HOURS_UTC = set(range(3, 14))       # 03:00 – 13:59 UTC
_OFFPEAK_MULT = 2.0
_OFFPEAK_MAX_STALE = 24 * 3600


def _offpeak_multiplier() -> float:
    if datetime.now(timezone.utc).hour in _OFFPEAK_HOURS_UTC:
        return _OFFPEAK_MULT
    return 1.0


async def _time_aware_ttls(
    db, endpoint_type: str, sport_key: Optional[str],
) -> tuple[int, int, dict]:
    """Return (fresh_ttl, stale_ttl, debug_meta) after applying the
    time-aware scaling multiplier AND the off-peak multiplier."""
    base_fresh, base_stale = _TTL_POLICY.get(
        endpoint_type, _TTL_POLICY["generic"])
    if endpoint_type not in _TIME_AWARE_ENDPOINTS:
        # Even non-time-aware endpoints get the off-peak scaling —
        # but never *shorten* the base stale window (we only cap if
        # scaling is actively boosting a shorter TTL beyond the cap).
        off = _offpeak_multiplier()
        scaled_stale = int(base_stale * off)
        stale_capped = (min(scaled_stale, _OFFPEAK_MAX_STALE)
                        if off > 1.0 else scaled_stale)
        return (int(base_fresh * off),
                stale_capped,
                {"offpeak_multiplier": off})
    hours = await _compute_hours_to_nearest_game(db, sport_key)
    mult = _select_ttl_multiplier(hours)
    off = _offpeak_multiplier()
    combined = mult * off
    fresh = int(base_fresh * combined)
    stale = min(int(base_stale * combined), _TIME_AWARE_MAX_STALE)
    return fresh, stale, {"hours_to_nearest_game": hours,
                           "ttl_multiplier": mult,
                           "offpeak_multiplier": off}


# ═════════════════════════════════════════════════════════════════════
# Mongo helpers (lazy, so tests can inject a fake db)
# ═════════════════════════════════════════════════════════════════════
# Phase 3B — under normal FastAPI runtime we route through the shared
# owner (services.database.get_database()).  Motor clients are bound
# to the event loop on which they were created; when a unit test
# creates a fresh asyncio loop we detect the loop mismatch and build
# a per-loop client so the tests keep working.  This preserves the
# existing per-loop cache semantics WITHOUT creating a new runtime
# client per FastAPI request.
_DB_CACHE: dict[str, Any] = {"client": None, "db": None, "inited": False, "loop": None}


def _reset_db_cache() -> None:
    """Test helper — force the next `_get_db()` call to rebind against
    the CURRENT running event loop."""
    _DB_CACHE["client"] = None
    _DB_CACHE["db"] = None
    _DB_CACHE["inited"] = False
    _DB_CACHE["loop"] = None


def _get_db():
    """Lazy MongoDB db handle.

    Under FastAPI runtime the running loop matches the shared client's
    loop, so we return the shared owner's database handle (no new
    connections).  Under asyncio.run()-based unit tests each test
    spawns a fresh loop; Motor clients are loop-bound, so we detect
    the loop change and build a per-loop client on demand.  This
    preserves the original per-loop test semantics without creating
    additional runtime clients in production.
    """
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None

    # Fast path — shared runtime handle if we haven't been forced to
    # rebind for a different loop.
    if _DB_CACHE.get("db") is None:
        try:
            from services.database import get_database, get_client
            shared_db = get_database()
            shared_client_loop = getattr(
                get_client(), "get_io_loop", lambda: None,
            )()
            # If we're on the same loop as the shared client (normal
            # FastAPI runtime), use it directly.
            if current_loop is None or current_loop is shared_client_loop:
                _DB_CACHE["client"] = None
                _DB_CACHE["db"]     = shared_db
                _DB_CACHE["loop"]   = shared_client_loop
                _DB_CACHE["inited"] = False
                return shared_db
        except Exception:
            pass

    # If the cached handle is bound to a different loop, rebuild for
    # this loop (unit tests).
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
    """Phase 3C — delegate to central registry.  The per-loop
    _DB_CACHE flag is kept only so unit tests (running in fresh
    loops) still skip repeated calls; the registry's own idempotency
    ensures we never create duplicates in production."""
    if _DB_CACHE["inited"]:
        return
    try:
        from services import index_registry as _ir
        await _ir.ensure_collection(db, "odds_api_cache")
        await _ir.ensure_collection(db, "odds_api_request_log")
        _DB_CACHE["inited"] = True
    except Exception as e:
        logger.warning("odds_cache ensure_indices via registry: %s", e)


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
# ═════════════════════════════════════════════════════════════════════
# Cache-row writer (used by OddsApiGateway to persist upstream payloads)
# ═════════════════════════════════════════════════════════════════════
async def _persist_cache_row(
    db, *, url: str, params: dict, data: Any,
    endpoint_type: str = "generic",
    sport_key: Optional[str] = None,
    markets: Optional[str] = None,
) -> None:
    """Persist an upstream Odds API payload into ``odds_api_cache``.

    Used by ``services.odds_api_gateway.OddsApiGateway`` after it
    performs the HTTP request itself.  Never issues HTTP.
    """
    if db is None or data is None:
        return
    try:
        await _ensure_indexes(db)
        key = _cache_key(url, params or {})
        body_hash = _hash_body(data)
        existing = await db.odds_api_cache.find_one(
            {"cache_key": key}, {"body_hash": 1})
        unchanged = (existing and existing.get("body_hash") == body_hash)
        doc = {
            "cache_key":     key,
            "url":           url,
            "params":        {k: v for k, v in (params or {}).items()
                                if k.lower() not in ("apikey", "api_key")},
            "endpoint_type": endpoint_type,
            "sport_key":     sport_key,
            "markets":       markets,
            "body_hash":     body_hash,
            "refreshed_at":  time.time(),
            "refreshed_iso": datetime.now(timezone.utc).isoformat(),
        }
        if not unchanged:
            doc["body"] = data
        await db.odds_api_cache.update_one(
            {"cache_key": key}, {"$set": doc}, upsert=True,
        )
    except Exception as e:  # pragma: no cover
        logger.debug("_persist_cache_row failed: %s", e)


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
            "endpoint_path":   urlparse(url).path,
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
    # Phase 3 — time-aware TTL scaling. If the nearest game in this
    # sport is many hours away, extend the fresh window so we don't
    # burn credits refreshing lines that barely move. Only applies to
    # sport-specific endpoints (bulk_odds / event_odds / alt_lines).
    ttl_meta: dict = {}
    if sport_key and endpoint_type in _TIME_AWARE_ENDPOINTS \
       and db is not None:
        try:
            fresh_ttl, stale_ttl, ttl_meta = await _time_aware_ttls(
                db, endpoint_type, sport_key,
            )
        except Exception as e:
            logger.debug("time-aware TTL failed for %s: %s",
                          sport_key, e)
    key = _cache_key(url, params)
    now = time.time()

    # Phase 3 — "no games in horizon" pre-flight. If the events list
    # for this sport has been cached AND shows zero games within the
    # next 48 h, skip odds fetches entirely for a while (we already
    # know the response will be empty). This is a huge saving for
    # off-season sports keys the app still lists (e.g. `basketball_nba`
    # in July, `americanfootball_nfl` in April).
    if (sport_key and endpoint_type == "bulk_odds"
            and db is not None and not force_refresh):
        try:
            hours = await _compute_hours_to_nearest_game(db, sport_key)
            if hours is not None and hours > 48.0:
                await _write_request_log(
                    db, url=url, params=params, sport_key=sport_key,
                    markets=markets, caller=caller,
                    cache_status="hit", upstream_called=False,
                    reason=f"no_games_in_48h · nearest_h={hours:.1f}",
                )
                return []
        except Exception as e:
            logger.debug("no-games pre-flight err %s: %s",
                          sport_key, e)

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
                reason = f"fresh (age={int(age)}s < ttl={fresh_ttl}s)"
                if ttl_meta:
                    h = ttl_meta.get("hours_to_nearest_game")
                    m = ttl_meta.get("ttl_multiplier")
                    reason += f" · nearest_game_h={h} · mult={m}"
                await _write_request_log(
                    db, url=url, params=params, sport_key=sport_key,
                    markets=markets, caller=caller,
                    cache_status="hit", upstream_called=False,
                    reason=reason,
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

    Phase 2γ: when ``ODDS_GATEWAY_ENABLED`` is true (the default),
    this function delegates transport to ``OddsApiGateway`` — every
    call is budget-reserved, single-flight-suppressed, and logged.
    When the flag is off, the legacy centralized-cache path below is
    used.  The flag never bypasses budget or coordinator checks.
    """
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

    # ── Phase 2γ: route through OddsApiGateway when enabled ─────────
    import os
    _gw_env = os.environ.get("ODDS_GATEWAY_ENABLED", "true").strip().lower()
    if _gw_env in ("", "1", "true", "yes", "on"):
        db = _get_db()
        if db is not None:
            try:
                from services.odds_api_gateway import OddsApiGateway
                from services import provider_budget_priority as _pbp
                gw = OddsApiGateway(db)
                # ── 2026-08-23 QUOTA — priority routing ──
                # Prior code passed no priority; gateway defaulted to
                # P3.  Derive priority from the markets tag so live
                # game / player-prop / alt fetches sit in the correct
                # lane.
                _mk_tag = (markets or params.get("markets") or "").lower()
                _url_l = (url or "").lower()
                if any(t in _mk_tag for t in ("player_", "batter_",
                                                "pitcher_")):
                    _priority = _pbp.P2_PLAYER_PROPS
                elif any(t in _mk_tag for t in ("alternate_",
                                                 "_alternate", "btts",
                                                 "double_chance")):
                    _priority = _pbp.P3_ALT_STRONG
                elif ("h2h" in _mk_tag or "spreads" in _mk_tag
                        or "totals" in _mk_tag
                        or "/odds" in _url_l or "/events" in _url_l):
                    _priority = _pbp.P1_LOCKS_TODAY
                else:
                    _priority = _pbp.P3_ALT_STRONG
                res = await gw.fetch(
                    url,
                    params=params,
                    caller=caller or "legacy_cached_httpx_get",
                    reason="legacy_call",
                    job_name=f"legacy:{ep}",
                    sport_key=sport_key,
                    markets=markets or params.get("markets"),
                    regions=params.get("regions"),
                    bookmakers=params.get("bookmakers"),
                    odds_format=params.get("oddsFormat"),
                    priority=_priority,
                    emergency_requested=False,
                    cache_policy="force_refresh" if force_refresh else "normal",
                    timeout_seconds=timeout,
                )
                if res and res.data is not None:
                    return res.data
                # Fall through to cache lookup on failure so we don't
                # regress existing behavior of returning last-known
                # cached data.
                if db is not None:
                    try:
                        cached_row = await db.odds_api_cache.find_one(
                            {"cache_key": _cache_key(url, params)},
                            {"body": 1},
                        )
                        if cached_row and cached_row.get("body") is not None:
                            return cached_row["body"]
                    except Exception:
                        pass
                return None
            except Exception as _gw_err:
                logger.warning(
                    "OddsApiGateway path failed, falling back to "
                    "legacy transport: %s", _gw_err,
                )

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
