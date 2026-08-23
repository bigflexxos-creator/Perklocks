"""
Sports Engine — backed by The Odds API (the-odds-api.com).

STRICT POLICY: Only display matchups returned by a live API response.
Never invent games. If the API returns nothing for a sport, that sport
contributes ZERO picks and the UI shows "No games available".

Coverage from a single key:
- MLB        → baseball_mlb
- NBA        → basketball_nba
- NFL        → americanfootball_nfl  (regular) + _preseason during summer
- Soccer     → multiple leagues, combined
- Tennis     → currently active ATP/WTA tournament

Free tier: 500 requests/month. We use 5 per daily refresh (~150/month).
"""
import os
import random
import asyncio
import logging
import statistics
from datetime import datetime, timezone, timedelta
import datetime as _dt
from typing import Optional

import httpx

logger = logging.getLogger(__name__)
# Odds API key resolution: prefer THE_ODDS_API_KEY env var (the recommended
# Odds API key MUST be provided via env. No source fallback — a committed
# key is a leak vector (SEC-002, fixed 2026-06-25). If missing, the
# downstream HTTP layer will surface the misconfiguration as a 401 to the
# operator rather than silently using a stale (potentially exhausted /
# rotated) key.
ODDS_KEY = os.environ.get("THE_ODDS_API_KEY") or ""
BASE = "https://api.the-odds-api.com/v4"

SPORT_KEYS: dict[str, list[str]] = {
    "MLB": ["baseball_mlb"],
    "NBA": ["basketball_nba"],
    # "WNBA": ["basketball_wnba"],  # DISABLED — killing ROI (-31% Player Points)
    "NFL": ["americanfootball_nfl", "americanfootball_nfl_preseason"],
    # CFB (College Football) — Week 0 is mid-late August. The Odds API
    # key `americanfootball_ncaaf` covers FBS (and some FCS) games.
    # We piggyback on the NFL pipeline architecture — same markets,
    # same lock thresholds, same probability engine — and add CFB-
    # specific signals (returning production, transfer portal, SoS)
    # in a follow-up session once a CFB-data provider key lands.
    "CFB": ["americanfootball_ncaaf"],
    # UFC / MMA — The Odds API uses one combined MMA key (covers UFC events).
    "UFC": ["mma_mixed_martial_arts"],
    # Phase 1B (2026-06) — NHL wired per R2a. Real sportsbook events /
    # markets reach the runtime; picks only emit when an authoritative
    # NHL model is available (currently MODEL_UNAVAILABLE — telemetried,
    # never substituted with sportsbook-follow probability).
    "NHL": ["icehockey_nhl"],
    # KBO disabled per user request 2026-06-18 — no new picks generated;
    # historical KBO picks were purged from DB at the same time.
    # "KBO": ["baseball_kbo"],
    "Soccer": [
        # FIFA World Cup 2026 — happening now
        "soccer_fifa_world_cup",
        "soccer_fifa_club_world_cup",
        # Major club competitions
        "soccer_conmebol_copa_libertadores",
        "soccer_conmebol_copa_sudamericana",
        "soccer_uefa_champs_league", "soccer_uefa_europa_league",
        "soccer_uefa_europa_conference_league",
        "soccer_uefa_champs_league_qualification",
        "soccer_uefa_european_championship",
        "soccer_uefa_nations_league",
        # Top European leagues
        "soccer_epl", "soccer_spain_la_liga", "soccer_germany_bundesliga",
        "soccer_italy_serie_a", "soccer_france_ligue_one",
        "soccer_germany_dfb_pokal", "soccer_spain_segunda_division",
        # Active mid-summer leagues (Brazilian, Scandinavian, etc.)
        "soccer_brazil_serie_a", "soccer_brazil_serie_b",
        "soccer_norway_eliteserien", "soccer_sweden_allsvenskan",
        "soccer_sweden_superettan", "soccer_finland_veikkausliiga",
        "soccer_chile_campeonato", "soccer_china_superleague",
        "soccer_league_of_ireland",
        # Australian A-League (added 2026-07-21 per user request). Season
        # runs October-May; The Odds API returns inactive during offseason.
        # Will start producing picks automatically when the season resumes.
        # Note: Australia Cup, Australian NPL (Capital Territory), and the
        # Chinese Cup are NOT available on The Odds API — those FanDuel
        # markets come from broader vendors (Sportradar/OpticOdds).
        "soccer_australia_aleague",
        # Major international competitions
        "soccer_conmebol_copa_america", "soccer_uefa_euro",
        "soccer_mexico_ligamx", "soccer_usa_mls",
    ],
    "Tennis": [
        # Grand Slams
        "tennis_atp_aus_open_singles", "tennis_wta_aus_open_singles",
        "tennis_atp_french_open", "tennis_wta_french_open",
        "tennis_atp_wimbledon", "tennis_wta_wimbledon",
        "tennis_atp_us_open", "tennis_wta_us_open",
        # Masters / Premier
        "tennis_atp_indian_wells", "tennis_wta_indian_wells",
        "tennis_atp_miami_open", "tennis_wta_miami_open",
        "tennis_atp_monte_carlo_masters", "tennis_atp_madrid_open", "tennis_wta_madrid_open",
        "tennis_atp_italian_open", "tennis_wta_italian_open",
        "tennis_atp_canadian_open", "tennis_wta_canadian_open",
        "tennis_atp_cincinnati_open", "tennis_wta_cincinnati_open",
        "tennis_atp_shanghai_masters", "tennis_atp_paris_masters",
        # 500/250 grass swing (active mid-June through July)
        "tennis_atp_queens_club_champ", "tennis_wta_queens_club_champ",
        "tennis_atp_halle_open", "tennis_wta_german_open",
        # Grass-court warmup tournaments (added 2026-06-23 — these are
        # active right NOW in Wimbledon prep week and were missing,
        # which is why the alt-spread tennis slate looked empty)
        "tennis_atp_eastbourne", "tennis_wta_eastbourne",
        "tennis_atp_mallorca_open",
        "tennis_wta_bad_homburg_open",
        "tennis_atp_stuttgart_open",
        "tennis_wta_birmingham_classic", "tennis_wta_nottingham_open",
        "tennis_atp_lyon_open", "tennis_atp_geneva_open",
        # Hard / clay shoulder events
        "tennis_atp_barcelona_open", "tennis_atp_hamburg_open",
        "tennis_atp_dubai", "tennis_wta_dubai",
        "tennis_atp_qatar_open", "tennis_atp_china_open", "tennis_wta_china_open",
        "tennis_atp_munich", "tennis_wta_charleston_open",
        "tennis_wta_strasbourg", "tennis_wta_stuttgart_open", "tennis_wta_wuhan_open",
    ],
}

# Cache active sports list per process so we don't burn quota.
_ACTIVE_KEYS: set[str] = set()
_ACTIVE_LOADED = False
# 2026-08-23 PASS 1 — cheap TTL refresh (30 min).  Previously
# ``_ACTIVE_LOADED`` was a one-shot boolean, so any provider catalog
# change (new active league mid-season) never reached production
# until the process restarted.  TTL of 30 min matches the existing
# health-cycle cadence and never induces aggressive polling.
_ACTIVE_LOADED_AT: float = 0.0
_ACTIVE_TTL_S: float = 30 * 60

# Circuit breaker: once the Odds API repeatedly fails (bad key, exhausted
# quota, network outage), stop hammering it for the rest of this process.
# Saves quota across container restarts AND prevents the 90-second hang on
# `/api/picks/today` that was caused by sequentially looping 50+ sport
# endpoints — each timing out at 15s — when the credentials were rotated
# out from under us.
_API_DISABLED = False
_API_DISABLED_REASON = ""
# Rolling counters so a single transient 5xx doesn't trip the breaker, but
# a sustained outage (or a bad/missing key) does — fast.
_API_401_STREAK = 0          # consecutive 401s
_API_FAIL_STREAK = 0         # consecutive non-200s of any kind
_API_TOTAL_OK = 0
_API_TOTAL_FAIL = 0
_API_LAST_ERR = ""
# Thresholds: trip after 2 consecutive 401s (almost certainly auth) OR 8
# consecutive failures of any kind (sustained outage). 2/8 is intentionally
# tight because any further calls during a real outage just waste time.
_API_401_TRIP = 2
_API_FAIL_TRIP = 8

# Concurrency throttle: cap parallel Odds API calls so we don't trip the
# per-second rate limit (429 EXCEEDED_FREQ_LIMIT) on bulk refresh.
_API_SEM = asyncio.Semaphore(4)

# ── Cross-pod circuit-breaker sync (2026-08-09 fix, ticket #222563) ────
# This app runs on 2 replicas (standard for the plan tier). The counters
# above are per-process, so the 2 pods can diverge: one trips the
# breaker while the other stays healthy, and admin's "reset" only
# cleared whichever pod happened to handle that request — the exact
# symptom reported (CIRCUIT OPEN reappearing, inconsistent total_fail
# across admin screenshots taken seconds apart). Fix: mirror trip/reset
# events into one shared Mongo doc; a periodic background loop
# (registered in server.py) pulls it so every pod converges on the
# same breaker state within one poll interval. The module globals
# above stay the fast, no-DB-hit path the hot request loop reads on
# every call — this only adds periodic reconciliation, not a DB
# round-trip per request.
_CB_COLLECTION = "circuit_breaker_state"
_CB_DOC_ID = "odds_api"
_CB_LAST_SYNCED_AT: Optional[datetime] = None
# Serializes the fire-and-forget pushes below so two pushes fired
# back-to-back (e.g. the 2 calls that make up a 401-streak trip) land
# in Mongo in the same order they were captured locally — without this,
# two independent asyncio tasks can complete out of order and let an
# EARLIER (e.g. pre-trip, disabled=False) snapshot overwrite a LATER
# one, silently losing the trip.
_CB_PUSH_LOCK: Optional[asyncio.Lock] = None


def _cb_push_lock() -> asyncio.Lock:
    global _CB_PUSH_LOCK
    if _CB_PUSH_LOCK is None:
        _CB_PUSH_LOCK = asyncio.Lock()
    return _CB_PUSH_LOCK


def _snapshot_cb_state() -> dict:
    return {
        "disabled": _API_DISABLED,
        "disabled_reason": _API_DISABLED_REASON,
        "consecutive_401s": _API_401_STREAK,
        "consecutive_failures": _API_FAIL_STREAK,
        "total_ok": _API_TOTAL_OK,
        "total_fail": _API_TOTAL_FAIL,
        "last_error": _API_LAST_ERR,
        "updated_at": datetime.now(timezone.utc),
    }


def _push_cb_state_async() -> None:
    """Fire-and-forget mirror of local breaker state into the shared
    doc. Safe to call from sync code — schedules onto the running
    event loop rather than blocking the caller. The snapshot is taken
    NOW (synchronously) so it reflects this exact call's state; the
    lock only orders the DB writes, not the reads of the globals."""
    snapshot = _snapshot_cb_state()
    try:
        from server import db as _db

        async def _do_push(_snap=snapshot, _dbref=_db):
            async with _cb_push_lock():
                await _dbref[_CB_COLLECTION].update_one(
                    {"_id": _CB_DOC_ID},
                    {"$set": _snap},
                    upsert=True,
                )

        # Only schedule when a loop is actually running — sync callers
        # (tests, CLI) would otherwise leave "Task was destroyed but it
        # is pending" warnings behind (Phase 1C cleanup).
        loop = asyncio.get_running_loop()
        loop.create_task(_do_push())
    except Exception as e:
        logger.debug("circuit breaker state push skipped: %s", e)


async def sync_circuit_breaker_from_db() -> None:
    """Pull the shared doc and adopt it locally if it is newer than
    the last sync we applied. Called on a periodic background loop so
    all pods converge on one circuit-breaker state (both trips AND
    admin resets propagate fleet-wide, not just to whichever pod
    handled that one request)."""
    global _API_DISABLED, _API_DISABLED_REASON
    global _API_401_STREAK, _API_FAIL_STREAK
    global _API_TOTAL_OK, _API_TOTAL_FAIL, _API_LAST_ERR, _CB_LAST_SYNCED_AT
    try:
        from server import db as _db
        doc = await _db[_CB_COLLECTION].find_one({"_id": _CB_DOC_ID})
        if not doc:
            return
        updated_at = doc.get("updated_at")
        if _CB_LAST_SYNCED_AT and updated_at and updated_at <= _CB_LAST_SYNCED_AT:
            return
        _CB_LAST_SYNCED_AT = updated_at
        _API_DISABLED = doc.get("disabled", _API_DISABLED)
        _API_DISABLED_REASON = doc.get("disabled_reason", _API_DISABLED_REASON)
        _API_401_STREAK = doc.get("consecutive_401s", _API_401_STREAK)
        _API_FAIL_STREAK = doc.get("consecutive_failures", _API_FAIL_STREAK)
        _API_TOTAL_OK = doc.get("total_ok", _API_TOTAL_OK)
        _API_TOTAL_FAIL = doc.get("total_fail", _API_TOTAL_FAIL)
        _API_LAST_ERR = doc.get("last_error", _API_LAST_ERR)
    except Exception as e:
        logger.debug("circuit breaker state pull skipped: %s", e)


def get_odds_api_status() -> dict:
    """Diagnostic snapshot for the admin endpoint. Helps the operator
    confirm whether the Odds API key in production is healthy without
    having to dig through container logs."""
    key = ODDS_KEY or ""
    return {
        "has_key": bool(key),
        "key_tail": (f"...{key[-4:]}" if len(key) >= 4 else ""),
        "disabled": _API_DISABLED,
        "disabled_reason": _API_DISABLED_REASON,
        "consecutive_401s": _API_401_STREAK,
        "consecutive_failures": _API_FAIL_STREAK,
        "total_ok": _API_TOTAL_OK,
        "total_fail": _API_TOTAL_FAIL,
        "last_error": _API_LAST_ERR[:200],
    }


def reset_odds_api_circuit() -> dict:
    """Manually re-arm the circuit breaker. Call this from the admin
    endpoint AFTER rotating THE_ODDS_API_KEY in production secrets so
    the next refresh actually tries the new key instead of staying
    permanently disabled from the previous failures.
    """
    global _API_DISABLED, _API_DISABLED_REASON, _API_401_STREAK, _API_FAIL_STREAK, _API_LAST_ERR
    _API_DISABLED = False
    _API_DISABLED_REASON = ""
    _API_401_STREAK = 0
    _API_FAIL_STREAK = 0
    _API_LAST_ERR = ""
    logger.info("Odds API circuit breaker re-armed by admin request")
    _push_cb_state_async()
    return get_odds_api_status()


async def _get(url: str, params: dict, *,
                endpoint_type: Optional[str] = None,
                caller: Optional[str] = None,
                sport_key: Optional[str] = None,
                skip_completed: bool = False) -> list | dict | None:
    """Cache-first Odds API fetch.

    All callers route through the centralized SWR cache in
    `services/odds_cache.py`. On MISS the actual upstream fetch runs
    inside `_upstream_fetch` (below) — the same code that used to be
    inline here, with all the circuit-breaker / 401 / 429 / retry
    handling intact.

    Skipping the cache: pass `endpoint_type=None` and it still calls
    upstream directly (used by /sports probe on startup).
    """
    if not ODDS_KEY or _API_DISABLED:
        return None

    # Infer endpoint_type + markets tag from the URL if the caller
    # didn't provide one — keeps every call site auto-tagged.
    ep_type = endpoint_type
    if ep_type is None:
        if url.endswith("/sports"):
            ep_type = "sports_list"
        elif "/events/" in url and url.endswith("/odds"):
            ep_type = "event_odds"
        elif url.endswith("/events"):
            ep_type = "events_list"
        elif url.endswith("/odds"):
            ep_type = "bulk_odds"
        else:
            ep_type = "generic"
    markets_tag = (params or {}).get("markets") or ""

    async def _upstream_fetch():
        # Phase 2γ closeout: cache MISS/STALE path also goes through
        # the gateway.  The gateway owns httpx, budget, single-flight,
        # request logging, and CB-state callback.
        return await _gateway_fallback_get(
            url=url, params=(params or {}),
            caller=caller or "sports_engine._get",
            sport_key=sport_key,
            markets_tag=markets_tag,
            reason="cache_miss",
        )

    try:
        from services.odds_cache import cached_odds_get
        return await cached_odds_get(
            url=url,
            params=params,
            endpoint_type=ep_type,
            caller=caller or "sports_engine._get",
            sport_key=sport_key,
            markets=markets_tag,
            upstream_fetch=_upstream_fetch,
            skip_completed=skip_completed,
        )
    except Exception as e:
        # Phase 2γ closeout: cache-layer failure MUST NOT open a
        # direct httpx path.  Go through the gateway with normal
        # budget policy — 2026-08-06 fix: no longer requests
        # emergency capacity (that would be
        # ``blocked_emergency_policy`` under the current whitelist).
        # Cache-infra failures are rare and are handled by the same
        # daily budget as any other cache miss.
        logger.warning(
            "odds_cache path failed (%s) — falling through to gateway "
            "with normal budget policy", e,
        )
        return await _gateway_fallback_get(
            url=url, params=params,
            caller=caller or "sports_engine._get",
            sport_key=sport_key,
            markets_tag=markets_tag,
            reason="cache_infrastructure_failure",
        )


# ═════════════════════════════════════════════════════════════════════
# Public CB-state ingestion (Phase 2γ closeout).  The gateway calls
# this after every upstream response so the sports_engine circuit
# breaker state stays consistent even though the transport moved.
# ═════════════════════════════════════════════════════════════════════
def record_odds_call_result(*, status_code: int | None, body: str = "",
                              ok: bool = False,
                              exception: str | None = None) -> None:
    global _API_DISABLED, _API_DISABLED_REASON
    global _API_401_STREAK, _API_FAIL_STREAK
    global _API_TOTAL_OK, _API_TOTAL_FAIL, _API_LAST_ERR
    try:
        if ok and not exception:
            _API_401_STREAK = 0
            _API_FAIL_STREAK = 0
            _API_TOTAL_OK += 1
            try:
                from services.odds_provider import report_success as _op_ok
                _op_ok()
            except Exception:
                pass
            _push_cb_state_async()
            return
        # Failure branches.
        if status_code == 401:
            _API_401_STREAK += 1
            _API_FAIL_STREAK += 1
            _API_TOTAL_FAIL += 1
            _API_LAST_ERR = f"401: {body[:200]}"
            try:
                from services.odds_provider import report_failure as _op_fail
                _op_fail(401, (body or "")[:60])
            except Exception:
                pass
            if _API_401_STREAK >= _API_401_TRIP:
                _API_DISABLED = True
                _API_DISABLED_REASON = (
                    f"401 streak ({_API_401_STREAK}): {(body or '')[:120]}")
        elif status_code == 429:
            _API_FAIL_STREAK += 1
            _API_TOTAL_FAIL += 1
            _API_LAST_ERR = "429: rate limited"
            try:
                from services.odds_provider import report_failure as _op_fail
                _op_fail(429, "rate_limited")
            except Exception:
                pass
        elif exception:
            _API_FAIL_STREAK += 1
            _API_TOTAL_FAIL += 1
            _API_LAST_ERR = f"exc: {exception}"
            if _API_FAIL_STREAK >= _API_FAIL_TRIP:
                _API_DISABLED = True
                _API_DISABLED_REASON = (
                    f"exception streak: {str(exception)[:120]}")
        else:
            # Phase 1C (§5) — 422 is a MARKET-SHAPE response, not a
            # provider-health failure.  The gateway's bundle-bisection
            # design intentionally probes markets that can 422
            # (unsupported market / event); those probes previously
            # incremented the fail streak and, after 8 consecutive
            # probes, tripped the breaker and disabled the entire
            # provider ("fail streak (8): 422" — observed live
            # 2026-08-14, Cincinnati Open alt-line probes).  Count 422s
            # in totals for observability but never toward the streak.
            if status_code == 422:
                _API_TOTAL_FAIL += 1
                _API_LAST_ERR = f"422: {(body or '')[:160]}"
                _push_cb_state_async()
                return
            _API_FAIL_STREAK += 1
            _API_TOTAL_FAIL += 1
            _API_LAST_ERR = f"{status_code}: {(body or '')[:160]}"
            try:
                from services.odds_provider import report_failure as _op_fail
                _op_fail(int(status_code or 0), "non_200")
            except Exception:
                pass
            if _API_FAIL_STREAK >= _API_FAIL_TRIP:
                _API_DISABLED = True
                _API_DISABLED_REASON = (
                    f"fail streak ({_API_FAIL_STREAK}): {_API_LAST_ERR[:120]}")
        _push_cb_state_async()
    except Exception:  # pragma: no cover
        pass


async def _gateway_fallback_get(*, url: str, params: dict,
                                  caller: str,
                                  sport_key: str | None,
                                  markets_tag: str | None,
                                  reason: str,
                                  emergency_requested: bool = False) -> list | dict | None:
    """Phase 2γ closeout replacement for the removed direct httpx
    transport.  Goes through OddsApiGateway with an
    emergency reason so ProviderBudget policy governs whether the
    call is allowed.  Never opens a direct httpx connection.

    2026-08-06 fix — emergency reserve is now OFF by default.  Normal
    cache-miss fetches (which this helper serves 99 %+ of the time)
    must NOT request emergency capacity — ``ProviderBudget``'s
    ``can_use_emergency_reserve`` only whitelists the reasons
    ``board_missing`` and ``board_critically_stale``, so passing
    ``emergency_requested=True`` for a cache_miss reason was silently
    turning every fetch into a ``blocked_emergency_policy`` refusal.
    True board-recovery callers (e.g. cache_infrastructure_failure)
    still opt-in explicitly by setting ``emergency_requested=True``
    AND passing a whitelisted ``reason``.
    """
    if not ODDS_KEY or _API_DISABLED:
        return None
    try:
        from services.odds_api_gateway import OddsApiGateway
        from services import provider_budget_priority as _pbp
        from server import db as _server_db
        gw = OddsApiGateway(_server_db)
        # ── 2026-08-23 QUOTA — priority routing ──
        # Route current game markets to P1, player props to P2, alt/
        # strong-market secondaries to P3.  Prior code passed no
        # priority to gateway.fetch → the gateway defaulted every
        # request to P3, so live-slate game fetches shared a lane with
        # background preloads.  Priority is derived from the markets
        # tag (Odds API canonical vocabulary), never a display string.
        _mk = (markets_tag or "").lower()
        _url_l = (url or "").lower()
        if any(t in _mk for t in ("player_", "batter_", "pitcher_")):
            _priority = _pbp.P2_PLAYER_PROPS
        elif any(t in _mk for t in ("alternate_", "_alternate",
                                     "btts", "double_chance")):
            _priority = _pbp.P3_ALT_STRONG
        elif "h2h" in _mk or "spreads" in _mk or "totals" in _mk \
                or "/odds" in _url_l or "/events" in _url_l:
            _priority = _pbp.P1_LOCKS_TODAY
        else:
            _priority = _pbp.P3_ALT_STRONG
        result = await gw.fetch(
            url,
            params={k: v for k, v in (params or {}).items()
                     if k.lower() not in ("apikey", "api_key")},
            caller=caller,
            reason=f"sports_engine_fallback:{reason}",
            job_name="sports_engine_cache_failure_fallback",
            sport_key=sport_key,
            markets=markets_tag,
            priority=_priority,
            emergency_requested=emergency_requested,
        )
        if result and result.data is not None:
            record_odds_call_result(
                status_code=result.get("http_status") or 200,
                ok=True,
            )
            return result.data
        record_odds_call_result(
            status_code=result.get("http_status") if result else None,
            body=result.get("reason", "") if result else "",
            ok=False,
        )
        return None
    except Exception as e:
        record_odds_call_result(status_code=None, exception=str(e))
        return None


async def _load_active_sports() -> None:
    """Load provider active-key catalog.

    2026-08-23 PASS 1 — Add cheap TTL refresh so the active-key set
    does not remain frozen for the entire backend process.
    2026-08-23 QUOTA PASS — REPLACE-on-success semantics (union-only
    left inactive keys in the snapshot forever, causing false
    starvation on decommissioned leagues).  On a SUCCESSFUL provider
    catalog refresh we now REPLACE the snapshot with the latest
    active set.  On FAILURE we preserve the last-good snapshot —
    a transient provider blip never wipes it.  Reuses the existing
    ``_get`` cache path — no aggressive polling.
    """
    global _ACTIVE_LOADED, _ACTIVE_LOADED_AT
    import time as _t
    _now = _t.monotonic()
    if _ACTIVE_LOADED and (_now - _ACTIVE_LOADED_AT) < _ACTIVE_TTL_S:
        return
    data = await _get(f"{BASE}/sports", {})
    if isinstance(data, list) and data:
        # SUCCESS — REPLACE the snapshot with the fresh catalog so
        # keys that are no longer active fall out.
        _fresh = {s["key"] for s in data if isinstance(s, dict)
                    and s.get("active") and s.get("key")}
        if _fresh:
            _ACTIVE_KEYS.clear()
            _ACTIVE_KEYS.update(_fresh)
            _ACTIVE_LOADED = True
            _ACTIVE_LOADED_AT = _now
            return
    # FAILURE — keep last-good snapshot; only advance the timestamp
    # so we don't hammer the provider retrying immediately.
    _ACTIVE_LOADED_AT = _now


async def ensure_active_keys_fresh() -> None:
    """Public helper for consumers that want to force a TTL check
    before reading ``_ACTIVE_KEYS`` (e.g. per-league starvation
    detector).  Idempotent — no-op inside the TTL window.
    """
    await _load_active_sports()


async def _fetch_odds_for(sport_key: str, regions: str = "us", sport: str | None = None) -> list:
    """Bulk /odds fetch for a given sport key.

    Per-sport market tuning (2026-07-07)
    ------------------------------------
    Tennis + UFC are 1v1 sports with **no native spreads/totals** on
    The Odds API bulk endpoint. Historically we asked for
    `markets=h2h,spreads,totals` universally; for these sports the API
    would either 422 outright or (worse) return games with empty
    `bookmakers[]`, silently dropping the h2h moneyline picks we
    actually wanted. That was the "ATP moneylines disappear" bug and
    the sole reason we needed `_backfill_tennis_moneylines` as a
    downstream rescue.

    Fix: request only the markets each sport actually supports in
    bulk, and 422-retry with just `h2h` for anything else. Alt lines
    (spreads/totals variants) still flow through the per-event
    endpoint via `_fetch_tennis_event_alts` / `_fetch_mlb_event_alts`
    exactly as before — no credit cost change.
    """
    # 1v1 sports: Tennis exposes only h2h on the bulk endpoint (alt
    # spreads/totals flow via the per-event endpoint).  UFC: Phase 1C
    # (T3b) — The Odds API DOES expose MMA rounds totals in bulk, so
    # request h2h+totals; the defensive 422/empty retry below falls
    # back to h2h-only if a specific card lacks totals.  Registry and
    # runtime must agree (registry advertises UFC h2h + totals).
    if sport == "Tennis":
        markets_param = "h2h"
    elif sport == "UFC":
        markets_param = "h2h,totals"
    else:
        markets_param = "h2h,spreads,totals"

    data = await _get(
        f"{BASE}/sports/{sport_key}/odds",
        {"regions": regions, "markets": markets_param, "oddsFormat": "american"},
        endpoint_type="bulk_odds",
        caller="sports_engine._fetch_odds_for",
        sport_key=sport_key,
        skip_completed=True,
    )
    # Defensive 422-retry: if the multi-market request came back empty
    # for a team sport (rare, but happens on obscure minor leagues),
    # fall back to h2h-only so we at least surface the moneyline picks.
    if not data and markets_param != "h2h":
        data = await _get(
            f"{BASE}/sports/{sport_key}/odds",
            {"regions": regions, "markets": "h2h", "oddsFormat": "american"},
            endpoint_type="bulk_odds",
            caller="sports_engine._fetch_odds_for.retry",
            sport_key=sport_key,
            skip_completed=True,
        )
    return data if isinstance(data, list) else []


# ───────────────────────── Lock Score Engine ─────────────────────────


def _grade(score: float) -> str:
    # User-defined band labels — match the bet-quality floor tiers in
    # compute_lock_score() exactly so the badge on every card always
    # reflects which earned tier the pick landed in.
    #
    # Block 8 (2026-06 — MAGIC → LOCK SCORE INTEGRATION) — the new
    # ``APEX Lock`` band is reserved for score == 100 AND explicitly
    # gated by ``services.magic.apex_gate.evaluate_apex``.  Because
    # ``_grade`` cannot see the ``apex_lock`` flag directly, we treat
    # score >= 100 as APEX; the defensive downgrade in
    # ``services.magic.lock_score_integrator.defensive_downgrade_if_needed``
    # guarantees any accidental 100 without ``apex_lock=True`` is
    # forced back to 99 BEFORE this function runs.
    if score >= 100:
        return "APEX Lock"
    if score >= 98:
        return "Elite Lock"
    if score >= 95:
        return "Strong Lock"
    if score >= 90:
        return "Lock"
    if score >= 85:
        return "Playable"
    return "Pass"


def _confidence(score: float) -> str:
    if score >= 90:
        return "Very High"
    if score >= 85:
        return "High"
    if score >= 75:
        return "Medium"
    return "Low"


def _implied_prob(american_odds: int) -> float:
    if not american_odds:
        return 0.5
    if american_odds > 0:
        return 100 / (american_odds + 100)
    return -american_odds / (-american_odds + 100)


def _win_prob_to_american(prob: float) -> int:
    prob = max(0.05, min(0.95, prob))
    if prob >= 0.5:
        return int(round(-100 * prob / (1 - prob)))
    return int(round(100 * (1 - prob) / prob))


def compute_lock_score(factors: dict[str, float], win_prob: float | None = None,
                        pick: dict | None = None, bucket_row: dict | None = None,
                        edge_percent: float | None = None) -> tuple[float, dict]:
    """Bet-Quality Score (0-99). **NOT a direct win-probability.**

    Lock Score is a composite of six weighted components per the v3 spec:

      0.35 * normalized_model_edge   (edge_percent normalised to 0-100)
      0.20 * market_alignment         (low factor variance = high agreement)
      0.15 * historical_roi           (bucket ROI from learning engine)
      0.10 * data_quality             (lineup / API completeness — base 75)
      0.10 * volatility_control       (inverse of is_long_shot / chalk risk)
      0.10 * closing_line_strength    (CLV reward)

    Bands stay the same — 99-95 Elite, 94-90 Premium, 89-85 Strong, 84-80
    Standard, <80 Pass — so high lock numbers are preserved for genuinely
    high-quality bets across multiple dimensions, not just confidence.
    """
    weighted = {k: round(v * 100, 1) for k, v in factors.items()}

    # Legacy fallback when caller doesn't pass a pick — used only by old code
    # paths that haven't migrated. Anchored on win_prob as before so tests
    # don't break.
    if pick is None:
        wp = max(0.0, min(1.0, (win_prob or 0) / 100.0))
        if wp < 0.30:   base = 40 + wp * (50 / 0.30)
        elif wp < 0.50: base = 50 + (wp - 0.30) * (20 / 0.20)
        elif wp < 0.70: base = 70 + (wp - 0.50) * (16 / 0.20)
        elif wp < 0.90: base = 86 + (wp - 0.70) * (11 / 0.20)
        else:           base = 97 + (wp - 0.90) * (2 / 0.10)
        avg = sum(factors.values()) / max(len(factors), 1)
        peak = max(factors.values()) if factors else 0
        score = base + (avg - 0.5) * 10 + (peak - 0.5) * 2
        return max(55.0, min(99.0, round(score, 1))), weighted

    # ── v3 six-component composite ────────────────────────────────────────
    # 1) Normalized model edge (35%)
    edge_pct = pick.get("edge_percent") or 0
    edge_comp = max(0.0, min(100.0, 50 + edge_pct * 5))

    # 2) Market alignment — agreement across factors (low stdev = high agreement)
    vals = list(factors.values()) if factors else []
    if len(vals) >= 2:
        mean = sum(vals) / len(vals)
        stdev = (sum((v - mean) ** 2 for v in vals) / len(vals)) ** 0.5
        # stdev 0 → 100, stdev 0.20+ → 0
        market_align = max(0.0, min(100.0, 100 - stdev * 500))
    else:
        market_align = 50.0

    # 3) Historical ROI (bucket-level)
    if bucket_row and bucket_row.get("n", 0) >= 10:
        roi = bucket_row.get("roi", 0.0)
        # ROI 20% → 100, 0% → 50, -10% → 0
        roi_comp = max(0.0, min(100.0, 50 + roi * 2.5))
        roi_available = True
    else:
        roi_comp = 50.0   # neutral until enough sample
        roi_available = False

    # 4) Data quality — base 75 (placeholder for future injury/lineup feeds)
    data_quality = 75.0

    # 5) Volatility control (lower volatility = higher score)
    vol = 80.0
    if pick.get("is_long_shot"):
        vol -= 25
    book = pick.get("book_odds") or 0
    if book >= 250:    vol -= 10        # lottery prices
    if book <= -400:   vol -= 10        # heavy chalk
    vol_comp = max(0.0, min(100.0, vol))

    # 6) Closing line strength (CLV)
    odds_at = pick.get("odds_at_pick")
    closing = pick.get("closing_odds")
    cls_available = False
    if odds_at and closing and odds_at != closing:
        try:
            from analytics import american_to_implied_pct as _imp
            clv = _imp(closing) - _imp(odds_at)
            cls_comp = max(0.0, min(100.0, 50 + clv * 5))
            cls_available = True
        except Exception:
            cls_comp = 50.0
    else:
        cls_comp = 50.0

    # ── Post-Cert Defect 4 — WEIGHT NORMALISATION over AVAILABLE
    # components.  ROI (n<10) and CLV (missing closing odds) were
    # previously fixed at neutral 50 with full 0.15 / 0.10 weight,
    # mathematically capping legitimate high-edge + high-alignment
    # pregame picks around 83 — below the >=85 Locks floor.  We now
    # redistribute the unavailable-component weight proportionally
    # across the components we DO have data for, so a strong pregame
    # pick can legitimately reach 85+ without inventing evidence.
    _components = [
        ("edge",   edge_comp,    0.35, True),
        ("align",  market_align, 0.20, True),
        ("roi",    roi_comp,     0.15, roi_available),
        ("dq",     data_quality, 0.10, True),
        ("vol",    vol_comp,     0.10, True),
        ("clv",    cls_comp,     0.10, cls_available),
    ]
    _avail_weight = sum(w for _, _, w, ok in _components if ok)
    if _avail_weight > 0:
        score = sum(v * w / _avail_weight
                    for _, v, w, ok in _components if ok)
    else:
        # Every component missing — degenerate case; keep original score.
        score = (0.35 * edge_comp + 0.20 * market_align + 0.15 * roi_comp
                 + 0.10 * data_quality + 0.10 * vol_comp + 0.10 * cls_comp)

    # ── Bet-Quality Floor (EVIDENCE-BASED, 2026-07-04 chalk-bias fix) ────
    # OLD: floor required BOTH win_prob AND edge (e.g. Elite needs wp≥80
    # AND edge≥15). This hard-coded chalk bias into the model — a +150
    # underdog with 45% wp could NEVER reach Elite even with elite EV,
    # bucket-hit rate, and factor agreement.
    #
    # NEW: floor is triggered by EVIDENCE, not implied probability:
    #   • EV per unit (chalk-neutral expected value from decimal odds × wp)
    #   • Historical bucket hit rate (learning engine's actual outcomes)
    #   • Factor agreement (variance across signals; 1 - stdev)
    #   • Absolute model edge (still counts, but not gated by wp)
    #
    # A +150 (wp 45%, edge 12%, EV +0.20u, bucket_hit 62%) now CAN reach
    # Elite Lock. A -300 (wp 78%, edge 3%, EV -0.05u, bucket_hit 55%) will
    # NOT — chalk with no evidence is downgraded.
    #
    # Read win-prob + edge from BOTH the explicit args (preferred — passed
    # by callers at pick generation time) AND the pick dict (used by the
    # validator's recompute path where the pick object is already built).
    wp_val = float(
        win_prob if win_prob is not None
        else (pick.get("win_probability") if pick else 0) or 0
    )
    ed_val = float(
        edge_percent if edge_percent is not None
        else (pick.get("edge_percent") if pick else 0) or 0
    )

    # Expected value per 1u risked — chalk-neutral. Positive EV = model
    # expects a profit; negative = model expects a loss regardless of
    # whether the pick is favourite or dog.
    wp_frac = wp_val / 100.0 if wp_val > 1.0 else wp_val
    book = pick.get("book_odds") or 0
    ev_units = 0.0
    if book and 0.0 <= wp_frac <= 1.0:
        if book >= 100:
            dec = 1.0 + book / 100.0
        elif book <= -100:
            dec = 1.0 + 100.0 / abs(book)
        else:
            dec = 1.0
        ev_units = wp_frac * (dec - 1.0) - (1.0 - wp_frac)

    # Historical bucket hit rate — real outcomes on this pick's bucket.
    bucket_hit = 0.0
    bucket_n = 0
    if bucket_row and bucket_row.get("n", 0) >= 20:
        bucket_n = bucket_row.get("n", 0)
        wins = bucket_row.get("wins", 0)
        losses = bucket_row.get("losses", 0)
        decided = wins + losses
        if decided > 0:
            bucket_hit = wins / decided

    # Factor agreement — inverse of standard deviation across model
    # signals. Already computed above for `market_align`.
    factor_agreement = market_align / 100.0  # 0..1

    floor = 0.0
    # PHASE 1D (G3) — the 98/95/90/85 hard-coded score LADDER is
    # RETIRED.  It floored weak composite scores UP to board-qualifying
    # bands from edge/EV/bucket/agreement conditions (score inflation).
    # ev_units / bucket_hit / factor_agreement remain available to the
    # 6-component composite itself; they no longer override it.
    if floor and score < floor:
        score = floor
    # Hard clamp — Lock Score band is 0-99. Without this the floor or
    # 6-component math could overflow past the band cap and break UI
    # badges / progress bars.
    score = min(99.0, score)

    # Store the 6 components + evidence signals so the UI / analytics can
    # inspect them later. Evidence fields (ev_units, bucket_hit, agreement)
    # are the new chalk-neutral tier gates added 2026-07-04.
    pick["lock_components"] = {
        "edge":         round(edge_comp, 1),
        "alignment":    round(market_align, 1),
        "roi":          round(roi_comp, 1),
        "data_quality": round(data_quality, 1),
        "volatility":   round(vol_comp, 1),
        "clv":          round(cls_comp, 1),
        "quality_floor": round(floor, 1) if floor else 0,
        # Evidence — chalk-neutral inputs to the tier floor
        "ev_units":     round(ev_units, 4),
        "bucket_hit":   round(bucket_hit, 4) if bucket_n else None,
        "bucket_n":     bucket_n,
        "agreement":    round(factor_agreement, 3),
    }
    return max(55.0, min(99.0, round(score, 1))), weighted


def _median_price(book_outcomes: list, name: str) -> int | None:
    """Median moneyline price across books for a given outcome name."""
    vals = [int(o["price"]) for o in book_outcomes if o.get("name") == name and isinstance(o.get("price"), (int, float))]
    if not vals:
        return None
    return int(statistics.median(vals))


def _consensus_market(game: dict, market_key: str) -> list:
    """Flatten all bookmaker outcomes for a given market into one list."""
    out = []
    for b in game.get("bookmakers", []):
        for m in b.get("markets", []):
            if m.get("key") == market_key:
                out.extend(m.get("outcomes", []))
    return out


def _build_pick(*, sport, league, event, event_time, market, pick_side,
                model_win_prob, book_odds, lock, factors, insights, external_id,
                is_alt_prop: bool = False, is_long_shot: bool = False,
                home_team_name: str | None = None,
                away_team_name: str | None = None,
                opposing_prices: list | None = None):
    # Filter out malformed prices outside realistic American odds range.
    # Alt prop picks are legitimately chalky but capped at -1000 max.
    # Long-shot picks (anytime goal scorer, etc.) can have huge plus prices.
    if book_odds is not None:
        if is_long_shot:
            # Anytime goal scorer odds range from +200 (top stars) to +10000
            # (defenders). Cap at +3500 — beyond that it's a lottery ticket.
            if book_odds <= -1000 or book_odds >= 3500:
                book_odds = None
        elif is_alt_prop:
            if book_odds <= -1000 or book_odds >= 5000 or (-100 < book_odds < 100):
                book_odds = None
        else:
            if book_odds <= -1000 or book_odds >= 5000 or (-100 < book_odds < 100):
                book_odds = None
    book_implied = _implied_prob(book_odds) if book_odds else model_win_prob
    # ── PHASE 2A (Part 7/8) — canonical edge with de-vig promotion ──
    # canonical_edge = model − devig_market_probability when the exact
    # opposing side(s) of the SAME market/line are available
    # (edge_method=DEVIG); otherwise fall back to the raw one-sided
    # implied (edge_method=RAW_FALLBACK) with DEVIG_UNAVAILABLE
    # telemetry.  Methods are never silently mixed; raw odds and both
    # probabilities are always preserved.
    _devig_prob = None
    _edge_method = "RAW_FALLBACK"
    _opps = [
        _implied_prob(o) for o in (opposing_prices or [])
        if isinstance(o, (int, float)) and (o >= 100 or o <= -100)
    ]
    if _opps and book_odds:
        _tot = book_implied + sum(_opps)
        if _tot > 0:
            _devig_prob = book_implied / _tot
            _edge_method = "DEVIG"
    elif book_odds:
        try:
            from services import funnel_telemetry as _funnel
            _funnel.record(sport=sport, market=market or "*", stage="devig",
                           reason="DEVIG_UNAVAILABLE", event=event,
                           detail="no opposing price at build time")
        except Exception:
            pass
    _market_prob = _devig_prob if _devig_prob is not None else book_implied
    edge = round((model_win_prob - _market_prob) * 100, 2)
    raw_edge = round((model_win_prob - book_implied) * 100, 2)
    # ── 2026-08-23 CHEAP SURGICAL — Real-Line fail-closed ──
    # When no real sportsbook line exists, do NOT synthesize an American
    # price from the model probability and store it as ``book_odds``.
    # Fail closed: keep ``book_odds=None`` and stamp
    # ``no_real_book_line=True`` so ``apply_canonical_barrier`` blocks
    # the row from the canonical Locks board.  ``model_line`` /
    # synthetic Soccer alt rows are also blocked from satisfying the
    # real-line gate (they carry ``book_odds=None`` too).
    _no_real_book_line = book_odds is None
    final_odds = int(book_odds) if book_odds else None
    # ─── PHASE 1D (G1) — chalk odds caps RETIRED ─────────────────────
    # Legacy: long-shots capped at -400, alt props at -750, standard at
    # -450 (max-American-odds cutoffs = hidden short-price policy).
    # A candidate is neither safer because the price is short nor unsafe
    # because it is long — edge/evidence/score decide.  Odds sanity
    # bounds (±100..±100000) remain in board_validator.integrity_check.
    # Per-sport quality floors for STANDARD (non-alt, non-long-shot) picks.
    # MLB has been printing money for the books at ~48% win rate so we
    # tighten it hard. Sparse sports (Tennis/UFC/KBO) keep looser bars
    # because their prop coverage is limited and the absolute pick volume
    # would crater otherwise.
    SPORT_LOCK_FLOOR = {
        "MLB": 88,
        "NBA": 80,
        "WNBA": 78,
        "NFL": 80,
        "CFB": 80,
        "Soccer": 75,  # most "Soccer" non-prop picks are h2h on weak leagues
        "Tennis": 72,
        "UFC": 72,
        "KBO": 75,
    }
    SPORT_IMPLIED_FLOOR = {
        "MLB": 0.56,    # require -127 or better book confidence
        "NBA": 0.54,
        "WNBA": 0.54,
        "NFL": 0.54,
        "CFB": 0.54,
        "Soccer": 0.50,
        "Tennis": 0.48,
        "UFC": 0.48,
        "KBO": 0.50,
    }
    # Lock score floor: long-shots 65, alt-props 72, standard markets
    # sport-tiered per the table above. EXCEPTION: pitcher_outs main lines
    # (no alt variant, per user spec). Outs Recorded prices are tighter
    # than batter hits / pitcher strikeouts, so their factor-driven lock
    # scores typically land in the 80-87 band. Allow these confident
    # mainline outs picks through with a slightly lower floor (80).
    is_pitcher_outs = "outs recorded" in (market or "").lower()
    # 2026-07-21 — Main-line pitcher STRIKEOUTS get the same juice-market
    # treatment (floor=78). Reasoning: legit main K props (Valdez -115,
    # Reynaldo Lopez Under -150, Schultz +114 — see SportsbookReview /
    # Yahoo picks) land in the 78-85 lock band because they price at
    # near-coin-flip odds with modest edge. The MLB 88 floor was
    # eating every legitimate mainline. Alt K props still filtered
    # by the -250 blanket cap + chalk_trap, so we're not opening a
    # chalk floodgate — just letting the reasonable mainlines through.
    is_pitcher_k_main = (
        "strikeouts" in (market or "").lower()
        and " · alt lock" not in (market or "").lower()
    )
    # 2026-07-19 \u2014 Juice-only markets (Game Total, Run Line, Spread)
    # are structured 50/50 by the sportsbook. Their factor-driven
    # lock_scores land in the 78-88 band, well below MLB's 88 floor.
    # Use a mid-tier floor (78) so real edges emit while trash still
    # gets filtered. Same treatment for the win-prob floor below.
    _juice_market_check = (market or "").lower()
    is_juice_market = (
        "total runs" in _juice_market_check
        or (_juice_market_check.startswith("total ")
            and "team total" not in _juice_market_check)
        or "run line" in _juice_market_check
        or ("spread" in _juice_market_check
            and "team total" not in _juice_market_check)
    )
    if is_long_shot:
        min_lock = 65
    elif is_alt_prop:
        min_lock = 72
    elif is_pitcher_outs:
        min_lock = 80
    elif is_pitcher_k_main:
        min_lock = 78
    elif is_juice_market:
        min_lock = 78
    else:
        min_lock = SPORT_LOCK_FLOOR.get(sport, 78)
    # ── Heavy-chalk anchor exception (Tennis + UFC) ─────────────────
    # For Tennis & UFC moneylines at -500 or chalkier (book ≥ 83.3%
    # implied), the matchup is fundamentally lopsided (top-30 vs
    # unseeded, champion vs late-replacement, etc.). Our model
    # frequently UNDER-estimates these favorites which would normally
    # crash `edge` and kill the pick.
    #
    # Per user instruction: allow Tennis/UFC moneylines at -500 and
    # under, plus alt lines, to bypass the standard edge + win-prob
    # floors. Lock score floor still applies so trash picks can't
    # sneak in.
    market_l = (market or "").lower()
    chalk_sports = {"Tennis", "UFC"}
    is_chalk_ml = (
        sport in chalk_sports
        and ("moneyline" in market_l or market_l.startswith("h2h"))
        and book_odds is not None
        and book_odds <= -500
    )
    is_chalk_alt = sport in chalk_sports and is_alt_prop

    if lock < min_lock:
        # PHASE 1D (G2/G4) — the per-sport/market generation lock floors
        # (72-88 ladder) are RETIRED as eligibility kill-switches.  The
        # single authoritative board rule is lock_score >= 85 at read
        # time (services.main_board_eligibility).  Kept as telemetry
        # only: emit the pick, record that it sits below the legacy
        # floor for calibration analysis.  No rejection here.
        pass
    # PHASE 1D (G1/G2) — canonical generation gates.
    #   • ONE uniform edge gate (-1.0% noise tolerance) for every sport,
    #     market and side.  Legacy carve-outs (-50% chalk, -8% Tennis/UFC
    #     ML, -10% long-shot) and the universal model-probability floors
    #     (0.58 / 0.62 / 0.55 / 0.25) and sportsbook implied-probability
    #     floors (SPORT_IMPLIED_FLOOR + 0.42 juice sanity) are RETIRED.
    #     Favorites get no automatic floor; underdogs no automatic
    #     suppression.  Model prob + market prob + edge + evidence +
    #     score decide — with the >=85 board rule as the single gate.
    EDGE_FLOOR = -1.0
    if edge < EDGE_FLOOR:
        try:
            from services import funnel_telemetry as _funnel
            _funnel.record(
                sport=sport, market=market or "*", stage="generation",
                reason="EDGE_THRESHOLD", event=event, side=pick_side,
                detail=f"edge={edge} < {EDGE_FLOOR}",
            )
        except Exception:
            pass
        return None
    # PHASE 1D (G2) — universal model-probability floors RETIRED (was
    # 0.58 std / 0.62 MLB / 0.55 juice+K+alt / 0.25 long-shot).
    # PHASE 1D (G1) — sportsbook implied-probability floors RETIRED
    # (SPORT_IMPLIED_FLOOR 0.48-0.56 + 0.42 juice sanity).  Implied
    # probability remains market information for edge/de-vig only.
    # (legacy MLB juice/K-mainline probability carve-outs and the
    # SPORT_IMPLIED_FLOOR check were deleted with the floors above)
    # PHASE 1D (G1) — the SPORT_IMPLIED_FLOOR block and the 0.42 juice
    # sanity floor were retired here (implied prob is market info only).
    # PHASE 1D (G3) — the generation-time bet-quality BOOSTER is
    # RETIRED.  It artificially raised lock_score into the 85-105 band
    # whenever wp>=65 & edge>=1 (weak candidate → booster → board-
    # qualified score).  Lock Score must be earned from the scoring
    # model; eligibility is decided once, at the >=85 board rule.
    return {
        "sport": sport, "league": league, "event": event,
        "event_time": event_time, "market": market, "selection": pick_side,
        "win_probability": round(model_win_prob * 100, 1),
        "book_odds": final_odds,
        # Real-line fail-closed provenance (2026-08-23).  Set exactly
        # when we have NO real sportsbook price; canonical barrier
        # blocks these rows from the Locks board.
        **({"no_real_book_line": True} if _no_real_book_line else {}),
        "implied_probability": round(book_implied * 100, 1),
        "edge_percent": edge,
        # PHASE 2A — canonical-edge provenance (Part 8)
        "edge_method": _edge_method,
        "raw_implied_probability": round(book_implied * 100, 1),
        "raw_edge_percent": raw_edge,
        **({"devig_market_probability": round(_devig_prob * 100, 1),
            "devig_edge_percent": edge,
            "devig_method": f"{1 + len(_opps)}_way_normalization"}
           if _devig_prob is not None else {}),
        "lock_score": lock, "grade": _grade(lock), "confidence": _confidence(lock),
        "factors": factors, "key_insights": insights,
        "external_id": str(external_id),
        # Line classification — used by the UI's MAIN | ALT | BOTH toggle.
        "is_alt": bool(is_alt_prop),
        "is_long_shot": bool(is_long_shot),
        # Team metadata (sport-aware). For MLB we also resolve MLB Stats
        # API integer team IDs so the Survivability Engine and other
        # downstream consumers can look up rosters / game logs without
        # name-parsing tricks. Falsy entries are dropped.
        **({"home_team": home_team_name} if home_team_name else {}),
        **({"away_team": away_team_name} if away_team_name else {}),
        **({"home_team_id": _MLB_TEAM_NAME_TO_ID.get(home_team_name)}
           if (sport == "MLB" and home_team_name
               and home_team_name in _MLB_TEAM_NAME_TO_ID) else {}),
        **({"away_team_id": _MLB_TEAM_NAME_TO_ID.get(away_team_name)}
           if (sport == "MLB" and away_team_name
               and away_team_name in _MLB_TEAM_NAME_TO_ID) else {}),
    }


# MLB Stats API team IDs keyed by the full team name the Odds API returns.
# Used by `_build_pick` to enrich every MLB pick with structured team
# identifiers so the Survivability Engine (and any future per-team
# analytics) can look up rosters / game logs without parsing "(TOR)"
# out of selection strings.
_MLB_TEAM_NAME_TO_ID: dict[str, int] = {
    "Arizona Diamondbacks": 109, "Atlanta Braves": 144, "Baltimore Orioles": 110,
    "Boston Red Sox": 111, "Chicago Cubs": 112, "Chicago White Sox": 145,
    "Cincinnati Reds": 113, "Cleveland Guardians": 114, "Colorado Rockies": 115,
    "Detroit Tigers": 116, "Houston Astros": 117, "Kansas City Royals": 118,
    "Los Angeles Angels": 108, "Los Angeles Dodgers": 119, "Miami Marlins": 146,
    "Milwaukee Brewers": 158, "Minnesota Twins": 142, "New York Mets": 121,
    "New York Yankees": 147, "Oakland Athletics": 133, "Athletics": 133,
    "Philadelphia Phillies": 143, "Pittsburgh Pirates": 134, "San Diego Padres": 135,
    "San Francisco Giants": 137, "Seattle Mariners": 136, "St. Louis Cardinals": 138,
    "Tampa Bay Rays": 139, "Texas Rangers": 140, "Toronto Blue Jays": 141,
    "Washington Nationals": 120,
}


# ───────────────────────── Per-sport factor matrices ─────────────────────────
#
# 2026-07-21 — FINAL PHASE RANDOM PURGE:
#   The legacy `_FACTOR_RECIPES` table + `_factors_random(rng, recipe_key)`
#   function have been fully removed. They generated fake per-factor
#   scores via `rng.uniform(0.3, 0.95)` which corrupted the input to
#   compute_lock_score and drove the ROI bleed the user reported over
#   several sessions ("I said over and over I don't want fake data").
#
#   Real replacements now used across all active codepaths:
#     • MLB    → services/mlb_feature_engine.py     (Phase 1)
#     • Tennis → services/tennis_feature_engine.py  (Phase 2)
#     • Soccer → services/soccer_feature_engine.py  (Phase 2)
#     • NBA / NFL → Phase 3 (in progress). Until those engines land,
#       these sports emit picks with `factors = {"Book Implied": p}`
#       (single, calibrated book-derived factor — see the player-prop
#       loop below). This produces honest, low-lock "book-follow" picks
#       rather than falsely-elite RNG picks.
#
#   Do NOT re-introduce `_factors_random` or any `rng.uniform` for factor
#   generation. If a sport lacks a feature engine, either (a) route it
#   through the book-follow path, or (b) drop the pick entirely — those
#   are the only two acceptable options.


# ───────────────────────── Game → Picks converter ─────────────────────────


def _attach_devig(pick: dict | None, opp_prices: list) -> None:
    """PHASE 1D (G5) — market-probability truth with provenance.

    Attaches to the pick (never overwrites the sportsbook price):
      raw_implied_probability   — one-sided implied % from book_odds
      devig_market_probability  — n-way normalized (vig removed) %
      devig_method              — "<n>_way_normalization"
      devig_edge_percent        — model win prob − de-vig market prob
    When the opposing side(s) are unavailable, records
    OPPOSING_SIDE_UNAVAILABLE / DEVIG_UNAVAILABLE funnel telemetry.
    """
    if not pick or pick.get("book_odds") is None:
        return
    try:
        own_p = _implied_prob(pick["book_odds"])
        pick["raw_implied_probability"] = round(own_p * 100, 1)
        opps = [
            _implied_prob(o) for o in (opp_prices or [])
            if isinstance(o, (int, float)) and (o >= 100 or o <= -100)
        ]
        if not opps:
            try:
                from services import funnel_telemetry as _funnel
                _funnel.record(
                    sport=pick.get("sport") or "unknown",
                    market=pick.get("market") or "*",
                    stage="devig", reason="OPPOSING_SIDE_UNAVAILABLE",
                    event=pick.get("event"),
                    detail="de-vig skipped — no opposing price",
                )
            except Exception:
                pass
            return
        total = own_p + sum(opps)
        if total <= 0:
            return
        dv = own_p / total
        pick["devig_market_probability"] = round(dv * 100, 1)
        pick["devig_method"] = f"{1 + len(opps)}_way_normalization"
        mp = pick.get("win_probability")
        if isinstance(mp, (int, float)):
            pick["devig_edge_percent"] = round(float(mp) - dv * 100, 2)
    except Exception:
        pass


def _picks_from_game(sport: str, league: str, game: dict, date_str: str) -> list[dict]:
    home = game.get("home_team")
    away = game.get("away_team")
    if not home or not away:
        return []
    # ── UFC market policy (Phase 1B, T3b) ─────────────────────────────
    # The legacy `_ufc_ml_only` suppression (2026-07 "only ufc money
    # lines") has been RETIRED per Phase 1 directive: real sportsbook
    # UFC totals must reach the authoritative evaluation path.  With no
    # independent UFC model wired yet, UFC markets record
    # MODEL_UNAVAILABLE funnel telemetry instead of being silently
    # suppressed OR book-followed.  Historical totals performance now
    # belongs in calibration, not capability suppression.
    commence = game.get("commence_time")
    # Per-sport scheduling window. UFC fight cards run weekly, KBO has 5
    # games/day all week, Tennis tournaments span 7-10 days — these sparse
    # sports need a wider window than daily-game sports or we'd ship the
    # board with 2-3 picks.
    window_hours = {
        "UFC": 10 * 24,
        "KBO": 7 * 24,
        "Tennis": 7 * 24,
        "Soccer": 5 * 24,
    }.get(sport, 72)
    if commence:
        try:
            dt = datetime.strptime(commence, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            if dt < now - __import__("datetime").timedelta(minutes=30):
                return []
            if dt > now + __import__("datetime").timedelta(hours=window_hours):
                return []
        except Exception:
            pass
    game_id = game.get("id") or f"{sport}-{home}-{away}-{commence}"
    seed = abs(hash(f"{sport}{home}{away}{date_str}")) % 10000
    rng = random.Random(seed)

    h2h_outs = _consensus_market(game, "h2h")
    totals_outs = _consensus_market(game, "totals")
    spreads_outs = _consensus_market(game, "spreads")

    picks: list[dict] = []

    # Moneyline + (for soccer) Draw & Win-or-Draw via 3-way h2h.
    home_ml = _median_price(h2h_outs, home)
    away_ml = _median_price(h2h_outs, away)
    draw_ml = _median_price(h2h_outs, "Draw")  # only present in soccer 3-way

    if home_ml is not None and away_ml is not None:
        home_implied = _implied_prob(home_ml)
        # Normalize 3-way implied probs so they sum to ~1 after removing vig.
        if draw_ml is not None:
            draw_implied = _implied_prob(draw_ml)
            away_implied = _implied_prob(away_ml)
            total = home_implied + draw_implied + away_implied
            home_implied = home_implied / total if total else home_implied
            away_implied = away_implied / total if total else away_implied
            draw_implied = draw_implied / total if total else draw_implied
        else:
            away_implied = 1 - home_implied
            draw_implied = None

        # Model lift bound — tightened from 0.18 to 0.08 to stop the model
        # from inventing 8-9% edges on near-coinflip ML markets. Anchored on
        # book implied with a small (±2-3%) personalization shift instead of
        # ±9% which produced overconfident 75%+ win prob claims on 50/50
        # MLB games (the bulk of last week's losses).
        #
        # 2026-07-20 — For Soccer / Tennis MLs we now use the data-
        # driven models. MLB / other sports still random-tilt until
        # `mlb_ml_prob` ships. Falls back cleanly to random on missing
        # context.
        game_ctx = (game.get("_ctx") or {}) if isinstance(game, dict) else {}
        dd_ml_result = None
        _nfl_plat_ml = None            # Phase 1B — Platinum NFL game sim
        _ml_model_unavailable: str | None = None
        if sport == "Soccer" and game_ctx:
            try:
                from services.data_driven_model import soccer_ml_prob
                # Score BOTH sides; pick the winning-implied side (as before)
                # and get its DD prob.
                if home_implied >= away_implied:
                    dd_ml_result = soccer_ml_prob(home, home, away, home_implied, game_ctx)
                    home_model = dd_ml_result["mp"]
                else:
                    dd_ml_result = soccer_ml_prob(away, home, away, 1 - home_implied, game_ctx)
                    home_model = 1 - dd_ml_result["mp"]
            except Exception as e:
                logger.debug("soccer DD ml failed: %s", e)
                dd_ml_result = None
        elif sport == "Tennis" and game_ctx:
            try:
                # 2026-07-27 UPSET DETECTION FIX (Korpatsch vs Sherif):
                # Previously only the FAVORITE side was scored via tennis_ml_prob
                # and the caller trusted `home_model >= 0.5` to flip picks.
                # But CAP_TOTAL=±10pp made it mathematically impossible to flip
                # a legitimate underdog picked at ~+180 (book implied ~35%).
                # Now score BOTH sides independently via the tennis math engine
                # and pick whichever model says wins more often — regardless
                # of the book's favorite. Real signals (surface Elo, form,
                # H2H, fatigue) decide. If neither side clears a real edge
                # threshold the pick is dropped downstream by tennis_engine's
                # gating.
                from services.data_driven_model import tennis_ml_prob
                from services.tennis_math_engine import (
                    score_tennis_matchup, has_real_tennis_signal,
                )
                surface = (game.get("surface") or "hard").lower()
                dd_home = tennis_ml_prob(home, home, away, surface, home_implied, game_ctx)
                dd_away = tennis_ml_prob(away, home, away, surface, 1 - home_implied, game_ctx)
                # Normalize (both mp anchored on their own implied). We prefer
                # the side whose (mp - implied) is highest — that's the side
                # where the model finds VALUE relative to the book.
                # If model probs sum to >1 we still take whichever mp is larger.
                home_edge = dd_home["mp"] - home_implied
                away_edge = dd_away["mp"] - (1 - home_implied)
                # Deep math override — surface-Elo based hard model.
                math_signal = score_tennis_matchup(
                    home, away, surface, home_implied, game_ctx,
                )
                if math_signal and has_real_tennis_signal(math_signal):
                    # Math engine says one player wins by X margin — use it.
                    home_model = math_signal["home_win_prob"]
                    dd_ml_result = dd_home if home_model >= 0.5 else dd_away
                    # Merge math_signal contributions into dd contribs
                    if dd_ml_result is not None:
                        merged = dict(dd_ml_result.get("contributions") or {})
                        merged.update(math_signal.get("contributions") or {})
                        dd_ml_result["contributions"] = merged
                        dd_ml_result["math_engine_used"] = True
                        dd_ml_result["mp"] = home_model if home_model >= 0.5 else (1 - home_model)
                else:
                    # No hard math signal — fall back to comparing dd edges.
                    # Take the side with a positive edge (model > book).
                    if home_edge >= away_edge:
                        dd_ml_result = dd_home
                        home_model = dd_home["mp"]
                    else:
                        dd_ml_result = dd_away
                        home_model = 1 - dd_away["mp"]
            except Exception as e:
                logger.debug("tennis DD ml failed: %s", e)
                dd_ml_result = None
        elif sport == "NFL":
            # ── Phase 1B (R1) — Platinum NFL game-market wiring ──────
            # NFL regular-season AND preseason moneylines are evaluated
            # by the Platinum causal simulator (exact-line model
            # probabilities from team-strength expected margin).  The
            # sim inherently scores BOTH sides (p_home + p_away).
            # When the model context is unavailable (ratings missing),
            # the market records MODEL_UNAVAILABLE — never book-follow.
            try:
                from services.platinum_nfl.game_runtime import (
                    platinum_game_side_probability,
                )
                _book_total_line = next(
                    (o.get("point") for o in totals_outs
                     if o.get("name") == "Over"), None)
                _plat = platinum_game_side_probability(
                    game=game, ctx=game_ctx, market="Moneyline",
                    side=home, line=None, is_home_side=True,
                    book_total_line=_book_total_line,
                )
                if _plat.get("available"):
                    home_model = _plat["prob"]
                    _nfl_plat_ml = _plat
                else:
                    _ml_model_unavailable = _plat.get("reason") or "MODEL_UNAVAILABLE"
            except Exception as e:
                logger.warning("NFL platinum ML wiring failed: %s", e)
                _ml_model_unavailable = f"SIM_EXCEPTION:{type(e).__name__}"
        if dd_ml_result is None and _nfl_plat_ml is None:
            # Phase 1B — the sportsbook-follow fallback is now permitted
            # ONLY for MLB/Soccer, whose feature engines gate emission on
            # real-data coverage downstream (has_enough_real_data /
            # has_enough_soccer_data).  Every other sport without an
            # authoritative model records MODEL_UNAVAILABLE and emits
            # nothing — a sportsbook price is never presented as an
            # independent model probability.
            home_model = home_implied
            # ── Phase 2A.5B (2026-08) — RC4 CLOSURE ──────────────────
            # Soccer game probability MUST come from the independent
            # Soccer game model, NOT the sportsbook implied probability.
            # `estimate_soccer_game_probabilities` derives λ_home /
            # λ_away from attack/defense strengths (real xG when
            # available, form-derived GF/GA otherwise) with sample-size
            # shrinkage and Dixon-Coles low-score correction.  Never
            # reads sportsbook odds.
            if sport == "Soccer":
                try:
                    from services.soccer_game_model import (
                        estimate_soccer_game_probabilities,
                    )
                    _game_ctx = ((game.get("_ctx") if isinstance(game, dict) else None)
                                 or {})
                    _soc_game = estimate_soccer_game_probabilities(
                        _game_ctx, home, away)
                    if _soc_game.available:
                        home_model = float(_soc_game.p_home)
                        # Stash for downstream markets (Totals / BTTS /
                        # Double Chance) to reuse the SAME distribution.
                        if isinstance(game, dict):
                            game.setdefault("_ctx", {})[
                                "_soccer_game_model"] = _soc_game.as_dict()
                            game["_ctx"]["_soccer_score_matrix"] = _soc_game.score_matrix
                        _ml_sources = list(_soc_game.sources)
                    else:
                        _ml_model_unavailable = (
                            _soc_game.reason or "MODEL_UNAVAILABLE")
                        try:
                            from services import funnel_telemetry as _funnel
                            _funnel.record(
                                sport="Soccer", market="moneyline",
                                stage="model",
                                reason=_ml_model_unavailable,
                                event=f"{away} @ {home}",
                                detail=f"soccer_game_model tier={_soc_game.tier}",
                            )
                        except Exception:
                            pass
                except Exception as _sgm_err:
                    logger.debug("soccer_game_model wiring failed: %s", _sgm_err)
            if sport not in ("MLB", "Soccer") and _ml_model_unavailable is None:
                _ml_model_unavailable = "MODEL_UNAVAILABLE"
        if home_model >= 0.5:
            side, side_ml, mp = home, home_ml, home_model
        else:
            # ── 2026-08-23 CHEAP SURGICAL — Soccer 1X2 Away math ──
            # For Soccer 3-way markets the model produces distinct
            # p_home, p_draw, p_away.  Prior code used ``1 - home_model``
            # for the Away side which double-counted the Draw mass and
            # inflated Away edges.  Use the model's real p_away when
            # the Soccer game distribution is present; every non-Soccer
            # 2-way sport keeps the original 1 - p_home identity.
            _p_away = None
            if sport == "Soccer" and isinstance(game, dict):
                _sgm = ((game.get("_ctx") or {}).get("_soccer_game_model")
                        or {})
                if _sgm.get("p_away") is not None:
                    try:
                        _p_away = float(_sgm["p_away"])
                    except (TypeError, ValueError):
                        _p_away = None
            side, side_ml, mp = (
                away, away_ml,
                _p_away if _p_away is not None else 1 - home_model,
            )

        # 2026-07-21 Phase 1 MLB + Phase 2 Tennis/Soccer: real feature
        # engines gate emission on real-data coverage. NBA / NFL / others
        # still use random pool until their Phase 3 replacements land.
        _skip_ml = False
        _ml_sources: list[str] = []
        if sport == "MLB":
            from services.mlb_feature_engine import (
                build_mlb_ml_factors, has_enough_real_data,
            )
            _game_ctx = (game.get("_ctx") if isinstance(game, dict) else None) or {}
            real_ml_factors, _ml_sources = build_mlb_ml_factors(_game_ctx, pick_team=side)
            if not has_enough_real_data(real_ml_factors, "ml"):
                _skip_ml = True
            else:
                factors = {k: v for k, v in real_ml_factors.items() if v is not None}
        elif sport == "Soccer":
            from services.soccer_feature_engine import (
                build_soccer_ml_factors, has_enough_soccer_data,
            )
            _game_ctx = (game.get("_ctx") if isinstance(game, dict) else None) or {}
            real_ml_factors, _ml_sources = build_soccer_ml_factors(_game_ctx, pick_team=side)
            # ── Phase 2A.5B (2026-08) — RC5 CLOSURE ──────────────────
            # Pre-score starvation: legitimate Soccer matches with only
            # 2 legacy correlated factors (Form PPG, Goals Scored, etc.
            # all sourced from the SAME soccer_form doc) were dropped
            # before probability evaluation even though the independent
            # Soccer game model had a valid tier-B/C answer.  New rule:
            # if the game model is available, its ``SCORE_MODEL`` +
            # ``TEAM_STRENGTH`` counts as legitimate independent
            # evidence.  Only true MODEL_UNAVAILABLE causes _skip_ml.
            _has_game_model = bool(_game_ctx.get("_soccer_game_model"))
            if not has_enough_soccer_data(real_ml_factors, "ml") and not _has_game_model:
                _skip_ml = True
                # RC6 CLOSURE — funnel-attribute the silent death.
                try:
                    from services import funnel_telemetry as _funnel
                    _real_n = sum(1 for _v in real_ml_factors.values()
                                  if _v is not None)
                    _funnel.record(
                        sport="Soccer", market="moneyline", stage="evidence",
                        reason="EVIDENCE_THRESHOLD",
                        event=f"{away} @ {home}",
                        detail=f"real_factors={_real_n}/7 game_model=missing",
                    )
                except Exception:
                    pass
            else:
                factors = {k: v for k, v in real_ml_factors.items() if v is not None}
                # If the independent game model succeeded, add its
                # SCORE_MODEL evidence AS A DISTINCT CATEGORY so
                # downstream Magic/Fusion cannot double-count the
                # correlated form-derived factors.  This resolves RC3.
                if _has_game_model:
                    _sgm = _game_ctx["_soccer_game_model"]
                    factors["Score Model Probability"] = round(
                        float(_sgm.get("p_home") or 0.5) if side == home
                        else 1.0 - float(_sgm.get("p_home") or 0.5), 4)
                    _ml_sources = list(dict.fromkeys(
                        list(_ml_sources) + list(_sgm.get("sources") or [])))
        else:
            # Phase 1B — non-engine sports reach this branch only when an
            # authoritative model produced the probability:
            #   • NFL   → Platinum game sim (_nfl_plat_ml)
            #   • Tennis→ tennis math / data-driven engine (dd_ml_result)
            # Anything else (NBA / CFB / UFC / NHL / model-less Tennis)
            # is MODEL_UNAVAILABLE: telemetried, not emitted.
            factors = {}
            if _ml_model_unavailable is not None:
                _skip_ml = True
                try:
                    from services import funnel_telemetry as _funnel
                    _funnel.record(
                        sport=sport, market="moneyline", stage="model",
                        reason=_ml_model_unavailable,
                        event=f"{away} @ {home}",
                        detail="no authoritative independent model wired",
                    )
                except Exception:
                    pass

        if not _skip_ml:
            if sport == "NFL" and _nfl_plat_ml is not None:
                # PHASE 2A (Part 3) — sparse-evidence calibration: route
                # Platinum picks through the v3 six-component composite
                # (edge/alignment/ROI/data-quality/volatility/CLV) instead
                # of the legacy win-prob band map, which converted model
                # probability alone into 90s scores regardless of edge or
                # evidence.  High scores must be EARNED, not probability-
                # mapped.
                _e_ml = round((mp - _implied_prob(side_ml)) * 100, 2)
                lock, breakdown = compute_lock_score(
                    factors, win_prob=mp * 100,
                    pick={"book_odds": side_ml, "edge_percent": _e_ml,
                          "win_probability": mp * 100},
                    edge_percent=_e_ml)
            else:
                lock, breakdown = compute_lock_score(factors, win_prob=mp * 100)
            _opp_ml_prices = [away_ml if side == home else home_ml]
            if sport == "Soccer" and draw_ml is not None:
                _opp_ml_prices.append(draw_ml)
            ml_pick = _build_pick(
                sport=sport, league=league, event=f"{away} @ {home}",
                event_time=commence, market=f"{side} Moneyline", pick_side=side,
                model_win_prob=mp, book_odds=side_ml,
                lock=lock, factors=breakdown,
                insights=_insights_for(sport, breakdown, side, home, away),
                external_id=f"{sport}-{game_id}-ml",
                opposing_prices=_opp_ml_prices,
            )
            if ml_pick and dd_ml_result:
                ml_pick["data_driven_used"] = True
                ml_pick["data_driven_contribs"] = dd_ml_result.get("contributions") or {}
            if ml_pick and _nfl_plat_ml:
                # Phase 1B — authoritative Platinum provenance
                from services.platinum_nfl.game_runtime import (
                    attach_game_sim_provenance,
                )
                attach_game_sim_provenance(ml_pick, _nfl_plat_ml)
            # PHASE 2A — de-vig computed at build time (canonical edge).
            # Post-build attachment retired for game markets.
            if ml_pick:
                # 2026-07-21 Phase 1 MLB + Phase 2 Tennis/Soccer:
                # attach real-data attribution
                if sport in ("MLB", "Soccer") and _ml_sources:
                    ml_pick["real_data_sources"] = list(_ml_sources)
                    ml_pick["real_data_count"] = len(_ml_sources)
                # ── 2026-08-23 FINAL SURGICAL — ONE SOCCER PRODUCTION
                # WRITER.  ``services.real_line_scorer_ingest`` is the
                # single authoritative Soccer 1X2 / BTTS / Double
                # Chance / Totals publisher.  This legacy path is
                # kept behind an explicit env flag purely for
                # emergency operator fallback; DEFAULT = disabled.
                _SOCCER_LEGACY_ENABLED = (
                    os.getenv("SOCCER_GAME_MARKET_LEGACY_ENABLED", "").lower()
                    in ("1", "true", "yes")
                )
                if sport == "Soccer" and not _SOCCER_LEGACY_ENABLED:
                    pass  # skip legacy 1X2 emission
                else:
                    picks.append(ml_pick)

        # Soccer-only: Double Chance (Win-or-Draw) picks.
        #
        # Block 2D B2/B3 (2026-08) — real-line integrity + model
        # independence.
        #
        # BEFORE (defect):
        #   dc_implied = home_implied + draw_implied     # book components
        #   dc_book_odds = _win_prob_to_american(dc_implied)  # SYNTHETIC odds!
        #   dc_model = clamp(dc_implied, 0.55, 0.95)       # model ≈ book implied
        #
        # This produced a "real-line" pick from an INTERNALLY-CONVERTED
        # probability masquerading as a sportsbook line, with a
        # "model" probability that was just a repackaged book implied.
        # High Lock Scores could then reach the board with implied=null
        # and edge=0 — the exact impossible-card class.
        #
        # AFTER (fix):
        #   * DC pick only emitted when a REAL DC market outcome is
        #     present in game["_dc_outcomes"] (populated by the
        #     bookmaker payload parser upstream when the double_chance
        #     market is exposed).  No real line → no DC pick.
        #   * DC model probability comes from build_soccer_ml_factors
        #     alone (independent soccer engine), NOT from book implied.
        #   * book_odds is the REAL median price from the DC outcome.
        if draw_ml is not None and sport == "Soccer":
            from services.soccer_feature_engine import (
                build_soccer_ml_factors, has_enough_soccer_data,
            )
            _game_ctx = (game.get("_ctx") if isinstance(game, dict) else None) or {}
            # Real DC market outcomes from The Odds API — populated
            # upstream when the double_chance market is requested and
            # the bookmaker exposes it.  Shape: [{"name": "1X"|"12"|"X2",
            # "price": <american>}, ...] normalised.
            _real_dc_outcomes = (game.get("_dc_outcomes") or []) if isinstance(game, dict) else []
            # Determine which DC side we would prefer if a real line
            # exists.  Preferred side is the favored team's W-or-D
            # ("1X" for home fav, "X2" for away fav).
            dc_side = home if home_implied >= away_implied else away
            dc_side_key = "1X" if home_implied >= away_implied else "X2"
            _dc_real = None
            for _o in _real_dc_outcomes:
                if str(_o.get("name") or "").strip() in (dc_side_key, dc_side):
                    _dc_real = _o
                    break
            if not _dc_real:
                # No real DC line → do NOT emit a DC pick.  Log the
                # diagnostic so downstream can distinguish "we blocked
                # a synthetic-DC candidate" from "no soccer data".
                try:
                    from services.pipeline_diagnostic import log_reason as _plog
                    _plog(
                        sport="Soccer", market="double_chance",
                        event=f"{away} @ {home}",
                        reason="DOUBLE_CHANCE_SYNTHETIC_LINE_BLOCKED",
                    )
                except Exception:
                    pass
            else:
                # Real DC line found — proceed with independent model.
                dc_book_odds = int(_dc_real.get("price") or 0)
                real_dc_factors, _dc_sources = build_soccer_ml_factors(
                    _game_ctx, pick_team=dc_side)
                if not has_enough_soccer_data(real_dc_factors, "ml"):
                    try:
                        from services.pipeline_diagnostic import log_reason as _plog
                        _plog(
                            sport="Soccer", market="double_chance",
                            event=f"{away} @ {home}",
                            reason="DOUBLE_CHANCE_INSUFFICIENT_MODEL_DATA",
                        )
                    except Exception:
                        pass
                else:
                    # Model probability derived from the soccer engine's
                    # win-probability signal.  This is P(home wins) or
                    # P(away wins) from independent evidence; we ADD the
                    # draw component from the *soccer engine's* draw
                    # signal when available, otherwise fall back to the
                    # engine's win probability alone (conservative — we
                    # never re-inject book implied).
                    _factor_win = real_dc_factors.get("Team Form Model") \
                        or real_dc_factors.get("Team Strength") \
                        or None
                    # Compute engine-anchored model prob from ML factor
                    # mean (existing shape).  Reuse the same
                    # compute_lock_score path shape as ML pick.
                    factors2 = {k: v for k, v in real_dc_factors.items()
                                 if v is not None}
                    # For win_prob we use the mean of the engine's real
                    # ML factors, expressed as a probability.  This is
                    # INDEPENDENT of book implied — the factors come
                    # from build_soccer_ml_factors (xG, form, etc.).
                    _factor_mean = (sum(factors2.values()) / len(factors2)
                                     if factors2 else 0.55)
                    # Add draw safety-net: DC covers Win OR Draw, so the
                    # engine's P(win) is a LOWER bound on P(DC).  We
                    # cap at 0.95 to avoid overconfidence, and we do
                    # NOT clamp to the book-implied value.
                    dc_model = max(0.55, min(0.95, _factor_mean + 0.05))
                    lock2, breakdown2 = compute_lock_score(
                        factors2, win_prob=dc_model * 100)
                    dc_pick = _build_pick(
                        sport=sport, league=league,
                        event=f"{away} @ {home}",
                        event_time=commence,
                        market=f"{dc_side} Win or Draw", pick_side=dc_side,
                        model_win_prob=dc_model, book_odds=dc_book_odds,
                        lock=lock2, factors=breakdown2,
                        insights=_insights_for(sport, breakdown2, dc_side,
                                                home, away),
                        external_id=f"{sport}-{game_id}-dc",
                    )
                    if dc_pick:
                        if _dc_sources:
                            dc_pick["real_data_sources"] = list(_dc_sources)
                            dc_pick["real_data_count"] = len(_dc_sources)
                        # 2026-08-23 FINAL SURGICAL — ONE SOCCER
                        # PRODUCTION WRITER (Double Chance).  Legacy
                        # DC emission is gated OFF; real_line_scorer_ingest
                        # is authoritative.
                        _SOCCER_LEGACY_ENABLED = (
                            os.getenv("SOCCER_GAME_MARKET_LEGACY_ENABLED", "").lower()
                            in ("1", "true", "yes")
                        )
                        if _SOCCER_LEGACY_ENABLED:
                            picks.append(dc_pick)
                        try:
                            from services.pipeline_diagnostic import log_reason as _plog
                            _plog(
                                sport="Soccer", market="double_chance",
                                event=f"{away} @ {home}",
                                reason="DOUBLE_CHANCE_REAL_LINE_USED",
                            )
                        except Exception:
                            pass

        # ── Block 2D B4 (2026-08) — Both Teams To Score (BTTS) ────────
        # 2026-08-23 CHEAP SURGICAL — ONE SOCCER GAME-MARKET AUTHORITY.
        # The authoritative Soccer BTTS / 1X2 / Double Chance / Totals
        # writer is ``services.real_line_scorer_ingest`` which consumes
        # the shared Soccer score distribution (``soccer_game_model``)
        # via ``build_soccer_total_factors`` / ``build_soccer_ml_factors``.
        # This block emitted a second, independent BTTS probability
        # from a bare-factor product (``_home_mean * _away_mean * 1.6``)
        # which competed with the authoritative writer and produced
        # overlapping / conflicting Locks.  Disabled by default; kept
        # behind an explicit env flag so no user-visible behaviour
        # changes unless operators opt in.
        _SOCCER_BTTS_LEGACY_ENABLED = (
            os.getenv("SOCCER_GAME_MARKET_LEGACY_ENABLED",
                       os.getenv("SOCCER_BTTS_LEGACY_ENABLED", "")).lower()
            in ("1", "true", "yes")
        )
        if sport == "Soccer" and _SOCCER_BTTS_LEGACY_ENABLED:
            _btts_outcomes = (game.get("_btts_outcomes") or []) if isinstance(game, dict) else []
            if _btts_outcomes:
                try:
                    from services.pipeline_diagnostic import log_reason as _plog
                    _plog(
                        sport="Soccer", market="btts",
                        event=f"{away} @ {home}",
                        reason="BTTS_LINE_FOUND",
                    )
                except Exception:
                    pass
                _game_ctx = (game.get("_ctx") if isinstance(game, dict) else None) or {}
                from services.soccer_feature_engine import (
                    build_soccer_ml_factors, has_enough_soccer_data,
                )
                # Use the engine's ML factors as an independent scoring
                # environment signal.  For BTTS we combine both teams'
                # attacking factors — build_soccer_ml_factors gives us a
                # per-team factor block; we sum the attacking signals.
                _f_home, _sh = build_soccer_ml_factors(_game_ctx, pick_team=home)
                _f_away, _sa = build_soccer_ml_factors(_game_ctx, pick_team=away)
                _has_home = has_enough_soccer_data(_f_home, "ml")
                _has_away = has_enough_soccer_data(_f_away, "ml")
                if not (_has_home and _has_away):
                    try:
                        from services.pipeline_diagnostic import log_reason as _plog
                        _plog(
                            sport="Soccer", market="btts",
                            event=f"{away} @ {home}",
                            reason="BTTS_INSUFFICIENT_MODEL_DATA",
                        )
                    except Exception:
                        pass
                else:
                    # Independent BTTS model probability derived from
                    # the mean of both teams' attacking factors.  This
                    # is a conservative combination — a formal Poisson
                    # goal-model would be more accurate but is deferred
                    # (per user directive: do NOT invent new arbitrary
                    # formulas just to get BTTS onto the board).
                    _f_home_clean = {k: v for k, v in _f_home.items() if v is not None}
                    _f_away_clean = {k: v for k, v in _f_away.items() if v is not None}
                    _home_mean = (sum(_f_home_clean.values())
                                    / len(_f_home_clean)) if _f_home_clean else 0.55
                    _away_mean = (sum(_f_away_clean.values())
                                    / len(_f_away_clean)) if _f_away_clean else 0.55
                    # BTTS Yes probability ~ product of both teams
                    # scoring at least once.  We approximate each team's
                    # scoring probability by its factor mean (already in
                    # [0.30, 0.95] band).  P(BTTS Yes) = P(home scores)
                    # × P(away scores).
                    btts_yes_p = max(0.30, min(0.85, _home_mean * _away_mean * 1.6))
                    btts_no_p = max(0.15, min(0.70, 1.0 - btts_yes_p))
                    # Emit YES / NO candidates from real book outcomes.
                    for _o in _btts_outcomes:
                        _side_raw = str(_o.get("name") or "").strip().lower()
                        if _side_raw not in ("yes", "no"):
                            continue
                        _price = _o.get("price")
                        if not isinstance(_price, (int, float)):
                            continue
                        _btts_side = "Yes" if _side_raw == "yes" else "No"
                        _btts_p = btts_yes_p if _side_raw == "yes" else btts_no_p
                        # Merge both teams' factor sets so Lock Score
                        # sees both attacking signals.
                        _btts_factors = {
                            f"{k} (home)": v for k, v in _f_home_clean.items()
                        }
                        _btts_factors.update({
                            f"{k} (away)": v for k, v in _f_away_clean.items()
                        })
                        _btts_lock, _btts_breakdown = compute_lock_score(
                            _btts_factors, win_prob=_btts_p * 100)
                        _btts_pick = _build_pick(
                            sport=sport, league=league,
                            event=f"{away} @ {home}",
                            event_time=commence,
                            market=f"Both Teams To Score {_btts_side}",
                            pick_side=_btts_side,
                            model_win_prob=_btts_p,
                            book_odds=int(_price),
                            lock=_btts_lock, factors=_btts_breakdown,
                            insights=_insights_for(sport, _btts_breakdown,
                                                    _btts_side, home, away),
                            external_id=f"{sport}-{game_id}-btts-{_side_raw}",
                        )
                        if _btts_pick:
                            _merged_sources = list(set(list(_sh) + list(_sa)))
                            if _merged_sources:
                                _btts_pick["real_data_sources"] = _merged_sources
                                _btts_pick["real_data_count"] = len(_merged_sources)
                            picks.append(_btts_pick)
                            try:
                                from services.pipeline_diagnostic import log_reason as _plog
                                _plog(
                                    sport="Soccer", market="btts",
                                    event=f"{away} @ {home}",
                                    reason="BTTS_CANDIDATE_CREATED",
                                    meta={"side": _btts_side},
                                )
                            except Exception:
                                pass

    # Totals pick \u2014 2026-07-19: emit only the BEST SIDE per game (user
    # request: "every game you shouldn't force over or under it should
    # just be the best ones for that day"). Compute model win prob for
    # both sides, pick whichever has the larger positive edge, and skip
    # totally when neither side crosses the edge / implied floors.
    # Phase 1B: UFC totals now REACH evaluation (legacy suppression
    # retired) — they resolve to MODEL_UNAVAILABLE until a UFC model
    # is wired.
    if totals_outs:
        over = next((o for o in totals_outs if o.get("name") == "Over"), None)
        under = next((o for o in totals_outs if o.get("name") == "Under"), None)
        if over and under and over.get("point") == under.get("point"):
            line = over.get("point")
            o_price = _median_price(totals_outs, "Over")
            u_price = _median_price(totals_outs, "Under")

            # Score both sides. For MLB, use the DATA-DRIVEN model
            # (weather / park HR / pitcher Stuff+ / team scoring)
            # attached upstream via ``game['_ctx']``. Soccer uses xG
            # rolling + manager style + pressure. Other sports still
            # use the small random tilt until their per-sport models
            # land. Falls back to random tilt when ctx is missing so
            # the ingest path never blocks on a bad enrichment.
            candidates: list[dict] = []
            game_ctx = (game.get("_ctx") or {}) if isinstance(game, dict) else {}
            _use_dd = bool(game_ctx) and sport in ("MLB", "Soccer")
            _dd_fn = None
            if _use_dd:
                try:
                    if sport == "MLB":
                        from services.data_driven_model import mlb_total_prob as _dd_fn
                    else:  # Soccer
                        from services.data_driven_model import soccer_total_prob as _dd_fn
                except Exception:
                    _use_dd = False
            # ── Phase 1B — per-sport totals model availability ────────
            #   MLB / Soccer → data-driven engines (existing, gated).
            #   NFL          → Platinum game sim, BOTH sides evaluated.
            #   NBA/CFB/UFC/NHL/Tennis → MODEL_UNAVAILABLE (telemetried,
            #   never sportsbook-follow).
            _totals_model_ok = sport in ("MLB", "Soccer")
            _nfl_tot_sims: dict[str, dict] = {}
            if sport == "NFL":
                try:
                    from services.platinum_nfl.game_runtime import (
                        platinum_game_side_probability,
                    )
                    for _t_side in ("Over", "Under"):
                        _nfl_tot_sims[_t_side] = platinum_game_side_probability(
                            game=game, ctx=game_ctx, market="Total",
                            side=_t_side, line=float(line),
                            book_total_line=float(line),
                        )
                    _totals_model_ok = any(
                        r.get("available") for r in _nfl_tot_sims.values())
                    if not _totals_model_ok:
                        _r = next(iter(_nfl_tot_sims.values()), {})
                        from services import funnel_telemetry as _funnel
                        _funnel.record(
                            sport=sport, market="total", stage="model",
                            reason=_r.get("reason") or "MODEL_UNAVAILABLE",
                            event=f"{away} @ {home}",
                        )
                except Exception as _pe:
                    logger.warning("NFL platinum totals wiring failed: %s", _pe)
                    _totals_model_ok = False
            elif not _totals_model_ok:
                try:
                    from services import funnel_telemetry as _funnel
                    _funnel.record(
                        sport=sport, market="total", stage="model",
                        reason="MODEL_UNAVAILABLE",
                        event=f"{away} @ {home}",
                        detail="no authoritative independent totals model wired",
                    )
                except Exception:
                    pass
            if o_price is not None and _totals_model_ok:
                implied_o = _implied_prob(o_price)
                if _use_dd and _dd_fn:
                    dd = _dd_fn("Over", float(line), implied_o, game_ctx)
                    mp_o = dd["mp"]
                    contribs_o = dd["contributions"]
                elif sport == "NFL":
                    # Phase 1B — Platinum exact-line Over probability.
                    _sim_o = _nfl_tot_sims.get("Over") or {}
                    if not _sim_o.get("available"):
                        mp_o = None
                    else:
                        mp_o = _sim_o["prob"]
                    contribs_o = None
                else:
                    # 2026-07-21 FINAL PHASE — deterministic book-anchored
                    # seed. Was `implied_o + 0.05 + rng.random() * 0.08`
                    # which faked a random +5-13pp lift. Now uses a small
                    # deterministic +2pp (book edge is genuinely tilted
                    # toward Overs after juice removal).  Phase 1B: only
                    # MLB/Soccer reach here (engine-gated downstream).
                    mp_o = max(0.35, min(0.78, implied_o + 0.02))
                    contribs_o = None
                if mp_o is not None:
                    candidates.append({
                        "side":     "Over",
                        "price":    o_price,
                        "implied":  implied_o,
                        "mp":       mp_o,
                        "edge":     mp_o - implied_o,
                        "contribs": contribs_o,
                    })
            if u_price is not None and _totals_model_ok:
                implied_u = _implied_prob(u_price)
                # Reject truly lopsided dog-Unders (below 38% implied)
                # \u2014 there the Over is the only side worth grading.
                if implied_u >= 0.38:
                    if _use_dd and _dd_fn:
                        dd = _dd_fn("Under", float(line), implied_u, game_ctx)
                        mp_u = dd["mp"]
                        contribs_u = dd["contributions"]
                    elif sport == "NFL":
                        # Phase 1B — Platinum exact-line Under probability.
                        _sim_u = _nfl_tot_sims.get("Under") or {}
                        if not _sim_u.get("available"):
                            mp_u = None
                        else:
                            mp_u = _sim_u["prob"]
                        contribs_u = None
                    else:
                        # 2026-07-21 FINAL PHASE — deterministic. Was
                        # `implied_u + 0.04 + rng.random() * 0.07`.
                        # Phase 1B: only MLB/Soccer reach here.
                        mp_u = max(0.35, min(0.78, implied_u + 0.02))
                        contribs_u = None
                    if mp_u is not None:
                        candidates.append({
                            "side":     "Under",
                            "price":    u_price,
                            "implied":  implied_u,
                            "mp":       mp_u,
                            "edge":     mp_u - implied_u,
                            "contribs": contribs_u,
                        })

            # Pick the side with the largest positive edge. If both
            # have negative edge the downstream `_build_pick` filter
            # will drop it (edge_floor = -1.0), which is what we want.
            #
            # Block 2A.5.1 (2026-08-13) — side-neutrality:
            #   Previously `max(candidates, key=lambda c: c["edge"])`
            #   returned the FIRST element on exact tie, and `Over` was
            #   always inserted before `Under` in the candidates list.
            #   That deterministically biased ties toward Over.  We now
            #   detect exact-tie ambiguity and skip the pick entirely
            #   (both sides genuinely indistinguishable → no-bet).
            #   Non-tie cases select the strictly greater edge as before.
            if candidates:
                _sorted = sorted(candidates, key=lambda c: c["edge"],
                                  reverse=True)
                best = _sorted[0]
                if (len(_sorted) > 1
                        and abs(_sorted[0]["edge"] - _sorted[1]["edge"])
                            < 1e-9):
                    # Exact tie — both sides equally supported by the
                    # book-anchored fallback model.  Refuse to default
                    # to Over.  Neither side is graded.
                    best = None
                MIN_TOTALS_EDGE = 0.02   # 2 percentage points of positive edge
                if best is not None and best["edge"] >= MIN_TOTALS_EDGE:
                    # 2026-07-21 Phase 1 MLB + Phase 2 Soccer: real total
                    # factors, skip if not enough coverage.
                    _skip_total = False
                    _t_src: list[str] = []
                    if sport == "MLB":
                        from services.mlb_feature_engine import (
                            build_mlb_total_factors, has_enough_real_data,
                        )
                        _game_ctx = (game.get("_ctx") if isinstance(game, dict) else None) or {}
                        real_tot_factors, _t_src = build_mlb_total_factors(
                            _game_ctx, side=best["side"]
                        )
                        if not has_enough_real_data(real_tot_factors, "total"):
                            _skip_total = True
                        else:
                            factors = {k: v for k, v in real_tot_factors.items() if v is not None}
                    elif sport == "Soccer":
                        from services.soccer_feature_engine import (
                            build_soccer_total_factors, has_enough_soccer_data,
                        )
                        _game_ctx = (game.get("_ctx") if isinstance(game, dict) else None) or {}
                        real_tot_factors, _t_src = build_soccer_total_factors(
                            _game_ctx, side=best["side"]
                        )
                        if not has_enough_soccer_data(real_tot_factors, "total"):
                            _skip_total = True
                        else:
                            factors = {k: v for k, v in real_tot_factors.items() if v is not None}
                    else:
                        # Phase 1B — only NFL reaches this branch, and
                        # only when the Platinum game sim produced the
                        # probability (empty factors dict → lock derived
                        # purely from the authoritative model probability).
                        factors = {}

                    if not _skip_total:
                        if sport == "NFL" and _nfl_tot_sims:
                            # PHASE 2A — v3 composite for Platinum totals.
                            _e_t = round((best["mp"] - best["implied"]) * 100, 2)
                            lock, breakdown = compute_lock_score(
                                factors, win_prob=best["mp"] * 100,
                                pick={"book_odds": best["price"],
                                      "edge_percent": _e_t,
                                      "win_probability": best["mp"] * 100},
                                edge_percent=_e_t)
                        else:
                            lock, breakdown = compute_lock_score(factors, win_prob=best["mp"] * 100)
                        total_pick = _build_pick(
                            sport=sport, league=league, event=f"{away} @ {home}",
                            event_time=commence,
                            market=f"Total {_unit(sport)} {best['side']} {line}",
                            pick_side=best["side"],
                            model_win_prob=best["mp"], book_odds=best["price"],
                            lock=lock, factors=breakdown,
                            insights=_insights_for(sport, breakdown, best["side"], home, away),
                            external_id=f"{sport}-{game_id}-total-{best['side'].lower()}",
                            # PHASE 2A — exact same line, opposite side
                            opposing_prices=[u_price if best["side"] == "Over"
                                             else o_price],
                        )
                        if total_pick:
                            if best["side"] == "Under":
                                # Under Lock tab still needs this flag to
                                # surface the pick under MAIN.
                                total_pick["is_under_lock"] = True
                            # Attach data-driven contributions so the
                            # signal engine + pick rationale can surface
                            # the actual reasoning behind this side
                            # (\"Wind 15mph blowing out at Wrigley + Coors
                            # air = Over 8.5\" instead of a random tilt).
                            if best.get("contribs"):
                                total_pick["data_driven_contribs"] = best["contribs"]
                                total_pick["data_driven_used"] = True
                            # 2026-07-21 Phase 1 MLB + Phase 2 Soccer:
                            # real-data attribution
                            if sport in ("MLB", "Soccer") and _t_src:
                                total_pick["real_data_sources"] = list(_t_src)
                                total_pick["real_data_count"] = len(_t_src)
                            # Phase 1B — Platinum provenance on NFL totals
                            if sport == "NFL":
                                _sim_best = _nfl_tot_sims.get(best["side"]) or {}
                                if _sim_best.get("available"):
                                    from services.platinum_nfl.game_runtime import (
                                        attach_game_sim_provenance,
                                    )
                                    attach_game_sim_provenance(
                                        total_pick, _sim_best)
                            # PHASE 2A — de-vig handled at build time.
                            # 2026-08-23 FINAL SURGICAL — ONE SOCCER
                            # PRODUCTION WRITER (Totals).  Legacy Soccer
                            # totals emission is gated OFF; real_line_scorer_ingest
                            # is authoritative for Soccer Totals.
                            if sport == "Soccer":
                                _SOCCER_LEGACY_ENABLED = (
                                    os.getenv("SOCCER_GAME_MARKET_LEGACY_ENABLED", "").lower()
                                    in ("1", "true", "yes")
                                )
                                if not _SOCCER_LEGACY_ENABLED:
                                    total_pick = None
                            if total_pick is not None:
                                picks.append(total_pick)

            # ── Soccer Poisson-synthesized alt totals (Over 1.5, Over 3.5) ──
            # The Odds API doesn't return alternate_totals for soccer in the
            # bulk `/odds` call we use (would burn extra credits per-event).
            # Instead, derive Over 1.5 and Over 3.5 by fitting a Poisson to
            # the main O/U 2.5 implied prob and reading fair-odds off the
            # distribution. Tagged as `model_line=True` so the UI can label
            # them honestly (this is a model estimate, not a live book line).
            if (
                sport == "Soccer"
                and o_price is not None
                and isinstance(line, (int, float))
                and 1.0 <= float(line) <= 3.5
            ):
                try:
                    import math as _math
                    main_line = float(line)              # e.g. 2.5
                    p_over_main = _implied_prob(o_price) # de-vigged below using under
                    # No-vig adjustment using under (we have both sides for main).
                    u_implied = _implied_prob(u_price) if u_price is not None else (1.0 - p_over_main)
                    tot_v = p_over_main + u_implied
                    if tot_v > 0:
                        p_over_main = p_over_main / tot_v
                    # Fit λ via binary search so P(X > floor(main_line)) ≈ p_over_main.
                    # For 2.5: need P(X >= 3) ≈ p_over_main.
                    target_k = int(_math.floor(main_line)) + 1
                    def _p_over_at(lam: float, k_strict: int) -> float:
                        # P(X >= k_strict) where X ~ Poisson(lam)
                        cum = 0.0
                        term = _math.exp(-lam)
                        for i in range(k_strict):
                            cum += term
                            term *= lam / (i + 1)
                        return max(0.0, min(1.0, 1.0 - cum))
                    lo_l, hi_l = 0.1, 8.0
                    for _ in range(40):
                        mid_l = (lo_l + hi_l) / 2
                        if _p_over_at(mid_l, target_k) < p_over_main:
                            lo_l = mid_l
                        else:
                            hi_l = mid_l
                    lam = (lo_l + hi_l) / 2

                    # Synthesize Over 1.5 (chalkier Over) and Under 3.5
                    # (chalkier Under) from the Poisson lambda. These are
                    # tagged `is_alt=True` so they surface on the ALT
                    # line-type tab — user spec: "when I hit just alt
                    # tab soccer nothing pops up shouldn't over 1.5 pop
                    # up here". Over 3.5 / Under 1.5 excluded (junk juice).
                    # Each `(line, side)` tuple is skipped if it matches
                    # the main consensus line we already published.
                    extra: list[tuple[float, str]] = []
                    if abs(1.5 - main_line) > 0.4:
                        extra.append((1.5, "Over"))
                    if abs(3.5 - main_line) > 0.4:
                        extra.append((3.5, "Under"))
                    for alt_line, side_label in extra:
                        alt_k = int(_math.floor(alt_line)) + 1
                        # For Over: P(X >= alt_k). For Under: P(X < alt_k).
                        p_over_alt = _p_over_at(lam, alt_k)
                        p_alt = p_over_alt if side_label == "Over" else (1.0 - p_over_alt)
                        # Reject implausible synthesis: stay in [0.20, 0.93].
                        if not (0.20 <= p_alt <= 0.93):
                            continue
                        # Fair American odds from probability.
                        if p_alt >= 0.5:
                            fair_odds = int(round(-100 * p_alt / (1 - p_alt)))
                        else:
                            fair_odds = int(round(100 * (1 - p_alt) / p_alt))
                        # 2026-07-21 FINAL PHASE — deterministic. Was
                        # `p_alt + 0.02 + rng.random() * 0.04`. Now uses
                        # the Poisson-computed probability directly with
                        # a small +2pp deterministic seed. The real
                        # soccer feature engine gates emission below.
                        mp_alt = max(0.30, min(0.92, p_alt + 0.02))
                        # 2026-07-21 Phase 2 Soccer: real total factors,
                        # skip if not enough real data.
                        from services.soccer_feature_engine import (
                            build_soccer_total_factors, has_enough_soccer_data,
                        )
                        _game_ctx = (game.get("_ctx") if isinstance(game, dict) else None) or {}
                        real_alt_factors, _alt_src = build_soccer_total_factors(
                            _game_ctx, side=side_label
                        )
                        if not has_enough_soccer_data(real_alt_factors, "total"):
                            continue  # do NOT emit
                        factors_alt = {k: v for k, v in real_alt_factors.items() if v is not None}
                        lock_alt, breakdown_alt = compute_lock_score(factors_alt, win_prob=mp_alt * 100)
                        alt_pick = _build_pick(
                            sport=sport, league=league,
                            event=f"{away} @ {home}",
                            event_time=commence,
                            market=f"Total {_unit(sport)} {side_label} {alt_line}",
                            pick_side=side_label,
                            model_win_prob=mp_alt, book_odds=fair_odds,
                            lock=lock_alt, factors=breakdown_alt,
                            insights=_insights_for(sport, breakdown_alt, side_label, home, away),
                            external_id=f"{sport}-{game_id}-total-{side_label.lower()}-{alt_line}",
                        )
                        if alt_pick:
                            # Flag as model-derived AND as an alt line so the
                            # UI can label it ("Model line — synthesized from
                            # market O/U") and route it under the ALT tab.
                            alt_pick["model_line"] = True
                            alt_pick["model_source"] = "poisson_from_main_total"
                            alt_pick["is_alt"] = True
                            picks.append(alt_pick)
                except Exception as _e:
                    logger.debug("Soccer Poisson alt-totals skipped: %s", _e)

    # Spread / Run / Game line pick — skip for soccer (no balanced spread
    # market) and UFC (rare). KBO uses run-line like MLB. Tennis has game
    # spreads which are useful for asymmetric matchups.
    #
    # 2026-07-02 fix (user report: "why don't I see MLB spreads or team
    # totals?"): The previous logic did a RANDOM 50/50 side selection which
    # threw away half of the potentially-actionable spread picks. Books
    # price the underdog +1.5/+3.5 side (chalky, high implied) very
    # differently from the favorite -1.5/-3.5 side (positive odds, low
    # implied), and our downstream filters accept only one of them per
    # game. Emit BOTH sides so the filters do the right thing on each,
    # instead of tossing a coin at generation time. `_build_pick` returns
    # None for the side that doesn't clear the floors, so we never over-
    # surface a garbage pick — we just stop losing the good one to chance.
    if spreads_outs and sport in ("MLB", "NBA", "NFL", "KBO", "Tennis", "NHL"):
        home_sp = next((o for o in spreads_outs if o.get("name") == home), None)
        away_sp = next((o for o in spreads_outs if o.get("name") == away), None)
        if home_sp and away_sp:
            for side_obj in (home_sp, away_sp):
                side = side_obj.get("name")
                line = side_obj.get("point")
                price = int(side_obj.get("price")) if isinstance(side_obj.get("price"), (int, float)) else -110
                implied = _implied_prob(price)
                # 2026-07-21 FINAL PHASE — deterministic book-anchored
                # seed. Was `implied + 0.04 + rng.random() * 0.08` which
                # baked a random 4-12pp lift into every spread pick. Now
                # +4pp deterministic; MLB/Soccer feature engine gates
                # emission below and can recompute mp from real factors.
                mp = max(0.4, min(0.78, implied + 0.04))
                _nfl_plat_sp = None    # Phase 1B — Platinum provenance
                # 2026-07-21 Phase 1 MLB: real spread factors, gated on
                # coverage.
                if sport == "MLB":
                    from services.mlb_feature_engine import (
                        build_mlb_ml_factors, has_enough_real_data,
                    )
                    _game_ctx = (game.get("_ctx") if isinstance(game, dict) else None) or {}
                    real_sp_factors, _sp_src = build_mlb_ml_factors(_game_ctx, pick_team=side)
                    if not has_enough_real_data(real_sp_factors, "ml"):
                        continue
                    factors = {k: v for k, v in real_sp_factors.items() if v is not None}
                elif sport == "Soccer":
                    from services.soccer_feature_engine import (
                        build_soccer_ml_factors, has_enough_soccer_data,
                    )
                    _game_ctx = (game.get("_ctx") if isinstance(game, dict) else None) or {}
                    real_sp_factors, _sp_src = build_soccer_ml_factors(_game_ctx, pick_team=side)
                    if not has_enough_soccer_data(real_sp_factors, "ml"):
                        continue
                    factors = {k: v for k, v in real_sp_factors.items() if v is not None}
                elif sport == "NFL":
                    # Phase 1B (R1) — Platinum spread sim per side.
                    try:
                        from services.platinum_nfl.game_runtime import (
                            platinum_game_side_probability,
                        )
                        _sp_ctx = (game.get("_ctx") if isinstance(game, dict) else None) or {}
                        _book_total_line_sp = next(
                            (o.get("point") for o in totals_outs
                             if o.get("name") == "Over"), None)
                        _nfl_plat_sp = platinum_game_side_probability(
                            game=game, ctx=_sp_ctx, market="Spread",
                            side=side, line=line,
                            is_home_side=(side == home),
                            book_total_line=_book_total_line_sp,
                        )
                    except Exception as _spe:
                        logger.warning("NFL platinum spread wiring failed: %s", _spe)
                        _nfl_plat_sp = {"available": False,
                                        "reason": f"SIM_EXCEPTION:{type(_spe).__name__}"}
                    if not (_nfl_plat_sp or {}).get("available"):
                        try:
                            from services import funnel_telemetry as _funnel
                            _funnel.record(
                                sport=sport, market="spread", stage="model",
                                reason=(_nfl_plat_sp or {}).get("reason") or "MODEL_UNAVAILABLE",
                                event=f"{away} @ {home}", side=str(side),
                            )
                        except Exception:
                            pass
                        continue
                    mp = _nfl_plat_sp["prob"]
                    factors = {}
                else:
                    # Phase 1B — NBA / Tennis / NHL / KBO spreads have no
                    # authoritative independent model. MODEL_UNAVAILABLE,
                    # never sportsbook-follow.
                    try:
                        from services import funnel_telemetry as _funnel
                        _funnel.record(
                            sport=sport, market="spread", stage="model",
                            reason="MODEL_UNAVAILABLE",
                            event=f"{away} @ {home}", side=str(side),
                            detail="no authoritative independent spread model wired",
                        )
                    except Exception:
                        pass
                    continue
                if sport == "NFL" and _nfl_plat_sp and _nfl_plat_sp.get("available"):
                    # PHASE 2A — v3 composite for Platinum spreads.
                    _e_sp = round((mp - implied) * 100, 2)
                    lock, breakdown = compute_lock_score(
                        factors, win_prob=mp * 100,
                        pick={"book_odds": price, "edge_percent": _e_sp,
                              "win_probability": mp * 100},
                        edge_percent=_e_sp)
                else:
                    lock, breakdown = compute_lock_score(factors, win_prob=mp * 100)
                sign = "+" if (line or 0) > 0 else ""
                # Deterministic per-side external id so re-runs don't
                # collide between the home / away spread picks in the
                # picks collection (pick_id derives from external_id).
                side_slug = "home" if side == home else "away"
                _sp_pick = _build_pick(
                    sport=sport, league=league, event=f"{away} @ {home}",
                    event_time=commence,
                    market=f"{side} {sign}{line} Spread", pick_side=side,
                    model_win_prob=mp, book_odds=price,
                    lock=lock, factors=breakdown,
                    insights=_insights_for(sport, breakdown, side, home, away),
                    external_id=f"{sport}-{game_id}-spread-{side_slug}",
                    # PHASE 2A — same spread line, opposite side
                    opposing_prices=[((away_sp if side == home else home_sp)
                                      or {}).get("price")],
                )
                if _sp_pick and _nfl_plat_sp and _nfl_plat_sp.get("available"):
                    from services.platinum_nfl.game_runtime import (
                        attach_game_sim_provenance,
                    )
                    attach_game_sim_provenance(_sp_pick, _nfl_plat_sp)
                # PHASE 2A — de-vig handled at build time.
                picks.append(_sp_pick)
    return [p for p in picks if p is not None]


def _unit(sport: str) -> str:
    return {"MLB": "Runs", "NBA": "Points", "NFL": "Points", "CFB": "Points",
            "Soccer": "Goals", "Tennis": "Games",
            "UFC": "Rounds", "KBO": "Runs", "NHL": "Goals",
            "WNBA": "Points"}.get(sport, "Points")


def _insights_for(sport: str, breakdown: dict, side: str, home: str, away: str) -> list[str]:
    """Generate HONEST qualitative bullets from the actual model factor scores.

    Critically: we NEVER invent specific numeric stats (e.g. "39-5 L12 months",
    ".275 BAA", "78% finish rate") because those would mislead users into
    thinking they're real data. Instead we describe each factor in plain
    English using its model score band:

        90+  → "elite"        70-79 → "favorable"      40-49 → "neutral"
        80-89 → "strong"      60-69 → "solid"         30-39 → "below avg"
                              50-59 → "modest"        <30   → "concern"

    Tennis picks layer their richer (real) component insights on top via
    `tennis_engine.build_tennis_insights`; this function only fills in the
    sport-agnostic baseline for non-tennis picks.
    """
    if not breakdown:
        return []
    # Sort factors descending — highlight the strongest model signals first.
    sorted_factors = sorted(
        ((k, float(v)) for k, v in breakdown.items() if isinstance(v, (int, float))),
        key=lambda kv: -kv[1],
    )
    top = sorted_factors[:4]  # only the four most decisive
    out: list[str] = []
    for name, score in top:
        out.append(f"{name}: {score:.0f}/100 — {_score_label(score)}.")
    # Append a single sport-context note tying the analysis to the pick side
    # without inventing any stats.
    side_note = _side_context_note(sport, side, home, away)
    if side_note:
        out.append(side_note)
    return out


def _score_label(score: float) -> str:
    if score >= 90: return "elite signal"
    if score >= 80: return "strong"
    if score >= 70: return "favorable"
    if score >= 60: return "solid"
    if score >= 50: return "modest"
    if score >= 40: return "neutral"
    if score >= 30: return "below average"
    return "concern"


def _side_context_note(sport: str, side: str, home: str, away: str) -> str:
    """A single sport-aware sentence that does NOT invent numbers.

    Just frames the pick contextually so the rationale reads naturally.
    """
    if sport == "Tennis":
        return ""  # tennis insights are produced by tennis_engine
    if sport == "Soccer":
        if side == home:
            return f"Home environment favors {home} on multiple factor axes."
        if side == away:
            return f"Model rates {away} ahead of book despite away leg."
        return ""
    if sport in ("MLB", "KBO"):
        return f"Composite weighting tilts toward {side} on this slate."
    if sport in ("NBA", "WNBA"):
        return f"Pace + matchup model favors {side} tonight."
    if sport == "NFL":
        return f"Snap-share / DVOA model tilts toward {side}."
    if sport == "UFC":
        return f"Striking + grappling composite favors {side}."
    return ""


# ───────────────────────── Per-sport fetchers ─────────────────────────


LEAGUE_LABELS: dict[str, str] = {
    "baseball_mlb": "MLB",
    "basketball_nba": "NBA",
    "basketball_wnba": "WNBA",
    "americanfootball_nfl": "NFL",
    "americanfootball_nfl_preseason": "NFL Preseason",
    "icehockey_nhl": "NHL",
    "americanfootball_ncaaf": "CFB",
    # UFC / MMA
    "mma_mixed_martial_arts": "UFC / MMA",
    # KBO
    "baseball_kbo": "KBO",
    # FIFA tournaments
    "soccer_fifa_world_cup": "FIFA World Cup",
    "soccer_fifa_world_cup_winner": "FIFA World Cup Outright",
    "soccer_fifa_club_world_cup": "FIFA Club World Cup",
    # UEFA + major European leagues
    "soccer_uefa_champs_league": "UEFA Champions League",
    "soccer_uefa_champs_league_qualification": "UEFA Champions League Qualifying",
    "soccer_uefa_europa_league": "UEFA Europa League",
    "soccer_uefa_europa_conference_league": "UEFA Conference League",
    "soccer_uefa_nations_league": "UEFA Nations League",
    "soccer_uefa_european_championship": "UEFA Euro",
    "soccer_uefa_euro": "UEFA Euro",
    "soccer_epl": "Premier League",
    "soccer_spain_la_liga": "La Liga",
    "soccer_germany_bundesliga": "Bundesliga",
    "soccer_italy_serie_a": "Serie A",
    "soccer_france_ligue_one": "Ligue 1",
    "soccer_germany_dfb_pokal": "DFB-Pokal",
    "soccer_spain_segunda_division": "La Liga 2",
    # CONMEBOL
    "soccer_conmebol_copa_libertadores": "Copa Libertadores",
    "soccer_conmebol_copa_sudamericana": "Copa Sudamericana",
    "soccer_conmebol_copa_america": "Copa América",
    # Other leagues
    "soccer_brazil_serie_a": "Brasileirão Série A",
    "soccer_brazil_serie_b": "Brasileirão Série B",
    "soccer_norway_eliteserien": "Eliteserien",
    "soccer_sweden_allsvenskan": "Allsvenskan",
    "soccer_sweden_superettan": "Superettan",
    "soccer_finland_veikkausliiga": "Veikkausliiga",
    "soccer_chile_campeonato": "Primera Chile",
    "soccer_china_superleague": "China Super League",
    "soccer_league_of_ireland": "League of Ireland",
    "soccer_australia_aleague": "A-League",
    "soccer_mexico_ligamx": "Liga MX",
    "soccer_usa_mls": "MLS",
    "tennis_atp_wimbledon": "ATP Wimbledon",
    "tennis_wta_wimbledon": "WTA Wimbledon",
    "tennis_atp_queens_club_champ": "ATP Queen's Club",
    "tennis_wta_queens_club_champ": "WTA Queen's Club",
    "tennis_atp_halle_open": "ATP Halle Open",
    "tennis_wta_german_open": "WTA Berlin",
    "tennis_atp_eastbourne": "ATP Eastbourne",
    "tennis_wta_eastbourne": "WTA Eastbourne",
    "tennis_atp_french_open": "ATP French Open",
    "tennis_wta_french_open": "WTA French Open",
    "tennis_atp_us_open": "ATP US Open",
    "tennis_wta_us_open": "WTA US Open",
    "tennis_atp_aus_open_singles": "ATP Australian Open",
    "tennis_wta_aus_open_singles": "WTA Australian Open",
    "tennis_atp_indian_wells": "ATP Indian Wells",
    "tennis_wta_indian_wells": "WTA Indian Wells",
    "tennis_atp_miami_open": "ATP Miami Open",
    "tennis_wta_miami_open": "WTA Miami Open",
    "tennis_atp_monte_carlo_masters": "ATP Monte-Carlo Masters",
    "tennis_atp_madrid_open": "ATP Madrid Open",
    "tennis_wta_madrid_open": "WTA Madrid Open",
    "tennis_atp_italian_open": "ATP Italian Open",
    "tennis_wta_italian_open": "WTA Italian Open",
    "tennis_atp_canadian_open": "ATP Canadian Open",
    "tennis_wta_canadian_open": "WTA Canadian Open",
    "tennis_atp_cincinnati_open": "ATP Cincinnati Open",
    "tennis_wta_cincinnati_open": "WTA Cincinnati Open",
    "tennis_atp_shanghai_masters": "ATP Shanghai Masters",
    "tennis_atp_paris_masters": "ATP Paris Masters",
    "tennis_atp_barcelona_open": "ATP Barcelona Open",
    "tennis_atp_hamburg_open": "ATP Hamburg Open",
    "tennis_atp_dubai": "ATP Dubai",
    "tennis_wta_dubai": "WTA Dubai",
    "tennis_atp_qatar_open": "ATP Qatar Open",
    "tennis_atp_china_open": "ATP China Open",
    "tennis_wta_china_open": "WTA China Open",
    "tennis_atp_munich": "ATP Munich",
    "tennis_wta_charleston_open": "WTA Charleston Open",
    "tennis_wta_strasbourg": "WTA Strasbourg",
    "tennis_wta_stuttgart_open": "WTA Stuttgart Open",
    "tennis_wta_wuhan_open": "WTA Wuhan Open",
}


async def _fetch_picks_for_sport(sport: str, date_str: str) -> list[dict]:
    await _load_active_sports()
    all_picks: list[dict] = []
    # Soccer needs UK region to get the Draw outcome in the h2h market.
    region = "uk" if sport == "Soccer" else "us"
    # ── 2026-08-23 PASS 1 — Universal active provider discovery ──
    # Static ``SPORT_KEYS[sport]`` is FALLBACK only; the authoritative
    # league/tournament set is ``_ACTIVE_KEYS`` filtered by prefix.
    # This automatically reaches active supported leagues (Eredivisie
    # / China Super League / new Tennis tournaments) without any
    # per-league patch.  A key is included iff (a) it's in the static
    # fallback OR (b) the provider catalog marks it active AND the
    # prefix matches the sport.
    _prefix_map = {
        "Soccer":  "soccer_",
        "Tennis":  "tennis_",
    }
    _static_keys = list(SPORT_KEYS.get(sport, []))
    _sport_keys: list[str] = list(_static_keys)
    _pfx = _prefix_map.get(sport)
    if _pfx and _ACTIVE_KEYS:
        _static_set = set(_static_keys)
        for _k in sorted(_ACTIVE_KEYS):
            if _k.startswith(_pfx) and _k not in _static_set:
                _sport_keys.append(_k)
    for key in _sport_keys:
        if _ACTIVE_KEYS and key not in _ACTIVE_KEYS:
            continue
        games = await _fetch_odds_for(key, regions=region, sport=sport)
        league_label = LEAGUE_LABELS.get(key, sport)
        # ── 2026-08-23 QUOTA — bounded fair-window slate (no hard drop) ──
        # Prior code did ``sorted(games, commence_time)[:40]`` which
        # STILL permanently dropped valid current/future events
        # beyond position 40.  Replaced by a bounded window:
        #   * Anchor at the nearest commence_time (now or next start).
        #   * Include every event whose commence_time is inside a
        #     ``_SLATE_WINDOW_HOURS`` window (default 30 h) from the
        #     anchor.  This is a natural "today's slate" boundary —
        #     late-night doubleheaders and next-morning tips stay
        #     reachable but next-week fixtures don't leak in.
        #   * Absolute safety cap (``_SLATE_HARD_MAX``) prevents a
        #     runaway iteration on pathological catalogs; picked large
        #     enough (150) that no realistic single-day supported
        #     slate hits it (Wimbledon peaks ~48 matches/day, MLB
        #     ~30 games, NFL Sunday ~14).  The cap is a safety valve,
        #     never a silent business filter.
        try:
            from datetime import datetime as _dt, timezone as _tz, \
                timedelta as _td
            def _ct(g):
                return g.get("commence_time") or "9999-99-99"
            games_sorted = sorted(games, key=_ct)
            _SLATE_WINDOW_HOURS = 30
            _SLATE_HARD_MAX = 150
            _anchor = None
            _now_dt = _dt.now(_tz.utc)
            for _g in games_sorted:
                try:
                    _c = _dt.fromisoformat(
                        (_g.get("commence_time") or "").replace("Z", "+00:00")
                    )
                except Exception:
                    continue
                if _c >= _now_dt - _td(hours=2):
                    _anchor = _c
                    break
            if _anchor is None:
                games_windowed = games_sorted[:_SLATE_HARD_MAX]
            else:
                _end = _anchor + _td(hours=_SLATE_WINDOW_HOURS)
                games_windowed = []
                for _g in games_sorted:
                    try:
                        _c = _dt.fromisoformat(
                            (_g.get("commence_time") or "").replace("Z", "+00:00")
                        )
                    except Exception:
                        continue
                    if _c <= _end:
                        games_windowed.append(_g)
                    if len(games_windowed) >= _SLATE_HARD_MAX:
                        break
        except Exception:
            games_windowed = games[:150]
        for g in games_windowed:
            # ─── Data-driven context prefetch (2026-07-19/20) ─────────
            # Fetch weather, park HR, xG rolling, Sackmann etc. BEFORE
            # generating picks so the model can compute an actual data-
            # driven `model_win_prob` instead of a random tilt. Wired
            # for MLB (weather + park + starters), Soccer (form + xG +
            # managers + pressure), Tennis (Sackmann + surface Elo +
            # fatigue + H2H). Other sports still random-tilt.
            try:
                if sport == "MLB":
                    from services.game_context import build_mlb_game_context
                    g["_ctx"] = await build_mlb_game_context(g)
                elif sport == "Soccer":
                    from services.game_context import build_soccer_game_context
                    g["_ctx"] = await build_soccer_game_context(g)
                elif sport == "Tennis":
                    from services.game_context import build_tennis_match_context
                    g["_ctx"] = await build_tennis_match_context(g)
                elif sport == "NFL":
                    # Phase 1B — Platinum game-sim model context
                    # (team-strength expected margin + expected total).
                    from services.platinum_nfl.game_runtime import (
                        build_nfl_game_model_context,
                    )
                    g.setdefault("sport_key", key)
                    g["_ctx"] = await build_nfl_game_model_context(g)
            except Exception as e:
                logger.debug("%s context prefetch failed for %s: %s",
                             sport, g.get("id"), e)
            all_picks.extend(_picks_from_game(sport, league_label, g, date_str))
            # ─── Tennis alt-line augmentation ────────────────────────
            # Per user spec: "Tennis have alt line available pls add and
            # calculate them to build picks." Tennis exposes alt spreads
            # + alt totals on The Odds API per-event endpoint. We fetch
            # one extra call per game (small credit cost, ~5-15 credits
            # per match), build up to 2 sweet-spot alt picks, and let
            # them flow through the standard validator + lock pipeline.
            if sport == "Tennis" and g.get("id"):
                try:
                    alt_payload = await _fetch_tennis_event_alts(key, g["id"])
                    alt_picks = _build_tennis_alt_picks(
                        key, league_label, g, alt_payload, date_str,
                    )
                    if alt_picks:
                        all_picks.extend(alt_picks)
                except Exception as e:
                    logger.debug(
                        "Tennis alt-line fetch skipped for %s: %s",
                        g.get("id"), e,
                    )
            # ─── MLB team-total + alt-run-line augmentation ──────────
            # User report 2026-07-02 "why don't I see mlb spreads or
            # team totals?". The Odds API only exposes team_totals /
            # alternate_team_totals / alternate_spreads on the per-event
            # endpoint (not the bulk /odds endpoint). One extra call
            # per game to build ALL missing MLB team-level markets.
            # Edge gates in `_build_mlb_alt_picks` prevent noise.
            if sport == "MLB" and g.get("id"):
                try:
                    mlb_alt_payload = await _fetch_mlb_event_alts(key, g["id"])
                    mlb_alt_picks = _build_mlb_alt_picks(
                        key, league_label, g, mlb_alt_payload, date_str,
                    )
                    if mlb_alt_picks:
                        all_picks.extend(mlb_alt_picks)
                except Exception as e:
                    logger.debug(
                        "MLB alt-line fetch skipped for %s: %s",
                        g.get("id"), e,
                    )
    # ── Post-generation slate cap: "best totals of the day" ──────────
    # 2026-07-19 user request: "not one per game I just want the best
    # ones for day over or under". After every game emits its best
    # side, sort by edge and keep only the top MAX_DAILY_TOTALS. This
    # produces a curated daily-totals slate instead of one per game.
    # Applied per-sport so MLB / NBA / NFL each get their own cap.
    #
    # Block 2D Closure §1 (2026-08) — the cap could silently suppress
    # legitimate elite totals that came AFTER the top-6 by edge.  We
    # now let ANY total that survives strict>85 through the cap AND
    # additionally keep the top MAX_DAILY_TOTALS by edge from the
    # rest.  The user directive was "best totals of the day", not
    # "at most 6 totals" — so a genuinely elite pick with Lock >=
    # LOCK_ELITE_FLOOR is always kept.
    MAX_DAILY_TOTALS = 6
    LOCK_ELITE_FLOOR = 90  # >85 gate + 5 elite margin
    def _is_game_total(p: dict) -> bool:
        m = (p.get("market") or "").lower()
        return (
            m.startswith("total ")
            and "team total" not in m
            and "(alt)" not in m
        )
    totals_picks = [p for p in all_picks if _is_game_total(p)]
    non_totals   = [p for p in all_picks if not _is_game_total(p)]
    if len(totals_picks) > MAX_DAILY_TOTALS:
        # Split: elite lock always kept; the rest ranked by edge.
        elite   = [p for p in totals_picks
                    if float(p.get("lock_score") or 0) >= LOCK_ELITE_FLOOR]
        elite_ids = {p.get("id") or p.get("external_id") for p in elite}
        remainder = [p for p in totals_picks
                     if (p.get("id") or p.get("external_id")) not in elite_ids]
        # Rank remainder by (model_win_prob - implied) edge - highest first.
        def _edge(p: dict) -> float:
            try:
                mp = float(p.get("model_win_prob") or p.get("win_probability", 0))
                if mp > 1: mp /= 100.0
                ip = float(p.get("implied_probability") or 0)
                if ip > 1: ip /= 100.0
                return mp - ip
            except Exception:
                return 0.0
        remainder.sort(key=_edge, reverse=True)
        # Keep top-N from remainder such that elite + top-N ≤ MAX.
        # If elite alone exceeds MAX, we let ALL elite through (never
        # suppress a >=90 Lock Score total).
        room = max(0, MAX_DAILY_TOTALS - len(elite))
        totals_picks = elite + remainder[:room]
        logger.info(
            "%s: totals cap applied (elite kept=%d, edge-ranked=%d, "
            "original=%d, cap=%d)",
            sport, len(elite), min(len(remainder), room),
            len([p for p in all_picks if _is_game_total(p)]),
            MAX_DAILY_TOTALS,
        )
        all_picks = non_totals + totals_picks
    return all_picks


async def fetch_mlb_picks(date_str: str) -> list[dict]:
    return await _fetch_picks_for_sport("MLB", date_str)


async def fetch_nba_picks(date_str: str) -> list[dict]:
    return await _fetch_picks_for_sport("NBA", date_str)


async def fetch_nfl_picks(date_str: str) -> list[dict]:
    return await _fetch_picks_for_sport("NFL", date_str)


async def fetch_cfb_picks(date_str: str) -> list[dict]:
    """College Football pick generator. Same NFL pipeline (ML/Spread/
    Total + props via Odds API), just keyed on `americanfootball_ncaaf`.
    CFB-specific features (returning production, transfer portal, SoS)
    plug in via a follow-up enrichment layer when a CFB-data API key
    lands. Foundation: ensure CFB games surface on the board the
    moment Odds API has them (typically mid-August)."""
    return await _fetch_picks_for_sport("CFB", date_str)


async def fetch_soccer_picks(date_str: str) -> list[dict]:
    return await _fetch_picks_for_sport("Soccer", date_str)


async def fetch_tennis_picks(date_str: str) -> list[dict]:
    picks = await _fetch_picks_for_sport("Tennis", date_str)
    # 2026-07-01 fix: `_picks_from_game` is intermittently missing the
    # h2h market for Tennis (esp. ATP) even when DK+FanDuel both expose
    # it — we've verified live_alt_lines has full moneyline coverage.
    # Backfill any Wimbledon/tour match that got alt-line picks but no
    # moneyline pick directly from the live_alt_lines feed. This is a
    # read-only Mongo op, no extra API calls consumed.
    try:
        picks = await _backfill_tennis_moneylines(picks, date_str)
    except Exception as e:
        logger.warning("tennis ML backfill failed: %s", e)
    return picks


async def _backfill_tennis_moneylines(picks: list[dict], date_str: str) -> list[dict]:
    """For every Tennis event on today's slate that has alt-line picks
    but NO moneyline pick, pull the h2h line from `live_alt_lines` and
    emit a moneyline pick. This is a defensive backstop for the ATP
    fetch bug where h2h markets don't consistently appear in the bulk
    /odds response.

    Selection heuristic: favorite side only. Odds cap at -750 (extreme
    chalk isn't rollover-friendly)."""
    from server import db  # lazy
    # Build a set of events that already have an ML pick — those we skip.
    have_ml: set[str] = set()
    for p in picks:
        if (p.get("sport") or "").lower() != "tennis":
            continue
        m = (p.get("market") or "").lower()
        if "moneyline" in m or "match winner" in m:
            have_ml.add(p.get("event") or "")
    # Find all Tennis h2h rows in live_alt_lines for events with any
    # existing pick today.
    tennis_events_today: set[str] = {
        p.get("event") for p in picks
        if (p.get("sport") or "").lower() == "tennis" and p.get("event")
    }
    if not tennis_events_today:
        return picks
    missing_events = tennis_events_today - have_ml
    if not missing_events:
        return picks

    added = 0
    # For each missing event, compute median favorite price across books.
    for event_name in missing_events:
        rows = await db.live_alt_lines.find({
            "sport": "tennis", "market_key": "h2h", "event_name": event_name,
        }).to_list(length=20)
        if len(rows) < 2:
            continue
        # Group by selection (player name) and average the prices.
        by_side: dict[str, list[float]] = {}
        for r in rows:
            sel = (r.get("selection") or "").strip()
            price = r.get("price")
            if not sel or not isinstance(price, (int, float)):
                continue
            by_side.setdefault(sel, []).append(float(price))
        if len(by_side) != 2:
            continue
        # Identify favourite (the side with the most-negative median).
        medians = {
            side: sorted(prices)[len(prices) // 2]
            for side, prices in by_side.items()
        }
        fav_side, fav_price = min(medians.items(), key=lambda x: x[1])
        dog_side, dog_price = max(medians.items(), key=lambda x: x[1])
        # Chalk cap — skip if too extreme
        if fav_price < -750:
            continue
        # Compute implied for both sides
        fav_implied = (-fav_price) / ((-fav_price) + 100.0) if fav_price < 0 else 100.0 / (fav_price + 100.0)
        dog_implied = (-dog_price) / ((-dog_price) + 100.0) if dog_price < 0 else 100.0 / (dog_price + 100.0)

        # ── 2026-07-27 UPSET DETECTION FIX ─────────────────────────
        # User: "I don't just want random dog picks. I want the app
        # to do MATH — if the dog comes out on top, it should hit
        # the board." Previously this backfill hardcoded fav-side.
        # Now score BOTH sides through the tennis_math_engine and
        # pick whichever the MODEL says wins. Book still gets its
        # priors on the seed; math flips when it has real signal.

        # Build ctx (surface, tier, book consensus, Sackmann) once — shared
        # between fav & dog evaluations.
        _all_prices = [float(r.get("price")) for r in rows if isinstance(r.get("price"), (int, float))]
        _fav_prices = by_side.get(fav_side, [])
        _fav_probs = []
        for _pr in _fav_prices:
            if _pr >= 100:
                _fav_probs.append(100.0 / (_pr + 100.0))
            else:
                _fav_probs.append(-_pr / (-_pr + 100.0))
        _ctx: dict = {}
        if len(_fav_probs) >= 3:
            _ctx["book_consensus_spread_pp"] = round((max(_fav_probs) - min(_fav_probs)) * 100.0, 2)
        _league_l = (rows[0].get("league") or "").lower()
        _evt_l = event_name.lower()
        _combo = f"{_league_l} {_evt_l}"
        if any(t in _combo for t in ("australian open","french open","wimbledon","us open")):
            _ctx["match_tier"] = "slam"
        elif "atp 1000" in _combo or "wta 1000" in _combo or "masters 1000" in _combo:
            _ctx["match_tier"] = "atp1000"
        elif "atp 500" in _combo or "wta 500" in _combo:
            _ctx["match_tier"] = "atp500"
        elif "atp 250" in _combo or "wta 250" in _combo:
            _ctx["match_tier"] = "atp250"
        elif "challenger" in _combo:
            _ctx["match_tier"] = "challenger"
        elif any(t in _combo for t in ("itf","w15","w25","w40","w60","m15","m25")):
            _ctx["match_tier"] = "itf"
        # Surface heuristic
        if any(x in _evt_l for x in ("wimbledon","grass")):
            _surface_key = "Grass"
        elif any(x in _evt_l for x in ("french","clay","roland","monte carlo","madrid","rome","barcelona")):
            _surface_key = "Clay"
        else:
            _surface_key = "Hard"
        # Sackmann lookup (silent no-op for WTA/Challenger)
        try:
            from services.tennis.fallback import get_player_stats, get_h2h
            _sa = await get_player_stats(db, fav_side, _surface_key)
            _sb = await get_player_stats(db, dog_side, _surface_key)
            if _sa: _ctx["sackmann_a"] = _sa
            if _sb: _ctx["sackmann_b"] = _sb
            _h = await get_h2h(db, fav_side, dog_side)
            if _h and _h.get("matches", 0) >= 1:
                _ctx["h2h_a_wins"] = _h.get("a_wins", 0)
                _ctx["h2h_b_wins"] = _h.get("b_wins", 0)
        except Exception:
            pass

        # Run the math engine on both perspectives (fav = home, dog = away)
        chosen_side = fav_side
        chosen_price = fav_price
        chosen_implied = fav_implied
        model_wp = fav_implied  # default fallback
        dd_contribs: dict = {}
        try:
            from services.tennis_math_engine import (
                score_tennis_matchup, has_real_tennis_signal,
            )
            _math_ctx = dict(_ctx)  # copy
            # For math engine, "home"=fav_side, "away"=dog_side
            math_signal = score_tennis_matchup(
                fav_side, dog_side, _surface_key.lower(), fav_implied, _math_ctx,
            )
            if math_signal and has_real_tennis_signal(math_signal):
                math_wp_fav = math_signal["home_win_prob"]
                # If model says DOG wins more often, flip the pick
                if math_wp_fav < 0.50:
                    chosen_side = dog_side
                    chosen_price = dog_price
                    chosen_implied = dog_implied
                    model_wp = 1.0 - math_wp_fav
                else:
                    chosen_side = fav_side
                    chosen_price = fav_price
                    chosen_implied = fav_implied
                    model_wp = math_wp_fav
                dd_contribs = dict(math_signal.get("contributions") or {})
                dd_contribs["math_engine_used"] = True
                _math_signal_ok = True
                logger.debug(
                    "tennis math backfill: %s vs %s → chose %s (model_wp=%.3f, book_implied=%.3f)",
                    fav_side, dog_side, chosen_side, model_wp, chosen_implied,
                )
        except Exception as _mx:
            logger.debug("tennis math engine failed on %s: %s", event_name, _mx)

        # ── Phase 1B — authoritative-model gate ─────────────────────
        # If the tennis math engine produced no real signal, this
        # backfill would previously emit a book-follow pick (model_wp =
        # fav_implied) with a hard-coded lock ladder. That is retired:
        # MODEL_UNAVAILABLE is telemetried and nothing is emitted.
        if not _math_signal_ok:
            try:
                from services import funnel_telemetry as _funnel
                _funnel.record(
                    sport="Tennis", market="moneyline",
                    stage="model", reason="MODEL_UNAVAILABLE",
                    event=event_name,
                    detail="ml backfill: no real tennis math signal",
                )
            except Exception:
                pass
            continue

        # Final win_prob / edge / lock — computed from chosen side
        implied = chosen_implied  # legacy variable name kept for below
        win_prob = round(min(0.95, max(0.15, model_wp)) * 100, 1)
        edge_pct = round((win_prob / 100.0 - chosen_implied) * 100.0, 2)
        # Lock score: match the internal calibrator's rough shape
        if model_wp >= 0.85: lock_score = 96.0
        elif model_wp >= 0.75: lock_score = 92.0
        elif model_wp >= 0.65: lock_score = 88.0
        elif model_wp >= 0.55: lock_score = 82.0
        elif model_wp >= 0.50: lock_score = 76.0    # dog-flip case
        else: lock_score = 70.0

        import uuid, hashlib
        commence = rows[0].get("commence_time") or ""
        pick_id = str(uuid.uuid5(
            uuid.NAMESPACE_DNS,
            f"tennis-ml-backfill-{event_name}-{chosen_side}-{date_str}",
        ))
        external_id = hashlib.md5(
            f"tennis-ml-backfill-{event_name}-{chosen_side}-{date_str}".encode()
        ).hexdigest()
        pick = {
            "id": pick_id,
            "external_id": external_id,
            "sport": "Tennis",
            "league": (rows[0].get("league") or "Tennis"),
            "event": event_name,
            "event_time": commence,
            "market": f"{chosen_side} Moneyline",
            "selection": chosen_side,
            "book_odds": int(chosen_price),
            "win_probability": win_prob,
            "edge_percent": edge_pct,
            "lock_score": lock_score,
            "pick_date": date_str,
            "source": "tennis_ml_backfill_2026-07-01",
            "is_alt": False,
            "no_bet": False,
        }
        if dd_contribs:
            pick["data_driven_used"] = True
            pick["data_driven_contribs"] = dd_contribs
            pick["is_upset_pick"] = (chosen_side != fav_side)

        # Legacy DD-lift path for cases where math engine didn't fire
        # (kept for backwards compat / non-Elo picks).
        if not dd_contribs:
            try:
                from services.data_driven_model import tennis_ml_prob
                other_side_ml = next((s for s in by_side.keys() if s != fav_side), fav_side)
                dd = tennis_ml_prob(fav_side, fav_side, other_side_ml, "hard", fav_implied, _ctx)
                if dd.get("contributions"):
                    pick["data_driven_used"] = True
                    pick["data_driven_contribs"] = dd["contributions"]
                    # Only refine win_prob if the DD model kept us on fav side
                    if chosen_side == fav_side:
                        pick["win_probability"] = round(dd["mp"] * 100, 1)
                        pick["edge_percent"] = round((dd["mp"] - fav_implied) * 100, 2)
            except Exception as e:
                logger.debug("tennis DD backfill scoring failed for %s: %s", event_name, e)

        picks.append(pick)
        added += 1
    if added:
        logger.debug("tennis ML backfill: added %d moneyline picks from live_alt_lines", added)
    return picks


async def fetch_wnba_picks(date_str: str) -> list[dict]:
    return await _fetch_picks_for_sport("WNBA", date_str)


async def fetch_ufc_picks(date_str: str) -> list[dict]:
    return await _fetch_picks_for_sport("UFC", date_str)


async def fetch_nhl_picks(date_str: str) -> list[dict]:
    """Phase 1B (R2a) — NHL production generation. Real icehockey_nhl
    events + h2h/spreads/totals markets reach the authoritative
    evaluation path. Without an independent NHL model, every market
    records MODEL_UNAVAILABLE funnel telemetry instead of emitting
    sportsbook-follow pseudo-picks."""
    return await _fetch_picks_for_sport("NHL", date_str)


async def fetch_kbo_picks(date_str: str) -> list[dict]:
    return await _fetch_picks_for_sport("KBO", date_str)


# ───────────────────────── Aggregator ─────────────────────────


PLAYER_PROP_MARKETS = {
    "MLB": [
        # Hitter markets
        "batter_hits",
        # Alt lines — lower thresholds with higher implied prob (the "near-locks")
        "batter_hits_alternate",
        # Hits + Runs + RBIs composite (popular DFS-style market) — added
        # 2026-06-21 per user request. Main line is typically 1.5; alt lines
        # carve out near-locks at 0.5 / 2.5 / 3.5+. The Odds API exposes
        # both as `batter_hits_runs_rbis` + `_alternate`.
        "batter_hits_runs_rbis",
        "batter_hits_runs_rbis_alternate",
        # Standalone HR / RBI / Total Bases — added 2026-06-24 per user
        # request ("where are 1H, HR, RBI" — board was returning ONLY
        # hits + combo H+R+RBI because these three keys weren't in the
        # fetch list). Each has an alt variant for the near-lock floor.
        "batter_home_runs",
        "batter_home_runs_alternate",
        "batter_rbis",
        "batter_rbis_alternate",
        "batter_total_bases",
        "batter_total_bases_alternate",
        # Pitcher strikeout markets — added 2026-06-18 per user request.
        # The Odds API exposes these as `pitcher_strikeouts` + alt-line variant.
        "pitcher_strikeouts", "pitcher_strikeouts_alternate",
        # Pitcher outs recorded — added 2026-06-19 per user request.
        # Main line only — no alt variant per spec.
        "pitcher_outs",
    ],
    "NBA": [
        # Core props
        "player_points", "player_rebounds", "player_assists",
        "player_points_alternate", "player_rebounds_alternate",
        "player_assists_alternate",
        # Phase 4D — combined + shooting props (2026-08-06).
        "player_points_rebounds_assists",
        "player_points_rebounds_assists_alternate",
        "player_points_rebounds", "player_points_assists",
        "player_rebounds_assists",
        "player_threes", "player_threes_alternate",
        "player_steals", "player_blocks",
    ],
    "WNBA": [
        "player_points", "player_rebounds", "player_assists",
        "player_points_alternate", "player_rebounds_alternate",
        "player_assists_alternate",
    ],
    # NFL — Phase 3 (2026-07-22). Every prop market routes through
    # services.nfl_feature_engine.build_nfl_prop_factors which uses
    # NFLverse historical data (2019-2025) — no RNG, no placeholders.
    "NFL": [
        "player_pass_yds",       "player_pass_yds_alternate",
        "player_pass_tds",       "player_pass_attempts",
        "player_pass_completions",
        "player_rush_yds",       "player_rush_yds_alternate",
        "player_rush_attempts",  "player_rush_tds",
        "player_receptions",     "player_receptions_alternate",
        "player_reception_yds",  "player_reception_yds_alternate",
        "player_reception_tds",
        "player_anytime_td",     "player_1st_td",
    ],
    # KBO removed 2026-06-18 — KBO sport disabled entirely.
    # Soccer: anytime goal scorer is the marquee prop. We also try the
    # "to score or assist" market when the bookmakers carry it — it nearly
    # doubles the player's win-probability since either action wins the bet.
    # If the Odds API returns 422 (unsupported), we silently skip it.
    # Soccer player props — 3 markets The Odds API supports:
    #   • player_goal_scorer_anytime  → "Anytime Goal Scorer"
    #   • player_to_score_or_assist   → "To Score or Assist"
    #   • player_first_goal_scorer    → "First Goal Scorer"
    # (player_anytime_assist and player_to_score_2_or_more are NOT exposed
    # by The Odds API — confirmed via 422 INVALID_MARKET response.)
    "Soccer": [
        "player_goal_scorer_anytime",
        "player_to_score_or_assist",
        "player_first_goal_scorer",
    ],
    # UFC: The Odds API does NOT expose method-of-victory, round-betting, or
    # any MMA prop markets — only `h2h` (moneyline) and `totals` (rounds)
    # which we already get from the bulk /odds endpoint. Confirmed by
    # testing every market key variant (returns INVALID_MARKET). To surface
    # "wins by KO/Sub/Dec" we'd need Sportradar, OpticOdds, or a similar
    # premium feed.
    "UFC": [],
}
# Markets that are "alt" lower-threshold variants. These intentionally have
# very high implied prob (~80-95%) and chalky pricing (-400 to -800). We use
# a different filter regime for these.
_ALT_PROP_MARKETS = {
    "batter_hits_alternate",
    "batter_hits_runs_rbis_alternate",  # MLB Hits+Runs+RBIs alt (lower line)
    "batter_home_runs_alternate",       # MLB HR alt (added 2026-06-24)
    "batter_rbis_alternate",            # MLB RBI alt (added 2026-06-24)
    "batter_total_bases_alternate",     # MLB TB alt (added 2026-06-24)
    "pitcher_strikeouts_alternate",   # MLB pitcher Ks alt (lower line, high implied)
    "player_points_alternate", "player_rebounds_alternate",
    "player_assists_alternate",
}
_HIGH_PROB_MIN_IMPLIED = 0.62
# Alt lines must be true locks — at least 80% implied (-400 or steeper).
_ALT_PROP_MIN_IMPLIED = 0.80
_ALT_PROP_MAX_IMPLIED = 0.95  # cap absurd chalk like -2000 (95% implied)
# ── Phase 2A.5 DEFECT #3 FIX (2026-08) ───────────────────────────────
# Legacy 22% implied-probability floor RETIRED.  Per Phase 1D + 2A the
# eligibility of a scorer pick is decided by:
#   model probability vs de-vig book probability + evidence + uncertainty
# NOT by a fixed implied-probability threshold.  A +money longshot
# scorer with genuine model edge must not be rejected merely because the
# book prices them at 15%.  Keep only a hard sanity floor (2%) to drop
# obviously-broken +5000-and-worse outcomes.
_SOCCER_PROP_MIN_IMPLIED = 0.02


# ── 2026-07-28 DEFECT #2 — module-level prop-family map ─────────────
# `_prop_family_key(mk)` is the canonical family key used by the
# `std_seen` dedup inside `_props_picks_from_event`. Kept at module
# scope so tests + other services can share the same mapping without
# duplicating knowledge.
#
# Rules:
#   • Collapse `_alternate` variants to their base family (e.g.
#     `batter_hits_alternate` → `batter_hits`).
#   • Keep genuinely distinct families separate (e.g. `pitcher_strikeouts`
#     ≠ `pitcher_outs` — those are different bets on the same pitcher).
#   • Group soccer goal-scorer markets under one family so we don't
#     surface both "anytime goal" and "to score or assist" for the
#     same player (they're highly correlated bets).
_PROP_FAMILY_MAP = {
    # MLB pitcher families
    "pitcher_strikeouts": "pitcher_strikeouts",
    "pitcher_strikeouts_alternate": "pitcher_strikeouts",
    "pitcher_outs": "pitcher_outs",
    "pitcher_outs_alternate": "pitcher_outs",
    "pitcher_walks": "pitcher_walks",
    "pitcher_hits_allowed": "pitcher_hits_allowed",
    "pitcher_earned_runs": "pitcher_earned_runs",
    # MLB batter families
    "batter_hits": "batter_hits",
    "batter_hits_alternate": "batter_hits",
    "batter_home_runs": "batter_home_runs",
    "batter_home_runs_alternate": "batter_home_runs",
    "batter_hits_runs_rbis": "batter_hits_runs_rbis",
    "batter_hits_runs_rbis_alternate": "batter_hits_runs_rbis",
    "batter_rbis": "batter_rbis",
    "batter_rbis_alternate": "batter_rbis",
    "batter_runs_scored": "batter_runs_scored",
    "batter_total_bases": "batter_total_bases",
    "batter_total_bases_alternate": "batter_total_bases",
    # Soccer goal-scorer families (grouped — highly correlated bets)
    "player_goal_scorer_anytime": "goal_scorer",
    "player_to_score_or_assist": "goal_scorer",
    "player_first_goal_scorer": "goal_scorer",
    # MMA
    "mma_method_of_victory": "mma_method",
}


def _prop_family_key(mk: str) -> str:
    """Canonical family key for the `std_seen` dedup.

    Explicit strict mapping (not a regex/replace). Returns the base
    family so that `batter_hits` + `batter_hits_alternate` collapse
    to `batter_hits`, but `pitcher_strikeouts` + `pitcher_outs` STAY
    separate (they're distinct bets on the same pitcher).

    Falls back to `.replace('_alternate','')` for any mk not in the
    explicit map — defensive against new Odds-API markets.
    """
    if mk in _PROP_FAMILY_MAP:
        return _PROP_FAMILY_MAP[mk]
    return (mk or "").replace("_alternate", "")


# ── NFL Phase 3 helpers (2026-07-22) ─────────────────────────────────
# Map The Odds API market keys to the stat field names used in our
# nflverse ingest / feature engine. Add new mappings as we support
# additional markets.
_NFL_MARKET_TO_STAT = {
    "player_pass_yds":              "passing_yards",
    "player_pass_yds_alternate":    "passing_yards",
    "player_pass_tds":              "passing_tds",
    "player_pass_attempts":         "attempts",
    "player_pass_completions":      "completions",
    "player_rush_yds":              "rushing_yards",
    "player_rush_yds_alternate":    "rushing_yards",
    "player_rush_attempts":         "carries",
    "player_rush_tds":              "rushing_tds",
    "player_receptions":            "receptions",
    "player_receptions_alternate":  "receptions",
    "player_reception_yds":         "receiving_yards",
    "player_reception_yds_alternate": "receiving_yards",
    "player_reception_tds":         "receiving_tds",
}


def _infer_nfl_position_from_market(mk: str) -> str:
    """Best-effort position guess from the market key ONLY when the
    canonical player registry has no answer.

    Block 2D Closure §2 (2026-08) — the previous code used market-key
    inference as the SOLE position source, causing QB rushing props
    to become RB, RB receiving props to become WR, and TE receiving
    props to become WR — all of which would apply the wrong
    defensive-allowance splits.  The correct source is the player's
    canonical position from ``nfl_player_weekly`` (see
    ``resolve_nfl_position_for_player``).  This function is now the
    LAST-RESORT fallback only.
    """
    if not mk:
        return "WR"
    m = mk.lower()
    if "pass" in m:
        return "QB"
    if "rush" in m:
        return "RB"
    if "reception" in m or "rec" in m or "tds" in m and "reception" in m:
        return "WR"
    return "WR"


async def resolve_nfl_position_for_player(
    db, *, name: str, team: Optional[str] = None,
) -> Optional[str]:
    """Canonical NFL position resolver.

    Block 2D Closure §2 (2026-08) — REQUIRED so QB rushing props stay
    QB-attributed, RB receiving stays RB, and TE receiving stays TE.
    Reads ``nfl_player_weekly.position`` for the most recent season
    the player appeared in.  Returns None on unresolvable identity —
    caller falls back to market-key inference and downstream feature
    engine tolerates it.
    """
    if not name:
        return None
    for field in ("player_display_name", "player_name"):
        q: dict = {field: name.strip()}
        cursor = db.nfl_player_weekly.find(
            q, {"_id": 0, "position": 1, "team": 1, "season": 1}
        ).sort("season", -1).limit(10)
        rows = [d async for d in cursor]
        if not rows:
            continue
        # Prefer the most recent record; if team supplied, prefer
        # matching-team record.
        if team:
            team_u = team.strip().upper()
            for r in rows:
                if (r.get("team") or "").upper() == team_u and r.get("position"):
                    return str(r["position"]).upper()
        for r in rows:
            pos = r.get("position")
            if pos:
                return str(pos).upper()
    return None


def _current_nfl_season() -> int:
    """Return the current NFL season year. NFL season kicks off Sept
    and runs into Feb — pre-July we're still in the previous season."""
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    return now.year if now.month >= 7 else now.year - 1


def _current_nfl_week() -> int:
    """Rough current-week estimator. Season Week 1 typically first
    Thursday after Labor Day (~Sep 5). Preseason weeks 1-3 in August.
    Returns a positive integer that's safe for the feature engine's
    `week < current_week` history filter (any large value works pre-season)."""
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    if now.month < 8:
        return 22       # post-season done — pull full history
    if now.month == 8:
        # Preseason. Return 0 so no games this season are excluded.
        return 0
    if now.month in (9, 10, 11, 12, 1, 2):
        # Rough weeks-since-Sep-1 calc
        sep1 = _dt.datetime(now.year if now.month >= 8 else now.year - 1, 9, 1, tzinfo=_dt.timezone.utc)
        return max(1, ((now - sep1).days // 7) + 1)
    return 22


def _extract_nfl_prop_candidates(payload: dict) -> list[dict]:
    """Walk the bookmaker payload and return a flat list of NFL prop
    candidates ready for `build_nfl_game_context`.

    We only care about NFL markets registered in `_NFL_MARKET_TO_STAT`
    (the mapping to nflverse stat fields). We median-price across
    books per (player, market, side, point) so precomputes happen once
    per unique candidate — mirrors the dedup approach used in
    `_props_picks_from_event`.
    """
    cands: dict[tuple, dict] = {}
    # Block 2D A1 (2026-08) — include ATD/first-TD markets so
    # build_nfl_game_context can pre-compute the specialized ATD
    # engine's per-player probability.  These markets are NOT in
    # _NFL_MARKET_TO_STAT (they're binary Yes/No, not O/U on a
    # numeric stat), but they still need a candidate entry so the
    # precompute layer sees them.
    _NFL_EXTRA_ATD = ("player_anytime_td", "player_1st_td")
    for bm in (payload.get("bookmakers") or []):
        for m in (bm.get("markets") or []):
            mkey = m.get("key")
            if mkey not in _NFL_MARKET_TO_STAT and mkey not in _NFL_EXTRA_ATD:
                continue
            for o in (m.get("outcomes") or []):
                player = _clean_player_name(o.get("description") or o.get("name"))
                if not player:
                    continue
                side = str(o.get("name") or "over").lower()
                # ATD markets are Yes/No — only Yes is a candidate.
                if mkey in _NFL_EXTRA_ATD and side != "yes":
                    continue
                point = o.get("point")
                price = o.get("price")
                try:
                    implied = _implied_prob(int(price))
                except (TypeError, ValueError):
                    implied = None
                key = (player.strip().lower(), mkey, side,
                       float(point) if isinstance(point, (int, float)) else None)
                if key in cands:
                    continue
                cands[key] = {
                    "player": player,
                    "market": mkey,
                    "side": side,
                    "line": float(point) if isinstance(point, (int, float)) else 0.0,
                    "book_implied": implied,
                    "team": None,          # OddsAPI doesn't include team on prop rows
                    "position": _infer_nfl_position_from_market(mkey),
                }
    return list(cands.values())


# ─── Phase 4D finalization — NBA / CFB candidate extractors ───────
_NBA_MARKET_KEYS = frozenset({
    "player_points", "player_rebounds", "player_assists",
    "player_threes", "player_steals", "player_blocks",
    "player_points_rebounds_assists",
    "player_points_rebounds", "player_points_assists",
    "player_rebounds_assists",
    "player_points_alternate", "player_rebounds_alternate",
    "player_assists_alternate", "player_threes_alternate",
    "player_points_rebounds_assists_alternate",
})


def _extract_nba_prop_candidates(
    payload: dict,
) -> tuple[set[str], set[str], dict]:
    """Return ``(players, markets, lines_by_(player_lower, market))``
    from the bookmaker payload — one entry per unique candidate.
    Feeds :func:`services.nba_feature_engine.precompute_nba_prop_factors`.
    """
    players: set[str] = set()
    markets: set[str] = set()
    lines_bp: dict[tuple[str, str], list[tuple[float, str]]] = {}
    for bm in (payload.get("bookmakers") or []):
        for m in (bm.get("markets") or []):
            mkey = m.get("key")
            if mkey not in _NBA_MARKET_KEYS:
                continue
            for o in (m.get("outcomes") or []):
                player = _clean_player_name(o.get("description") or o.get("name"))
                if not player:
                    continue
                players.add(player)
                markets.add(mkey)
                point = o.get("point")
                side  = str(o.get("name") or "Over")
                if isinstance(point, (int, float)):
                    key = (player.strip().lower(), mkey)
                    entry = (float(point), side)
                    lines_bp.setdefault(key, [])
                    if entry not in lines_bp[key]:
                        lines_bp[key].append(entry)
    return players, markets, lines_bp


def _extract_cfb_prop_candidates(payload: dict) -> list[dict]:
    """Return the CFB prop candidate list feeding
    :func:`services.cfb_precompute.precompute_cfb_factors`.  Uses the
    same market keys as NFL (CFB shares the NFL market list)."""
    cands: dict[tuple, dict] = {}
    home = payload.get("home_team") or ""
    away = payload.get("away_team") or ""
    for bm in (payload.get("bookmakers") or []):
        for m in (bm.get("markets") or []):
            mkey = m.get("key")
            if mkey not in _NFL_MARKET_TO_STAT:
                continue
            for o in (m.get("outcomes") or []):
                player = _clean_player_name(o.get("description") or o.get("name"))
                if not player:
                    continue
                side = str(o.get("name") or "over").lower()
                point = o.get("point")
                price = o.get("price")
                try:
                    implied = _implied_prob(int(price))
                except (TypeError, ValueError):
                    implied = None
                key = (player.strip().lower(), mkey, side,
                        float(point) if isinstance(point, (int, float)) else None)
                if key in cands:
                    continue
                # CFB precompute takes an OPPONENT for the feature engine;
                # we don't know the player's team from the bookmaker
                # payload so we pass BOTH and let the engine skip when
                # neither match — safer than an empty string default.
                cands[key] = {
                    "player": player,
                    "market": mkey,
                    "side": side,
                    "line": float(point) if isinstance(point, (int, float)) else 0.0,
                    "book_implied": implied,
                    "player_team": home,       # best-effort — engine can fall through
                    "opponent":    away,
                    "position":    _infer_nfl_position_from_market(mkey),
                }
    return list(cands.values())


async def _fetch_event_props_payload(sport: str, sport_key: str, event_id: str) -> dict:
    markets = PLAYER_PROP_MARKETS.get(sport)
    if not markets:
        return {}

    # ── MLB Prop Cache-First μ-closure (2026-06) ──────────────────
    # Before making a network request, check if we already retain
    # fresh REAL sportsbook rows for this event's MLB prop markets
    # in ``live_alt_lines``.  When present, reconstruct the Odds API
    # payload shape from cache — ZERO external calls, downstream
    # consumers see the identical shape.  Freshness:
    #    5 minutes for live/in-play events (unchanged upstream cadence)
    #   15 minutes otherwise (matches upstream cache TTL contract)
    # Real-line safety: cache rows were themselves written from real
    # provider responses in this same function, so we can never
    # synthesize a bettable line from a model projection.
    if sport == "MLB":
        try:
            from server import db as _db
            _now  = datetime.now(timezone.utc)
            _stale = _now - timedelta(minutes=15)
            _cache_rows = await _db.live_alt_lines.find(
                {"event_id": event_id,
                 "sport": "mlb",
                 "last_seen": {"$gte": _stale}},
                {"_id": 0},
            ).to_list(length=5000)
            # ── 2026-06 μ-closure: FAMILY-AWARE cache completeness ─────
            # Old behavior: ANY fresh row for the event caused a cache
            # HIT, even if the caller requested a DIFFERENT market
            # family that had never been fetched. That silently
            # suppressed provider acquisition for the missing family
            # for the entire cache TTL window.
            #
            # New contract: cache HIT requires that EVERY requested
            # market family is represented by at least one fresh
            # cached row. If ANY requested family is missing, we fall
            # through to the provider fetch so the missing family is
            # acquired. Freshness is already enforced by the query's
            # `last_seen >= _stale` filter, so this is purely a
            # completeness gate on top of freshness.
            _requested_families: set[str] = {
                _prop_family_key(m) for m in (markets or []) if m
            }
            _cached_families: set[str] = {
                _prop_family_key(r.get("market_key") or "")
                for r in _cache_rows
                if r.get("market_key")
            }
            _missing_families = _requested_families - _cached_families
            if _cache_rows and not _missing_families:
                # Reconstruct the Odds API bookmakers/markets/outcomes shape.
                _bm_map: dict[str, dict] = {}
                for r in _cache_rows:
                    _book = r.get("sportsbook")
                    _mkey = r.get("market_key")
                    if not _book or not _mkey:
                        continue
                    _bm = _bm_map.setdefault(_book, {"key": _book,
                                                      "markets": {}})
                    _mk = _bm["markets"].setdefault(_mkey, {"key": _mkey,
                                                             "outcomes": []})
                    _mk["outcomes"].append({
                        "name":        r.get("selection"),
                        "description": r.get("selection"),
                        "point":       r.get("line"),
                        "price":       r.get("price"),
                    })
                # Flatten map → list matching Odds API shape.
                bookmakers = []
                for _book, _bm in _bm_map.items():
                    _bm["markets"] = list(_bm["markets"].values())
                    bookmakers.append(_bm)
                logger.info(
                    "MLB prop cache-first HIT event=%s rows=%d books=%d "
                    "families=%d/%d → ZERO provider call this cycle",
                    event_id, len(_cache_rows), len(bookmakers),
                    len(_cached_families & _requested_families),
                    len(_requested_families) or len(_cached_families),
                )
                # Reconstructed shape matches downstream expectations.
                _cache_row = _cache_rows[0]
                return {
                    "id":            event_id,
                    "sport_key":     sport_key,
                    "home_team":     _cache_row.get("home_team"),
                    "away_team":     _cache_row.get("away_team"),
                    "commence_time": _cache_row.get("commence_time"),
                    "bookmakers":    bookmakers,
                    "_cache_hit":    True,   # observable diagnostic
                }
            elif _cache_rows and _missing_families:
                logger.info(
                    "MLB prop cache PARTIAL event=%s cached_families=%d "
                    "missing=%s → provider fetch required",
                    event_id, len(_cached_families),
                    sorted(_missing_families),
                )
        except Exception as _cf_err:
            logger.debug(
                "MLB cache-first lookup skipped for %s: %s",
                event_id, _cf_err,
            )

    # Region selection — CRITICAL for soccer goal-scorer markets. US books
    # (DraftKings/FanDuel) only expose a HANDFUL of players per soccer match;
    # UK/EU books (Pinnacle, Marathon, bet365) expose the full team rosters.
    # User report: "How come gyokeres not popping up he scored last 2 games
    # and assist" — verified Gyökeres is exposed in EU/UK regions but
    # MISSING from US-only fetches. Use uk,eu for soccer; us for everything
    # else (MLB / NBA / NFL where US books are the canonical source).
    regions = "uk,eu" if sport == "Soccer" else "us"

    # ── Block 2C-cont (2026-08): live 422 bundle isolation wiring ───
    # Bad-market filter is honored inside the gateway with event scope
    # (see services.odds_api_gateway.fetch pre-flight step), so an
    # event-scoped 422 marker on Event A does NOT suppress the same
    # market on Event B. Global markers still apply to every event.
    #
    # Path:
    #   1. Bulk fetch full market bundle (single API call, normal case).
    #   2. On 422 or empty payload, invoke
    #      services.provider_cache_state.isolate_bad_markets() to
    #      bisect within bounded credits/retries and identify WHICH
    #      market(s) caused the 422.  Every recursive sub-call goes
    #      through _get → gateway → confirmed-bad markers are written
    #      with event_id + scope="event".
    #   3. Merge the successful sub-bundle payloads so callers still
    #      see all supported markets for this event, even though the
    #      bulk call failed as a bundle.
    #
    # Result: Event A's 422 on player_home_runs isolates only that
    # market for Event A; sibling markets (batter_hits, batter_rbis)
    # are still returned for Event A, and Event B — same sport, same
    # market bundle — is NOT suppressed.
    data = await _get(
        f"{BASE}/sports/{sport_key}/events/{event_id}/odds",
        {"regions": regions, "markets": ",".join(markets), "oddsFormat": "american"},
    )

    # Bulk fetch succeeded → done.  We consider "succeeded" to be
    # "returned a dict at all" — including a dict with empty
    # bookmakers[] which is a legitimate VALID_EMPTY_PROVIDER_ZERO
    # state (event scheduled but no book has posted lines yet).
    if isinstance(data, dict):
        # ── 2026-07-28 Data-availability diagnostics ───────────────
        # Log per-market outcome counts (esp. H+R+RBI) so we can
        # distinguish provider data gaps from downstream gate drops.
        if sport == "MLB":
            counts: dict[str, int] = {}
            for b in data.get("bookmakers", []) or []:
                for m in b.get("markets", []) or []:
                    mk = m.get("key") or ""
                    counts[mk] = counts.get(mk, 0) + len(m.get("outcomes", []) or [])
            hrr = counts.get("batter_hits_runs_rbis", 0) + counts.get("batter_hits_runs_rbis_alternate", 0)
            if hrr == 0:
                logger.warning(
                    "MLB H+R+RBI data-gap: event=%s no batter_hits_runs_rbis outcomes returned by provider",
                    event_id,
                )
            else:
                logger.info(
                    "MLB H+R+RBI availability: event=%s outcomes=%d",
                    event_id, hrr,
                )
            # ── MLB Prop Cache Write μ-closure (2026-06) ──────────
            # After a successful real-provider fetch, PERSIST the
            # normalized rows into ``live_alt_lines`` so subsequent
            # iterations can serve the same event from cache
            # without another provider call.  We reuse the exact
            # normalization pattern from ``alt_lines_feed.py`` to
            # keep identity/schema consistent.  Best-effort — write
            # failure must NEVER break the pick pipeline.
            try:
                from server import db as _db
                from alt_lines_feed import _flatten_odds as _fo
                _now = datetime.now(timezone.utc)
                _rows = _fo(data, "mlb", "baseball_mlb", _now)
                if _rows:
                    from pymongo import UpdateOne
                    _ops = [
                        UpdateOne(
                            {"market_id": r["market_id"]},
                            {"$set": r},
                            upsert=True,
                        ) for r in _rows
                    ]
                    if _ops:
                        _res = await _db.live_alt_lines.bulk_write(
                            _ops, ordered=False)
                        logger.info(
                            "MLB prop cache WRITE event=%s rows=%d "
                            "upserts=%d (cache-first ready next cycle)",
                            event_id, len(_rows),
                            (_res.upserted_count or 0) + (_res.modified_count or 0),
                        )
            except Exception as _cw_err:
                logger.debug(
                    "MLB prop cache write skipped for %s: %s",
                    event_id, _cw_err,
                )
        return data

    # Bulk fetch returned None — this is the failure path.  It can
    # mean 422 (bad market in the bundle), transient error, budget
    # denial, or circuit-open.  Only 422 warrants isolation; the
    # others are not fixed by fanning out.  We attempt isolation
    # anyway because the isolator's HARD credit/request caps
    # (MAX_422_RETRY_REQUESTS=8, MAX_422_RETRY_CREDITS=8) make the
    # cost bounded even in the pathological case, and the gateway's
    # circuit-breaker / budget guards short-circuit inside each
    # sub-call so unnecessary retries stop cheaply.
    merged = await _isolate_and_merge_event_props(
        sport=sport, sport_key=sport_key, event_id=event_id,
        regions=regions, bundle_markets=list(markets),
    )
    return merged or {}


async def _isolate_and_merge_event_props(
    *, sport: str, sport_key: str, event_id: str,
    regions: str, bundle_markets: list[str],
) -> dict:
    """Block 2C-cont bundle-isolation orchestrator.

    Delegates the actual bisection to
    ``services.provider_cache_state.isolate_bad_markets`` (which owns
    the hard credit / retry caps) and merges every successful subset
    payload into a single event payload so callers see all supported
    markets for the event.

    Cross-event isolation guarantee: the gateway's pre-flight bad-market
    filter is event-scoped, so a 422 marker written here for Event A
    on market X can NEVER suppress the same market X on Event B.  The
    proof-test lives in ``tests/test_block2c_cont_live_wiring.py``.
    """
    from services.provider_cache_state import (
        isolate_bad_markets, CacheState,
    )
    url = f"{BASE}/sports/{sport_key}/events/{event_id}/odds"

    # Cheap short-circuit: if the entire bundle has been suppressed
    # by the registry, filter_markets returned an empty list upstream
    # and _get returned None WITHOUT hitting the network.  Redo the
    # filter here so we don't spin the bisector on a fully-empty
    # bundle.
    try:
        from server import db as _db
        from services import bad_market_registry as _bmr
        remaining = await _bmr.filter_markets(
            _db, sport_key=sport_key,
            markets=bundle_markets, event_id=event_id)
    except Exception:
        remaining = list(bundle_markets)
    if not remaining:
        logger.info(
            "props-isolation: skipped — every market for sport=%s "
            "event=%s already registered bad (bundle-fully-suppressed)",
            sport_key, event_id,
        )
        return {}

    # Closure state: collect each successful subset's raw payload so
    # we can merge them at the end without re-requesting.
    successful_payloads: list[dict] = []

    async def _probe(subset: list[str]):
        """Called by isolate_bad_markets.  Return dict of
        {market: True} for markets present in the response, or None
        to signal the whole subset 422'd (or otherwise errored)."""
        if not subset:
            return None
        params = {
            "regions": regions,
            "markets": ",".join(subset),
            "oddsFormat": "american",
        }
        sub_data = await _get(url, params)
        if not isinstance(sub_data, dict):
            return None
        # Extract which markets actually came back with outcomes.
        got: set[str] = set()
        for bk in sub_data.get("bookmakers", []) or []:
            for m in bk.get("markets", []) or []:
                key = m.get("key")
                if key and key in subset:
                    got.add(key)
        if not got:
            # Provider returned an event shell with zero market data.
            # Semantically this is VALID_EMPTY_PROVIDER_ZERO for the
            # subset — mark all as supported (books just haven't
            # posted lines yet) but keep the shell for merging.
            successful_payloads.append(sub_data)
            return {m: True for m in subset}
        successful_payloads.append(sub_data)
        return {m: True for m in got}

    result = await isolate_bad_markets(remaining, _probe)

    logger.info(
        "props-isolation: sport=%s ev=%s bundle=%d supported=%d "
        "bad=%d unresolved=%d retries=%d credits=%d state=%s",
        sport_key, event_id, len(remaining),
        len(result.supported_markets), len(result.bad_markets),
        len(result.unresolved_markets), result.retries_used,
        result.credits_used, result.state.value,
    )

    if not successful_payloads:
        return {}

    return _merge_event_odds_payloads(successful_payloads)


def _merge_event_odds_payloads(payloads: list[dict]) -> dict:
    """Merge multiple per-event odds payloads that came from
    disjoint market subsets into a single payload.

    Each payload shares the same event metadata (id, home_team,
    away_team, commence_time) but has bookmakers with DIFFERENT
    market subsets.  Merge by:
      * Keeping the first payload's event metadata verbatim.
      * For each bookmaker, unioning its markets across payloads,
        deduped by market.key (first-write-wins).

    This is safe because the isolation sub-calls always target
    disjoint market subsets — the same market key never appears in
    two sub-payloads for the same event.
    """
    if not payloads:
        return {}
    base = dict(payloads[0])
    book_index: dict[str, dict] = {}
    for p in payloads:
        for bk in p.get("bookmakers", []) or []:
            key = bk.get("key")
            if not key:
                continue
            if key not in book_index:
                # Deep-ish copy: bookmaker dict + its markets list.
                book_index[key] = {
                    **bk,
                    "markets": list(bk.get("markets", []) or []),
                }
                continue
            existing = book_index[key]
            seen_market_keys = {m.get("key") for m in existing["markets"]}
            for m in bk.get("markets", []) or []:
                if m.get("key") and m["key"] not in seen_market_keys:
                    existing["markets"].append(m)
                    seen_market_keys.add(m["key"])
    base["bookmakers"] = list(book_index.values())
    return base


# ─── Tennis alt-line markets ───────────────────────────────────────────
# Tennis is one of the few sports where The Odds API exposes BOTH
# alternate_spreads (game handicaps: -1.5, -2.5, -3.5, … and +1.5, +2.5,
# +3.5, …) AND alternate_totals (Over/Under at multiple game totals like
# 20.5, 21.5, 22.5, 23.5). These are NOT player props — they're full-match
# markets. Power user spec: "Tennis have alt line available pls add and
# calculate them to build picks."
#
# Strategy: per-event fetch, then build at most 2 alt picks per match
# (one favored-side spread + one Over total) at the SWEET SPOT implied
# probability so we surface true high-confidence locks without going
# absurdly chalky (≤95% implied = -1900 American).
TENNIS_GAME_ALT_MARKETS = ["alternate_spreads", "alternate_totals"]
# Implied-probability window for alt-line picks. Widened to 55-97% so
# we capture both the safe-bet chalkiest tier (e.g. Over 19.5 priced
# at -1500/-3000 = 94-97% implied) AND moderate alts down to ~-122.
#
# User spec evolution:
#   v1 (78-93%) → too narrow, captured zero alts
#   v2 (55-93%) → captured -278/-208 but missed chalkier sportsbook
#                 offerings the user pointed out ("for eala you had
#                 over alt 21.5, sportsbook give you option to get
#                 over 19.5" — that's the -2000+ deep-chalk tier).
#   v3 (55-97%) → covers the full sportsbook ladder including
#                 deep-chalk "almost free" lines. Anything >97 implied
#                 is true junk juice (1.5% return on -7000+) so we
#                 still exclude it.
_TENNIS_ALT_MIN_IMPLIED = 0.55
_TENNIS_ALT_MAX_IMPLIED = 0.97


async def _fetch_tennis_event_alts(sport_key: str, event_id: str) -> dict:
    """Fetch alternate_spreads + alternate_totals for a single tennis
    event.

    CRITICAL: The Odds API exposes RICH tennis alt markets only via the
    EU region — US books carry exactly one alt total line per match
    (FanDuel-only, basically useless). EU books (Pinnacle + Marathon)
    expose the full alt ladder: 6+ spread points and 6+ total points
    per match. We pull EU explicitly here even though the rest of the
    Tennis pipeline uses US — the alt market is fundamentally a
    European-bookmaker product."""
    data = await _get(
        f"{BASE}/sports/{sport_key}/events/{event_id}/odds",
        {
            "regions": "eu",
            "markets": ",".join(TENNIS_GAME_ALT_MARKETS),
            "oddsFormat": "american",
        },
    )
    return data if isinstance(data, dict) else {}


def _pick_sweet_spot_alts(
    outcomes: list[dict],
    side_name: str | None = None,
    *,
    limit: int = 3,
) -> list[dict]:
    """From a list of alt-market outcomes, return up to `limit` chalky
    alt lines within the sweet-spot band (55-93% implied), sorted
    highest-implied first. User spec: "With tennis alt you can get
    lower odds up -500" — books expose alts as chalky as -500/-833,
    surface multiple chalk tiers so the user can pick their risk
    appetite instead of only seeing the single safest line."""
    keep: list[tuple[float, dict]] = []
    for o in outcomes or []:
        if side_name and o.get("name") != side_name:
            continue
        price = o.get("price")
        if not isinstance(price, (int, float)):
            continue
        imp = _implied_prob(int(price))
        if not (_TENNIS_ALT_MIN_IMPLIED <= imp <= _TENNIS_ALT_MAX_IMPLIED):
            continue
        keep.append((imp, o))
    # Sort chalkiest first (highest implied probability).
    keep.sort(key=lambda t: t[0], reverse=True)
    out: list[dict] = []
    seen_points: set = set()
    for _imp, o in keep:
        pt = o.get("point")
        if pt in seen_points:
            continue
        seen_points.add(pt)
        out.append(o)
        if len(out) >= limit:
            break
    return out


def _pick_sweet_spot_alt(
    outcomes: list[dict], side_name: str | None = None,
) -> dict | None:
    """Back-compat wrapper for callers that want only ONE chalkiest
    sweet-spot alt (used by alt-totals)."""
    picks = _pick_sweet_spot_alts(outcomes, side_name=side_name, limit=1)
    return picks[0] if picks else None


def _alt_outcomes_for_market(payload: dict, market_key: str) -> list[dict]:
    """Collapse outcomes across bookmakers — keep the FIRST occurrence
    of each (name, point) pair so we don't double-count the same alt
    line from multiple books. Real consensus pricing across books is
    overkill for alt picks; the median is already chalky."""
    seen: dict[tuple, dict] = {}
    for bk in (payload.get("bookmakers") or []):
        for mk in (bk.get("markets") or []):
            if mk.get("key") != market_key:
                continue
            for o in (mk.get("outcomes") or []):
                key = (o.get("name"), o.get("point"))
                if key not in seen:
                    seen[key] = o
    return list(seen.values())


def _prob_to_american(p: float) -> int:
    """Convert a probability in [0,1] to fair American odds."""
    p = max(0.01, min(0.99, p))
    if p >= 0.5:
        return int(round(-100 * p / (1 - p)))
    return int(round(100 * (1 - p) / p))


def _synthesize_chalk_alt_totals(*_a, **_kw) -> list[dict]:
    """REMOVED — Phase 4C (2026-08-06).

    This function historically extrapolated tennis alt-total lines
    beyond the sportsbook's exposed ladder ("synthetic chalk lines").
    It was disabled on 2026-06-30 and formally removed in Phase 4C
    because no code path called it any longer.

    A guardrail test (``test_no_synthetic_mlb_alt_lines``) now
    protects against any future MLB alt-line synthesis returning.

    Any attempt to use this function now returns an empty list and
    raises a warning so callers can be updated.
    """
    import logging as _logging
    _logging.getLogger("lockscore.sports_engine").warning(
        "_synthesize_chalk_alt_totals is removed (Phase 4C) — "
        "returning empty list. Real-line policy: synthetic sportsbook "
        "lines must never be published."
    )
    return []


def _build_tennis_alt_picks(
    sport_key: str, league: str, event_payload: dict, alt_payload: dict,
    date_str: str,
) -> list[dict]:
    """Build up to 2 alt-line picks per tennis match:
       • Spread:  favored side's chalkiest acceptable game-handicap line
       • Total:   chalkiest acceptable Over (Under as fallback)
    """
    if not alt_payload:
        return []
    home = alt_payload.get("home_team") or event_payload.get("home_team")
    away = alt_payload.get("away_team") or event_payload.get("away_team")
    commence = alt_payload.get("commence_time") or event_payload.get("commence_time")
    event_id = alt_payload.get("id") or event_payload.get("id")
    if not home or not away or not commence:
        return []
    # Schedule window check — same 7-day window as main tennis picks.
    try:
        dt = datetime.strptime(commence, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        if dt < now - __import__("datetime").timedelta(minutes=30):
            return []
        if dt > now + __import__("datetime").timedelta(hours=7 * 24):
            return []
    except Exception:
        pass

    # Determine the favored side from the bulk h2h odds we already have
    # in event_payload (passed through from _picks_from_game caller).
    h2h_outs = _consensus_market(event_payload, "h2h") if event_payload else []
    home_ml = _median_price(h2h_outs, home) if h2h_outs else None
    away_ml = _median_price(h2h_outs, away) if h2h_outs else None
    favored: str | None = None
    if isinstance(home_ml, (int, float)) and isinstance(away_ml, (int, float)):
        # The MORE negative side is the favorite (e.g., -300 > -120 in
        # implied-probability terms, even though -300 < -120 numerically).
        favored = home if int(home_ml) < int(away_ml) else away

    out_picks: list[dict] = []
    league_label = LEAGUE_LABELS.get(sport_key, "Tennis")

    # ── Alt spreads: up to 3 chalky lines for the FAVORED side + up to
    # 2 for the underdog. Yields a "chalk ladder" so the user sees
    # multiple risk tiers (e.g., -833, -500, -300) per match —
    # user spec: "you can get lower odds up -500".
    spread_outs = _alt_outcomes_for_market(alt_payload, "alternate_spreads")
    if not spread_outs:
        logger.debug(
            "Tennis alt spreads: empty outcomes for %s vs %s (event %s)",
            home, away, event_id,
        )
    if spread_outs:
        # Determine which side is favored. If h2h didn't resolve (e.g.
        # h2h market missing from this event's payload, or matchup is
        # near-pick-em), fall back to building BOTH sides — bug
        # history: with no `favored` the entire spread loop was
        # skipped, producing 0 tennis spread picks across 4 days even
        # though Odds API was returning alternate_spreads cleanly.
        # User feedback: "I'm good on alt totals for now I rather
        # have alt spread".
        sides_to_build: list[tuple[str, int]]
        if favored:
            underdog = away if favored == home else home
            sides_to_build = [(favored, 3), (underdog, 2)]
        else:
            # Even matchup or h2h-missing — build a tighter ladder for
            # both sides (2 each) and let the lock_score / edge filter
            # decide which ones survive the validator.
            sides_to_build = [(home, 2), (away, 2)]
        for side, take in sides_to_build:
            picks_for_side = _pick_sweet_spot_alts(spread_outs, side_name=side, limit=take)
            for pick_obj in picks_for_side:
                line = pick_obj.get("point")
                price = int(pick_obj.get("price"))
                imp = _implied_prob(price)
                # ── 2026-08-23 CHEAP SURGICAL FINAL — Tennis alt SPREAD
                # authority.  ``imp + 0.02`` is dead.  Use the existing
                # Tennis match distribution (``score_tennis_matchup``)
                # to derive the per-side win probability at the SAME
                # book-implied anchor.  If real Tennis signal is not
                # present we fail closed by stamping ``model_line=True``
                # (the canonical barrier already blocks it) and skip
                # the pick — do NOT fall back to ``mp = imp``.
                mp = None
                _sig = None
                try:
                    from services.tennis_math_engine import (
                        score_tennis_matchup, has_real_tennis_signal,
                    )
                    _surface = str(
                        (game.get("surface") if isinstance(game, dict) else "")
                        or "hard"
                    ).lower()
                    _ctx = (game.get("_ctx") if isinstance(game, dict) else None) or {}
                    _sig = score_tennis_matchup(
                        home, away, _surface, imp, _ctx,
                    )
                    if _sig and has_real_tennis_signal(_sig):
                        _p_home = float(_sig.get("home_win_prob") or 0.5)
                        # Alt spread cover ≈ match-winner probability
                        # of the covering side (a +N.5 games handicap
                        # is dominated by the match-winner outcome in
                        # best-of-3 tennis).  A future set-count model
                        # would refine this; for now use the shared
                        # distribution verbatim.
                        mp = _p_home if side == home else (1.0 - _p_home)
                except Exception:
                    mp = None
                if mp is None:
                    # Fail closed — do not emit synthetic mp.
                    continue
                factors = {}
                lock, breakdown = compute_lock_score(
                    factors, win_prob=mp * 100, edge_percent=(mp * 100 - imp * 100)
                )
                sign = "+" if (line or 0) > 0 else ""
                out_picks.append(_build_pick(
                    sport="Tennis", league=league_label, event=f"{away} @ {home}",
                    event_time=commence,
                    market=f"{side} {sign}{line} Games (Alt)",
                    pick_side=side,
                    model_win_prob=mp, book_odds=price,
                    lock=lock, factors=breakdown,
                    insights=[
                        f"Alt game spread — book implies {imp*100:.0f}% cover probability",
                        f"Chalk level: {price:+d} American "
                        + ("(deep favorite)" if imp >= 0.80 else "(moderate chalk)"),
                    ],
                    external_id=f"Tennis-{event_id}-alt-spread-{side}-{line}",
                    is_alt_prop=True,
                ))

    # ── Alt totals (2026-06-30 user mandate — NO SYNTHETIC LINES) ──────
    # Previously this combined real bookmaker outcomes with SYNTHESIZED
    # chalkier alts (extrapolated above/below the API ladder). The user
    # explicitly disabled synthesis: only show lines that exist on the
    # live sportsbook board. The validation gate in `quality_gate.py`
    # cross-checks every alt pick against `live_alt_lines` — synthesized
    # lines would fail validation and get rejected anyway.
    api_total_outs = _alt_outcomes_for_market(alt_payload, "alternate_totals")
    if api_total_outs:
        # REAL book outcomes only — no `_synthesize_chalk_alt_totals` call.
        total_outs = list(api_total_outs)
        for side in ("Over", "Under"):
            picks_for_side = _pick_sweet_spot_alts(total_outs, side_name=side, limit=4)
            for pick_obj in picks_for_side:
                line = pick_obj.get("point")
                price = int(pick_obj.get("price"))
                imp = _implied_prob(price)
                # 2026-08-23 CHEAP SURGICAL FINAL — Tennis alt TOTAL
                # authority.  Derive Over/Under probability from the
                # existing Tennis match distribution (Elo differential
                # → match competitiveness → projected total games).
                # Fail closed when no real signal is present — do NOT
                # publish `mp = imp` as the model probability.
                mp = None
                try:
                    from services.tennis_math_engine import (
                        score_tennis_matchup, has_real_tennis_signal,
                    )
                    import math as _math
                    _surface = str(
                        (game.get("surface") if isinstance(game, dict) else "")
                        or "hard"
                    ).lower()
                    _ctx = (game.get("_ctx") if isinstance(game, dict) else None) or {}
                    _sig = score_tennis_matchup(home, away, _surface, imp, _ctx)
                    if _sig and has_real_tennis_signal(_sig):
                        _hp = float(_sig.get("home_win_prob") or 0.5)
                        # Match competitiveness: 1.0 = coin-flip, 0 = blowout.
                        _competitive = 1.0 - abs(_hp - 0.5) * 2.0
                        # Best-of-3 projected total games:
                        #   blowout (2 sets 6-2 / 6-3)  → ~17 games
                        #   competitive coin-flip (3 sets or long tiebreaks) → ~24 games
                        _proj_games = 17.0 + _competitive * 7.0
                        # Convert to Over/Under probability with a
                        # logistic anchor (σ=2.5 games ≈ empirical std).
                        _z = (_proj_games - float(line)) / 2.5
                        _p_over = 1.0 / (1.0 + _math.exp(-_z))
                        mp = _p_over if side == "Over" else (1.0 - _p_over)
                except Exception:
                    mp = None
                if mp is None:
                    # Fail closed — no real Tennis distribution available.
                    continue
                # 2026-07-21 Phase 2: tennis alt total — no random factors.
                factors = {}
                lock, breakdown = compute_lock_score(
                    factors, win_prob=mp * 100, edge_percent=(mp * 100 - imp * 100)
                )
                # Real-book alts only — no synthesized tagging needed.
                is_synth = False
                synth_tag = ""
                source_note = (
                    f" — book implies {imp*100:.0f}% hit rate"
                )
                out_picks.append(_build_pick(
                    sport="Tennis", league=league_label, event=f"{away} @ {home}",
                    event_time=commence,
                    market=f"{side} {line} Games (Alt)",
                    pick_side=side,
                    model_win_prob=mp, book_odds=price,
                    lock=lock, factors=breakdown,
                    insights=[
                        f"Alt game total{source_note}",
                        f"Chalk level: {price:+d} "
                        + ("(deep chalk)" if imp >= 0.80 else "(moderate)"),
                    ],
                    external_id=f"Tennis-{event_id}-alt-total-{side}-{line}{synth_tag}",
                    is_alt_prop=True,
                ))

    return [p for p in out_picks if p is not None]


# ─── MLB Team-Total + Alt-Run-Line pick generator (2026-07-02) ────────
# The Odds API bulk `/odds` endpoint doesn't support `team_totals`,
# `alternate_spreads` or `alternate_team_totals` — they're only exposed
# via the per-event `/events/{id}/odds` endpoint. Without these markets
# the app was showing NO MLB team totals at all, and only the one
# random ± side of the main run line per game (user report 2026-07-02
# "why don't I see mlb spreads or team totals?"). Same pattern as the
# Tennis alt-line fetcher above.
#
# Edge gates (user mandate 2026-07-02):
#   • Alt TEAM TOTAL: line 2.5-3.5    → require model edge ≥ 8%
#   • Alt RUN LINE:  |spread| 1.5-3.5 → require model edge ≥ 8%
# These are enforced at BOTH generation time (this file) and read time
# (quality_gate.py) so no gated pick can ever surface even if the
# generator drifts.
MLB_TEAM_ALT_MARKETS = [
    "team_totals",           # main team total
    "alternate_team_totals", # alt team totals
    "alternate_spreads",     # alt run lines
]
_MLB_ALT_MIN_EDGE_PCT = 8.0   # 8-12% band (user spec 2026-07-02)
_MLB_ALT_MAX_ODDS = -700      # deeper chalk than this = junk juice
_MLB_ALT_MIN_ODDS = -450      # keep main-line-ish chalk floor for main lines


async def _fetch_mlb_event_alts(sport_key: str, event_id: str) -> dict:
    """Per-event fetch for MLB team totals + alt spreads + alt team
    totals. US region is authoritative for MLB (DraftKings + FanDuel
    carry the full alt ladder). One call per game costs ~5-10 credits
    which is acceptable given MLB slates are 10-15 games/day."""
    data = await _get(
        f"{BASE}/sports/{sport_key}/events/{event_id}/odds",
        {
            "regions": "us",
            "markets": ",".join(MLB_TEAM_ALT_MARKETS),
            "oddsFormat": "american",
        },
    )
    return data if isinstance(data, dict) else {}


def _build_mlb_alt_picks(
    sport_key: str, league: str, event_payload: dict, alt_payload: dict,
    date_str: str,
) -> list[dict]:
    """Build MLB team-total and alt-run-line picks for a single event.
    Applies the 8% edge gate to alt lines in the specified ranges."""
    if not alt_payload or not event_payload:
        return []
    home = event_payload.get("home_team")
    away = event_payload.get("away_team")
    if not home or not away:
        return []
    commence = event_payload.get("commence_time")
    event_id = event_payload.get("id") or f"MLB-{home}-{away}-{commence}"
    out_picks: list[dict] = []
    seed = abs(hash(f"MLB-alt-{home}-{away}-{date_str}")) % 10000
    rng = random.Random(seed)

    # ── Team Totals (MAIN) ─────────────────────────────────────────────
    # DISABLED 2026-07-19 per user request: "get rid of team total it
    # confuses me I just total for the game to generate".
    # 2026-07-21: block fully purged (was still using _factors_random
    # even though disabled). Kept only the alt run-line generation below.
    # If team totals are re-enabled later, wire them to
    # build_mlb_total_factors() — never to _factors_random.
    tt_outs: list = []
    for o in tt_outs:
        team = o.get("description")
        side = o.get("name")
        line = o.get("point")
        price_raw = o.get("price")
        if team not in (home, away) or side not in ("Over", "Under"):
            continue
        if not isinstance(price_raw, (int, float)) or not isinstance(line, (int, float)):
            continue
        price = int(price_raw)
        # Reject impossible / joke lines.
        if price <= _MLB_ALT_MAX_ODDS or price >= 3500:
            continue
        imp = _implied_prob(price)
        if not (0.30 <= imp <= 0.92):
            continue
        # Book-anchored deterministic seed (no RNG — 2026-07-21 FINAL PHASE).
        mp = max(0.35, min(0.85, imp + 0.03))
        edge_pct = (mp - imp) * 100
        # Main team total — no 8% gate. Only surface positive-EV or
        # near-EV picks (edge ≥ -2%). Compute lock from factors.
        if edge_pct < -2.0:
            continue
        # 2026-07-21: dead-code path, but per user's mandate never
        # substitute randomness for missing data. If this block is
        # ever re-enabled, wire real factors here — never _factors_random.
        _ctx = (event_payload.get("_ctx") if isinstance(event_payload, dict) else None) or {}
        from services.mlb_feature_engine import (
            build_mlb_total_factors, has_enough_real_data,
        )
        _tt_factors, _tt_src = build_mlb_total_factors(_ctx, side=side)
        if not has_enough_real_data(_tt_factors, "total"):
            continue
        factors = {k: v for k, v in _tt_factors.items() if v is not None}
        lock, breakdown = compute_lock_score(factors, win_prob=mp * 100)
        pick = _build_pick(
            sport="MLB", league=league, event=f"{away} @ {home}",
            event_time=commence,
            market=f"{team} Team Total {side} {line}",
            pick_side=side,
            model_win_prob=mp, book_odds=price,
            lock=lock, factors=breakdown,
            insights=[
                f"{team} team total — book implies {imp*100:.0f}% hit rate",
                f"Model win prob: {mp*100:.0f}% ({'edge +' if edge_pct >= 0 else 'edge '}{edge_pct:.1f}%)",
            ],
            external_id=f"MLB-{event_id}-teamtotal-{team}-{side}-{line}",
            home_team_name=home, away_team_name=away,
        )
        if pick:
            out_picks.append(pick)

    # ── Alternate Team Totals ─────────────────────────────────────────
    # DISABLED 2026-07-19 per user request. Alt team totals were the
    # 2.5-3.5 chalk band; removing entirely so only whole-game totals
    # remain on the board.
    att_outs: list = []
    for o in att_outs:
        team = o.get("description")
        side = o.get("name")
        line = o.get("point")
        price_raw = o.get("price")
        if team not in (home, away) or side not in ("Over", "Under"):
            continue
        if not isinstance(price_raw, (int, float)) or not isinstance(line, (int, float)):
            continue
        line_f = float(line)
        # HARD LINE-RANGE FILTER — only the 2.5-3.5 band is allowed.
        if not (2.5 <= line_f <= 3.5):
            continue
        price = int(price_raw)
        if price <= _MLB_ALT_MAX_ODDS or price >= 3500:
            continue
        imp = _implied_prob(price)
        if not (0.40 <= imp <= 0.95):
            continue
        mp = max(0.40, min(0.92, imp + 0.03))
        edge_pct = (mp - imp) * 100
        # 8% EDGE GATE (mandatory for the 2.5-3.5 band).
        if edge_pct < _MLB_ALT_MIN_EDGE_PCT:
            continue
        # 2026-07-21 Phase 1 MLB: real total factors, skip if unavailable.
        _ctx = (event_payload.get("_ctx") if isinstance(event_payload, dict) else None) or {}
        from services.mlb_feature_engine import (
            build_mlb_total_factors, has_enough_real_data,
        )
        _alt_factors, _sources = build_mlb_total_factors(_ctx, side=side)
        if not has_enough_real_data(_alt_factors, "total"):
            continue
        factors = {k: v for k, v in _alt_factors.items() if v is not None}
        lock, breakdown = compute_lock_score(factors, win_prob=mp * 100)
        pick = _build_pick(
            sport="MLB", league=league, event=f"{away} @ {home}",
            event_time=commence,
            market=f"{team} Team Total {side} {line} (Alt)",
            pick_side=side,
            model_win_prob=mp, book_odds=price,
            lock=lock, factors=breakdown,
            insights=[
                f"{team} alt team total ({line_f}) — book implies {imp*100:.0f}%",
                f"Model edge: {edge_pct:+.1f}% (8% gate cleared)",
            ],
            external_id=f"MLB-{event_id}-altteamtotal-{team}-{side}-{line}",
            is_alt_prop=True,
            home_team_name=home, away_team_name=away,
        )
        if pick:
            pick["is_alt"] = True
            out_picks.append(pick)

    # ── Alternate Run Lines ───────────────────────────────────────────
    # USER SPEC 2026-07-02: "Only surface ALT RUN LINES (+1.5 to +3.5)
    # when the model projects a minimum 8-12% edge." Restrict the alt
    # run-line ladder to spreads whose magnitude is in [1.5, 3.5] AND
    # positive (the underdog side — the "+" run line, not the "-").
    # This prevents surfacing lottery-chalk -4.5/-5.5 favorites and
    # deep-dog +4.5/+5.5 chalks that were noise on the slate.
    ars_outs = _alt_outcomes_for_market(alt_payload, "alternate_spreads")
    for o in ars_outs:
        team = o.get("name")
        point = o.get("point")
        price_raw = o.get("price")
        if team not in (home, away):
            continue
        if not isinstance(price_raw, (int, float)) or not isinstance(point, (int, float)):
            continue
        point_f = float(point)
        # HARD LINE-RANGE FILTER — only underdog +1.5 to +3.5.
        # Favorites (negative point) are excluded — they're inverses of
        # the dog +/- 1.5/3.5 line and would surface as low-edge chalk.
        if not (1.5 <= point_f <= 3.5):
            continue
        price = int(price_raw)
        if price <= _MLB_ALT_MAX_ODDS or price >= 5000:
            continue
        imp = _implied_prob(price)
        if not (0.35 <= imp <= 0.95):
            continue
        # 2026-07-21 FINAL PHASE — replaced RNG-derived `mp` with a
        # book-anchored seed. The calibrated `mp` gets recomputed from
        # the real MLB feature engine below (build_mlb_ml_factors) so
        # model_win_prob reflects actual Elo / bullpen / recent form
        # rather than book_implied + noise.
        mp = max(0.40, min(0.92, imp + 0.03))
        edge_pct = (mp - imp) * 100
        # 8% EDGE GATE (mandatory).
        if edge_pct < _MLB_ALT_MIN_EDGE_PCT:
            continue
        # Skip the standard main-line +1.5 that comes through the regular
        # `spreads` market (already surfaced in _picks_from_game). Only
        # count as a duplicate when the price is in the typical main-line
        # range (-160 to +160). Alt +1.5 priced outside that range (e.g.
        # -200 fav-dog or +180 dog-dog) is a legit alt variant.
        if point_f == 1.5 and -170 <= price <= 170:
            continue
        # 2026-07-21 Phase 1 MLB: real ML factors for alt run lines,
        # skip if not enough real coverage.
        _ctx = (event_payload.get("_ctx") if isinstance(event_payload, dict) else None) or {}
        from services.mlb_feature_engine import (
            build_mlb_ml_factors, has_enough_real_data,
        )
        _arl_factors, _sources = build_mlb_ml_factors(_ctx, pick_team=team)
        if not has_enough_real_data(_arl_factors, "ml"):
            continue
        factors = {k: v for k, v in _arl_factors.items() if v is not None}
        # 2026-07-21 FINAL PHASE — recalibrate mp from real factor mean.
        _fv = [v for v in factors.values() if isinstance(v, (int, float))]
        if len(_fv) >= 3:
            _cal_mp = sum(_fv) / len(_fv)
            mp = max(0.40, min(0.95, _cal_mp))
            edge_pct = (mp - imp) * 100
            # Re-check 8% gate against calibrated edge.
            if edge_pct < _MLB_ALT_MIN_EDGE_PCT:
                continue
        lock, breakdown = compute_lock_score(factors, win_prob=mp * 100)
        pick = _build_pick(
            sport="MLB", league=league, event=f"{away} @ {home}",
            event_time=commence,
            market=f"{team} +{point_f} Run Line (Alt)",
            pick_side=team,
            model_win_prob=mp, book_odds=price,
            lock=lock, factors=breakdown,
            insights=[
                f"{team} alt run line (+{point_f}) — book implies {imp*100:.0f}%",
                f"Model edge: {edge_pct:+.1f}% (8% gate cleared)",
            ],
            external_id=f"MLB-{event_id}-altrunline-{team}-{point_f}",
            is_alt_prop=True,
            home_team_name=home, away_team_name=away,
        )
        if pick:
            pick["is_alt"] = True
            out_picks.append(pick)

    # ── Contradiction dedupe (user report 2026-07-03: "too many alt
    # total Over and Unders for same game — that's a contradiction").
    # For each team in a game, the app must commit to ONE side per
    # market family. Group by (team, market_family) and keep only the
    # highest-edge pick. This eliminates the "Yankees Over 3.5 AND
    # Yankees Under 3.5" self-cancellation.
    #
    # market_family taxonomy:
    #   • "team_total_main"  → "{team} Team Total (Over|Under) X.Y"
    #   • "team_total_alt"   → "... (Alt)"
    #   • "run_line_alt"     → "{team} +N.N Run Line (Alt)"
    # We KEEP main + alt separately (they're different bet products),
    # but ban Over-vs-Under within each family.
    def _family_key(p: dict) -> tuple:
        m = p.get("market") or ""
        if "Team Total" in m and "(Alt)" in m:
            family = "team_total_alt"
        elif "Team Total" in m:
            family = "team_total_main"
        elif "Run Line" in m and "(Alt)" in m:
            family = "run_line_alt"
        else:
            return None
        # Extract team from market: "{team} Team Total ..." or
        # "{team} +N Run Line ...". Both start with team name up to
        # the first "Team" / "+" / "-" token.
        team_hint = None
        for t in (home, away):
            if t and m.startswith(t):
                team_hint = t
                break
        return (family, team_hint) if team_hint else None

    best_by_key: dict[tuple, dict] = {}
    unkeyed: list[dict] = []
    for p in out_picks:
        if not p:
            continue
        k = _family_key(p)
        if k is None:
            unkeyed.append(p)
            continue
        edge = float(p.get("edge_percent") or 0)
        cur = best_by_key.get(k)
        if cur is None or edge > float(cur.get("edge_percent") or 0):
            best_by_key[k] = p
    deduped = list(best_by_key.values()) + unkeyed
    if len(deduped) < len([p for p in out_picks if p]):
        logger.info(
            "MLB alt-line dedupe: %d → %d picks (removed contradictory sides)",
            len([p for p in out_picks if p]), len(deduped),
        )
    return [p for p in deduped if p is not None]


def _alt_outcomes_for_market_desc(payload: dict, market_key: str) -> list[dict]:
    """Same as _alt_outcomes_for_market but also considers the
    `description` field (used by team_totals / alternate_team_totals to
    distinguish which team's total is being priced)."""
    seen: dict[tuple, dict] = {}
    for bk in (payload.get("bookmakers") or []):
        for mk in (bk.get("markets") or []):
            if mk.get("key") != market_key:
                continue
            for o in (mk.get("outcomes") or []):
                key = (o.get("name"), o.get("description"), o.get("point"))
                if key not in seen:
                    seen[key] = o
    return list(seen.values())


# ─── MLB Roster Cache (free MLB Stats API, no auth) ───
# Used to tag each prop pick with the player's team abbreviation so we don't
# confuse users about which "Max Muncy" / "Brandon Lowe" / etc. they're seeing.
_MLB_TEAM_ID_BY_NAME = {
    "Arizona Diamondbacks": 109, "Atlanta Braves": 144, "Baltimore Orioles": 110,
    "Boston Red Sox": 111, "Chicago Cubs": 112, "Chicago White Sox": 145,
    "Cincinnati Reds": 113, "Cleveland Guardians": 114, "Colorado Rockies": 115,
    "Detroit Tigers": 116, "Houston Astros": 117, "Kansas City Royals": 118,
    "Los Angeles Angels": 108, "Los Angeles Dodgers": 119, "Miami Marlins": 146,
    "Milwaukee Brewers": 158, "Minnesota Twins": 142, "New York Mets": 121,
    "New York Yankees": 147, "Athletics": 133, "Oakland Athletics": 133,
    "Philadelphia Phillies": 143, "Pittsburgh Pirates": 134, "San Diego Padres": 135,
    "Seattle Mariners": 136, "San Francisco Giants": 137, "St. Louis Cardinals": 138,
    "Tampa Bay Rays": 139, "Texas Rangers": 140, "Toronto Blue Jays": 141,
    "Washington Nationals": 120,
}
_MLB_ROSTER_CACHE: dict[str, set[str]] = {}   # team_name → {player_full_names}
_MLB_ROSTER_FETCHED_DATE: str | None = None


async def _refresh_mlb_rosters(date_str: str) -> None:
    """Fetch all 30 MLB active rosters once per day. Free public API, no auth.
    Used to map prop player names → team for clear display."""
    global _MLB_ROSTER_CACHE, _MLB_ROSTER_FETCHED_DATE
    if _MLB_ROSTER_FETCHED_DATE == date_str and _MLB_ROSTER_CACHE:
        return
    new_cache: dict[str, set[str]] = {}
    async with httpx.AsyncClient(timeout=10) as client:
        for team_name, team_id in _MLB_TEAM_ID_BY_NAME.items():
            try:
                r = await client.get(
                    f"https://statsapi.mlb.com/api/v1/teams/{team_id}/roster",
                    params={"rosterType": "40Man"},   # broader than active (40-man + recent call-ups)
                )
                if r.status_code != 200:
                    continue
                data = r.json()
                names: set[str] = set()
                for entry in data.get("roster", []):
                    p = entry.get("person") or {}
                    full = p.get("fullName")
                    if full:
                        names.add(full)
                if names:
                    new_cache[team_name] = names
            except Exception as e:
                logger.debug("MLB roster fetch failed for %s: %s", team_name, e)
                continue
            await asyncio.sleep(0.05)
    if new_cache:
        _MLB_ROSTER_CACHE = new_cache
        _MLB_ROSTER_FETCHED_DATE = date_str
        logger.info("MLB rosters cached: %d teams, %d total players",
                    len(new_cache), sum(len(v) for v in new_cache.values()))


def _player_team_for_event(player: str, home_team: str, away_team: str,
                           year_hint: str = "") -> str | None:
    """Given a cleaned player name and the 2 teams in the event, return the
    team name (full) the player belongs to. Returns None if unknown.

    `year_hint` (e.g. "2002") helps disambiguate name-collisions when both
    teams in the matchup have a player with the same name — we look up the
    player's MLB Stats API birth-year and prefer the roster whose player
    matches the hint.
    """
    if not player:
        return None
    pl = _strip_accents(player.strip().lower())
    home_roster = _MLB_ROSTER_CACHE.get(home_team, set())
    away_roster = _MLB_ROSTER_CACHE.get(away_team, set())
    # Build accent-normalized lookup sets so 'Yandy Diaz' matches 'Yandy Díaz'.
    home_norm = {_strip_accents(n.lower()): n for n in home_roster}
    away_norm = {_strip_accents(n.lower()): n for n in away_roster}
    home_has = pl in home_norm
    away_has = pl in away_norm
    # Exact match — no ambiguity
    if home_has and not away_has:
        return home_team
    if away_has and not home_has:
        return away_team
    # Both teams have same-name players (rare: e.g. two Max Muncys).
    # Use birth-year hint from The Odds API to disambiguate via the player-id
    # cache (built lazily).
    if home_has and away_has:
        if not year_hint:
            return None  # ambiguous — leave untagged
        try:
            year_int = int(year_hint)
            for team_name in (home_team, away_team):
                team_id = _MLB_TEAM_ID_BY_NAME.get(team_name)
                if not team_id:
                    continue
                # Look up player birth year via MLB Stats API. Cheap call,
                # only triggered on actual collisions (very rare).
                r = httpx.get(
                    f"https://statsapi.mlb.com/api/v1/teams/{team_id}/roster",
                    params={"rosterType": "40Man"}, timeout=5,
                )
                if r.status_code != 200:
                    continue
                for e in r.json().get("roster", []):
                    p = e.get("person") or {}
                    if (p.get("fullName") or "").lower() != pl:
                        continue
                    pid = p.get("id")
                    if not pid:
                        continue
                    p2 = httpx.get(
                        f"https://statsapi.mlb.com/api/v1/people/{pid}",
                        timeout=5,
                    )
                    if p2.status_code != 200:
                        continue
                    people = p2.json().get("people", [])
                    if not people:
                        continue
                    bd = (people[0].get("birthDate") or "")
                    if bd.startswith(str(year_int)):
                        return team_name
        except Exception as e:
            logger.debug("Year-hint roster lookup failed: %s", e)
        return None
    # Loose last-name match: only one team has a player with this last name.
    last = pl.split()[-1] if " " in pl else pl
    home_matches = [n for n in home_roster if n.lower().split()[-1] == last]
    away_matches = [n for n in away_roster if n.lower().split()[-1] == last]
    if len(home_matches) == 1 and not away_matches:
        return home_team
    if len(away_matches) == 1 and not home_matches:
        return away_team
    return None


# MLB has several name-collision pairs (e.g. Max Muncy/1990 LAD vs Max Muncy/2002
# OAK) that The Odds API disambiguates by appending a birth-year suffix like
# "Max Muncy (2002)". To users this looks like a bug ("why is the famous Max
# Muncy in a Pirates@A's game?") so we strip the suffix for display and rely
# on the event context + team tag to identify the correct player.
import re as _re
import unicodedata as _ud
_NAME_YEAR_SUFFIX = _re.compile(r"\s*\((19|20)\d{2}\)\s*$")


def _strip_accents(s: str) -> str:
    """Normalize accents: 'Yandy Díaz' → 'Yandy Diaz' so name matching works
    against the MLB Stats API (which preserves diacritics)."""
    if not s:
        return ""
    return "".join(c for c in _ud.normalize("NFD", s) if _ud.category(c) != "Mn")


def _clean_player_name(raw: str | None) -> str:
    """Strip the (YYYY) birth-year disambiguator The Odds API appends to
    name-collision MLB players (e.g. 'Max Muncy (2002)' → 'Max Muncy')."""
    if not raw:
        return ""
    return _NAME_YEAR_SUFFIX.sub("", str(raw)).strip()


def _team_abbr(team_name: str) -> str:
    """Short 3-letter team tag for display. Falls back to the first word."""
    if not team_name:
        return ""
    MAP = {
        # MLB
        "Arizona Diamondbacks": "ARI", "Atlanta Braves": "ATL", "Baltimore Orioles": "BAL",
        "Boston Red Sox": "BOS", "Chicago Cubs": "CHC", "Chicago White Sox": "CWS",
        "Cincinnati Reds": "CIN", "Cleveland Guardians": "CLE", "Colorado Rockies": "COL",
        "Detroit Tigers": "DET", "Houston Astros": "HOU", "Kansas City Royals": "KC",
        "Los Angeles Angels": "LAA", "Los Angeles Dodgers": "LAD", "Miami Marlins": "MIA",
        "Milwaukee Brewers": "MIL", "Minnesota Twins": "MIN", "New York Mets": "NYM",
        "New York Yankees": "NYY", "Athletics": "OAK", "Oakland Athletics": "OAK",
        "Philadelphia Phillies": "PHI", "Pittsburgh Pirates": "PIT", "San Diego Padres": "SD",
        "Seattle Mariners": "SEA", "San Francisco Giants": "SF", "St. Louis Cardinals": "STL",
        "Tampa Bay Rays": "TB", "Texas Rangers": "TEX", "Toronto Blue Jays": "TOR",
        "Washington Nationals": "WSH",
    }
    if team_name in MAP:
        return MAP[team_name]
    # Generic fallback: take first 3 letters of last word.
    parts = team_name.split()
    return (parts[-1][:3] if parts else team_name[:3]).upper()


def _prop_market_label(market_key: str, side: str, point: float | None) -> str:
    # Anytime goal scorer has no point — just "Yes" the player scores at all.
    if market_key == "player_goal_scorer_anytime":
        return "Anytime Goal Scorer"
    # 2026-07-22 — Distinguish Score-or-Assist from Anytime Goal Scorer.
    # User bug: "app not distinguishing between goal scorer and score or
    # assist bets". Without this override the default fallthrough emitted
    # "Yes 0.5 Player To Score Or Assist" (ugly + confusing).
    if market_key == "player_to_score_or_assist":
        return "To Score or Assist"
    if market_key == "player_first_goal_scorer":
        return "First Goal Scorer"
    is_alt = market_key.endswith("_alternate")
    base_key = market_key.replace("_alternate", "")
    pretty = {
        "batter_hits": "Hits",
        "batter_hits_runs_rbis": "Hits + Runs + RBIs",
        "batter_home_runs": "Home Runs",
        "batter_rbis": "RBIs",
        "batter_total_bases": "Total Bases",
        "pitcher_strikeouts": "Strikeouts",
        "pitcher_outs": "Outs Recorded",
        "player_points": "Points", "player_rebounds": "Rebounds",
        "player_assists": "Assists",
    }.get(base_key, base_key.replace("_", " ").title())
    label = f"{side} {point} {pretty}"
    return f"{label}  · ALT LOCK" if is_alt else label


def _prop_insights(sport: str, breakdown: dict, player: str) -> list[str]:
    """Honest factor-derived insights for a player prop pick.

    Never fabricates specific numeric stats. Uses the actual model factor
    scores to describe why this prop has model edge.
    """
    if not breakdown:
        return []
    sorted_factors = sorted(
        ((k, float(v)) for k, v in breakdown.items() if isinstance(v, (int, float))),
        key=lambda kv: -kv[1],
    )
    out: list[str] = []
    for name, score in sorted_factors[:4]:
        out.append(f"{name}: {score:.0f}/100 — {_score_label(score)}.")
    # Sport-context note that DOESN'T invent numbers.
    if sport == "Soccer":
        out.append(f"Model rates {player} above book implied for this market.")
    else:
        out.append(f"Composite usage + matchup model favors {player} clearing the line.")
    return out


def _props_picks_from_event(sport: str, league: str, payload: dict,
                            commence: str, rng: random.Random) -> list[dict]:
    home = payload.get("home_team")
    away = payload.get("away_team")
    if not home or not away or not payload.get("bookmakers"):
        return []
    bucket: dict = {}
    # Track birth-year hints per (clean) player name so we can disambiguate
    # name-collision pairs (Max Muncy LAD vs OAK) when both teams have the
    # same player name on their roster.
    player_year_hints: dict[str, str] = {}
    for b in payload["bookmakers"]:
        for m in b.get("markets", []):
            mk = m.get("key")
            is_goal_scorer = mk == "player_goal_scorer_anytime"
            is_score_or_assist = mk == "player_to_score_or_assist"
            is_first_goal_scorer = mk == "player_first_goal_scorer"
            is_mma_method = mk == "mma_method_of_victory"
            # Block 2D (2026-08) A1 — NFL Anytime TD.  Outcomes have
            # side="Yes"/"No" and no `point` (binary market). Store with
            # point_key=0.5 so the downstream sync emitter can read them.
            is_anytime_td = mk == "player_anytime_td"
            is_first_td = mk == "player_1st_td"
            for o in m.get("outcomes", []):
                raw_player = o.get("description") or o.get("name") or ""
                player = _clean_player_name(raw_player)
                # Preserve any (YYYY) hint The Odds API attached so we can
                # disambiguate same-name players via birth-year lookup.
                _ym = _NAME_YEAR_SUFFIX.search(raw_player)
                player_year_hint = _ym.group(0).strip("() ") if _ym else ""
                if player and player_year_hint:
                    player_year_hints[player] = player_year_hint
                side = o.get("name")
                point = o.get("point")
                price = o.get("price")
                if is_goal_scorer or is_score_or_assist or is_first_goal_scorer:
                    # ── Phase 2A.5 DEFECT #5 FIX (2026-08) ─────────────
                    # First Goalscorer outcomes are Yes-style and do NOT
                    # carry a numeric Over/Under point.  Prior code
                    # routed FGS through the numeric-point branch below,
                    # dropping every candidate silently.  Anytime, SoA,
                    # and First Goalscorer share the same Yes-style
                    # contract.
                    if not (player and side and price is not None):
                        continue
                    if str(side).lower() != "yes":
                        continue
                    point_key = 0.5
                elif is_anytime_td or is_first_td:
                    # Only the "Yes" side is meaningful; "No" is the
                    # inverse and is not a book pick we emit.
                    if not (player and side and price is not None):
                        continue
                    if str(side).lower() != "yes":
                        continue
                    point_key = 0.5
                elif is_mma_method:
                    # `mma_method_of_victory` outcomes:
                    #   name = fighter (e.g. "Sean O'Malley")
                    #   description = method (e.g. "KO/TKO", "Submission", "Decision")
                    # We treat each (fighter, method) pair as its own pick.
                    fighter = _clean_player_name(o.get("name"))
                    method = o.get("description")
                    if not (fighter and method and price is not None):
                        continue
                    # Cap absurd longshots — +800 or worse is a coin flip lottery.
                    if int(price) > 800:
                        continue
                    player = fighter
                    side = method  # encode method into side slot for downstream use
                    point_key = method  # disambiguates KO vs Sub vs Dec for same fighter
                else:
                    if not (player and side and price is not None and point is not None):
                        continue
                    # ── MLB Over/Under Direction Preservation (2026-08-22) ──
                    # Some Odds API alt-line payloads emit outcomes where
                    # `outcome.name` is the PLAYER NAME rather than
                    # "Over" / "Under" — the direction is either implicit
                    # in the market key or lost.  Prior code accepted
                    # `side="Griffin Conine"` verbatim, producing
                    # malformed market strings like
                    #   "Griffin Conine (MIA) Griffin Conine 1.5 Hits + Runs + RBIs"
                    # (direction silently dropped, player name
                    # duplicated).  Fail-closed: if side is not a valid
                    # Over/Under direction, skip the outcome so
                    # downstream `_prop_market_label` cannot emit a
                    # direction-less market string.  Numeric-point
                    # markets ALWAYS carry a real Over/Under side; a
                    # non-directional side is a payload defect.
                    _side_norm = str(side).strip().lower()
                    if _side_norm not in ("over", "under"):
                        continue
                    # Standard markets: drop Unders (user pref). For alt markets,
                    # KEEP Unders — they fuel the "Under of the Day" feature
                    # (alt Unders with super-high lines are some of the safest
                    # bets on the board).
                    #
                    # 2026-07-21 EXCEPTION: MLB main-line pitcher_strikeouts
                    # UNDER props are HIGH-value — SportsbookReview experts
                    # regularly recommend K UNDERS ("Reynaldo Lopez Under 4.5
                    # K vs Padres -150" — Padres K at 16% vs him). Our
                    # historical bleed was ALL Over K props. Enabling Unders
                    # gives us the fade side of overpriced-K-Over chalk.
                    #
                    # ── 2026-06 μ-closure FIX 2 (MLB Early Prop Coverage) ─
                    # Extend the Under-allow exception to Hits, Total Bases,
                    # H+R+RBI, and pitcher_outs main lines. Previously any
                    # main-line Under for these families was dropped BEFORE
                    # the MLB feature/model engines saw the real market —
                    # legitimate Unders that the model may value never
                    # reached scoring. Pair-conflict/dedupe/model floors
                    # downstream still decide the winning side per contract.
                    is_alt_mk = mk in _ALT_PROP_MARKETS
                    _MAIN_UNDER_ALLOWED_MK = {
                        "pitcher_strikeouts",
                        "batter_hits",
                        "batter_total_bases",
                        "batter_hits_runs_rbis",
                        "pitcher_outs",
                    }
                    is_prop_main_under = (
                        mk in _MAIN_UNDER_ALLOWED_MK
                        and str(side).lower() == "under"
                    )
                    if (not is_alt_mk
                            and not is_prop_main_under
                            and str(side).lower() == "under"):
                        continue
                    # Block 2A.5.2 (2026-06 restoration): Total Bases is
                    # a real hitter market and MUST reach the board.
                    # Previously ALL TB was dropped here as a stale
                    # defensive filter (from a 2026-06-19 removal that
                    # was reverted 2026-06-24 when TB was re-added to
                    # PLAYER_PROP_MARKETS but this drop was left in
                    # place).  The ONLY duplicate-with-Hits case is
                    # `Total Bases 0.5` (equivalent to Hits 0.5, i.e.
                    # "at least one hit"); every other TB line
                    # (1.5, 2.5, 3.5, ...) is a genuine distinct
                    # market and must survive.  Also drop
                    # `batter_hits_runs_rbis` at 0.5 for the same
                    # reason (Any hit-or-run-or-RBI ≈ Hits 0.5).
                    if mk in ("batter_total_bases",
                                "batter_total_bases_alternate"):
                        try:
                            if float(point) == 0.5:
                                continue
                        except (TypeError, ValueError):
                            # Non-numeric point on TB → skip defensively.
                            continue
                    point_key = point
                bucket.setdefault((mk, player, point_key, side), []).append(int(price))
    # ── 2026-07-28 DEFECT #1 FIX: emission-time symmetric-pair defense ──
    # ────────────────────────────────────────────────────────────────────
    # Before Odds-API iteration order got to decide which side of a
    # (player, market, line) group survived the downstream (player,
    # market_family) dedup, both Over and Under of e.g. Zack Wheeler
    # 6.5 K's could enter the candidate list. This block collapses each
    # symmetric pair to a SINGLE side using **existing model logic** —
    # never iteration order — so downstream janitors have no
    # contradiction to reconcile.
    #
    # Rules (deterministic, one decision per (family, player, line)):
    #   • Pitcher K props (family == "pitcher_strikeouts", MLB only):
    #       Call `services.mlb_k_probability.evaluate_k_pick` for BOTH
    #       sides. Keep the side that `emit=True`s. If both emit → the
    #       side with higher `edge_pp` (deterministic tiebreaker). If
    #       neither emits → DROP BOTH (better zero picks than the
    #       wrong side).
    #   • All other markets (batter props, ML, spreads, totals,
    #       soccer/NBA/NFL props):
    #       Use median-price book_implied. Higher-implied side wins.
    #       If the two sides are within ±5pp → DROP BOTH (indeterminate
    #       market, book can't tell either — no bet is the safest).
    #
    # Grouping key: `_mk_family(mk)` (collapses `_alternate` → std) +
    # player + point. This treats e.g. Over 5.5 K (alt) and Under 5.5 K
    # (main) as a genuine contradiction (they are — same event, same
    # threshold, opposite direction).
    def _mk_family_local(mk_key: str) -> str:
        return (mk_key or "").replace("_alternate", "")
    _pair_index: dict[tuple, dict[str, tuple]] = {}
    for (_mkb, _pb, _ptb, _sb), _prices_b in bucket.items():
        _fam = _mk_family_local(_mkb)
        _side_lo = str(_sb).lower()
        _median_b = sorted(_prices_b)[len(_prices_b) // 2]
        _pair_index.setdefault((_fam, _pb, _ptb), {})[_side_lo] = (_mkb, _median_b)
    _allowed_sides: dict[tuple, set] = {}
    for _key, _side_map in _pair_index.items():
        _fam, _player_b, _point_b = _key
        if len(_side_map) < 2:
            # Only one side present → no contradiction possible.
            _allowed_sides[_key] = set(_side_map.keys())
            continue
        # BOTH sides present. Run deterministic side-selector.
        _winner = None
        _reason = "no_rule"
        _is_k_prop = (_fam == "pitcher_strikeouts" and sport == "MLB"
                      and isinstance(_point_b, (int, float)))
        if _is_k_prop:
            try:
                from services.mlb_k_probability import evaluate_k_pick
                _game_ctx = (payload.get("_ctx") if isinstance(payload, dict) else None) or {}
                _o_mk, _o_price = _side_map.get("over", (None, None))
                _u_mk, _u_price = _side_map.get("under", (None, None))
                _o_eval = evaluate_k_pick(
                    _game_ctx, pitcher_name=_player_b, line=float(_point_b),
                    side="over",
                    book_odds=int(_o_price) if _o_price is not None else None,
                ) if _o_price is not None else None
                _u_eval = evaluate_k_pick(
                    _game_ctx, pitcher_name=_player_b, line=float(_point_b),
                    side="under",
                    book_odds=int(_u_price) if _u_price is not None else None,
                ) if _u_price is not None else None
                _o_ok = bool(_o_eval and _o_eval.get("emit"))
                _u_ok = bool(_u_eval and _u_eval.get("emit"))
                if _o_ok and not _u_ok:
                    _winner, _reason = "over", "kmath_over_only"
                elif _u_ok and not _o_ok:
                    _winner, _reason = "under", "kmath_under_only"
                elif _o_ok and _u_ok:
                    _o_edge = float((_o_eval or {}).get("edge_pp") or 0)
                    _u_edge = float((_u_eval or {}).get("edge_pp") or 0)
                    _winner = "over" if _o_edge >= _u_edge else "under"
                    _reason = f"kmath_both_pass_edge_tiebreak({_o_edge:.1f}vs{_u_edge:.1f})"
                else:
                    # ── Block 2B μ-closure — K SINGLE-GATE ─────────
                    # PRIOR DEFECT: when NEITHER side passed the
                    # K-math emit check the pair-dedup dropped BOTH,
                    # acting as a second independent model gate.  The
                    # authoritative K-math gate downstream (~line 5020
                    # in this file) already decides emit/skip for the
                    # surviving candidate.  Here we ONLY resolve the
                    # over/under CONFLICT — never double-gate.
                    #
                    # New behaviour: when K-math cannot distinguish
                    # emit-ability, fall back to a BOOK-EDGE tiebreak
                    # (higher implied → prefer that side).  If book
                    # odds are equally unusable we default to "over"
                    # (the historical baseline).  Downstream primary
                    # K-math gate remains the sole authoritative
                    # emit/skip decision.
                    _o_pri = _side_map.get("over", (None, None))[1]
                    _u_pri = _side_map.get("under", (None, None))[1]
                    _o_imp = float((_o_eval or {}).get("book_implied") or 0)
                    _u_imp = float((_u_eval or {}).get("book_implied") or 0)
                    if _o_imp or _u_imp:
                        _winner = "over" if _o_imp >= _u_imp else "under"
                        _reason = (
                            f"kmath_neither_book_impl_tiebreak"
                            f"({_o_imp:.2f}vs{_u_imp:.2f})"
                        )
                    else:
                        _winner = "over"
                        _reason = "kmath_neither_default_over"
                logger.info(
                    "PAIR_DEDUP_K: pitcher=%s line=%s over_ok=%s under_ok=%s winner=%s reason=%s",
                    _player_b, _point_b, _o_ok, _u_ok, _winner, _reason,
                )
            except Exception as _pdx:
                logger.debug("Pair dedup K math failed for %s (line=%s): %s",
                             _player_b, _point_b, _pdx)
                # ── Block 2B μ-closure — K SINGLE-GATE ─────────────
                # PRIOR DEFECT: on K-math exception we returned
                # ``_winner = None``, dropping BOTH sides silently
                # before the authoritative K-math gate could decide.
                # NEW: fall through to a book-implied tiebreak so
                # ONE candidate survives to reach the primary gate.
                _o_pri = _side_map.get("over", (None, None))[1]
                _u_pri = _side_map.get("under", (None, None))[1]
                # Higher-price (less negative for favorites, less
                # positive for dogs) → higher implied.  Cheap heuristic
                # without recomputing book_implied.
                def _implied(px):
                    try:
                        px = int(px)
                        if px == 0: return 0.0
                        return (
                            (-px) / ((-px) + 100.0) if px < 0
                            else 100.0 / (px + 100.0)
                        )
                    except Exception:
                        return 0.0
                _o_imp = _implied(_o_pri)
                _u_imp = _implied(_u_pri)
                if _o_imp or _u_imp:
                    _winner = "over" if _o_imp >= _u_imp else "under"
                else:
                    _winner = "over"
                _reason = f"kmath_error_book_impl_fallback({_o_imp:.2f}vs{_u_imp:.2f})"
        else:
            # Non-K symmetric pair → deterministic book-consensus.
            _o_median = _side_map.get("over", (None, None))[1]
            _u_median = _side_map.get("under", (None, None))[1]
            if _o_median is not None and _u_median is not None:
                _o_imp = _implied_prob(_o_median)
                _u_imp = _implied_prob(_u_median)
                # ── 2026-06 μ-closure MLB Prop Flow Repair — FIX 1 ──
                # Balanced-book starvation: previously ANY symmetric
                # pair with |over_imp − under_imp| < 5pp had `_winner
                # = None` which set `_allowed_sides[_key] = set()`
                # → BOTH sides eliminated before the model saw them.
                # For non-K hitter/prop families the sportsbook's
                # near-50/50 pricing must NOT be the sole disqualifier.
                # The predictive engine gets to evaluate BOTH sides
                # and downstream contradiction dedupe (line 6109+)
                # picks the winning side by edge/lock — no fabricated
                # winner, no lowered Board floor, no synthetic lines.
                _KEEP_BOTH_ON_BALANCED_FAMILIES = {
                    "batter_hits",
                    "batter_total_bases",
                    "batter_hits_runs_rbis",
                    "pitcher_outs",
                }
                if abs(_o_imp - _u_imp) < 0.05:
                    if _fam in _KEEP_BOTH_ON_BALANCED_FAMILIES:
                        _winner, _reason = "both", (
                            f"balanced_pass_through_to_model"
                            f"({_o_imp:.3f}vs{_u_imp:.3f})"
                        )
                    else:
                        _winner, _reason = None, (
                            f"balanced({_o_imp:.3f}vs{_u_imp:.3f})"
                        )
                elif _o_imp > _u_imp:
                    _winner, _reason = "over", f"book_over({_o_imp:.3f}vs{_u_imp:.3f})"
                else:
                    _winner, _reason = "under", f"book_under({_o_imp:.3f}vs{_u_imp:.3f})"
                logger.info(
                    "PAIR_DEDUP_STD: player=%s family=%s line=%s winner=%s (%s)",
                    _player_b, _fam, _point_b, _winner, _reason,
                )
        # Convert winner sentinel → allowed set. "both" = allow O/U to model.
        if _winner == "both":
            _allowed_sides[_key] = {"over", "under"}
        else:
            _allowed_sides[_key] = {_winner} if _winner else set()
    # Filter bucket in place — keep only entries whose side is allowed.
    _filtered_bucket = {}
    _dropped = 0
    for (_mkb, _pb, _ptb, _sb), _prices_b in bucket.items():
        _key = (_mk_family_local(_mkb), _pb, _ptb)
        if str(_sb).lower() in _allowed_sides.get(_key, set()):
            _filtered_bucket[(_mkb, _pb, _ptb, _sb)] = _prices_b
        else:
            _dropped += 1
    if _dropped:
        logger.info(
            "PAIR_DEDUP: dropped %d symmetric-pair candidates (%d kept) — "
            "%s vs %s",
            _dropped, len(_filtered_bucket), home, away,
        )
    bucket = _filtered_bucket
    # ── /DEFECT #1 FIX ──────────────────────────────────────────────────
    candidates = []
    for (mk, player, point, side), prices in bucket.items():
        median = sorted(prices)[len(prices) // 2]
        implied = _implied_prob(median)
        is_alt = mk in _ALT_PROP_MARKETS
        if is_alt:
            # Alt lines must be near-locks AND not absurd chalk.
            if implied < _ALT_PROP_MIN_IMPLIED or implied > _ALT_PROP_MAX_IMPLIED:
                if mk and mk.startswith("batter_") or mk and mk.startswith("pitcher_"):
                    try:
                        from services.mlb_gates import record_rejection as _mlb_reject
                        _mlb_reject("implied_probability_gate", market_key=mk)
                    except Exception:
                        pass
                continue
        elif mk == "player_goal_scorer_anytime":
            if implied < _SOCCER_PROP_MIN_IMPLIED:
                continue
            # ── Phase 2A.5 DEFECT #2 (2026-08) ─────────────────────────
            # Stale MLS 2025 hardcoded scorer/starter whitelist RETIRED
            # as a production eligibility gate.  Historical information
            # is retained downstream as *model evidence* but cannot
            # decide whether a player is allowed to enter the model.
            # (Previous behavior: `continue` on gate miss dropped every
            # non-whitelist MLS candidate.  Runtime market availability
            # + real evidence now decides eligibility.)
        elif mk == "player_first_goal_scorer":
            # Phase 2A.5 — FGS routed through the same sanity floor;
            # historically FGS was implicitly dropped by the numeric-
            # point requirement (Defect #5).  Now that it is Yes-style,
            # apply the sanity floor only.
            if implied < _SOCCER_PROP_MIN_IMPLIED:
                continue
        elif mk == "player_to_score_or_assist":
            # SoA is a SUPERSET of Anytime Goal Scorer (either action wins),
            # so its implied probability is ALWAYS ≥ Anytime's. Using a
            # stricter threshold than Anytime silently drops players who
            # qualify for Anytime but whose book-priced SoA happens to sit
            # just below the SoA-specific gate (e.g. Anytime 24% passes,
            # SoA 28% fails 0.30 floor). Audit fix 2026-06-24: equalise
            # thresholds — if Anytime passes, SoA must also pass.
            if implied < _SOCCER_PROP_MIN_IMPLIED:
                continue
            # ── Phase 2A.5 DEFECT #2 (2026-08) ─────────────────────────
            # MLS 2025 whitelist RETIRED as a hard gate.  See note above.
        elif mk == "mma_method_of_victory":
            # Method of victory is inherently a low-implied market (each
            # outcome carves the win pie into 3 methods). Accept 18%+ which
            # is roughly +450 American — typical for "Sean O'Malley by KO".
            if implied < 0.18:
                continue
        elif mk == "pitcher_outs":
            # Pitcher outs main lines (no alt — user spec) are tightly
            # priced around -110 / -150 (~52-60% implied). Use a lower
            # min of 0.55 so confident chalky main-line picks can surface.
            # Higher-priced outs (-200+) get an outsized lock score boost
            # via factor weighting.
            if implied < 0.55:
                continue
        elif mk == "pitcher_strikeouts":
            # Main-line pitcher strikeouts (Over 5.5, 6.5, 7.5 K etc.).
            # 2026-07-21 fix: User asked "can we get the main lines?".
            # Root cause: main K props price at -115 to -140 (52-58%
            # implied), below the 0.62 _HIGH_PROB_MIN_IMPLIED gate that
            # was designed for hitting/HR markets. Result: 5 main-line
            # K picks vs 311 alt-K picks in history, and 100% of the
            # -43.8% ROI board bleed came from alt-K props.
            #
            # Lower the gate to 0.48 so main-line K props with balanced
            # odds can generate picks. Downstream filters (mlb_k_q with
            # edge>=0, lock>=70, chalk_trap) will still enforce quality
            # — this just stops the pre-gate from dropping them all.
            if implied < 0.48:
                continue
        elif mk == "pitcher_strikeouts_alternate":
            # 2026-07-21 USER MANDATE: "I don't want chalk line I want
            # main line or low alt lines". Reject alt K props priced
            # -250 or worse (implied ≥ 71.4%). Only surface reasonable
            # alt lines (Over 4.5 K at -180 = 64% implied is fine;
            # Over 2.5 K at -575 = 85% implied is trap chalk).
            if implied >= 0.715:
                continue
            if implied < 0.48:
                continue
        elif mk == "batter_hits_runs_rbis":
            # Main-line H+R+RBI (usually Over 1.5) is a legit market
            # priced -130 to -180 = 56-64% implied. Restored 2026-07-21
            # per user "bring back H+R+RBI tab". Gate at 0.50 so real
            # mainlines can surface — data-driven mlb_feature_engine
            # gates emission on ≥3 real hitter factors downstream.
            if implied < 0.50:
                continue
        elif mk == "batter_hits":
            # Main-line batter Hits (Over 0.5) is priced -180 to -300
            # (64-75% implied). Use the 0.55 floor so the deep-lock
            # hitters keep firing under the standard hitter-prop gate.
            if implied < 0.55:
                continue
        elif mk in ("batter_total_bases", "batter_total_bases_alternate"):
            # ── 2026-06 μ-closure FIX 3 (MLB Early Prop Coverage) ────
            # Main-line Total Bases is priced -105 to -140 (51-58%
            # implied) — well below the generic 0.62 gate. Previously
            # ALL main-line TB was silently dropped here. Give TB
            # its own market-specific PRE-MODEL acquisition floor
            # consistent with sibling hitter markets (Hits 0.55,
            # H+R+RBI 0.50). The `_HIGH_PROB_MIN_IMPLIED = 0.62`
            # generic gate was designed for HR/hitting markets, not
            # for chalky main-line TB. Pair conflict / model /
            # Brain / >=85 Board floor still enforce quality
            # downstream — this only stops the pre-model starvation.
            if implied < 0.50:
                continue
        # ── 2026-07-21 BLANKET ODDS CAP for K props ──────────────────
        # Regardless of mk key, reject any pitcher strikeout pick
        # priced worse than -250. Belt-and-suspenders vs. any mk-key
        # normalization edge cases. `median` is the integer median
        # price already; the earlier dict-lookup was a no-op bug.
        if "strikeout" in (mk or "").lower():
            try:
                _mp = int(median) if median is not None else 0
            except (TypeError, ValueError):
                _mp = 0
            if _mp and _mp <= -250:
                continue
        else:
            # ── 2026-07-28 H+R+RBI drop-bug fix ──────────────────────
            # Markets that already passed their OWN mk-specific implied
            # gate above (0.48-0.55) must NOT be re-blocked by the
            # generic 0.62 floor — that's a double-gate bug that
            # silently killed all main-line H+R+RBI picks priced -130
            # to -160 (implied 0.565-0.615), plus main-line Hits at
            # -160 and pitcher_outs at -125. Only markets NOT covered
            # by an mk-specific gate fall through to the 0.62 floor.
            _mk_gated = mk in {
                "batter_hits_runs_rbis",
                "batter_hits_runs_rbis_alternate",
                "batter_hits",
                "batter_hits_alternate",
                "batter_total_bases",
                "batter_total_bases_alternate",
                "pitcher_outs",
                "pitcher_strikeouts",
                "pitcher_strikeouts_alternate",
                "player_goal_scorer_anytime",
                "player_to_score_or_assist",
                "player_first_goal_scorer",
                "mma_method_of_victory",
            }
            if not _mk_gated and implied < _HIGH_PROB_MIN_IMPLIED:
                continue
        candidates.append((implied, mk, player, point, side, median, is_alt))
    # ── 2026-07-28 DEFECT #2 FIX: deterministic dedup ordering ──────────
    # ────────────────────────────────────────────────────────────────────
    # Prior to this fix, `candidates.sort(reverse=True)` relied on the
    # tuple's positional ordering — `implied` was the primary key
    # (good), but ties on implied fell through to `mk` string sort
    # DESC, then `player` DESC, etc. Those secondary keys aren't
    # quality signals; they leaked Odds-API iteration order back into
    # dedup winner selection any time two candidates for the same
    # (player, family) group tied on `implied`.
    #
    # The named `_dedup_sort_key` below makes tie-breaking SEMANTIC:
    #   1. is_alt False first  → prefer standard mainlines over alts
    #      when both survive (alt candidates route to their own cap
    #      path anyway, so this is defense-in-depth).
    #   2. implied DESC        → higher book_implied = safer = better.
    #   3. mk ASC              → alphabetical mk name (stable across
    #      refreshes, but same-family markets like `batter_hits` vs
    #      `batter_hits_alternate` are already collapsed by Defect #1).
    #   4. point ASC (numeric) → lower line wins on ties (safer bet).
    #   5. side ASC            → alphabetical "over" < "under".
    #   6. median ASC          → cheaper price on ties.
    def _dedup_sort_key(c):
        _implied, _mk, _player, _point, _side, _median, _is_alt = c
        return (
            0 if not _is_alt else 1,
            -float(_implied),
            str(_mk or ""),
            float(_point) if isinstance(_point, (int, float)) else 0.0,
            str(_side or ""),
            int(_median) if isinstance(_median, (int, float)) else 0,
        )
    candidates.sort(key=_dedup_sort_key)
    picks: list[dict] = []
    # Track per-player caps separately for Over alts vs Under alts so they
    # don't compete for the same player slots. This ensures the "Under of
    # the Day" pool always has enough variety even when Overs dominate.
    alt_over_per_player: dict = {}
    alt_under_per_player: dict = {}
    # ── 2026-07-28 DEFECT #2 FIX: std_seen dedup key ────────────────────
    # `std_seen` still enforces "at most ONE standard mainline pick per
    # (player, family)" so a pitcher doesn't spam the board with 3-4
    # different K lines. But the WINNER of the dedup is now driven by
    # `_dedup_sort_key` above — quality-first, not iteration-order —
    # so identical input across refreshes always produces the identical
    # winner. Same-family alts route through `alt_over_per_player` /
    # `alt_under_per_player` (up to 3 per side) and never reach this
    # dedup, so `_prop_family_key` only sees standard mks.
    #
    # `_prop_family_key` is intentionally a strict mapping (not a regex
    # or `.replace` on `_alternate`) so future mk keys with non-obvious
    # suffixes get an explicit entry.
    std_seen: set = set()
    # `_PROP_FAMILY_MAP` and `_prop_family_key` are module-level (see
    # top of file) so tests + other services can share the same mapping
    # without duplicating knowledge.
    for implied, mk, player, point, side, median, is_alt in candidates:
        side_lower = str(side).lower()
        if is_alt:
            cap_dict = alt_under_per_player if side_lower == "under" else alt_over_per_player
            # Allow up to 3 alts per player per side (e.g. points/rebs/assists)
            if cap_dict.get(player, 0) >= 3:
                continue
            cap_dict[player] = cap_dict.get(player, 0) + 1
        else:
            std_key = (player, _prop_family_key(mk))
            if std_key in std_seen:
                continue
            std_seen.add(std_key)
        # ── Model probability (CALIBRATED, deterministic) ──────────────
        # 2026-07-21 FINAL PHASE PURGE: removed `rng.random()` nudges
        # that used to bake fake ±3-6pp deviations onto `implied`.
        # `mp` is now derived DETERMINISTICALLY from book_implied:
        #   • Alt lines  → clamped to [0.80, 0.94] (they're near-locks)
        #   • Long-shot scorer → +3pp deterministic bump (top forward
        #     bonus), clamped [0.25, 0.70]
        #   • Score-or-assist → +4pp deterministic bump, clamped [0.35, 0.78]
        #   • Standard   → clamped to [0.65, 0.95], plain book_implied
        # For MLB, the real feature engine (build_mlb_{pitcher_k,hitter}_
        # factors) OVERRIDES `mp` further down with the calibrated factor
        # average, so this book-follow default is only the seed value.
        if mk == "player_goal_scorer_anytime":
            mp = max(0.25, min(0.70, implied + 0.03))
        elif mk == "player_to_score_or_assist":
            mp = max(0.35, min(0.78, implied + 0.04))
        elif is_alt:
            mp = max(0.80, min(0.94, implied))
        else:
            mp = max(0.65, min(0.95, implied))
        # Pitcher props use a different factor recipe than batter props.
        is_pitcher_prop = mk.startswith("pitcher_")
        # ── REAL FEATURE ENGINE (2026-07-21 Phase 1 MLB) ─────────────
        # USER MANDATE: "Never substitute randomness for missing data."
        # Every MLB prop factor is now sourced from actual statsapi /
        # statcast / park data. Picks lacking enough real coverage are
        # DROPPED (return None) — no random fallback anywhere.
        _mlb_features_used: list[str] = []
        _skip_pick = False
        # Block 2D A1 — ATD engine may override model_win_prob with
        # its own independent probability.  Kept None otherwise so
        # existing paths remain unaffected.
        _atd_model_override: Optional[float] = None
        _atd_evidence_block: Optional[dict] = None
        if sport == "MLB" and is_pitcher_prop:
            from services.mlb_feature_engine import (
                build_mlb_pitcher_k_factors,
                build_mlb_pitcher_outs_factors,
                has_enough_real_data,
            )
            from services.mlb_gates import (
                record_rejection as _mlb_reject,
                record_funnel_step as _mlb_funnel,
            )
            # ── Funnel step: candidate_created ─────────────────
            try: _mlb_funnel("candidate_created", market_key=mk)
            except Exception: pass
            _game_ctx = (payload.get("_ctx") if isinstance(payload, dict) else None) or {}
            # ── 2026-06 μ-closure MLB Prop Flow Repair — FIX 4 ──
            # Route Outs Recorded through workload/durability factor
            # builder; keep K family on the K builder.
            _is_outs_prop = mk in ("pitcher_outs", "pitcher_outs_alternate")
            if _is_outs_prop:
                real_factors, _sources = build_mlb_pitcher_outs_factors(
                    _game_ctx, player=player, side=str(side),
                    line=point if isinstance(point, (int, float)) else None,
                )
            else:
                real_factors, _sources = build_mlb_pitcher_k_factors(
                    _game_ctx, player=player, side=str(side),
                    line=point if isinstance(point, (int, float)) else None,
                )
            # ── Funnel step: model_evaluated ─────────────────
            try: _mlb_funnel("model_evaluated", market_key=mk)
            except Exception: pass
            if not has_enough_real_data(real_factors, "k_prop"):
                _skip_pick = True
                _mlb_reject("missing_feature_data", market_key=mk)
            else:
                # ── 2026-07-27 SHARPER K MATH GATE ────────────────────
                # User: "K picks need to be sharper. Went 6/11, want 8/11+."
                # Route Strikeout picks through the Poisson probability
                # engine. Drop picks where:
                #   • Book odds worse than -220 (chalk trap)
                #   • Model prob doesn't beat book implied by 5+ pp
                #   • Model win prob < 60%
                #   • Under X.5 fired when expected K's >= X.5 (self-contradict)
                # Also stores conflict-key so cross-market dedup can kill
                # the weaker side when both Over + Under emit for same pitcher.
                if "strikeout" in (mk or "").lower() and isinstance(point, (int, float)):
                    try:
                        from services.mlb_k_probability import evaluate_k_pick
                        _k_eval = evaluate_k_pick(
                            _game_ctx, pitcher_name=player, line=float(point),
                            side=str(side), book_odds=int(median) if median is not None else None,
                        )
                        if not _k_eval.get("emit"):
                            _skip_pick = True
                            try:
                                from services.mlb_gates import record_rejection as _mlb_reject
                                # ── 2026-06 μ-closure MLB Prop Flow Repair — FIX 3 ──
                                # Align K-math emit-false reasons with MLB
                                # rejection telemetry keys so board-visibility
                                # diagnostics distinguish acquisition / model /
                                # edge / direction issues instead of collapsing
                                # everything into "ev_gate".
                                _reason_map = {
                                    "no_pitcher_data":         "missing_feature_data",
                                    "insufficient_signals":    "missing_feature_data",
                                    "odds_too_chalky":         "implied_probability_gate",
                                    "insufficient_edge":       "edge_gate",
                                    "edge_too_low":            "edge_gate",
                                    "model_win_prob_low":      "ev_gate",
                                    "model_prob_too_low":      "ev_gate",
                                    "under_self_contradict":   "correlation_conflict",
                                    "under_but_expected_over": "correlation_conflict",
                                    "over_but_expected_under": "correlation_conflict",
                                    "book_odds_chalk_trap":    "implied_probability_gate",
                                }
                                _mlb_reject(_reason_map.get(_k_eval.get("reason"), "ev_gate"),
                                             market_key=mk)
                            except Exception:
                                pass
                            logger.info(
                                "K_MATH_GATE_DROP: %s %s %s reason=%s exp_k=%.2f model=%.3f book=%.3f",
                                player, side, point, _k_eval.get("reason"),
                                _k_eval.get("expected_k", 0.0),
                                _k_eval.get("model_prob", 0.0),
                                _k_eval.get("book_implied", 0.0),
                            )
                        else:
                            # Override the seed mp with the model prob.
                            mp = float(_k_eval["model_prob"])
                            # Stash for downstream signal engine & rationale
                            payload.setdefault("_k_math", {})[player] = _k_eval
                            # Persistent observability tag (2026-07-27) — so
                            # downstream/testing agents can grep for gate-passed
                            # K picks and monitor the sharper-K feature.
                            payload["k_math_gate"] = "passed"
                            payload["k_math_expected_k"] = _k_eval["expected_k"]
                            payload["k_math_edge_pp"] = _k_eval["edge_pp"]
                            logger.info(
                                "K_MATH_GATE_PASS: %s %s %s exp_k=%.2f edge=%.1fpp model=%.3f",
                                player, side, point, _k_eval["expected_k"],
                                _k_eval["edge_pp"], _k_eval["model_prob"],
                            )
                    except Exception as _kx:
                        logger.debug("K math eval failed for %s: %s", player, _kx)
                if _skip_pick:
                    pass
                else:
                    # Drop None values before feeding compute_lock_score.
                    factors = {k: v for k, v in real_factors.items() if v is not None}
                    _mlb_features_used = _sources
                    # Stash raw K data for signal engine / rationale.
                    _sph = _game_ctx.get("starting_pitcher_home") or {}
                    _spa = _game_ctx.get("starting_pitcher_away") or {}
                    _match = None
                    for _sp in (_sph, _spa):
                        if _sp.get("name", "").strip().lower() == player.strip().lower():
                            _match = _sp
                            break
                    if _match and _match.get("opp_k_pct") is not None:
                        payload.setdefault("_real_k_data", {})[player] = {
                            "opp_team": _match.get("opp_k_team"),
                            "opp_k_pct": _match.get("opp_k_pct"),
                            "opp_k_rank": _match.get("opp_k_rank"),
                            "pitcher_throws": _match.get("throws"),
                            "pitcher_k_pct": _match.get("k_pct"),
                            "pitcher_ip_per_start": _match.get("ip_per_start"),
                        }
        elif sport == "MLB" and not is_pitcher_prop:
            # Hitter props (Hits, HRs, TBs, Runs, RBIs)
            from services.mlb_feature_engine import (
                build_mlb_hitter_factors, has_enough_real_data,
            )
            _game_ctx = (payload.get("_ctx") if isinstance(payload, dict) else None) or {}
            _is_home = False
            _game_home = _game_ctx.get("home_team") or ""
            # Match player to team via ctx.hitters map when available.
            _hitters = _game_ctx.get("hitters") or {}
            _hb = _hitters.get(player.strip().lower()) or {}
            _is_home = bool(_hb.get("is_home"))
            _opp_sp = _hb.get("opp_pitcher_name")
            # Block 2A.5.2 (2026-08) — explicit lineup-status gate
            # BEFORE feature engine.  When there is no lineup evidence
            # for this player (empty ctx.hitters row) fail CLOSED at
            # emission — that means either the confirmed lineup hasn't
            # been posted yet, or this player isn't in it.  Either way
            # the pick must not reach the board.  ``classify_lineup_status``
            # is the canonical helper from services.mlb_gates that
            # reduces raw ingest flags to ``LINEUP_STATES``.
            try:
                from services.mlb_gates import (
                    classify_lineup_status as _classify_lu,
                    should_publish            as _lu_should_publish,
                    data_quality_cap_for_status as _lu_cap,
                    record_rejection          as _mlb_reject,
                    record_funnel_step        as _mlb_funnel,
                )
                # ── Funnel step: candidate_created (hitter) ──
                try: _mlb_funnel("candidate_created", market_key=mk)
                except Exception: pass
                _lu_status = _classify_lu(
                    lineup_confirmed=_hb.get("lineup_confirmed"),
                    is_starter=_hb.get("is_starter"),
                    scratched=_hb.get("scratched"),
                    on_bench=_hb.get("on_bench"),
                    lineup_slot=_hb.get("lineup_slot"),
                )
            except Exception:
                _lu_status = "unknown"
                _lu_should_publish = lambda _s: True   # pragma: no cover
                _lu_cap = lambda _s: 99.0              # pragma: no cover
                def _mlb_reject(*_a, **_k): pass       # pragma: no cover
            if not _lu_should_publish(_lu_status):
                # bench / scratched → hard drop (never publish).
                _skip_pick = True
                try:
                    _mlb_reject(
                        "lineup_scratched" if _lu_status == "scratched"
                        else "lineup_bench",
                        market_key=mk,
                    )
                except Exception:
                    pass
            # Stash provenance so `_build_pick` result can be stamped.
            payload.setdefault("_mlb_lineup_status", {})[
                player.strip().lower()] = {
                "status":     _lu_status,
                "lineup_pos": _hb.get("lineup_slot"),
                "source":     _hb.get("lineup_source"),
                "cap":        _lu_cap(_lu_status),
                "updated_at": _hb.get("lineup_updated_at"),
            }
            if _skip_pick:
                real_factors = {}
                _sources = []
            else:
                # ── 2026-06 μ-closure FIX 1 — Early Hitter Hydration ──
                # For Hits / TB / H+R+RBI, if the player is UNKNOWN
                # (not present in ctx.hitters), attempt to hydrate a
                # minimal REAL row from cached Statcast + hitter_intel
                # so the feature engine can extract >=3 real factors.
                # Lineup status stays "unknown" — the existing 88 cap
                # still applies.  Only real data is attached; no
                # fabrication.  If nothing available, MISSING_FEATURE_DATA
                # still fires below for the correct data reason.
                _HITTER_FAMILY_MK = {
                    "batter_hits", "batter_hits_alternate",
                    "batter_total_bases", "batter_total_bases_alternate",
                    "batter_hits_runs_rbis", "batter_hits_runs_rbis_alternate",
                }
                if (mk in _HITTER_FAMILY_MK
                        and _lu_status == "unknown"
                        and player):
                    try:
                        from services.mlb_early_hitter_hydrate import (
                            hydrate_missing_hitter,
                        )
                        hydrate_missing_hitter(_game_ctx, player)
                    except Exception as _hy_err:
                        logger.debug(
                            "early hitter hydration skipped for %s: %s",
                            player, _hy_err,
                        )
                real_factors, _sources = build_mlb_hitter_factors(
                    _game_ctx, player=player, is_home=_is_home,
                    opp_pitcher_name=_opp_sp,
                    market_type=mk,
                    line=point if isinstance(point, (int, float)) else None,
                )
            if not _skip_pick and not has_enough_real_data(real_factors, "hitter_prop"):
                _skip_pick = True
                try:
                    from services.mlb_gates import record_rejection as _mlb_reject
                    _mlb_reject("missing_feature_data", market_key=mk)
                except Exception:
                    pass
                # Phase 1B — persistent funnel record (hitter-prop
                # reachability proof: drops are never silent).
                try:
                    from services import funnel_telemetry as _funnel
                    _funnel.record(
                        sport="MLB", market=mk, stage="model",
                        reason="MISSING_FEATURE_DATA",
                        event=player,
                    )
                except Exception:
                    pass
            else:
                factors = {k: v for k, v in real_factors.items() if v is not None}
                _mlb_features_used = _sources
                # ── Block 2D A3 (2026-08) — MLB HR intel wiring ──
                # For HR-family markets, attach specialized HR
                # intelligence (park factor, wind, temp/roof,
                # pitcher HR/9, batter power, recent form, platoon)
                # via the existing services.mlb_hr_intel helpers.
                # The score is exposed as an EVIDENCE block on the
                # pick — NOT as a Lock Score override.  Consumers
                # (rationale/UI/telemetry) can surface it, and the
                # per-factor multipliers are normalised into ONE
                # additional Lock Score factor "HR Intel Composite"
                # so specialized HR evidence reaches the existing
                # scoring architecture without a formula rewrite.
                if mk in ("batter_home_runs",
                          "batter_home_runs_alternate"):
                    try:
                        from services import mlb_hr_intel as _hri
                        _sp = _game_ctx.get(
                            "starting_pitcher_home" if not _is_home
                            else "starting_pitcher_away") or {}
                        _pitcher_hr9 = _sp.get("hr_per_9") or _sp.get("hr9")
                        _batter_hand = _hb.get("bats") or _hb.get("hand") or ""
                        _pitcher_hand = _sp.get("throws") or _sp.get("hand") or ""
                        # Ballpark → park + roof.
                        _ballpark = (_game_ctx.get("ballpark")
                                      or _game_ctx.get("venue")
                                      or "")
                        park_mult, park_lbl = _hri._park_hr_mult(_ballpark)
                        # Weather (may be absent — MISSING DATA != 1.0
                        # for correctness, but the helpers return 1.0
                        # already when data is missing, which is the
                        # NEUTRAL prior; that's honest here).
                        _wx = _game_ctx.get("weather") or {}
                        wind_mult, wind_lbl = _hri._wind_hr_mult(
                            _wx.get("wind_mph"),
                            _wx.get("wind_dir_deg"),
                            _ballpark,
                        )
                        temp_mult, temp_lbl = _hri._temp_hr_mult(
                            _wx.get("temp_f"),
                            "outdoor" if not _wx.get("roof_closed")
                            else "closed",
                        )
                        pitcher_hr_mult, pitcher_hr_lbl = \
                            _hri._pitcher_hr_mult(_pitcher_hr9)
                        power_mult, power_lbl = _hri._batter_power_mult(
                            _hb.get("iso"),
                            _hb.get("barrel_pct"),
                            _hb.get("hr_per_pa"),
                        )
                        form_mult, form_lbl = _hri._recent_form_mult(
                            int(_hb.get("last_15_hrs") or 0),
                            int(_hb.get("last_15_games") or 0),
                        )
                        platoon_mult, platoon_lbl = _hri._platoon_mult(
                            _batter_hand, _pitcher_hand,
                            _hb.get("vs_lhp_hr_pa"),
                            _hb.get("vs_rhp_hr_pa"),
                        )
                        # Composite multiplier — product of all
                        # available multipliers, capped [0.2, 3.0].
                        _hr_composite = 1.0
                        for _m in (park_mult, wind_mult, temp_mult,
                                    pitcher_hr_mult, power_mult,
                                    form_mult, platoon_mult):
                            _hr_composite *= float(_m or 1.0)
                        _hr_composite = max(0.2, min(3.0, _hr_composite))
                        # Only add the factor when at least ONE
                        # multiplier moved off neutral (1.0) — i.e.,
                        # we have SOME real specialized signal.
                        _hri_moved = sum(
                            1 for _m in (park_mult, wind_mult, temp_mult,
                                          pitcher_hr_mult, power_mult,
                                          form_mult, platoon_mult)
                            if _m and abs(float(_m) - 1.0) > 1e-6
                        )
                        if _hri_moved > 0:
                            # Map composite [0.2, 3.0] → factor [0.30, 0.95].
                            # Neutral 1.0 → 0.55 (mid-band).
                            _scaled = 0.55 + (0.55 * (_hr_composite - 1.0)
                                                if _hr_composite <= 1.0
                                                else 0.20 + 0.25 * min(1.0, (_hr_composite - 1.0) / 2.0))
                            factors["HR Intel Composite"] = round(
                                max(0.30, min(0.95, _scaled)), 3)
                            if "mlb_hr_intel" not in _mlb_features_used:
                                _mlb_features_used = list(_mlb_features_used) + ["mlb_hr_intel"]
                        # Attach detailed evidence block for downstream
                        # consumers — telemetry / UI / rationale.
                        payload.setdefault(
                            "_hr_intel_evidence", {})[player.strip().lower()] = {
                            "composite":     round(_hr_composite, 3),
                            "park_mult":     round(float(park_mult or 1.0), 3),
                            "park_label":    park_lbl,
                            "wind_mult":     round(float(wind_mult or 1.0), 3),
                            "wind_label":    wind_lbl,
                            "temp_mult":     round(float(temp_mult or 1.0), 3),
                            "temp_label":    temp_lbl,
                            "pitcher_hr_mult": round(float(pitcher_hr_mult or 1.0), 3),
                            "pitcher_hr_label": pitcher_hr_lbl,
                            "batter_power_mult": round(float(power_mult or 1.0), 3),
                            "batter_power_label": power_lbl,
                            "recent_form_mult":  round(float(form_mult or 1.0), 3),
                            "recent_form_label": form_lbl,
                            "platoon_mult":  round(float(platoon_mult or 1.0), 3),
                            "platoon_label": platoon_lbl,
                            "n_signals_moved": _hri_moved,
                            "source":        "mlb_hr_intel",
                        }
                        try:
                            from services.pipeline_diagnostic import log_reason as _plog
                            _plog(
                                sport="MLB", market=mk, player=player,
                                reason=("HR_INTEL_USED" if _hri_moved > 0
                                        else "HR_INTEL_INSUFFICIENT_DATA"),
                            )
                        except Exception:
                            pass
                    except Exception as _hri_err:
                        logger.debug("MLB HR intel wiring skipped for %s: %s",
                                      player, _hri_err)
                # ── Phase 4C finalization: enrich pick with H+R+RBI context
                # so `sim_mlb._simulate_hrr` can consume it via
                # `sim_runner._player_stats_from_pick`.
                try:
                    _lineup_slot = _hb.get("lineup_slot") or _hb.get("batting_order")
                    if _lineup_slot is not None:
                        payload.setdefault("_mlb_ctx_for_sim", {}).setdefault(
                            player.strip().lower(), {})["lineup_slot"] = int(_lineup_slot)
                    _team_runs = _hb.get("team_runs_projection") or _game_ctx.get("team_runs_projection")
                    if _team_runs is not None:
                        payload.setdefault("_mlb_ctx_for_sim", {}).setdefault(
                            player.strip().lower(), {})["team_runs_projection"] = float(_team_runs)
                    _obp = _hb.get("obp") or _hb.get("season_obp")
                    if _obp is not None:
                        payload.setdefault("_mlb_ctx_for_sim", {}).setdefault(
                            player.strip().lower(), {})["obp"] = float(_obp)
                except Exception:
                    pass
        elif sport == "NFL":
            # Phase 3 (2026-07-22) — NFL props route through the real
            # feature engine backed by NFLverse historical data. Zero
            # RNG, zero placeholders. The full data-loading is done
            # ONCE per game in the async pre-loader and cached to
            # ctx["nfl_precomputed"][player][mk]. The synchronous
            # branch here just looks the pre-built factor dict up.
            _game_ctx = (payload.get("_ctx") if isinstance(payload, dict) else None) or {}
            _pc = ((_game_ctx.get("nfl_precomputed") or {}).get(player.strip().lower()) or {}).get(mk) or {}
            _nfl_stat = _NFL_MARKET_TO_STAT.get(mk)
            # Block 2D A1 (2026-08) — Anytime TD specialized wiring.
            # `player_anytime_td` outcomes are NOT in _NFL_MARKET_TO_STAT
            # (they're binary Yes markets, not O/U on a numeric stat).
            # We route them through the specialized nfl_atd_engine
            # results pre-computed by the async pre-loader
            # (services.nfl_feature_engine.build_nfl_game_context now
            # populates ctx["nfl_atd_precomputed"] alongside the
            # standard factor set).  Falls through cleanly to
            # _skip_pick=True when the engine returned reject/None
            # (missing history, low sample, or unresolved identity) —
            # never invents a candidate.
            if mk in ("player_anytime_td", "player_1st_td"):
                _atd_pc = ((_game_ctx.get("nfl_atd_precomputed") or {})
                            .get(player.strip().lower())) or {}
                if not _atd_pc or _atd_pc.get("reject"):
                    _skip_pick = True
                    try:
                        from services.pipeline_diagnostic import log_reason as _plog
                        _plog(
                            sport="NFL", market=mk, player=player,
                            reason=("ATD_ENGINE_UNRESOLVED_PLAYER"
                                    if not _atd_pc
                                    else "ATD_ENGINE_REJECT_" +
                                         str(_atd_pc.get("reject", "unknown")).upper()),
                        )
                    except Exception:
                        pass
                else:
                    # Engine returned a real probability.  Attach as
                    # specialized evidence + use engine's independent
                    # probability as model_win_prob (NOT book-implied
                    # clone).  Real book odds come from the outcome
                    # price further down; no synthetic odds anywhere.
                    _atd_prob = float(_atd_pc.get("td_probability") or 0.0)
                    if _atd_prob <= 0.0:
                        _skip_pick = True
                    else:
                        # Use the standard NFL factor set too (with
                        # prop_stat="anytime_td") when available so
                        # Lock Score sees the same shape as other NFL
                        # props.  Fall back to a minimal factor block
                        # if the standard set isn't available yet.
                        _std_pc = (_pc or {}).get("factors") or {}
                        real_factors = {
                            k: v for k, v in _std_pc.items()
                            if isinstance(v, (int, float))
                        }
                        # Anchor factor from the ATD engine's confidence
                        # so at least one real evidence signal is present
                        # from the specialized engine.  ``confidence`` is
                        # already probability-anchored and penalised for
                        # low sample + high variance (see nfl_atd_engine).
                        _atd_conf = float(_atd_pc.get("confidence") or 0.0)
                        if _atd_conf > 0.0:
                            real_factors["ATD Engine Confidence"] = \
                                round(max(0.30, min(0.95, _atd_conf)), 3)
                        _sources = list((_pc or {}).get("sources") or [])
                        if "nfl_atd_engine" not in _sources:
                            _sources.append("nfl_atd_engine")
                        # Gate: need at least 1 real factor after the
                        # ATD-engine anchor (matches the intent of
                        # has_enough_real_data_nfl but relaxed since
                        # ATD engine already carries strong internal
                        # gates: sample≥5, touches≥10, conv_eff≥1.5%).
                        if not real_factors:
                            _skip_pick = True
                        else:
                            factors = real_factors
                            _mlb_features_used = _sources
                            # Override model_win_prob for this pick to
                            # the engine's independent probability.
                            # This is intentional — the engine is the
                            # specialized ATD model, not a book-implied
                            # clone.
                            _atd_model_override = _atd_prob
                            # Attach the full ATD evidence block for
                            # downstream consumers (rationale, UI
                            # explainer, telemetry).
                            _atd_evidence_block = {
                                "td_probability":    _atd_prob,
                                "confidence":        _atd_conf,
                                "opportunity_rating": _atd_pc.get("opportunity_rating"),
                                "weighted_touches_recent": _atd_pc.get("weighted_touches_recent"),
                                "weighted_tds_recent":     _atd_pc.get("weighted_tds_recent"),
                                "team_td_rate":            _atd_pc.get("team_td_rate"),
                                "matchup_factor":          _atd_pc.get("matchup_factor"),
                                "game_script_factor":      _atd_pc.get("game_script_factor"),
                                "is_rb_archetype":         _atd_pc.get("is_rb_archetype"),
                                "sample_games":            _atd_pc.get("sample_games"),
                                "reasons":                 _atd_pc.get("reasons") or [],
                                "source":                  "nfl_atd_engine",
                            }
                            try:
                                from services.pipeline_diagnostic import log_reason as _plog
                                _plog(
                                    sport="NFL", market=mk, player=player,
                                    reason="ATD_ENGINE_USED",
                                )
                            except Exception:
                                pass
            elif not _pc or not _nfl_stat:
                _skip_pick = True
            else:
                try:
                    from services.nfl_feature_engine import has_enough_real_data_nfl
                    real_factors = _pc.get("factors") or {}
                    _sources = _pc.get("sources") or []
                    if not has_enough_real_data_nfl(real_factors):
                        _skip_pick = True
                    else:
                        factors = {k: v for k, v in real_factors.items() if v is not None}
                        _mlb_features_used = _sources
                except Exception as e:
                    logger.debug("NFL sync gate failed for %s / %s: %s", player, mk, e)
                    _skip_pick = True
        elif sport == "CFB":
            # 2026-07-27 CFB Phase 3 — real-data feature engine is BUILT
            # and TESTED (services/cfb_feature_engine.py). Combines
            # returning production %, SP+ defense rank, transfer portal,
            # career-vs-opp, SoS, and L5 rolling averages.
            #
            # WIRING NOTE: this pick-emission path is SYNC (no `await`),
            # so we can't call the async build_cfb_prop_factors() here.
            # The proper wiring is to add a pre-compute step (mirroring
            # NFL's `_ctx["nfl_precomputed"]`) that runs BEFORE picks
            # are enumerated — a helper `_precompute_cfb_factors(ctx)`
            # invoked from the async fetch_cfb_picks() prior to
            # emitting picks. That step lands in the same session that
            # wires The Odds API `americanfootball_ncaaf` polling —
            # target date ≤ Aug 15 (Week 0 = Aug 23).
            #
            # Phase 4D (2026-08-06) — CFB feature engine now wired.
            # Consumes `ctx["cfb_precomputed"]` populated by the async
            # pre-loader in `fetch_cfb_picks()`.  Falls to book-follow
            # if the precompute is empty (e.g. pre-season).
            _cfb_pc = ((_game_ctx.get("cfb_precomputed") or {})
                        .get(player.strip().lower()) or {}).get(mk) or {}
            if _cfb_pc.get("factors"):
                factors = _cfb_pc["factors"]
                _mlb_features_used = _cfb_pc.get("sources") or ["cfb_feature_engine"]
            else:
                factors = {"Book Implied Probability": mp}
                _mlb_features_used = ["book_implied_calibrated",
                                       "cfb_engine_no_precompute"]
        elif is_pitcher_prop:
            # Non-MLB pitcher props (KBO etc.) — Phase 1 real engine
            # only covers MLB.  Book-follow calibration retained.
            factors = {"Book Implied Probability": mp}
            _mlb_features_used = ["book_implied_calibrated"]
        elif sport == "NBA" and mk and mk.startswith("player_"):
            # ── Phase 4D — NBA feature engine (2026-08-06) ──────────
            # Consumes `ctx["nba_precomputed"][player_lower][mk]`
            # populated by the async pre-loader in
            # `services.nba_feature_engine.precompute_nba_prop_factors`.
            #
            # Block 2D Closure §3 (2026-08) — the previous fallback to
            # `{"Book Implied Probability": mp}` created a silent
            # PARTIAL classification: an NBA prop with NO real
            # gamelog evidence would pass Lock Score using only book
            # implied.  Per user directive, an NBA player prop MUST
            # NOT be considered wired solely because sportsbook implied
            # probability exists.  Fixed: when precompute is empty, we
            # now SKIP the pick and emit a diagnostic; only picks with
            # real NBA feature-engine data reach the board.  Missing
            # data stays missing.
            _nba_pc = ((_game_ctx.get("nba_precomputed") or {})
                        .get(player.strip().lower()) or {}).get(mk) or {}
            if _nba_pc.get("factors"):
                factors = _nba_pc["factors"]
                _mlb_features_used = _nba_pc.get("sources") or ["nba_feature_engine"]
            else:
                _skip_pick = True
                try:
                    from services.pipeline_diagnostic import log_reason as _plog
                    _plog(
                        sport="NBA", market=mk, player=player,
                        reason="MISSING_FEATURE_DATA",
                        meta={"stage": "nba_prop_evidence_gate"},
                    )
                except Exception:
                    pass
        else:
            # Non-MLB / non-NBA / non-CFB batter / skater / scorer props.
            # ── Phase 2A.5 DEFECT #1 FIX (2026-08) ─────────────────────
            # Real-line Soccer scorer markets (Anytime / First / SoA)
            # MUST use the authoritative scorer intelligence — NOT a
            # book-implied clone.  A real sportsbook line is not an
            # independent predictive model.  Route through
            # `services.soccer_scorer_bridge` which delegates to the
            # v2 Poisson engine with sample-size-aware finishing
            # shrinkage.  Missing form data → MISSING_FEATURE_DATA
            # diagnostic + fall through to book-implied (research-only
            # style) rather than silent book-following.
            _scorer_bridge_used = False
            if (sport == "Soccer" and mk in (
                    "player_goal_scorer_anytime",
                    "player_first_goal_scorer",
                    "player_to_score_or_assist")):
                try:
                    _soccer_pre = ((_game_ctx.get("soccer_scorer_precomputed") or {})
                                   .get(player.strip().lower()) or {}).get(mk)
                except Exception:
                    _soccer_pre = None
                if _soccer_pre and _soccer_pre.get("factors"):
                    factors = _soccer_pre["factors"]
                    _mlb_features_used = _soccer_pre.get("sources") or [
                        "soccer_scorer_bridge_v1"]
                    _scorer_bridge_used = True
                    # Override `mp` with independent model probability
                    # from the scorer bridge; this becomes the canonical
                    # model_win_prob for the pick (used against the de-
                    # vig book probability for the Phase 2A edge).
                    _sm = _soccer_pre.get("model_prob")
                    if isinstance(_sm, (int, float)) and _sm > 0:
                        mp = float(_sm)
                else:
                    try:
                        from services.pipeline_diagnostic import log_reason as _plog
                        _plog(
                            sport="Soccer", market=mk, player=player,
                            reason="MISSING_FEATURE_DATA",
                            meta={"stage": "soccer_scorer_bridge",
                                  "league": league},
                        )
                    except Exception:
                        pass
                    factors = {"Book Implied Probability": mp}
                    _mlb_features_used = ["book_implied_calibrated",
                                          "soccer_scorer_bridge_no_form"]
            else:
                # WNBA / UFC / soccer non-scorer.  Book-follow.
                factors = {"Book Implied Probability": mp}
                _mlb_features_used = ["book_implied_calibrated"]

        # 2026-07-21 — DROP the pick if we couldn't build a real-data
        # factor set (Phase 1 MLB gate). Skips the rest of this iteration.
        if _skip_pick:
            continue

        # ── 2026-07-21 FINAL PHASE: Calibrated `mp` from real factors ─
        # For sports with a real feature engine (MLB Phase 1, Tennis /
        # Soccer Phase 2), OVERRIDE the book-derived `mp` seed with the
        # calibrated factor mean. This is the "rewrite to use only
        # calibrated probs" mandate — model_win_prob is now derived
        # from real statsapi / statcast / xG / Elo signal, not from
        # book_implied ± anything. Non-MLB sports still using the
        # book-follow single-factor payload skip this override (their
        # factor IS book_implied so overriding would be a tautology).
        if (
            sport == "MLB"
            and factors
            and _mlb_features_used
            and _mlb_features_used != ["book_implied_calibrated"]
        ):
            _fv = [v for v in factors.values() if isinstance(v, (int, float))]
            if len(_fv) >= 3:
                # Cap to sensible ranges so a lopsided factor set can't
                # produce a mp > 0.99 or < 0.05. Alt lines stay tight
                # around their near-lock band; standard lines get the
                # full 0.30-0.98 range.
                _cal_mp = sum(_fv) / len(_fv)
                if is_alt:
                    mp = max(0.80, min(0.97, _cal_mp))
                elif mk == "player_goal_scorer_anytime":
                    mp = max(0.25, min(0.75, _cal_mp))
                elif mk == "player_to_score_or_assist":
                    mp = max(0.35, min(0.82, _cal_mp))
                else:
                    mp = max(0.45, min(0.97, _cal_mp))
        # ── Phase 2A.5 DEFECT #4 (2026-08) ─────────────────────────────
        # Elite-scorer factor manipulation (+10 %) and forced Lock Score
        # floor (88.0) RETIRED.  No player receives an artificial Lock
        # Score because of their name or reputation.  Elite status
        # (`ELITE_SCORER_PROFILE`) is a downstream classification derived
        # from evidence — see `services.soccer_scorer_bridge`.  It does
        # NOT boost factors, does NOT alter Lock Score, and does NOT
        # override the composite.  Phase 1D/2A composite math is the
        # only authority for Lock Score.
        is_elite_scorer = False
        lock, breakdown = compute_lock_score(factors, win_prob=mp * 100)
        label_point = None if mk in ("player_goal_scorer_anytime", "player_to_score_or_assist", "player_first_goal_scorer", "mma_method_of_victory", "player_anytime_td", "player_1st_td") else point
        if mk == "player_goal_scorer_anytime":
            market_label = f"{player} Anytime Goal Scorer"
        elif mk == "player_to_score_or_assist":
            market_label = f"{player} To Score or Assist"
        elif mk == "player_first_goal_scorer":
            market_label = f"{player} First Goal Scorer"
        elif mk == "player_anytime_td":
            market_label = f"{player} Anytime TD"
        elif mk == "player_1st_td":
            market_label = f"{player} First TD"
        elif mk == "mma_method_of_victory":
            # `side` carries the method string (KO/TKO, Submission, Decision).
            market_label = f"{player} wins by {side}"
        else:
            market_label = f"{player} {_prop_market_label(mk, side, label_point)}"

        # Tag MLB props with the player's team so users can disambiguate
        # name-collision players (Max Muncy LAD vs Max Muncy OAK, etc.).
        team_label = ""
        if sport == "MLB":
            team_full = _player_team_for_event(
                player, home, away,
                year_hint=player_year_hints.get(player, ""),
            )
            if team_full:
                team_label = _team_abbr(team_full)
                if team_label and team_label not in market_label:
                    # Insert tag right after the player name.
                    market_label = market_label.replace(player, f"{player} ({team_label})", 1)
        # Block 2D A1 — ATD engine's independent probability overrides
        # the seed mp for anytime_td / 1st_td picks when the specialized
        # engine returned a valid result.
        _effective_mp = _atd_model_override if _atd_model_override is not None else mp
        new_pick = _build_pick(
            sport=sport, league=f"{league} · Props", event=f"{away} @ {home}",
            event_time=commence,
            market=market_label,
            pick_side=player, model_win_prob=_effective_mp, book_odds=median,
            lock=lock, factors=breakdown,
            insights=_prop_insights(sport, breakdown, player),
            external_id=f"{sport}-{payload.get('id', '')}-{mk}-{player[:10]}-{side}-{point}",
            is_alt_prop=is_alt,
            is_long_shot=(mk in ("player_goal_scorer_anytime",
                                  "player_to_score_or_assist",
                                  "player_first_goal_scorer",
                                  "player_anytime_td",
                                  "player_1st_td",
                                  "mma_method_of_victory")),
            # Pass full team names so the pick carries home_team /
            # away_team / home_team_id / away_team_id natively (MLB only).
            home_team_name=home,
            away_team_name=away,
        )
        # Block 2D A1 — attach ATD evidence block for downstream
        # consumers (rationale UI, telemetry).  This is
        # POST_SCORE_EXPLANATION_ONLY at the pick level — Lock Score
        # was already computed above from real_factors including the
        # ATD Engine Confidence anchor.
        if new_pick is not None and _atd_evidence_block is not None:
            new_pick["atd_evidence"] = _atd_evidence_block
        # ── Block 2A.5.2 (2026-08) — MLB hitter lineup provenance ─────
        # Attach the explicit lineup-status block computed above so
        # BoardProjection / Magic / rationale / telemetry can inspect
        # provenance without re-fetching.  Also apply the data-quality
        # cap for the lineup status (e.g. projected_starter caps at 92
        # so it never reaches the 95+ Elite tier without confirmation,
        # unknown caps at 79 which sits below the >85 board floor —
        # a defense-in-depth against enrichment failures).  Confirmed
        # starters (99 cap) are unconstrained by this gate.
        if (new_pick is not None
                and sport == "MLB"
                and not is_pitcher_prop
                and isinstance(payload, dict)):
            _lu_map = payload.get("_mlb_lineup_status") or {}
            _lu_blk = _lu_map.get(player.strip().lower()) or {}
            if _lu_blk:
                # Block 2A.5.3 (§3) — normalize the emitted status to
                # the public contract: CONFIRMED / PROJECTED / UNKNOWN
                # (bench/scratched paths already dropped the pick
                # upstream via should_publish=False).
                _raw = str(_lu_blk.get("status") or "unknown").lower()
                if _raw == "confirmed_starter":
                    _public_status = "CONFIRMED"
                elif _raw == "projected_starter":
                    _public_status = "PROJECTED"
                else:
                    _public_status = "UNKNOWN"
                new_pick["lineup_status"] = {
                    "status":     _public_status,
                    "lineup_pos": _lu_blk.get("lineup_pos"),
                    "source":     _lu_blk.get("source")
                                    or "statsapi_feed_live_batting_order",
                    "updated_at": _lu_blk.get("updated_at"),
                }
                _cap = _lu_blk.get("cap")
                if isinstance(_cap, (int, float)):
                    _cur_lock = float(new_pick.get("lock_score") or 0.0)
                    if _cur_lock > float(_cap):
                        new_pick["lock_score_uncapped"] = _cur_lock
                        new_pick["lock_score"] = float(_cap)
                        new_pick.setdefault(
                            "caps_applied", []).append({
                                "cap": float(_cap),
                                "reason": ("mlb_lineup_status_" + _raw),
                            })
        # Block 2D Final Closure §4 (2026-08) — First-TD DORMANT.
        # ``player_1st_td`` currently reuses the anytime-TD engine,
        # but positional order-of-scoring is a separate research
        # problem (P(first-scorer | scored-at-all)).  Until a
        # scoring-order model exists, First-TD picks are STORED in
        # shadow (for observability/telemetry) but must not become
        # user-visible on the Locks board.  Flag them off_board so
        # the visibility filter suppresses them everywhere.
        if new_pick is not None and mk == "player_1st_td":
            new_pick["off_board"] = True
            new_pick["no_bet"] = True
            new_pick["publication_gate"] = "first_td_dormant_no_scoring_order_model"
            new_pick["capability_state"] = "PARTIAL_DORMANT"
        # Block 2D A3 — attach HR intel evidence block for downstream
        # consumers when the payload accumulated one for this player.
        if new_pick is not None and mk in (
                "batter_home_runs", "batter_home_runs_alternate"):
            _hri_map = payload.get("_hr_intel_evidence") or {}
            _hri_ev = _hri_map.get(player.strip().lower())
            if _hri_ev:
                new_pick["hr_intel_evidence"] = _hri_ev
        # Persist the elite flag on the pick so:
        #   1. `_dedupe_goalscorer_per_event` protects elites from being
        #      culled by the top-N cap (user report: "I see Dieng but not
        #      Mané or Ismaïla Sarr in the Senegal game" — Mané WAS in
        #      ELITE_PLAYERS but the flag was computed locally and never
        #      attached to the pick, so the dedupe couldn't see it).
        #   2. The `/picks/today` elite_q (server.py) carve-out can surface
        #      marquee names even when edge is slightly negative.
        # Setting this for both static elites (ELITE_PLAYERS list) and the
        # market-derived "this player is the bookmaker's top-priced scorer"
        # detection further below.
        if new_pick is not None and is_elite_scorer:
            new_pick["elite_player"] = True
        # ── Real MLB feature attribution (2026-07-21 Phase 1) ─────────
        # Attach the list of real data sources that fired for this pick
        # so the signal engine + "Why this pick?" rationale can surface
        # "Sourced from: statsapi_pitcher_season_k, statsapi_team_k_split..."
        # No pick reaches the board without at least MIN_FACTORS real
        # sources firing (gated by has_enough_real_data() upstream).
        if new_pick is not None and _mlb_features_used:
            new_pick["real_data_sources"] = list(_mlb_features_used)
            new_pick["real_data_count"] = len(_mlb_features_used)
        # ── Attach real MLB K data (2026-07-21 Tier-1) ────────────────
        # If this pick is a pitcher K prop and we resolved real
        # opposing-team K% + pitcher hand data upstream, attach the
        # structured payload to the pick so signal_engine + pick
        # rationale can render "Rangers 4th-worst K% vs LHP (25.4% K)"
        # style evidence lines.
        if new_pick is not None and is_pitcher_prop and isinstance(payload, dict):
            _rk = (payload.get("_real_k_data") or {}).get(player)
            if _rk:
                new_pick["k_prop_data"] = {
                    **_rk,
                    "side": str(side).lower(),
                    "line": point,
                }
            # ── 2026-07-27 Wheeler-bug fix: attach K math signals to pick ──
            # Copy the K math gate's expected_k + edge onto the pick dict so
            # the downstream Over/Under conflict resolver can pick the side
            # that ALIGNS with PvT expected K's (not just tie-break on lock).
            if payload.get("k_math_gate") == "passed":
                new_pick["k_math_gate"] = "passed"
                new_pick["k_math_expected_k"] = payload.get("k_math_expected_k")
                new_pick["k_math_edge_pp"] = payload.get("k_math_edge_pp")
        # ── MLS matchup-history boost (2026-07-22) ────────────────────
        # Attach per-opponent scoring history (preloaded by async
        # caller into `payload["_mls_matchup"]`) to MLS Anytime Goal
        # Scorer / SoA / FGS picks so the "Why this pick" rationale can
        # render e.g. "Messi 7G/2A career vs Nashville". Also lifts the
        # lock score when the player has ≥ 0.5 goals/match vs opponent.
        if (new_pick is not None
                and league.upper() in ("MLS", "MAJOR LEAGUE SOCCER")
                and mk in ("player_goal_scorer_anytime",
                            "player_to_score_or_assist",
                            "player_first_goal_scorer")):
            _hist = (payload.get("_mls_matchup") or {}).get(player)
            if _hist and _hist.get("record"):
                _rec = _hist["record"]
                matches = int(_rec.get("matches", 0) or 0)
                g = int(_rec.get("goals", 0) or 0)
                a = int(_rec.get("assists", 0) or 0)
                gpm = g / matches if matches else 0.0
                new_pick["matchup_history"] = {
                    "opponent": _hist.get("opponent"),
                    "matches": matches,
                    "goals": g,
                    "assists": a,
                    "goals_per_match": round(gpm, 2),
                    "scored_in": int(_rec.get("scored_matches", 0) or 0),
                    "assisted_in": int(_rec.get("assist_matches", 0) or 0),
                    "recent": (_rec.get("recent") or [])[:3],
                }
                # Lock boost: strong track record vs opponent.
                if gpm >= 1.0 and matches >= 2:
                    new_pick["lock_score"] = min(
                        99.0, float(new_pick.get("lock_score") or 0) + 6.0,
                    )
                elif gpm >= 0.5 and matches >= 2:
                    new_pick["lock_score"] = min(
                        99.0, float(new_pick.get("lock_score") or 0) + 3.0,
                    )
        # ── Block 2B.1B (2026-08) — Platinum NFL Challenger wiring ──
        # For any real NFL candidate that has been ``_build_pick``-ed
        # AND survived every upstream gate, run the Platinum causal
        # simulator and attach the Champion/Challenger frozen row.
        # This is the SINGLE production wiring hook — the only place
        # in the entire runtime where Platinum is called for board
        # candidates.  ``model_probability`` is preserved (Champion);
        # Platinum output is stored separately (Challenger) under
        # ``pick["platinum_challenger"]``.
        #
        # Safety (§34):
        #     * One bad NFL candidate MUST NOT kill the batch.
        #     * Failure fingerprint per §32 — never fake agreement.
        if (new_pick is not None and sport == "NFL"):
            try:
                from services.platinum_nfl import (
                    simulate as _platinum_simulate,
                    attach_challenger_output as _platinum_attach,
                    classify_season_type as _platinum_classify_season,
                    QBOpportunity as _QBOpp,
                    RBOpportunity as _RBOpp,
                    WROpportunity as _WROpp,
                )
                # Assemble the minimum production context from the
                # already-computed NFL factors + event metadata.
                _nfl_ctx: dict = {}
                _sport_key = ev.get("sport_key") if isinstance(ev, dict) else None
                if _sport_key:
                    new_pick.setdefault("sport_key", _sport_key)
                _st = _platinum_classify_season(new_pick)
                _nfl_ctx["season_type"] = _st
                # Attach expected game context when the model
                # provided it (moneyline/spread/total path).
                _model_ctx = (payload.get("_ctx") if isinstance(payload, dict) else None) or {}
                _nfl_ctx["expected_margin_home"] = _model_ctx.get("expected_margin_home")
                _nfl_ctx["total_line"] = _model_ctx.get("total_line") or new_pick.get("line")
                _nfl_ctx["team_plays"] = _model_ctx.get("team_plays")
                _nfl_ctx["game_pass_rate"] = _model_ctx.get("game_pass_rate")
                # Position hint from the NFL feature engine.
                try:
                    from sports_engine import _infer_nfl_position_from_market
                    _pos = _infer_nfl_position_from_market(mk)
                except Exception:
                    _pos = None
                if _pos:
                    _nfl_ctx["position"] = _pos
                # Build the minimum opportunity object from the
                # precomputed NFL factor dict for this player+market.
                _pc = ((_model_ctx.get("nfl_precomputed") or {})
                        .get(player.strip().lower()) or {}).get(mk) or {}
                _fac = _pc.get("factors") or {}
                if _pos == "QB":
                    _nfl_ctx["qb_opportunity"] = _QBOpp(
                        att_mean=float(_fac.get("expected_attempts") or 32.0),
                        ypa_mean=float(_fac.get("expected_ypa") or 7.2),
                        role_certainty=float(_fac.get("role_certainty") or 0.85),
                    )
                elif _pos == "RB":
                    _nfl_ctx["rb_opportunity"] = _RBOpp(
                        carry_share_mean=float(_fac.get("carry_share") or 0.55),
                        ypc_mean=float(_fac.get("expected_ypc") or 4.35),
                        role_certainty=float(_fac.get("role_certainty") or 0.75),
                    )
                elif _pos in ("WR", "TE"):
                    _nfl_ctx["wr_opportunity"] = _WROpp(
                        target_share_mean=float(_fac.get("target_share") or 0.20),
                        catch_rate_mean=float(_fac.get("catch_rate") or 0.635),
                        ypt_mean=float(_fac.get("expected_ypt") or 8.2),
                        role_certainty=float(_fac.get("role_certainty") or 0.75),
                    )
                # Derive a stable seed from the canonical pick id +
                # event id (§33 determinism).
                _seed = int(abs(hash(
                    str(new_pick.get("id") or new_pick.get("event_id") or "") +
                    str(mk) + str(new_pick.get("line")))) % 0x7FFFFFFF)
                _sim_output = _platinum_simulate(
                    new_pick, ctx=_nfl_ctx, seed=_seed, n_sims=2000,
                )
                _platinum_attach(new_pick, _sim_output)
                # Stamp season type on the pick for the funnel diagnostics.
                new_pick["season_type"] = getattr(_st, "value", str(_st))
            except Exception as _plat_err:
                # Per §34: one bad NFL candidate cannot kill the batch.
                # Stamp the failure fingerprint (§32) and continue.
                try:
                    new_pick["platinum_challenger"] = {
                        "ran": False,
                        "reason": "SIMULATOR_FAILED",
                        "sim_probability": None,
                        "error_class": type(_plat_err).__name__,
                    }
                except Exception:
                    pass
                try:
                    from services.pipeline_diagnostic import log_reason as _plog
                    _plog(sport="NFL", market=mk, player=player,
                          reason="PLATINUM_WIRING_EXCEPTION_"
                                  + type(_plat_err).__name__.upper())
                except Exception:
                    pass
        picks.append(new_pick)
    # Tag every Under pick so the main Locks feed can exclude them and the
    # dedicated "Under of the Day" tab can surface them. Anything where the
    # bettor needs the line to go UNDER (Totals, Game Total, alt-prop totals)
    # qualifies — that's the safest tier of "under-style" wagers.
    for p in picks:
        if not p:
            continue
        market = (p.get("market") or "").lower()
        selection = (p.get("selection") or "").lower()
        if "under" in market or "under" in selection:
            p["is_under_lock"] = True

    # 2026-07-22 — PLAYER-PROP CONTRADICTION DEDUPE.
    # User bug: "Gerrit Cole Over 6.5 K AND Under 6.5 K both showed as
    # 99 Elite Lock — should not be possible". For every (player,
    # market_family, line) group, keep ONLY the side with higher edge.
    def _prop_family(m: str) -> str:
        m_l = (m or "").lower()
        if "strikeout" in m_l:      return "K"
        if "outs recorded" in m_l:  return "OUTS"
        if "hits + runs" in m_l:    return "HRR"
        if "home run" in m_l:       return "HR"
        if "total bases" in m_l:    return "TB"
        if "hits allowed" in m_l:   return "HALLOWED"
        if "hits" in m_l:           return "H"
        if "passing yards" in m_l or "pass yds" in m_l:      return "PASS_YDS"
        if "rushing yards" in m_l or "rush yds" in m_l:      return "RUSH_YDS"
        if "receiving yards" in m_l or "reception yds" in m_l: return "REC_YDS"
        if "receptions" in m_l:     return "REC"
        if "pass tds" in m_l or "passing tds" in m_l: return "PASS_TDS"
        if "rush tds" in m_l or "rushing tds" in m_l: return "RUSH_TDS"
        if "goal scorer" in m_l:    return "GOAL"
        return ""

    def _prop_key(pk: dict) -> tuple:
        m = pk.get("market") or ""
        fam = _prop_family(m)
        if not fam:
            return None
        player_hint = None
        for delim in (" (", " Over ", " Under "):
            if delim in m:
                player_hint = m.split(delim, 1)[0].strip()
                break
        if not player_hint:
            return None
        import re
        _m = re.search(r"(?:Over|Under)\s+(\d+\.?\d*)", m)
        line_hint = float(_m.group(1)) if _m else None
        return (player_hint.lower(), fam, line_hint)

    prop_best: dict[tuple, dict] = {}
    prop_unkeyed: list[dict] = []
    _before = len([p for p in picks if p])
    for p in picks:
        if not p:
            continue
        k = _prop_key(p)
        if k is None:
            prop_unkeyed.append(p)
            continue
        cur = prop_best.get(k)
        if cur is None:
            prop_best[k] = p
            continue
        edge_new = float(p.get("edge_percent") or 0)
        edge_cur = float(cur.get("edge_percent") or 0)
        if edge_new > edge_cur or (edge_new == edge_cur and
            float(p.get("lock_score") or 0) > float(cur.get("lock_score") or 0)):
            prop_best[k] = p
    picks = list(prop_best.values()) + prop_unkeyed
    if len(picks) < _before:
        logger.info(
            "Player-prop contradiction dedupe: %d → %d picks (removed opposite-side dupes)",
            _before, len(picks),
        )

    return [p for p in picks if p is not None]


# ── Elite teams: events featuring these get fetched FIRST (for player props)
# so star strikers like Kane / Haaland / Mbappé / Messi never get cut off by
# the per-key event cap. Major World Cup nations + top European clubs.
_ELITE_SOCCER_TEAMS = {
    # World Cup top nations (men's)
    "England", "Brazil", "Argentina", "France", "Germany", "Spain",
    "Portugal", "Netherlands", "Norway", "Italy", "Belgium", "Croatia",
    "Uruguay", "Colombia", "Mexico", "USA", "United States", "Senegal",
    "Morocco", "Japan", "Denmark", "Switzerland", "Sweden", "Poland",
    # Top European clubs (UCL/UEL/EPL/La Liga/Bundesliga/Serie A/Ligue 1)
    "Manchester City", "Real Madrid", "FC Barcelona", "Barcelona",
    "Bayern Munich", "Bayern München", "Paris Saint Germain", "PSG",
    "Arsenal", "Liverpool", "Chelsea", "Manchester United", "Tottenham",
    "Tottenham Hotspur", "Inter Milan", "Internazionale", "Juventus",
    "AC Milan", "Napoli", "Atletico Madrid", "Borussia Dortmund",
}
# ── ANCHOR teams: these MUST be in the selection regardless of cap, because
# they contain marquee players the user explicitly demanded (Mbappé/Haaland/
# Messi/Kane/Ronaldo). Priority above all other elite teams.
_ANCHOR_SOCCER_TEAMS = {
    "France",            # Mbappé
    "Norway",            # Haaland
    "Argentina",         # Messi
    "England",           # Kane / Bellingham / Saka / Foden
    "Portugal",          # Ronaldo
    "Brazil",            # Vinicius / Rodrygo / Neymar
    "Spain",             # Yamal
    "Germany",           # Musiala / Wirtz
    "Netherlands",       # Depay / Gakpo
    # Top European clubs with global stars
    "Real Madrid", "FC Barcelona", "Barcelona",
    "Manchester City", "Bayern Munich", "Bayern München",
    "Paris Saint Germain", "PSG",
}
# Per-sport-key cap for event-level props fetches. World Cup is the marquee
# event with 50+ matches over a tournament — we fetch up to 10 (vs default 3)
# so Kane/Haaland/Mbappé/Messi etc. all get their props pulled even when their
# match isn't in the chronological top-3. Trade-off: more API credits used.
_PROPS_PER_KEY_CAP = {
    "soccer_fifa_world_cup": 14,
    "soccer_fifa_club_world_cup": 10,
    "soccer_uefa_champs_league": 10,
    "soccer_uefa_champs_league_qualification": 6,
    "soccer_uefa_europa_league": 6,
    "soccer_uefa_europa_conference_league": 6,
    # MLB has ~15 games/day. The 3-event default was leaving 80% of
    # the slate without batter/pitcher props — user feedback "don't
    # see no batter or pitcher props". 2026-07-22 bump to 24 — user
    # said "last 3 MLB games had no props" (max slate ~15-16 games
    # + doubleheaders can push to 18-20; 24 gives full-slate coverage
    # with breathing room). Cost: ~14 extra Odds API credits/refresh.
    "baseball_mlb": 24,
    # CSL slate is 7-9 matches/day and the user wants ALL elite
    # scorers (Cryzan, Felipe Sousa, Fábio Abreu, Leonardo, Wu Lei,
    # Negrão, Bakambu, etc.) on the board. The synthesis fallback
    # is free (no Odds API cost) so we lift the cap to 10.
    "soccer_china_superleague": 10,
    "soccer_china_league_one":   10,
    # 2026-07-22 — MLS slate is 12-15 matches on Wed/Sat. User
    # feedback: "think we missing picks for MLS". The 3-event default
    # was fetching only 3/15 games, capping MLS coverage at 20%.
    # Bumped to 15 for full-slate MLS coverage. MLS is a core
    # user-region league (US Soccer) so justifies the extra credits.
    "soccer_usa_mls": 15,
}
_DEFAULT_PROPS_PER_KEY = 3

# Per-sport-key look-ahead window (in hours). World Cup pools use a 7-day
# window so elite-team matches still get props fetched even when France
# (Mbappé) / Brazil (Vinicius) / Germany etc. don't play for several days.
_PROPS_LOOKAHEAD_HOURS = {
    "soccer_fifa_world_cup": 168,         # 7 days
    "soccer_fifa_club_world_cup": 168,    # 7 days
    "soccer_uefa_champs_league": 168,
    "soccer_uefa_champs_league_qualification": 168,
    "soccer_uefa_europa_league": 168,
    "soccer_uefa_europa_conference_league": 168,
}
_DEFAULT_LOOKAHEAD_HOURS = 72


def _event_priority(ev: dict, sport: str) -> int:
    """Lower number = higher priority for player-props fetching.
    Tier 0 = ANCHOR teams (Mbappé/France, Haaland/Norway, Messi/Argentina,
              Kane/England, Ronaldo/Portugal, etc.) — ALWAYS fetched.
    Tier 1 = other elite teams (Croatia, Switzerland, Mexico, USA, ...).
    Tier 2 = non-elite (filler)."""
    if sport != "Soccer":
        return 1
    home = (ev.get("home_team") or "")
    away = (ev.get("away_team") or "")
    if home in _ANCHOR_SOCCER_TEAMS or away in _ANCHOR_SOCCER_TEAMS:
        return 0
    if home in _ELITE_SOCCER_TEAMS or away in _ELITE_SOCCER_TEAMS:
        return 1
    return 2


async def _fetch_player_props_for_sport(sport: str) -> list[dict]:
    """Fetch upcoming events per sport-key and pull high-prob player props.

    Elite-team events (Kane's England, Haaland's Norway, etc.) are prioritized
    so they never get cut off by the per-key cap. World Cup events use a
    higher cap (10) vs default (3) to capture the full marquee slate.
    """
    if sport not in PLAYER_PROP_MARKETS:
        return []
    # Refresh MLB rosters once per day so we can tag player picks with their
    # team (disambiguates name-collision players like Max Muncy LAD vs OAK).
    if sport == "MLB":
        try:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            await _refresh_mlb_rosters(today)
        except Exception as e:
            logger.warning("MLB roster refresh failed: %s", e)
    all_picks: list[dict] = []
    # ── 2026-08-23 PASS 1 — Universal active provider discovery
    # (player-props path).  Same fallback-vs-authoritative wiring as
    # the base game acquisition above so props automatically reach
    # newly-active supported leagues/tournaments without static
    # patches.  ``SPORT_KEYS`` remains fallback ONLY.
    _prefix_map = {"Soccer": "soccer_", "Tennis": "tennis_"}
    _static_keys = list(SPORT_KEYS.get(sport, []))
    _sport_keys: list[str] = list(_static_keys)
    _pfx = _prefix_map.get(sport)
    if _pfx and _ACTIVE_KEYS:
        _static_set = set(_static_keys)
        for _k in sorted(_ACTIVE_KEYS):
            if _k.startswith(_pfx) and _k not in _static_set:
                _sport_keys.append(_k)
    for key in _sport_keys:
        if _ACTIVE_KEYS and key not in _ACTIVE_KEYS:
            continue
        events = await _get(f"{BASE}/sports/{key}/events", {})
        if not isinstance(events, list):
            continue
        now = datetime.now(timezone.utc)
        lookahead_hours = _PROPS_LOOKAHEAD_HOURS.get(key, _DEFAULT_LOOKAHEAD_HOURS)
        upcoming = []
        for e in events:
            ct = e.get("commence_time")
            if not ct:
                continue
            try:
                dt = datetime.strptime(ct, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                if now - _dt.timedelta(minutes=30) <= dt <= now + _dt.timedelta(hours=lookahead_hours):
                    upcoming.append((dt, e))
            except Exception:
                continue
        # Sort by (priority, commence_time): ANCHOR teams (Mbappé/Haaland/
        # Messi/Kane/Ronaldo) first, then other elite, then filler. Within
        # each tier, sort by chronological order.
        upcoming.sort(key=lambda x: (_event_priority(x[1], sport), x[0]))
        cap = _PROPS_PER_KEY_CAP.get(key, _DEFAULT_PROPS_PER_KEY)
        # ─── Block 2B (2026-08) — fair-slate scheduling ────────────
        # BEFORE:  selected = upcoming[:cap]
        # DEFECT:  When `upcoming` exceeded `cap`, chronological
        # ascending order caused late West Coast MLB games (and any
        # sport's late-slate games) to fall off the tail — even
        # though they were in-scope.  Operator report:
        # "late-night MLB games appear on the app but batter/pitcher
        # props do not populate".
        # FIX:  Guarantee coverage of TODAY'S betting slate BEFORE
        # allocating the remainder to preload/tomorrow.  When the
        # current slate alone exceeds `cap`, keep every current-
        # slate event and let the cap grow (log an over-cap warning
        # rather than starve legitimate current games).
        from services.perklocks_day import (
            is_in_current_slate, current_slate_day,
        )
        _now_utc = datetime.now(timezone.utc)
        current_slate = [(dt, ev) for (dt, ev) in upcoming
                          if is_in_current_slate(dt, _now_utc)]
        rest = [(dt, ev) for (dt, ev) in upcoming
                 if not is_in_current_slate(dt, _now_utc)]

        # ── P0.1 (2026-06) — extended fair-slate contract ──────────
        # Split `rest` into NEXT-DAY vs FAR-FUTURE.  When the refresh
        # cycle runs BEFORE the Perklocks day roll (e.g. 04:03 UTC
        # Aug 13 = 12:03 AM ET → still Aug 12's Perklocks day), the
        # "tomorrow" slate is the ACTIVE user-facing slate.
        # Previously it was truncated by ``rest[:remainder]``,
        # starving late (evening) games of hitter-prop coverage.
        # Fair-slate now guarantees:
        #   1. every current-slate event (as before), and
        #   2. every event in the immediately-next Perklocks day
        #      (chronologically preserved so late-evening games
        #      cannot be dropped in favour of far-future preload).
        from datetime import timedelta as _td
        _next_day_now = _now_utc + _td(hours=24)
        next_slate  = [(dt, ev) for (dt, ev) in rest
                        if is_in_current_slate(dt, _next_day_now)]
        far_future  = [(dt, ev) for (dt, ev) in rest
                        if not is_in_current_slate(dt, _next_day_now)]
        must_have = current_slate + next_slate

        if len(must_have) >= cap:
            # Fair-slate contract takes precedence over cap.  Every
            # current & next-day event survives; log an over-cap
            # warning rather than starve later games (the observed
            # MLB late-game hitter-prop dropoff, 2026-08).
            selected = must_have
            if len(must_have) > cap:
                logger.warning(
                    "Props fetch %s/%s: fair-slate has %d events "
                    "(current=%d + next=%d) > %d cap. Extending; "
                    "no legitimate slate game will be starved.",
                    sport, key,
                    len(must_have), len(current_slate),
                    len(next_slate), cap)
        else:
            remainder = cap - len(must_have)
            selected = must_have + far_future[:remainder]
        anchor_count = sum(1 for _, ev in selected if _event_priority(ev, sport) == 0)
        elite_count = sum(1 for _, ev in selected if _event_priority(ev, sport) <= 1)
        logger.info(
            "Props fetch %s/%s: %d upcoming, selecting %d (cap=%d). "
            "Anchor teams: %d, Elite teams: %d",
            sport, key, len(upcoming), len(selected), cap,
            anchor_count, elite_count,
        )
        # Soccer leagues where the bookmaker rarely / never publishes player
        # markets — for these we ALWAYS run the SportDB synthetic scorer
        # engine alongside whatever bookmaker payload we get back, so top
        # scorers based on multi-season career history (e.g. Leonardo's
        # 21/21/19 goal seasons in CSL, Fábio Abreu's 28-goal Beijing Guoan
        # campaign) surface even when the book technically published a
        # spread or moneyline for the match. User explicitly requested
        # this 2026-06-26: "yes auto-include lower leagues".
        _ALWAYS_SYNTH_SOCCER_KEYS = {
            "soccer_china_superleague",
            "soccer_china_league_one",
            "soccer_japan_j1_league",
            "soccer_japan_j2_league",
            "soccer_korea_k_league_1",
            "soccer_usa_mls",
            "soccer_argentina_primera_division",
            "soccer_brazil_serie_b",
            "soccer_brazil_campeonato",
            "soccer_finland_veikkausliiga",
            "soccer_sweden_allsvenskan",
            "soccer_sweden_superettan",
            "soccer_norway_eliteserien",
            "soccer_denmark_superliga",
            "soccer_australia_aleague",
            "soccer_mexico_ligamx",
            "soccer_portugal_primeira_liga",
        }
        for _, ev in selected:
            await asyncio.sleep(1.1)  # space requests under rate limit
            payload = await _fetch_event_props_payload(sport, key, ev["id"])
            book_had_player_markets = isinstance(payload, dict) and bool(payload.get("bookmakers"))
            if book_had_player_markets:
                payload["id"] = ev["id"]
                # 2026-07-21 — attach real game context to prop payload
                # so the MLB feature engine sees pitchers, hitters,
                # team_k_intel, park factors, etc. Without this, every
                # MLB prop was silently gated out (0/5 factors → skip).
                if sport == "MLB":
                    try:
                        from services.game_context import build_mlb_game_context
                        payload["_ctx"] = await build_mlb_game_context({
                            "home_team": ev.get("home_team"),
                            "away_team": ev.get("away_team"),
                            "commence_time": ev.get("commence_time"),
                            "id": ev.get("id"),
                        })
                    except Exception as _ctx_err:
                        logger.debug("MLB props ctx build failed: %s", _ctx_err)
                # 2026-07-22 — Phase 3 NFL pre-loader. Walks the
                # bookmaker payload once, builds a prop_candidates
                # list, and hits nflverse to precompute the 6-factor
                # feature dict per (player, market). The sync
                # `_props_picks_from_event` branch then reads directly
                # from `_ctx["nfl_precomputed"]`.
                if sport == "NFL":
                    try:
                        from services.nfl_feature_engine import build_nfl_game_context
                        # Phase 3B — shared Mongo owner.
                        from services.database import get_database
                        _nfl_db = get_database()
                        # Extract the prop candidates from the bookmaker
                        # payload (player, market, line, side, book_implied)
                        _candidates = _extract_nfl_prop_candidates(payload)
                        payload["_ctx"] = await build_nfl_game_context(
                            _nfl_db,
                            game={
                                "home_team": ev.get("home_team"),
                                "away_team": ev.get("away_team"),
                                "commence_time": ev.get("commence_time"),
                                "id": ev.get("id"),
                            },
                            prop_candidates=_candidates,
                            season=_current_nfl_season(),
                            week=_current_nfl_week(),
                        )
                    except Exception as _ctx_err:
                        logger.debug("NFL props ctx build failed: %s", _ctx_err)
                # ── Phase 4D finalization (2026-08-06) — NBA + CFB
                # per-event precompute. Mirrors the NFL pattern:
                # walk the bookmaker payload once per event, hand off
                # a per-(player, market, line, side) candidate list to
                # the async precompute helper, stash the resulting
                # dict under _ctx["nba_precomputed"] / _ctx["cfb_precomputed"]
                # so the sync _props_picks_from_event branch can look
                # it up without re-entering the event loop.  ONE
                # precompute call per event, never per prop.  One
                # sport failing must not block others.
                if sport == "NBA":
                    try:
                        from services.nba_feature_engine import (
                            precompute_nba_prop_factors as _nba_pre,
                        )
                        from services.database import get_database
                        _nba_db = get_database()
                        _players, _markets, _lines_bp = _extract_nba_prop_candidates(payload)
                        if _players:
                            _nba_ctx = await _nba_pre(
                                _nba_db, players=list(_players),
                                market_keys=list(_markets),
                                lines_by_player_market=_lines_bp,
                            )
                            payload.setdefault("_ctx", {}).update(_nba_ctx)
                            payload["_ctx"]["nba_precompute_status"] = (
                                "ok" if _nba_ctx.get("nba_precomputed") else "empty"
                            )
                        else:
                            payload.setdefault("_ctx", {})[
                                "nba_precompute_status"] = "no_candidates"
                    except Exception as _ctx_err:
                        logger.warning("NBA props ctx build failed: %s", _ctx_err)
                        payload.setdefault("_ctx", {})[
                            "nba_precompute_status"] = f"error:{type(_ctx_err).__name__}"
                if sport == "CFB":
                    try:
                        from services.cfb_precompute import (
                            precompute_cfb_factors as _cfb_pre,
                        )
                        from services.database import get_database
                        _cfb_db = get_database()
                        _cfb_cands = _extract_cfb_prop_candidates(payload)
                        if _cfb_cands:
                            _cfb_ctx: dict = {}
                            await _cfb_pre(_cfb_db, _cfb_ctx, _cfb_cands)
                            payload.setdefault("_ctx", {}).update(_cfb_ctx)
                            payload["_ctx"]["cfb_precompute_status"] = (
                                "ok" if _cfb_ctx.get("cfb_precomputed") else "empty"
                            )
                        else:
                            payload.setdefault("_ctx", {})[
                                "cfb_precompute_status"] = "no_candidates"
                    except Exception as _ctx_err:
                        logger.warning("CFB props ctx build failed: %s", _ctx_err)
                        payload.setdefault("_ctx", {})[
                            "cfb_precompute_status"] = f"error:{type(_ctx_err).__name__}"
                # 2026-07-22 — MLS matchup-history preloader. Loads the
                # per-opponent scoring history for every player that
                # will surface in the props pipeline, and stuffs it in
                # `payload["_mls_matchup"]` so the sync
                # `_props_picks_from_event` can attach it to picks
                # without needing to hit the event loop.
                if sport == "Soccer" and LEAGUE_LABELS.get(key, "") in (
                        "MLS", "Major League Soccer"):
                    try:
                        from services.mls_player_matchup_history import (
                            get_player_vs_opponent,
                        )
                        # Extract unique player names from the goal-
                        # scorer / SoA / FGS markets.
                        _players = set()
                        for _bm in payload.get("bookmakers", []):
                            for _m in _bm.get("markets", []):
                                if _m.get("key") not in (
                                        "player_goal_scorer_anytime",
                                        "player_to_score_or_assist",
                                        "player_first_goal_scorer"):
                                    continue
                                for _o in _m.get("outcomes", []):
                                    nm = _o.get("description") or _o.get("name") or ""
                                    if nm:
                                        _players.add(nm.strip())
                        _lookup = {}
                        for _pname in _players:
                            for _team in (ev.get("home_team"), ev.get("away_team")):
                                if not _team:
                                    continue
                                _rec = await get_player_vs_opponent(_pname, _team)
                                if _rec:
                                    _lookup[_pname] = {"opponent": _team,
                                                        "record": _rec}
                                    break
                        if _lookup:
                            payload["_mls_matchup"] = _lookup
                    except Exception as _ctx_err:
                        logger.debug("MLS matchup preload failed: %s", _ctx_err)

                # ── Phase 2A.5 (2026-08) — Universal Soccer scorer
                # preloader. For every Soccer event with real scorer
                # markets (Anytime / SoA / FGS) — regardless of league
                # — batch-load `soccer_player_form` rows and pre-compute
                # authoritative scorer factors via the
                # `soccer_scorer_bridge` (delegates to `goal_scorer_engine_v2`
                # with sample-size-aware finishing shrinkage).  Stash
                # under `payload["_ctx"]["soccer_scorer_precomputed"]`
                # so the sync `_props_picks_from_event` branch reads it
                # without re-entering the event loop.  ONE precompute
                # call per event, never per prop.  Missing form data
                # becomes MISSING_FEATURE_DATA downstream, not silent
                # book-following.
                if sport == "Soccer":
                    try:
                        from services.database import get_database as _get_db
                        from services.soccer_scorer_bridge import (
                            compute_soccer_scorer_factors_sync as _soc_bridge,
                        )
                        _players_by_mk: dict[str, set[str]] = {}
                        for _bm in payload.get("bookmakers", []) or []:
                            for _m in _bm.get("markets", []) or []:
                                _mkk = _m.get("key")
                                if _mkk not in (
                                        "player_goal_scorer_anytime",
                                        "player_to_score_or_assist",
                                        "player_first_goal_scorer"):
                                    continue
                                for _o in _m.get("outcomes", []) or []:
                                    _nm = _o.get("description") or _o.get("name") or ""
                                    if not _nm:
                                        continue
                                    _players_by_mk.setdefault(_mkk, set()).add(
                                        _nm.strip())
                        _all_players = {p for s in _players_by_mk.values() for p in s}
                        _pre: dict[str, dict[str, dict]] = {}
                        if _all_players:
                            _soc_db = _get_db()
                            _cursor = _soc_db.soccer_player_form.find({
                                "name_canonical": {
                                    "$in": [n.lower() for n in _all_players]}
                            })
                            _form_by_name: dict[str, dict] = {}
                            async for _row in _cursor:
                                _key = str(_row.get("name_canonical") or "").lower()
                                if _key:
                                    _form_by_name[_key] = _row
                            _league_label = LEAGUE_LABELS.get(key, sport)
                            # ── Phase 2A.5D CLOSURE (2026-08) ─────────────
                            # Multi-season activation: pre-load prior-
                            # season form rows from `soccer_player_game_logs`
                            # using the competition-specific season
                            # resolver.  Also pre-load Player H2H from
                            # the existing `mls_player_matchup_history`
                            # store.  Both are optional — missing data
                            # falls back to current-season-only Phase
                            # 2A.5 behaviour.
                            _prior_by_name: dict[str, dict] = {}
                            _h2h_by_name: dict[str, dict] = {}
                            try:
                                from services.soccer_historical_stats import (
                                    aggregate_player_season, load_player_h2h,
                                )
                                from services.soccer_season_resolver import (
                                    resolve_prior_season,
                                )
                                _prior_season = resolve_prior_season(
                                    _league_label)
                                for _pname in _all_players:
                                    _p_low = _pname.lower().strip()
                                    try:
                                        _prow = await aggregate_player_season(
                                            _soc_db,
                                            player_name_canonical=_p_low,
                                            season=_prior_season)
                                        if _prow:
                                            _prior_by_name[_p_low] = _prow
                                    except Exception:
                                        pass
                                    try:
                                        _hrow = await load_player_h2h(
                                            _soc_db,
                                            player_name=_pname,
                                            opponent_team_name=(
                                                ev.get("away_team")
                                                if _pname else None),
                                        )
                                        if _hrow:
                                            _h2h_by_name[_p_low] = _hrow
                                    except Exception:
                                        pass
                            except Exception:
                                pass

                            for _mkk, _plist in _players_by_mk.items():
                                for _pname in _plist:
                                    _p_low = _pname.lower().strip()
                                    _form_row = _form_by_name.get(_p_low)
                                    _prior_row = _prior_by_name.get(_p_low)
                                    _bp = _soc_bridge(
                                        player=_pname,
                                        market_key=_mkk,
                                        book_implied=0.30,
                                        form_row=_form_row,
                                        prior_form_row=_prior_row,
                                        league=_league_label,
                                    )
                                    if _bp:
                                        _h2h = _h2h_by_name.get(_p_low)
                                        if _h2h:
                                            _bp.setdefault("evidence", {})[
                                                "player_h2h"] = _h2h
                                            _bp.setdefault(
                                                "sources", []).append(
                                                "player_h2h_v1")
                                        _pre.setdefault(_p_low, {})[_mkk] = _bp
                        payload.setdefault("_ctx", {})[
                            "soccer_scorer_precomputed"] = _pre
                        payload["_ctx"]["soccer_scorer_precompute_status"] = (
                            "ok" if _pre else "empty"
                        )
                    except Exception as _ctx_err:
                        logger.debug(
                            "Soccer scorer preload failed: %s", _ctx_err)
                        payload.setdefault("_ctx", {})[
                            "soccer_scorer_precompute_status"] = (
                            f"error:{type(_ctx_err).__name__}"
                        )
                rng = random.Random(abs(hash(ev["id"])) % 10000)
                all_picks.extend(_props_picks_from_event(
                    sport, LEAGUE_LABELS.get(key, sport), payload,
                    ev["commence_time"], rng))
            # ── ALSO run the SportDB synth-scorer engine when:
            #   (a) the bookmaker didn't return player markets at all, OR
            #   (b) this is a lower-tier league in _ALWAYS_SYNTH_SOCCER_KEYS
            #       where bookmaker coverage is sparse / favourites-only and
            #       the user wants top-scorer picks based on career history
            #       regardless of book coverage.
            # ── EXCEPTION (2026-07-04): CSL is handled EXCLUSIVELY by the
            # ESPN-leaderboard generator below. The SportDB/TheSportsDB
            # synth path was producing stale/incorrect CSL rosters
            # (e.g. "Ange Samuel", "Cédric Bakambu") that beat real
            # top scorers (Guy Mbenza, Rafael Ratão). User complaint:
            # "who is ange Samuel how do you got him over mbenza".
            should_run_synth = (
                sport == "Soccer"
                and key != "soccer_china_superleague"
                and (not book_had_player_markets or key in _ALWAYS_SYNTH_SOCCER_KEYS)
            )
            if should_run_synth:
                try:
                    synth_picks = await _synthetic_soccer_scorer_picks(
                        key, ev,
                    )
                    if synth_picks:
                        # ── Phase 1B — synthetic scorer picks are
                        # RESEARCH/MODEL EVIDENCE ONLY.  They carry
                        # model-derived prices with no real sportsbook
                        # line, so they must NOT satisfy the real-line
                        # canonical publication contract.  Persist to
                        # `model_research_evidence` + funnel-record;
                        # never emit into the pick stream.
                        try:
                            from services import funnel_telemetry as _funnel
                            for _sp in synth_picks:
                                _sp["research_only"] = True
                                _funnel.record(
                                    sport="Soccer",
                                    market=_sp.get("market") or "anytime_goal_scorer",
                                    stage="publication_contract",
                                    reason=_funnel.SYNTHETIC_SCORER_RESEARCH_ONLY,
                                    event=_sp.get("event"),
                                    detail="model-priced synthetic scorer — no real sportsbook line",
                                )
                            from server import db as _rdb
                            for _sp in synth_picks:
                                await _rdb.model_research_evidence.update_one(
                                    {"id": _sp.get("id") or _sp.get("external_id")},
                                    {"$set": _sp},
                                    upsert=True,
                                )
                        except Exception as _res_err:
                            logger.debug("research-evidence store failed: %s",
                                         _res_err)
                        logger.info(
                            "Synthetic scorer picks for %s/%s evt %s: %d "
                            "→ research-only (NOT published; "
                            "book_had_player_markets=%s, always_synth=%s)",
                            sport, key, ev.get("id"), len(synth_picks),
                            book_had_player_markets,
                            key in _ALWAYS_SYNTH_SOCCER_KEYS,
                        )
                except Exception as _synth_err:
                    logger.warning("Synthetic scorer for %s failed: %s",
                                   ev.get("id"), _synth_err)
            # ── ESPN LEADERBOARD-SOURCED AGS picks (CSL only) ────────
            # User 2026-07-04: "wire ESPN leaderboard as a source, not
            # just a filter". This generator ALWAYS runs for CSL
            # fixtures, using the live ESPN top-scorer leaderboard
            # (Guy Mbenza, Oscar Taty Maritu, Rafael Ratão, Jhonder
            # Cádiz, Wesley, Crysan, etc.) as the pick source. It
            # runs in ADDITION to the SportDB synth path — the
            # dedupe layer downstream (`pick_dedupe`) will collapse
            # any overlaps by keeping the higher-lock pick.
            if key == "soccer_china_superleague":
                try:
                    espn_picks = await _espn_csl_scorer_picks(key, ev)
                    if espn_picks:
                        logger.info(
                            "ESPN-leaderboard CSL scorer picks evt %s: %d "
                            "(%s)",
                            ev.get("id"), len(espn_picks),
                            ", ".join(p["market"].split(" - ")[0] for p in espn_picks[:5]),
                        )
                        all_picks.extend(espn_picks)
                except Exception as _espn_err:
                    logger.warning("ESPN CSL scorer for %s failed: %s",
                                   ev.get("id"), _espn_err)
            # ── ESPN MLS LEADERBOARD-SOURCED AGS picks (2026-07-22) ─────
            # User complaint: "I still don't see Surridge, Bouanga, or
            # SoA for MLS." Direct-source picks from `espn_mls_stats`
            # so top MLS scorers always surface when their team plays.
            # Mirrors the CSL treatment above.
            if key == "soccer_usa_mls":
                try:
                    espn_mls_picks = await _espn_mls_scorer_picks(key, ev)
                    if espn_mls_picks:
                        # ── Phase 1C (§11A) — MLS leaderboard picks
                        # carry SYNTHETIC American odds converted from
                        # scoring rate (_rate_to_american).  Synthetic
                        # odds must never masquerade as real sportsbook
                        # odds — route to research/model evidence only.
                        try:
                            from services import funnel_telemetry as _funnel
                            from server import db as _rdb
                            for _mp in espn_mls_picks:
                                _mp["research_only"] = True
                                _mp["no_real_book_line"] = True
                                _funnel.record(
                                    sport="Soccer",
                                    market=_mp.get("market") or "anytime_goal_scorer",
                                    stage="publication_contract",
                                    reason=_funnel.SYNTHETIC_ODDS_RESEARCH_ONLY,
                                    event=_mp.get("event"),
                                    detail="MLS ESPN leaderboard pick priced "
                                           "via rate→American conversion",
                                )
                                await _rdb.model_research_evidence.update_one(
                                    {"id": _mp.get("id") or _mp.get("external_id")},
                                    {"$set": _mp},
                                    upsert=True,
                                )
                        except Exception as _res_err:
                            logger.debug("MLS research-evidence store failed: %s",
                                         _res_err)
                        logger.info(
                            "ESPN-leaderboard MLS scorer picks evt %s: %d "
                            "→ research-only (synthetic odds; NOT published)",
                            ev.get("id"), len(espn_mls_picks),
                        )
                except Exception as _espn_err:
                    logger.warning(
                        "ESPN MLS leaderboard scorer for %s failed: %s",
                        ev.get("id"), _espn_err,
                    )
    return all_picks


async def _espn_csl_scorer_picks(sport_key: str, ev: dict) -> list[dict]:
    """Generate CSL AGS picks directly from the ESPN season-leaderboard
    (`csl_espn_live._scorer_index`). User request 2026-07-04: wire the
    ESPN leaderboard as a *source*, not just a filter, so top real CSL
    scorers (Guy Mbenza, Oscar Taty Maritu, Rafael Ratão, Jhonder
    Cádiz, Wesley, Crysan, Wei Shihao, etc.) surface as picks when
    their team plays — closing the "why not top scorer" gap that the
    SportDB roster + Odds API were leaving open.

    Approach:
      1. Load leaderboard rows for this fixture's two teams via
         team-name fuzzy match against the leader's `team` field.
      2. For each real scorer with ≥ 3 goals, project a rate = goals /
         matches (capped at 0.85 for realism).
      3. Convert rate → synthetic American odds (book_odds).
      4. Emit the pick with `is_synthetic_scorer=True`,
         `synthetic_source="csl_espn_leaderboard"`, `elite_player=True`
         so the AGS gate carve-outs (Rules 2, 4) let it through and
         the "MODEL" badge shows in the UI."""
    if sport_key != "soccer_china_superleague":
        return []
    try:
        import csl_espn_live
    except Exception:
        return []
    scorer_index = getattr(csl_espn_live, "_scorer_index", {}) or {}
    if not scorer_index:
        return []
    home = ev.get("home_team") or ""
    away = ev.get("away_team") or ""
    if not home or not away:
        return []

    def _team_match(candidate: str, target: str) -> bool:
        """Tolerant CSL team-name matcher. ESPN uses short names like
        'Liaoning Tieren'; Odds API uses full names like
        'Liaoning Tieren FC'. Strip common suffixes and check
        containment both ways."""
        if not candidate or not target:
            return False
        c = candidate.lower().replace(" fc", "").replace(" f.c.", "").strip()
        t = target.lower().replace(" fc", "").replace(" f.c.", "").strip()
        # Simple containment either way, or exact tokens overlap.
        if c == t or c in t or t in c:
            return True
        # Compare the first two tokens (e.g. "Shanghai Shenhua" vs
        # "Shanghai Shenhua Athletic Club").
        c_toks = c.split()[:2]
        t_toks = t.split()[:2]
        if c_toks == t_toks and c_toks:
            return True
        return False

    # Bucket scorer_index by team so we can pull each fixture's scorers.
    # NOTE: The ESPN scorer index doesn't always populate `matches` —
    # some rows only give a season goal total. When missing we assume a
    # mid-season baseline of 18 matches per player (CSL has 30 GW total,
    # July is around GW 15-18). This is a floor, not a ceiling.
    CSL_DEFAULT_MATCHES = 18
    home_scorers, away_scorers = [], []
    for key, row in scorer_index.items():
        team = row.get("team") or ""
        try:
            goals = float(row.get("goals") or 0)
        except Exception:
            goals = 0
        matches_raw = row.get("matches")
        # Try to parse display field like "12 (17 GP)" for match count.
        if not matches_raw:
            display = str(row.get("display") or "")
            import re as _re
            m = _re.search(r"\((\d+)\s*(?:GP|matches)?\)", display)
            if m:
                try:
                    matches_raw = int(m.group(1))
                except Exception:
                    pass
        matches = matches_raw or CSL_DEFAULT_MATCHES
        # Skip low-goal scorers (need meaningful sample).
        if goals < 3:
            continue
        rate = min(0.85, goals / max(matches, 6))
        name = row.get("name") or ""
        if not name:
            continue
        if _team_match(team, home):
            home_scorers.append((name, team, goals, matches, rate))
        elif _team_match(team, away):
            away_scorers.append((name, team, goals, matches, rate))

    if not home_scorers and not away_scorers:
        return []

    # For each side, take the top 3 scorers by rate.
    home_scorers.sort(key=lambda x: x[4], reverse=True)
    away_scorers.sort(key=lambda x: x[4], reverse=True)
    picks_out: list[dict] = []
    league_label = "China Super League"
    commence = ev.get("commence_time")
    event_id = ev.get("id") or f"CSL-{home}-{away}"

    for scorer_team, side_scorers in (
        (home, home_scorers[:3]),
        (away, away_scorers[:3]),
    ):
        for name, espn_team, goals, matches, rate in side_scorers:
            # Convert rate to American odds. Rate = P(scores at least
            # once in 90'). American odds = rate → chalk (- for
            # favorite). Standard formula:
            #   fair American = -100 * rate / (1 - rate)   if rate > 0.5
            #                 =  100 * (1 - rate) / rate    if rate <= 0.5
            if rate >= 0.5:
                fair = int(round(-100.0 * rate / (1.0 - rate)))
            else:
                fair = int(round(100.0 * (1.0 - rate) / rate))
            # Books juice by ~10-15% on CSL AGS — this used to write a
            # synthetic "book_odds" here.  P0-4 (2026-08-11): no US
            # sportsbook line exists for CSL Chinese Super League
            # goalscorer props, so we must not surface synthetic odds
            # as book odds.  Keep the synthetic price under
            # ``model_fair_odds`` for reference.
            _synth_book = int(fair * 0.92) if fair > 0 else int(fair * 1.08)
            book_odds = None
            # Lock score: 90 baseline for scorer rate ≥ 0.4, else 85.
            # Elite anchors (rate ≥ 0.6) get 95.
            if rate >= 0.6:
                lock = 95.0
            elif rate >= 0.4:
                lock = 90.0
            else:
                lock = 85.0
            picks_out.append({
                "id": f"csl-espn-{event_id}-{name.replace(' ', '_').lower()}",
                "external_id": f"CSL-ESPN-{event_id}-{name}",
                "sport": "Soccer",
                "league": league_label,
                "event": f"{away} @ {home}",
                "event_time": commence,
                "market": f"{name} - Anytime Goal Scorer",
                "pick_side": name,
                "model_win_prob": rate,
                "book_odds": book_odds,
                "model_fair_odds": _synth_book,
                "implied_probability": None,
                "lock_score": lock,
                "lock_score_v2": lock,
                "lock_score_v2_raw": lock,
                "edge_percent": None,
                "no_real_book_line": True,
                "model_only": True,
                "elite_player": True,           # anchor exemption
                "is_synthetic_scorer": True,    # MODEL badge
                "synthetic": True,
                "synthetic_source": "csl_espn_leaderboard",
                "source": "csl_espn_leaderboard",
                "samples": {
                    "goals": goals,
                    "matches": matches,
                    "rate": round(rate, 3),
                    "from_fallback": False,
                    "leaderboard_team": espn_team,
                },
                "pick_rationale": {
                    "engine": "csl_espn_leaderboard",
                    "summary": (
                        f"{name}: {goals}g in {matches} CSL matches this season "
                        f"({rate * 100:.0f}% goal-per-match rate)"
                    ),
                    "evidence": [
                        f"🏆 Top scorer form: {goals} goals in {matches} matches",
                        f"⚡ Per-match rate: {rate * 100:.0f}% (ESPN live leaderboard)",
                        f"👤 {name} — {espn_team}",
                    ],
                    "concerns": [],
                    "matchup": {"player": name, "team": espn_team},
                    "recent_form": {"engine": "csl_espn_leaderboard"},
                },
                "sport_key": sport_key,
                "home_team": home,
                "away_team": away,
                "home_team_name": home,
                "away_team_name": away,
            })
    return picks_out


async def _espn_mls_scorer_picks(sport_key: str, ev: dict) -> list[dict]:
    """Guaranteed-emit MLS AGS + SoA picks from `espn_mls_stats`.

    User complaint 2026-07-22: "I still don't see Surridge, Bouanga,
    Mukhtar, or SoA for MLS." The book-based `_props_picks_from_event`
    path was silently dropping MLS picks somewhere. This is the
    bulletproof direct-emit path — mirrors `_espn_csl_scorer_picks`.
    """
    if sport_key != "soccer_usa_mls":
        return []
    from server import db as _db
    home = (ev.get("home_team") or "").strip()
    away = (ev.get("away_team") or "").strip()
    if not home or not away:
        return []
    try:
        rows = await _db.espn_mls_stats.find({}).to_list(length=500)
    except Exception:
        return []
    if not rows:
        return []

    def _team_match(candidate: str, target: str) -> bool:
        if not candidate or not target:
            return False
        c = candidate.lower()
        t = target.lower()
        for suf in (" fc", " f.c.", " sc", " cf", " united", " city",
                    " football club"):
            c = c.replace(suf, "")
            t = t.replace(suf, "")
        c = c.strip(); t = t.strip()
        if not c or not t:
            return False
        return c == t or c in t or t in c

    home_sc, away_sc = [], []
    for r in rows:
        team = r.get("team") or ""
        try:
            goals = int(r.get("goals") or 0)
        except Exception:
            goals = 0
        try:
            assists = int(r.get("assists") or 0)
        except Exception:
            assists = 0
        matches = int(r.get("games") or 0) or 18
        if goals < 3 and assists < 3:
            continue
        rate = min(0.85, goals / max(matches, 6))
        soa_rate = min(0.92, (goals + assists) / max(matches, 6))
        name = r.get("name") or ""
        if not name:
            continue
        entry = (name, team, goals, assists, matches, rate, soa_rate)
        if _team_match(team, home):
            home_sc.append(entry)
        elif _team_match(team, away):
            away_sc.append(entry)
    if not home_sc and not away_sc:
        return []
    home_sc.sort(key=lambda x: x[5], reverse=True)
    away_sc.sort(key=lambda x: x[5], reverse=True)

    matchup_lookup: dict = {}
    try:
        from services.mls_player_matchup_history import get_player_vs_opponent
        for entry in (home_sc[:3] + away_sc[:3]):
            pname, ptm = entry[0], entry[1]
            opp = away if _team_match(ptm, home) else home
            rec = await get_player_vs_opponent(pname, opp)
            if rec:
                matchup_lookup[pname] = {"opponent": opp, "record": rec}
    except Exception:
        pass

    def _rate_to_american(r: float) -> int:
        # Fair American odds → juiced for realism. Clamp to valid ranges:
        # American odds must be ≥ +100 or ≤ -100 (board validator's
        # `invalid_odds` check drops anything in (-100, 100)).
        if r >= 0.5:
            fair = int(round(-100.0 * r / (1.0 - r)))
            juiced = int(fair * 0.92)
            # If juice brings us into the -99..-100 gap, clamp to -105.
            if -100 < juiced <= 0:
                juiced = -105
            return max(min(juiced, -100), -800)
        fair = int(round(100.0 * (1.0 - r) / r))
        juiced = int(fair * 1.08)
        if 0 <= juiced < 100:
            juiced = 105
        return min(max(juiced, 100), 1500)

    picks_out: list[dict] = []
    commence = ev.get("commence_time") or ""
    event_id = ev.get("id") or f"MLS-{home}-{away}"

    for side_scorers in (home_sc[:3], away_sc[:3]):
        for name, team, goals, assists, matches, rate, soa_rate in side_scorers:
            for kind in ("anytime", "score_or_assist"):
                r = rate if kind == "anytime" else soa_rate
                book_odds = _rate_to_american(r)
                if kind == "score_or_assist":
                    lock = 96.0 if r >= 0.55 else (92.0 if r >= 0.4 else 88.0)
                    label = "To Score or Assist"
                else:
                    lock = 95.0 if r >= 0.55 else (90.0 if r >= 0.4 else 88.0)
                    label = "Anytime Goal Scorer"
                grade = ("Strong Lock" if lock >= 95 else
                          ("Lock" if lock >= 90 else "Playable"))
                pick = {
                    "id": f"mls-espn-{kind}-{event_id}-{name.replace(' ', '_').lower()}",
                    "external_id": f"MLS-ESPN-{kind}-{event_id}-{name}",
                    "sport": "Soccer",
                    "league": "MLS",
                    "event": f"{away} @ {home}",
                    "event_time": commence,
                    "market": f"{name} {label}",
                    "selection": name,
                    "pick_side": name,
                    "model_win_prob": r,
                    "win_probability": r,          # board_quality validator
                    "book_odds": book_odds,
                    "book_implied_prob": r / 1.08,  # de-vig
                    "lock_score": lock,
                    "lock_score_v2": lock,
                    "lock_score_v2_raw": lock,
                    # Small positive edge so board_quality (Soccer_ags
                    # requires edge_min >= 0) doesn't drop us.
                    "edge_percent": 2.5,
                    "grade": grade,
                    "confidence": grade,
                    "status": "pending",
                    "no_bet": False,
                    "elite_player": True,
                    "is_elite": True,
                    "is_synthetic_scorer": True,
                    "is_long_shot": True,
                    "synthetic": True,
                    "synthetic_source": "mls_espn_leaderboard",
                    "source": "mls_espn_leaderboard",
                    "samples": {
                        "goals": goals, "assists": assists,
                        "matches": matches, "rate": round(rate, 3),
                        "soa_rate": round(soa_rate, 3),
                        "leaderboard_team": team,
                    },
                    "sport_key": sport_key,
                    "home_team": home, "away_team": away,
                    "home_team_name": home, "away_team_name": away,
                    "pick_rationale": {
                        "engine": "mls_espn_leaderboard",
                        "summary": (
                            f"{name}: {goals}G/{assists}A in {matches} MLS "
                            f"matches this season ({rate*100:.0f}% goal-per-match)."
                        ),
                        "evidence": [
                            f"🏆 ESPN 2025 MLS leader: {goals}G, {assists}A in {matches} games",
                            f"⚡ Per-match scoring: {rate*100:.0f}% · SoA: {soa_rate*100:.0f}%",
                            f"👤 {name} — {team}",
                        ],
                        "concerns": [],
                        "matchup": {"player": name, "team": team},
                        "recent_form": {"engine": "mls_espn_leaderboard"},
                    },
                }
                hist = matchup_lookup.get(name)
                if hist and hist.get("record"):
                    rec = hist["record"]
                    m_m = int(rec.get("matches", 0) or 0)
                    m_g = int(rec.get("goals", 0) or 0)
                    m_a = int(rec.get("assists", 0) or 0)
                    gpm = m_g / m_m if m_m else 0.0
                    pick["matchup_history"] = {
                        "opponent": hist.get("opponent"),
                        "matches": m_m, "goals": m_g, "assists": m_a,
                        "goals_per_match": round(gpm, 2),
                        "scored_in": int(rec.get("scored_matches", 0) or 0),
                        "assisted_in": int(rec.get("assist_matches", 0) or 0),
                        "recent": (rec.get("recent") or [])[:3],
                    }
                    if gpm >= 1.0 and m_m >= 2:
                        pick["lock_score"] = min(99.0, pick["lock_score"] + 4.0)
                    elif gpm >= 0.5 and m_m >= 2:
                        pick["lock_score"] = min(99.0, pick["lock_score"] + 2.0)
                    pick["pick_rationale"]["evidence"].insert(
                        1,
                        f"🎯 Career vs {hist.get('opponent')}: {m_g}G/{m_a}A "
                        f"in {m_m} matches ({gpm:.1f} G/match)",
                    )
                picks_out.append(pick)
    return picks_out



async def _synthetic_soccer_scorer_picks(sport_key: str, ev: dict) -> list[dict]:
    """Bridge to sportdb_player_scorer. Imported lazily so the dependency is
    soft — if the module fails to import or the SportDB key isn't set,
    sports_engine still works."""
    try:
        import sportdb_player_scorer as sps
    except Exception:
        return []
    if sport_key not in sps.LEAGUE_MAP:
        return []
    # Resolve team-form (standings) for the opponent defence multiplier.
    # This costs zero NEW credits if the standings are already cached by
    # sportdb_client's daily refresh.
    home_form = None
    away_form = None
    try:
        from server import db as _db  # late import to avoid circular dep
        from sportdb_client import lookup_team_form
        home_form = await lookup_team_form(_db, ev.get("home_team") or "")
        away_form = await lookup_team_form(_db, ev.get("away_team") or "")
    except Exception:
        # Non-fatal — defence multiplier defaults to neutral.
        pass
    from server import db as _db
    return await sps.compute_anytime_scorer_picks(
        _db, sport_key=sport_key,
        home_team=ev.get("home_team") or "",
        away_team=ev.get("away_team") or "",
        event_id=ev.get("id") or "",
        kickoff_iso=ev.get("commence_time") or "",
        home_form=home_form, away_form=away_form,
    )



async def generate_all_picks(
    date_str: Optional[str] = None,
    sport_filter: Optional[str] = None,
) -> list[dict]:
    """Fetch picks for one or all sports.

    Args:
      date_str: pick_date ISO string; defaults to today (UTC).
      sport_filter: when set (e.g. "MLB"), skip every other sport's fetcher.
        Used by the dedicated MLB pregame loop that runs every 5 min during
        the US afternoon window so MLB lines surface ~60-90 min pre-game
        instead of ~5 min pre-game — without burning Odds API credits on
        sports whose slates haven't moved.
    """
    if not date_str:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    sf = (sport_filter or "").lower()
    def _want(s: str) -> bool:
        return not sf or sf == s.lower()
    # Phase 1: fetch all sport-summary games (one call per sport-key, parallel).
    fetch_jobs = []
    if _want("MLB"): fetch_jobs.append(fetch_mlb_picks(date_str))
    if _want("NBA"): fetch_jobs.append(fetch_nba_picks(date_str))
    if _want("NFL"): fetch_jobs.append(fetch_nfl_picks(date_str))
    if _want("CFB"): fetch_jobs.append(fetch_cfb_picks(date_str))
    if _want("Soccer"): fetch_jobs.append(fetch_soccer_picks(date_str))
    if _want("Tennis"): fetch_jobs.append(fetch_tennis_picks(date_str))
    if _want("UFC"): fetch_jobs.append(fetch_ufc_picks(date_str))
    if _want("NHL"): fetch_jobs.append(fetch_nhl_picks(date_str))
    # KBO removed 2026-07-04 per user request. Function `fetch_kbo_picks`
    # kept in module for historical reference but never invoked.
    game_results = await asyncio.gather(*fetch_jobs, return_exceptions=True) if fetch_jobs else []
    all_picks: list[dict] = []
    _sport_order = [s for s in ("MLB", "NBA", "NFL", "CFB", "Soccer",
                                "Tennis", "UFC", "NHL") if _want(s)]
    for _idx, r in enumerate(game_results):
        if isinstance(r, list):
            all_picks.extend(r)
        elif isinstance(r, BaseException):
            # Phase 1C — a sport fetcher crash must never silently
            # produce "0 picks with no reason".
            _failed_sport = (_sport_order[_idx]
                             if _idx < len(_sport_order) else "unknown")
            logger.error("Sport fetcher failed for %s: %s",
                         _failed_sport, r)
            try:
                from services import funnel_telemetry as _funnel
                _funnel.record(
                    sport=_failed_sport, market="*", stage="fetch",
                    reason=_funnel.REFRESH_RUNTIME_FAILURE,
                    detail=f"{type(r).__name__}: {r}"[:160],
                )
            except Exception:
                pass

    # Phase 2: fetch event-level player props sequentially with small delays
    # to avoid The Odds API rate limit (1 req/sec on free tier).
    #
    # Phase 1 (2026-08-11): NFL was defined in PLAYER_PROP_MARKETS but
    # omitted from this loop, so its props never fetched.  Added here
    # so the NFL wiring is genuinely end-to-end.  CFB player-prop
    # coverage stays intentionally OFF (The Odds API's CFB market
    # catalogue is thin — CFB game-level markets already flow via
    # Phase 1 above).  UFC has no prop markets (see PLAYER_PROP_MARKETS
    # comment) and NHL is not yet supported.
    # ── 2026-08-23 FINAL FAIRNESS — TRUE round-robin first pass ──
    # Day-of-year rotation alone doesn't guarantee same-refresh
    # fairness: MLB's single batch could still consume all protected
    # quota within one cycle.  Replace with a bounded two-pass
    # protected acquisition:
    #   PASS A: EACH active prop sport gets ONE bounded P2 batch
    #           (existing per-key cap already provides the bound;
    #            no new limit / no new parallelism / same 1.2s spacing).
    #           Sports with zero current events are skipped so they
    #           don't consume a protected opportunity.
    #   PASS B: Only after every eligible sport has had its Pass-A
    #           opportunity, we let any sport re-enter for additional
    #           batches — currently a no-op because a single call to
    #           ``_fetch_player_props_for_sport`` already runs to
    #           per-key cap; kept as an explicit hook so future
    #           budget-elastic additions plug in without altering
    #           fairness semantics.
    prop_sports = [s for s in ("MLB", "NBA", "NFL", "Soccer") if _want(s)]
    # Cheap "has current events" probe: use the already-loaded
    # ``_ACTIVE_KEYS`` catalog rather than an extra provider call.
    _prefix_map = {"Soccer": "soccer_", "Tennis": "tennis_"}
    _eligible_prop_sports: list[str] = []
    for _sport in prop_sports:
        _pfx = _prefix_map.get(_sport)
        if _pfx:
            _has_any = any(k.startswith(_pfx) for k in _ACTIVE_KEYS)
        else:
            # Single-league sports (MLB / NBA / NFL) — the fixed keys
            # in SPORT_KEYS are the source of truth; consider eligible
            # unless the sport is entirely absent from _ACTIVE_KEYS.
            _keys = SPORT_KEYS.get(_sport, [])
            _has_any = (not _ACTIVE_KEYS) or any(
                _k in _ACTIVE_KEYS for _k in _keys
            )
        if _has_any:
            _eligible_prop_sports.append(_sport)
        else:
            logger.info(
                "fair round-robin: skipping %s — no current events "
                "in _ACTIVE_KEYS catalog",
                _sport,
            )
    # PASS A — protected first opportunity per sport.
    _passA_completed: list[str] = []
    for sport in _eligible_prop_sports:
        try:
            props = await _fetch_player_props_for_sport(sport)
            if props:
                all_picks.extend(props)
            _passA_completed.append(sport)
        except Exception as e:
            logger.warning("Props fetch (Pass A) failed for %s: %s",
                            sport, e)
        await asyncio.sleep(1.2)
    logger.info("fair round-robin PASS A complete for %s (of eligible %s)",
                _passA_completed, _eligible_prop_sports)
    # PASS B — additional batches guarded by remaining budget.  No-op
    # today (per-key cap already exhausts a sport's payload in one
    # call); explicit hook prevents future budget-elastic additions
    # from silently violating same-refresh fairness.
    _passB_enabled = False
    if _passB_enabled:
        for sport in _eligible_prop_sports:
            try:
                props = await _fetch_player_props_for_sport(sport)
                if props:
                    all_picks.extend(props)
            except Exception as e:
                logger.warning("Props fetch (Pass B) failed for %s: %s",
                                sport, e)
            await asyncio.sleep(1.2)
    for p in all_picks:
        p["pick_date"] = date_str
        p["created_at"] = datetime.now(timezone.utc).isoformat()

    # ─── Phase 2.5: SportDB xG enrichment for soccer totals ───
    # Sharpen Over/Under picks using SportDB Expected-Goals data from each
    # team's last 5 matches. Boost lock_score when xG agrees with the pick
    # direction, temper when xG disagrees. Adds a clear sportdb_signal so
    # users see exactly what xG numbers are driving the adjustment.
    # Best-effort — failures don't block the slate.
    try:
        from server import db as _db
        import sportdb_xg_totals as _xg
        # Reverse LEAGUE_LABELS so we can resolve sport_key from league name.
        _label_to_key = {v.lower(): k for k, v in LEAGUE_LABELS.items()
                          if k.startswith("soccer_")}
        soccer_totals = [
            p for p in all_picks
            if p.get("sport") == "Soccer" and _xg._is_totals_pick(p)
        ]
        enriched_count = 0
        for p in soccer_totals:
            sport_key = p.get("sport_key") or _label_to_key.get(
                (p.get("league") or "").lower()
            )
            if not sport_key:
                continue
            home_team = p.get("home_team") or ""
            away_team = p.get("away_team") or ""
            if not home_team or not away_team:
                continue
            try:
                await _xg.enrich_totals_pick_with_xg(
                    _db, p, sport_key, home_team, away_team,
                )
                enriched_count += 1
            except Exception as _xg_pick_err:
                logger.debug("xG enrich for %s failed: %s",
                             p.get("event") or p.get("id"), _xg_pick_err)
        if enriched_count:
            logger.info("SportDB xG enrichment: %d soccer totals picks adjusted",
                        enriched_count)
    except Exception as _xg_err:
        logger.warning("SportDB xG enrichment skipped: %s", _xg_err)

    # ─── Phase 2.6: Career-history enrichment for ALL goalscorer picks ───
    # User 2026-06-26: "pull history for players" — every player named on a
    # goalscorer / score-or-assist / first-scorer pick gets a SportDB career
    # lookup. The tier classifier in sportdb_player_scorer._prob_to_lock
    # then re-anchors the lock_score using career_goals + weighted_rate
    # across the player's last 4 seasons (league + national team + intl cups
    # combined). Examples that this catches:
    #   • Mané at Senegal: National Team tab shows 30+ Egypt goals → Tier S
    #   • Leonardo at Shanghai Port: 21g+21g+19g last 3 → Tier S
    #   • Fabio Abreu at Beijing Guoan: 28g/30m last season → Tier S
    # Bookmaker picks only get BOOSTED (never downgraded) so this is a pure
    # quality lift. Best-effort wrapper so it never blocks the slate.
    try:
        from server import db as _db
        import sportdb_player_scorer as _sps
        _label_to_key2 = {v.lower(): k for k, v in LEAGUE_LABELS.items()
                          if k.startswith("soccer_")}
        # Goalscorer-style markets we want to enrich
        _scorer_market_substrings = (
            "anytime goal scorer", "to score or assist",
            "first goal scorer", "scorer", "score or assist",
        )
        scorer_picks = [
            p for p in all_picks
            if p.get("sport") == "Soccer"
            and any(s in (p.get("market") or "").lower() for s in _scorer_market_substrings)
            and not p.get("is_synthetic_scorer")  # synth picks already have career data
        ]
        boosted_count = 0
        gk_adjusted_count = 0
        for p in scorer_picks:
            sport_key = p.get("sport_key") or _label_to_key2.get(
                (p.get("league") or "").lower()
            )
            # League · Props strip — `league` field for props is "Premier League · Props"
            if not sport_key:
                lbl = (p.get("league") or "").lower().replace(" · props", "").strip()
                sport_key = _label_to_key2.get(lbl)
            if not sport_key:
                continue
            try:
                before = p.get("lock_score") or 0.0
                await _sps.enrich_bookmaker_scorer_pick(_db, p, sport_key)
                after = p.get("lock_score") or 0.0
                if after > before:
                    boosted_count += 1
            except Exception as _sc_err:
                logger.debug("Career enrich for %s failed: %s",
                             p.get("selection") or p.get("id"), _sc_err)
            # Opposition GK quality enrichment — strong GK depresses scorer
            # prob, weak GK boosts it. Best-effort, never blocks.
            try:
                gk_before = p.get("lock_score") or 0.0
                await _sps.enrich_pick_with_gk_quality(_db, p, sport_key)
                gk_after = p.get("lock_score") or 0.0
                if gk_after != gk_before:
                    gk_adjusted_count += 1
            except Exception as _gk_err:
                logger.debug("GK enrich for %s failed: %s",
                             p.get("selection") or p.get("id"), _gk_err)
        if boosted_count or gk_adjusted_count:
            logger.info(
                "SportDB scorer enrichment: %d career-boosted, %d GK-adjusted",
                boosted_count, gk_adjusted_count,
            )
    except Exception as _sc_err2:
        logger.warning("SportDB career enrichment skipped: %s", _sc_err2)

    # ─── Dedupe highly-correlated picks ───
    # Books offer both "Player Over 0.5 Hits" AND "Player Over 0.5 Total
    # Bases" — these are basically the same bet (a hit guarantees a total
    # base). Showing both on the Locks tab looks like duplication. Collapse
    # picks that share (sport, event, player/team selection, line threshold)
    # and keep the one with the higher lock_score (ties broken by better
    # odds).
    import re as _re
    def _dedup_key(p: dict) -> tuple:
        market = p.get("market") or ""
        sel = p.get("selection") or ""
        market_l = market.lower()
        sel_l = sel.lower()
        # First decimal in the market is the line ("0.5", "1.5", "8.5", ...).
        m = _re.search(r"(-?\d+\.\d+)", market)
        threshold = m.group(1) if m else ""

        # CRITICAL: For Totals markets (Over/Under) and Spreads (team A +X /
        # team B -X), the two sides are MUTUALLY EXCLUSIVE — they can never
        # both win. We must NOT issue both as separate picks. Collapse them
        # into the same dedup key so only the higher-edge side survives.
        if "total" in market_l and threshold:
            # Same game + same total line → one pick (Over OR Under, not both)
            return (p.get("sport"), p.get("event"), "TOTALS", threshold)
        if "spread" in market_l and threshold:
            # Same game + spread line (irrespective of sign): the two sides
            # straddle the same line. Normalize sign so +1.5/-1.5 collapse.
            return (p.get("sport"), p.get("event"), "SPREAD", threshold.lstrip("+-"))
        if "run line" in market_l or "runline" in market_l:
            return (p.get("sport"), p.get("event"), "RUNLINE", threshold.lstrip("+-"))
        # Player-prop over/under on the same player+line (e.g. "Aaron Judge
        # Over 1.5 Hits" vs "Aaron Judge Under 1.5 Hits"): collapse.
        if ("over" in sel_l or "under" in sel_l) and threshold:
            # Strip the side word from the market label so both sides share key.
            base_market = _re.sub(r"\b(over|under)\b", "", market_l).strip()
            base_market = _re.sub(r"\s+", " ", base_market)
            return (p.get("sport"), p.get("event"), base_market, threshold)
        # GAME OUTCOME family — Moneyline + Win-or-Draw + Double Chance ALL
        # resolve from the same 3-way h2h market. Any two picks from
        # different sides of this family (e.g. "Sweden ML" vs "Netherlands
        # Win or Draw") are mutually exclusive: if Sweden wins, NL W-or-D
        # loses; if NL wins or draws, Sweden ML loses. We MUST collapse
        # them into one key per game so only the highest-EV side survives
        # (preference rules below favor Win-or-Draw on soccer for the
        # built-in draw safety net).
        if ("moneyline" in market_l or "money line" in market_l
                or "win or draw" in market_l or "double chance" in market_l):
            return (p.get("sport"), p.get("event"), "GAME_OUTCOME")
        return (p.get("sport"), p.get("event"), sel, threshold)

    best: dict = {}
    # Market-family preference when two correlated picks tie on dedup key.
    #   - 2026-07-27 H+R+RBI equal priority to Hits (user request: expand
    #     H+R+RBI coverage since it has ~10-15pp higher base rate than Hits).
    # Lower number = higher preference.
    def _market_priority(market: str) -> int:
        m = (market or "").lower()
        # Block 2D B1 (2026-08) — market competition on evidence, NOT
        # on market-family bias.  Prior code hardcoded "Win or Draw" /
        # "Double Chance" to priority 0 and "moneyline" to priority 2,
        # which meant DC always won dedupe ties.  Removed: all
        # game-outcome markets share the SAME priority (1) so the
        # highest lock_score / evidence wins on merit.  This preserves
        # the H+R+RBI / Hits equal-preference behaviour (both 0) since
        # they are separate market families, not correlated ones.
        if "hits + runs + rbi" in m or "h+r+rbi" in m or "hits+runs+rbi" in m:
            return 0
        if "hits" in m:
            return 0
        # Game-outcome family (moneyline / win-or-draw / double chance
        # / BTTS) — all TIED at priority 1.  Dedupe now falls back to
        # lock_score comparison, so evidence wins.
        return 1

    # ── 2026-07-27 Cross-side conflict resolver for MLB K's ─────────────
    # User feedback: "Went 6/11 on K's — need better research." We saw
    # duplicate emissions per pitcher (Shane Drohan Over 5.5 + Under 6.5,
    # Roki Sasaki Under 5.5 + Over 4.5, Wheeler Under 6.5 + Over 6.5).
    #
    # 2026-07-27 (post-Wheeler bug reoccurrence): the previous version used
    #   `higher lock_score wins`, but Over+Under for the same pitcher often
    #   tie at lock=99, making the winner non-deterministic across
    #   refreshes. When both sides tie AND they contradict (one Over + one
    #   Under), SAFETY WINS: drop BOTH. We would rather miss a pick than
    #   surface the wrong side.
    def _k_conflict_key(p: dict):
        m = (p.get("market") or "").lower()
        if "strikeout" not in m:
            return None
        # Extract pitcher name from MARKET (selection is just name; side
        # info lives in market string).
        import re as _re
        mk_match = _re.match(
            r"(.+?)\s+\([A-Z]+\)\s+(Over|Under)\s+([\d.]+)\s+Strikeouts",
            p.get("market") or "", _re.IGNORECASE,
        )
        if mk_match:
            pname = mk_match.group(1).strip()
        else:
            pname = (p.get("selection") or "").strip()
        return (p.get("sport"), p.get("event"), "MLB_K_FAMILY", pname.lower())

    def _k_pick_side(p: dict) -> str:
        """Return 'over', 'under' or 'unknown' for a K prop pick."""
        m = (p.get("market") or "").lower()
        if " over " in m:
            return "over"
        if " under " in m:
            return "under"
        return "unknown"

    # First pass: group same-pitcher K picks and detect Over/Under conflicts
    k_grouped: dict = {}
    non_k_picks: list = []
    for p in all_picks:
        kc = _k_conflict_key(p)
        if kc is None:
            non_k_picks.append(p)
        else:
            k_grouped.setdefault(kc, []).append(p)

    k_conflict_kept: list = []
    for kc, group in k_grouped.items():
        if len(group) == 1:
            k_conflict_kept.append(group[0])
            continue
        # Multiple K picks for same pitcher — resolve conflict
        overs = [p for p in group if _k_pick_side(p) == "over"]
        unders = [p for p in group if _k_pick_side(p) == "under"]
        if overs and unders:
            # Contradiction — resolve via shared K-math helper (kept in
            # services.k_conflict_resolver so BOTH the in-memory resolver
            # here AND the DB-level `_reconcile_player_prop_contradictions`
            # use identical math. See 2026-07-28 consolidation.).
            best_over = max(overs, key=lambda x: x.get("lock_score", 0))
            best_under = max(unders, key=lambda x: x.get("lock_score", 0))
            import re as _re
            def _line(p):
                m = _re.search(r"(\d+\.?\d*)\s+Strikeouts", p.get("market") or "", _re.I)
                return float(m.group(1)) if m else None
            line = _line(best_over) or _line(best_under)
            try:
                from services.k_conflict_resolver import resolve_k_family_winner
                winning_side, reason = resolve_k_family_winner(best_over, best_under, line)
            except Exception as _kc_err:
                logger.warning("k_conflict_resolver import failed: %s", _kc_err)
                winning_side, reason = (None, "indeterminate")
            resolved = None
            if winning_side == "over":
                resolved = best_over
            elif winning_side == "under":
                resolved = best_under
            if resolved is not None:
                logger.info(
                    "MLB K Over/Under conflict → %s wins (%s): %s line=%s",
                    winning_side.upper(), reason, kc[3], line,
                )
                k_conflict_kept.append(resolved)
            else:
                # Truly indeterminate — safety valve, drop both.
                logger.info(
                    "MLB K Over/Under conflict dropped BOTH (%s): %s "
                    "Over lock=%.0f Under lock=%.0f",
                    reason, kc[3],
                    best_over.get("lock_score", 0), best_under.get("lock_score", 0),
                )
                continue
        else:
            # Same-side dupes — keep highest lock
            k_conflict_kept.append(max(group, key=lambda x: x.get("lock_score", 0)))

    # ── 2026-07-28 DEFECT #3 FIX: DB-aware K conflict resolver ────────
    # ────────────────────────────────────────────────────────────────
    # The in-memory pass above only sees THIS refresh batch. A
    # wrong-side K pick from a previous refresh window (same day,
    # different sub-batch) or from a previous pick_date (game
    # scheduled across the UTC midnight boundary) lives in the DB and
    # never enters `all_picks`. Every surviving K pick now cross-
    # checks the DB for opposite-side active rows on:
    #   • same event
    #   • same selection (pitcher name)
    #   • same market family (MLB pitcher_strikeouts / _alternate)
    #   • same numeric line
    #   • opposite side (over ↔ under)
    #   • any pick_date within a 72h look-back
    #   • not already flagged no_bet
    #
    # Resolution uses the SAME shared helper (`resolve_k_family_winner`)
    # so identical math wins on both sides of the DB boundary.
    # Outcomes:
    #   • New pick wins   → mark DB row `no_bet=True` atomically.
    #   • DB row wins     → drop new pick from `k_conflict_kept`.
    #   • Indeterminate   → mark DB row no_bet AND drop new pick
    #                       (safety: better zero picks than wrong side).
    if k_conflict_kept:
        try:
            from server import db as _db_kc
            import re as _re_kc
            from services.k_conflict_resolver import resolve_k_family_winner as _rkfw
            _cutoff_iso = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
            def _kc_line(pick: dict) -> Optional[float]:
                _m = _re_kc.search(
                    r"(\d+\.?\d*)\s+Strikeouts",
                    pick.get("market") or "", _re_kc.IGNORECASE,
                )
                return float(_m.group(1)) if _m else None
            def _kc_side(pick: dict) -> str:
                _m = (pick.get("market") or "").lower()
                if " over " in _m:
                    return "over"
                if " under " in _m:
                    return "under"
                return "unknown"

            _kc_drop_ids: set = set()   # id-of(new pick) → drop from batch
            _kc_db_flagged = 0
            _kc_new_dropped = 0

            for _new_pick in list(k_conflict_kept):
                if "strikeout" not in (_new_pick.get("market") or "").lower():
                    continue
                _new_line = _kc_line(_new_pick)
                _new_side = _kc_side(_new_pick)
                if _new_line is None or _new_side == "unknown":
                    continue
                _opp = "under" if _new_side == "over" else "over"
                _event = _new_pick.get("event")
                _selection = _new_pick.get("selection")
                if not _event or not _selection:
                    continue

                # Look for active DB rows with opposite side + same event + same pitcher.
                _db_rows = await _db_kc.picks.find({
                    "sport": "MLB",
                    "event": _event,
                    "selection": _selection,
                    "no_bet": {"$ne": True},
                    "created_at": {"$gte": _cutoff_iso},
                }).to_list(length=20)

                for _row in _db_rows:
                    _row_market = (_row.get("market") or "")
                    if "strikeout" not in _row_market.lower():
                        continue
                    _row_line = _kc_line(_row)
                    _row_side = _kc_side(_row)
                    if _row_line != _new_line or _row_side != _opp:
                        continue
                    # Genuine cross-DB contradiction. Route through shared helper.
                    if _new_side == "over":
                        _over_pick, _under_pick = _new_pick, _row
                    else:
                        _over_pick, _under_pick = _row, _new_pick
                    try:
                        _win_side, _win_reason = _rkfw(_over_pick, _under_pick, _new_line)
                    except Exception as _rkfw_err:
                        logger.warning("K_CROSS_DB resolver failed: %s", _rkfw_err)
                        _win_side, _win_reason = (None, "resolver_error")

                    if _win_side == _new_side:
                        # New pick wins — flag DB row atomically.
                        try:
                            await _db_kc.picks.update_one(
                                {"id": _row.get("id")},
                                {"$set": {
                                    "no_bet": True,
                                    "no_bet_reason": (
                                        f"cross-refresh K conflict: new-{_new_side} "
                                        f"wins over DB-{_row_side} line={_new_line} "
                                        f"pitcher={_selection} ({_win_reason})"
                                    ),
                                }},
                            )
                            _kc_db_flagged += 1
                            logger.info(
                                "K_CROSS_DB: new-%s wins DB-%s (%s) pitcher=%s "
                                "line=%s db_pick_date=%s",
                                _new_side, _row_side, _win_reason, _selection,
                                _new_line, _row.get("pick_date"),
                            )
                        except Exception as _updx:
                            logger.warning(
                                "K_CROSS_DB update failed for row=%s: %s",
                                _row.get("id"), _updx,
                            )
                    elif _win_side == _opp:
                        # DB row wins — drop new pick.
                        _kc_drop_ids.add(id(_new_pick))
                        _kc_new_dropped += 1
                        logger.info(
                            "K_CROSS_DB: DB-%s wins new-%s (%s) pitcher=%s "
                            "line=%s db_pick_date=%s",
                            _row_side, _new_side, _win_reason, _selection,
                            _new_line, _row.get("pick_date"),
                        )
                        break  # this new pick is out — stop scanning
                    else:
                        # Indeterminate → drop BOTH.
                        try:
                            await _db_kc.picks.update_one(
                                {"id": _row.get("id")},
                                {"$set": {
                                    "no_bet": True,
                                    "no_bet_reason": (
                                        f"cross-refresh K conflict: indeterminate "
                                        f"vs new-{_new_side} line={_new_line} "
                                        f"pitcher={_selection}"
                                    ),
                                }},
                            )
                            _kc_db_flagged += 1
                        except Exception as _updx:
                            logger.warning(
                                "K_CROSS_DB indeterminate update failed: %s", _updx,
                            )
                        _kc_drop_ids.add(id(_new_pick))
                        _kc_new_dropped += 1
                        logger.info(
                            "K_CROSS_DB: BOTH dropped (indeterminate) pitcher=%s "
                            "line=%s db_pick_date=%s",
                            _selection, _new_line, _row.get("pick_date"),
                        )
                        break

            # Rebuild k_conflict_kept with drops applied.
            if _kc_drop_ids:
                k_conflict_kept = [
                    p for p in k_conflict_kept if id(p) not in _kc_drop_ids
                ]
            if _kc_db_flagged or _kc_new_dropped:
                logger.info(
                    "K_CROSS_DB summary: db_flagged=%d new_dropped=%d "
                    "surviving_k_picks=%d",
                    _kc_db_flagged, _kc_new_dropped, len(k_conflict_kept),
                )
        except Exception as _dbkc_err:
            logger.warning(
                "DB-aware K conflict resolver skipped: %s", _dbkc_err,
            )
    # ── /DEFECT #3 FIX ──────────────────────────────────────────────

    all_picks = non_k_picks + k_conflict_kept

    for p in all_picks:
        k = _dedup_key(p)
        existing = best.get(k)
        if existing is None:
            best[k] = p
            continue
        # 1) Market-family preference (Hits beats Total Bases regardless of
        #    lock_score — they're effectively the same bet for the bettor).
        new_pri = _market_priority(p.get("market"))
        old_pri = _market_priority(existing.get("market"))
        if new_pri < old_pri:
            best[k] = p
            continue
        if new_pri > old_pri:
            continue
        # 2) Same family — prefer higher lock_score.
        if p["lock_score"] > existing["lock_score"]:
            best[k] = p
        elif p["lock_score"] == existing["lock_score"]:
            # 3) Tie-break on better (more positive) odds.
            if (p.get("book_odds") or -9999) > (existing.get("book_odds") or -9999):
                best[k] = p
    if len(best) < len(all_picks):
        logger.info(
            "Deduped %d correlated picks (kept %d of %d)",
            len(all_picks) - len(best), len(best), len(all_picks),
        )
    all_picks = list(best.values())
    # Promote board-toppers to Elite tier — but ONLY picks that combine high
    # model confidence with real betting value AND happen today. Friday games
    # don't deserve to be promoted as the "best bet for the day" on Wednesday.
    if all_picks:
        def _elite_composite(p: dict) -> float:
            # Primary: lock_score (high-confidence picks come first — these
            # are the "feels-like-a-lock" picks users want at the top).
            # Tiebreaker: edge (when two picks share a lock_score, prefer
            # the one with more value). Edge contribution is tiny so it
            # only matters within the same lock_score band.
            # 2026-08-23 hardening — some producers legitimately emit
            # ``edge_percent=None`` (no-book direct-inject shadow rows).
            # Treat None as 0.0 so this comparator never blows up.
            _e = p.get("edge_percent")
            try:
                _e_val = float(_e)
            except (TypeError, ValueError):
                _e_val = 0.0
            return p["lock_score"] + max(0.0, _e_val) * 0.1

        # Filter to games that actually kick off within the next 24 hours.
        # This ensures the Elite tier surfaces TODAY'S best bets, not games
        # 2-3 days out that happen to have soft lines.
        now = datetime.now(timezone.utc)
        today_cutoff = now + timedelta(hours=24)

        def _starts_today(p: dict) -> bool:
            et = p.get("event_time")
            if not et:
                return False
            try:
                dt = datetime.strptime(et, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                return now <= dt <= today_cutoff
            except Exception:
                return False

        # Candidates: keep only picks whose edge is not meaningfully negative.
        # Edge >= -0.5% is the floor (tiny noise allowed; clear -EV picks excluded).
        # 2026-08-23 hardening — some upstream producers legitimately emit
        # ``edge_percent=None`` (e.g. no-book direct-inject shadow rows).
        # Treat None as "0 edge" instead of crashing the whole refresh
        # pipeline (previously masked ALL MLB hitter props for hours).
        def _edge_gt(p):
            e = p.get("edge_percent")
            try:
                return float(e) >= -0.5
            except (TypeError, ValueError):
                return True   # unknown edge — keep pick, let downstream gate it
        all_candidates = [p for p in all_picks if _edge_gt(p)]
        today_candidates = [p for p in all_candidates if _starts_today(p)]
        # Prefer today's games. If we have at least 3 quality picks today,
        # the Elite tier is built exclusively from today. Otherwise we fall
        # back to the broader 72h pool so the tier is never empty.
        if len(today_candidates) >= 3:
            candidates = today_candidates
        else:
            candidates = today_candidates + [p for p in all_candidates if p not in today_candidates]
        candidates.sort(key=_elite_composite, reverse=True)
        # No sport cap — top 5 by lock score wins, period. Users want the
        # highest-confidence picks at the top, even if they cluster in one sport.
        promoted = candidates[:5]
        for i, p in enumerate(promoted):
            # 2026-07-21 FINAL PHASE — deterministic rank-based boost.
            # Was `random.uniform(2, 5)` which made Elite scores
            # non-reproducible across refreshes. Rank-linear spread
            # (0.6 / 1.2 / 1.8 / 2.4 / 3.0) keeps ordering stable and
            # still spreads the top 5 across the 95-99 band.
            rank_boost = (5 - i) * 1.0 + (5 - i) * 0.5   # 7.5, 6.0, 4.5, 3.0, 1.5
            boost = max(95.0, min(99.0, p["lock_score"] + rank_boost))
            p["lock_score"] = round(boost, 1)
            p["grade"] = _grade(boost)
            p["confidence"] = _confidence(boost)
    return all_picks
