"""OddsApiGateway — Phase 2γ single choke point for The Odds API.

Every paid Odds-API request in the codebase must go through this
module.  Responsibilities:

  • Distributed single-flight (services.single_flight)
  • Budget reservation via services.provider_budget
  • Bad-market registry consultation and market filtering
  • Tournament-registry consultation for /events discovery
  • Circuit breaker awareness (sports_engine.get_odds_api_status)
  • odds_api_request_log completeness (via services.odds_cache)
  • Actual-cost reconciliation from provider quota headers when
    available (``x-requests-used`` / ``x-requests-remaining``)
  • Retry classification — safe fan-out only on confirmed 422

The feature flag ``ODDS_GATEWAY_ENABLED`` toggles the new transport
path.  When disabled, callers fall back to the pre-2γ centralized
cached provider path (services.odds_cache).  The flag never bypasses
budget or coordinator checks — those remain active in both modes.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

logger = logging.getLogger("lockscore.odds_api_gateway")

# ── Configuration (env-driven, safe defaults) ────────────────────────
ODDS_API_BASE = "https://api.the-odds-api.com/v4"

# Per-job caps for the narrow 422-retry path (was
# `_fetch_event_odds_individual` fan-out).  Both caps must be exhausted
# for the retry to stop.
MAX_422_RETRY_REQUESTS = 4     # never more than N narrow retries per job
MAX_422_RETRY_CREDITS  = 40    # or more than M credits worth of retries

# Endpoint categories.  Used for logging, budget scaling, and the
# 422-retry gate (per-market/per-event fan-out is only ever allowed
# when the caller is in APPROVED_422_ENDPOINTS).
ENDPOINT_SPORTS_LIST      = "sports_list"
ENDPOINT_EVENTS_LIST      = "events_list"
ENDPOINT_BULK_ODDS        = "bulk_odds"
ENDPOINT_EVENT_ODDS       = "event_odds"
ENDPOINT_SCORES           = "scores"
ENDPOINT_ALT_LINES        = "alt_lines"
ENDPOINT_GENERIC          = "generic"

APPROVED_422_ENDPOINTS = {ENDPOINT_EVENT_ODDS, ENDPOINT_ALT_LINES}


def _gateway_enabled() -> bool:
    """Feature flag.  Default: on."""
    v = os.environ.get("ODDS_GATEWAY_ENABLED", "true").strip().lower()
    return v in ("", "1", "true", "yes", "on")


def _global_refresh_mode() -> str:
    v = os.environ.get("ODDS_GLOBAL_REFRESH_MODE", "snapshot").strip().lower()
    if v not in ("snapshot", "legacy_hourly"):
        return "snapshot"
    return v


def _api_key() -> str:
    return (os.environ.get("THE_ODDS_API_KEY") or "").strip()


def _classify_endpoint(url: str) -> str:
    u = url.split("?")[0]
    if u.endswith("/sports") or u.endswith("/sports/"):
        return ENDPOINT_SPORTS_LIST
    if u.endswith("/events") or u.endswith("/events/"):
        return ENDPOINT_EVENTS_LIST
    if "/events/" in u and (u.endswith("/odds") or u.endswith("/odds/")):
        return ENDPOINT_EVENT_ODDS
    if u.endswith("/odds") or u.endswith("/odds/"):
        return ENDPOINT_BULK_ODDS
    if u.endswith("/scores") or u.endswith("/scores/"):
        return ENDPOINT_SCORES
    return ENDPOINT_GENERIC


def _sport_key_from_url(url: str) -> Optional[str]:
    m = re.search(r"/sports/([^/?]+)", url)
    return m.group(1) if m else None


def _event_id_from_url(url: str) -> Optional[str]:
    m = re.search(r"/events/([^/?]+)", url)
    return m.group(1) if m else None


class GatewayResult(dict):
    """Return envelope from the gateway.  Truthy when the upstream
    responded successfully (or a cache hit was served)."""

    def __bool__(self) -> bool:  # type: ignore[override]
        return self.get("ok") is True

    @property
    def data(self) -> Any:
        return self.get("data")


class OddsApiGateway:
    """One gateway to bind them all."""

    def __init__(self, db) -> None:
        self.db = db
        # Lazy imports to keep the transport module free of heavy deps.
        from services.provider_budget import ProviderBudget
        from services.single_flight import SingleFlight
        from services.tournament_registry import TournamentRegistry
        from services import bad_market_registry
        self.budget    = ProviderBudget(db, provider="odds_api")
        self.flight    = SingleFlight(db)
        self.tourney   = TournamentRegistry(db)
        self._bad_mkt  = bad_market_registry

    async def ensure_indices(self) -> None:
        await self.flight.ensure_indices()
        await self.tourney.ensure_indices()
        try:
            await self._bad_mkt.ensure_indices(self.db)
        except Exception:  # pragma: no cover
            pass

    # ─────────────────────────────────────────────────────────
    # Cost estimation
    # ─────────────────────────────────────────────────────────
    @staticmethod
    def estimate_credits(endpoint_type: str, *,
                          markets: Optional[str] = None,
                          regions: Optional[str] = None,
                          bookmakers: Optional[str] = None) -> int:
        """Return an upper-bound credit estimate for a single request.

        The Odds API bills per (market × region) tuple.  These
        constants track the current pricing model and are used ONLY
        as a reservation upper bound — actual cost is reconciled from
        provider quota headers when available.
        """
        if endpoint_type == ENDPOINT_SPORTS_LIST:
            return 1
        if endpoint_type == ENDPOINT_EVENTS_LIST:
            return 1
        if endpoint_type == ENDPOINT_SCORES:
            return 1
        n_markets = len([m for m in (markets or "").split(",") if m.strip()])
        n_regions = len([r for r in (regions or "").split(",") if r.strip()])
        n_markets = max(1, n_markets)
        n_regions = max(1, n_regions)
        if endpoint_type in (ENDPOINT_BULK_ODDS, ENDPOINT_EVENT_ODDS,
                              ENDPOINT_ALT_LINES):
            # 1 credit per (market × region).  Bookmakers do not
            # multiply cost.
            return n_markets * n_regions
        return max(1, n_markets * n_regions)

    # ─────────────────────────────────────────────────────────
    # Actual-cost reconciliation
    # ─────────────────────────────────────────────────────────
    @staticmethod
    def read_actual_cost(response_headers: dict, *,
                          fallback_estimate: int = 1) -> Optional[int]:
        """Read The Odds API's own quota header to compute the exact
        cost for the request just completed.

        Header: ``x-requests-used`` — cumulative day counter.  The
        delta between two consecutive requests IS the actual cost of
        the intervening request.  We stash the last-known value on
        the gateway instance for delta calculation.
        """
        try:
            used = response_headers.get("x-requests-used") \
                or response_headers.get("X-Requests-Used")
            if used is not None:
                return int(used)
        except Exception:
            return None
        return None

    # ─────────────────────────────────────────────────────────
    # Public fetch entry-point
    # ─────────────────────────────────────────────────────────
    async def fetch(
        self, url: str, *,
        params: Optional[dict] = None,
        caller: str,
        reason: str,
        job_name: Optional[str] = None,
        sport_key: Optional[str] = None,
        markets: Optional[str] = None,
        regions: Optional[str] = None,
        bookmakers: Optional[str] = None,
        odds_format: Optional[str] = None,
        emergency_requested: bool = False,
        cache_policy: str = "normal",
        allow_stale_seconds: int = 300,
        timeout_seconds: float = 15.0,
        priority: Optional[int] = None,
    ) -> GatewayResult:
        """Perform a paid Odds API call.

        Parameters
        ──────────
        caller, reason, job_name : REQUIRED
            No paid request is allowed without a named caller + reason.
        cache_policy : "normal" | "force_refresh" | "stale_ok"
            Same semantics as the pre-2γ ``cached_httpx_get``.
        emergency_requested : bool
            If True and the ProviderBudget policy permits, the request
            may draw from the emergency reserve.
        priority : int | None
            Phase 4 (2026-08-11) — P1..P5 tier from
            ``services.provider_budget_priority``.  When provided the
            gateway consults the shared budget-priority helper BEFORE
            reserving credits.  Low-priority requests (P5/P4) are
            rejected first when the daily budget headroom is tight
            so P1/P2 current-board pipelines cannot be starved.  When
            omitted, defaults to P3 (neutral middle tier).
        """
        if not caller or not reason:
            raise ValueError("OddsApiGateway.fetch requires caller AND reason")

        # ── Phase 4 priority gate (additive; never bypasses budget) ──
        try:
            from services import provider_budget_priority as _pbp
            _pri = priority if priority is not None else _pbp.P3_ALT_STRONG
            if _pri not in _pbp.VALID_PRIORITIES:
                raise ValueError(f"invalid priority {_pri}")
            # Consult live budget state via the ProviderBudget layer.
            used = int(getattr(self.budget, "_last_used_daily", 0) or 0)
            limit = int(getattr(self.budget, "_last_limit_daily", 0) or 0)
            _dec = _pbp.decide(_pri, used, limit)
            if not _dec.allowed:
                logger.info(
                    "gateway: priority-shed p%d caller=%s reason=%s "
                    "headroom=%.1f%% threshold=p%d",
                    _pri, caller, reason, _dec.headroom_pct, _dec.threshold,
                )
                return GatewayResult(
                    ok=False, status="priority_shed",
                    reason=_dec.reason, data=None,
                    used_cache=False, from_stale=False,
                    endpoint=url, sport_key=None,
                    event_id=None,
                )
        except ImportError:
            pass   # priority helper not present in a legacy deploy

        params = dict(params or {})
        endpoint_type = _classify_endpoint(url)
        s_key = sport_key or _sport_key_from_url(url)
        event_id = _event_id_from_url(url)

        # ── Bad-market filter (pre-flight) ──────────────────────────
        if markets and s_key:
            try:
                good = await self._bad_mkt.filter_markets(
                    self.db, sport_key=s_key,
                    markets=markets.split(","))
                markets_effective = ",".join(good) if good else ""
                if not markets_effective:
                    logger.info(
                        "gateway: all markets bad-listed for %s ev=%s "
                        "(caller=%s)", s_key, event_id, caller,
                    )
                    return GatewayResult(
                        ok=False, reason="all_markets_bad_listed",
                        data=None,
                        endpoint_type=endpoint_type,
                        sport_key=s_key, event_id=event_id,
                    )
                if markets_effective != markets:
                    params["markets"] = markets_effective
                    markets = markets_effective
            except Exception as e:
                logger.debug("bad_market filter err: %s", e)

        est_credits = self.estimate_credits(
            endpoint_type,
            markets=markets or params.get("markets"),
            regions=regions or params.get("regions"),
            bookmakers=bookmakers or params.get("bookmakers"),
        )

        # ── Build deterministic request key ─────────────────────────
        from services.single_flight import build_request_key
        # Strip caller-specific noise from params for the key.
        param_view = {
            k: v for k, v in params.items()
            if k.lower() not in ("apikey", "api_key")
        }
        rk = build_request_key(
            provider="odds_api",
            endpoint=urlparse(url).path,
            sport_key=s_key,
            event_id=event_id,
            markets=markets or params.get("markets"),
            regions=regions or params.get("regions"),
            bookmakers=bookmakers or params.get("bookmakers"),
            odds_format=odds_format or params.get("oddsFormat"),
            extra_params={
                k: v for k, v in param_view.items()
                if k.lower() not in {
                    "markets", "regions", "bookmakers", "oddsformat",
                }
            },
        )

        # ── Tournament registry filter for discovery (/events) ──────
        if endpoint_type == ENDPOINT_EVENTS_LIST and s_key:
            eligible = await self.tourney.is_eligible(s_key)
            if not eligible:
                logger.debug(
                    "gateway: %s events discovery suppressed by "
                    "tournament_registry (caller=%s)", s_key, caller,
                )
                return GatewayResult(
                    ok=False, reason="tournament_suppressed",
                    data=None, endpoint_type=endpoint_type,
                    sport_key=s_key,
                )

        # ── Distributed single-flight ───────────────────────────────
        won, current = await self.flight.acquire(rk, ttl_seconds=30)
        if not won:
            # Someone else is fetching the same contract.  Wait a
            # short time for their result; on timeout, try to serve a
            # stale cache row.
            logger.debug("gateway single-flight wait: rk=%s caller=%s", rk[:32], caller)
            waited = await self.flight.wait_for_result(rk, timeout=4.0)
            # Regardless, prefer the stored cache for this contract.
            from services.odds_cache import _cache_key
            ckey = _cache_key(url, params)
            cache_row = await self.db.odds_cache.find_one(
                {"cache_key": ckey}, {"_id": 0, "data": 1})
            if cache_row and cache_row.get("data") is not None:
                await self._log_paid_request(
                    endpoint_type=endpoint_type,
                    url=url, params=param_view,
                    caller=caller, reason=reason,
                    job_name=job_name, sport_key=s_key,
                    event_id=event_id, markets=markets,
                    request_key=rk, cache_outcome="single_flight_stale_hit",
                    duplicate_suppressed=True,
                    reservation_id=None,
                    estimated_credits=0, actual_credits=0,
                    http_status=None, retry_reason=None,
                    emergency_used=False, duration_ms=0,
                    upstream_called=False,
                )
                return GatewayResult(
                    ok=True, reason="single_flight_hit",
                    data=cache_row.get("data"),
                    endpoint_type=endpoint_type,
                    sport_key=s_key, event_id=event_id,
                    request_key=rk, duplicate_suppressed=True,
                )
            # No cache available — still credit-safe to return miss.
            return GatewayResult(
                ok=False, reason="single_flight_miss",
                data=None, endpoint_type=endpoint_type,
                request_key=rk, duplicate_suppressed=True,
            )

        # ── Budget reservation ─────────────────────────────────────
        owner_token = current["owner_token"] if current else None
        reservation = await self.budget.reserve(
            estimated_credits=est_credits,
            endpoint_type=endpoint_type,
            caller=caller,
            job_name=job_name or f"gateway:{endpoint_type}",
            sport=s_key,
            market=markets or params.get("markets"),
            emergency_requested=bool(emergency_requested),
            reason=reason,
            request_key=rk,
            ttl_seconds=180,
        )
        if not reservation.get("allowed"):
            # Release the flight slot so a subsequent caller with
            # different budget context can try.
            await self.flight.fail(rk, owner_token or "",
                                    error=f"budget_denied:{reservation.get('outcome')}")
            await self._log_paid_request(
                endpoint_type=endpoint_type,
                url=url, params=param_view,
                caller=caller, reason=reason,
                job_name=job_name, sport_key=s_key,
                event_id=event_id, markets=markets,
                request_key=rk, cache_outcome="budget_denied",
                duplicate_suppressed=False,
                reservation_id=None,
                estimated_credits=est_credits, actual_credits=0,
                http_status=None, retry_reason=None,
                emergency_used=False, duration_ms=0,
                upstream_called=False, budget_outcome=reservation.get("outcome"),
            )
            return GatewayResult(
                ok=False, reason="budget_denied",
                budget_outcome=reservation.get("outcome"),
                data=None, endpoint_type=endpoint_type,
                request_key=rk,
            )
        intent_id = reservation.get("intent_id")

        # ── Circuit-breaker guard ──────────────────────────────────
        try:
            from sports_engine import get_odds_api_status
            st = get_odds_api_status() or {}
            if st.get("disabled"):
                await self.budget.release(intent_id,
                                            reason="circuit_open")
                await self.flight.fail(rk, owner_token or "",
                                        error="circuit_open")
                await self._log_paid_request(
                    endpoint_type=endpoint_type, url=url, params=param_view,
                    caller=caller, reason=reason, job_name=job_name,
                    sport_key=s_key, event_id=event_id, markets=markets,
                    request_key=rk, cache_outcome="circuit_open",
                    duplicate_suppressed=False, reservation_id=intent_id,
                    estimated_credits=est_credits, actual_credits=0,
                    http_status=None, retry_reason="circuit_open",
                    emergency_used=False, duration_ms=0,
                    upstream_called=False,
                )
                return GatewayResult(
                    ok=False, reason="circuit_open",
                    data=None, endpoint_type=endpoint_type,
                    request_key=rk,
                )
        except Exception:  # pragma: no cover
            pass

        # ── Actual upstream HTTP ───────────────────────────────────
        t0 = time.monotonic()
        http_status: Optional[int] = None
        actual_credits = est_credits
        upstream_error: Optional[str] = None
        response_data: Any = None
        quota_headers: dict = {}
        try:
            import httpx
            full_params = {**params}
            api_key = _api_key()
            if api_key:
                full_params["apiKey"] = api_key
            async with httpx.AsyncClient(timeout=timeout_seconds) as cx:
                resp = await cx.get(url, params=full_params)
                http_status = resp.status_code
                _hdrs = getattr(resp, "headers", None)
                if _hdrs:
                    try:
                        quota_headers = {
                            k.lower(): v for k, v in _hdrs.items()
                            if k.lower().startswith("x-requests-")
                        }
                    except Exception:
                        quota_headers = {}
                else:
                    quota_headers = {}
                if http_status == 200:
                    try:
                        response_data = resp.json()
                    except ValueError as jerr:
                        upstream_error = f"json_decode:{jerr}"
                        response_data = None
                elif http_status == 422 and endpoint_type in APPROVED_422_ENDPOINTS:
                    # Mark the (sport, market) pair(s) bad. The retry
                    # decision itself is left to the caller — this
                    # gateway only reports the 422 outcome.
                    try:
                        m_list = [m.strip()
                                    for m in (markets or "").split(",")
                                    if m.strip()]
                        if m_list and s_key:
                            await self._bad_mkt.mark_bad(
                                self.db, sport_key=s_key,
                                markets=m_list,
                                reason="422_unsupported_market",
                            )
                    except Exception:  # pragma: no cover
                        pass
                    upstream_error = "422"
                else:
                    upstream_error = f"http_{http_status}"
        except Exception as e:
            upstream_error = f"exc:{type(e).__name__}:{e}"
        duration_ms = int((time.monotonic() - t0) * 1000)

        # ── Sports-engine CB state sync (Phase 2γ closeout) ─────────
        try:
            from sports_engine import record_odds_call_result
            record_odds_call_result(
                status_code=http_status,
                body=str(upstream_error or "")[:200],
                ok=(response_data is not None and http_status == 200),
                exception=upstream_error
                    if (upstream_error and upstream_error.startswith("exc:")) else None,
            )
        except Exception:  # pragma: no cover
            pass

        # ── Signal → tournament registry (events discovery) ─────────
        if endpoint_type == ENDPOINT_EVENTS_LIST and s_key:
            if isinstance(response_data, list):
                await self.tourney.mark_events_seen(
                    s_key, count=len(response_data))
                if len(response_data) == 0:
                    await self.tourney.mark_empty(s_key)
            elif upstream_error:
                await self.tourney.mark_empty(
                    s_key, failure_reason=str(upstream_error)[:120])

        # ── Actual cost from quota headers ──────────────────────────
        used_now = None
        try:
            u = quota_headers.get("x-requests-used")
            if u is not None:
                used_now = int(u)
        except Exception:
            used_now = None
        # Cache the last-known used value on the DB so multi-worker
        # deltas can be reconciled cheaply.
        try:
            state = await self.db.odds_api_quota_state.find_one(
                {"_id": "odds_api"}) or {}
            prev_used = state.get("last_used")
            if used_now is not None:
                await self.db.odds_api_quota_state.update_one(
                    {"_id": "odds_api"},
                    {"$set": {"last_used": used_now,
                                "updated_at": datetime.now(timezone.utc)}},
                    upsert=True,
                )
                if isinstance(prev_used, int) and used_now >= prev_used:
                    actual_credits = min(
                        used_now - prev_used, est_credits * 4
                    )
        except Exception:  # pragma: no cover
            pass
        # Failed request charging: 0 credits when The Odds API says the
        # request was rejected before market processing (401/422/429/5xx).
        if upstream_error and used_now is None:
            if str(http_status or "") in {"401", "403", "422", "429"} or \
               (http_status or 0) >= 500:
                actual_credits = 0
            else:
                actual_credits = est_credits

        # ── Persist to DB cache (odds_cache) ────────────────────────
        if response_data is not None:
            try:
                from services.odds_cache import _persist_cache_row
                await _persist_cache_row(
                    self.db, url=url, params=param_view,
                    data=response_data, sport_key=s_key,
                    markets=markets,
                )
            except Exception as e:
                logger.debug("gateway cache persist err: %s", e)

        # ── Budget commit + flight completion ───────────────────────
        try:
            # Phase 2γ closeout: atomic top-up if actual exceeds
            # estimate.  If the top-up fails we still commit at the
            # estimated cap and log an overage — follow-up fan-out
            # must be blocked by the caller.
            if actual_credits > est_credits:
                top = await self.budget.top_up(
                    intent_id, extra=actual_credits - est_credits,
                    emergency_requested=bool(reservation.get("emergency")),
                    reason="actual_over_estimate",
                )
                if not top.get("ok"):
                    logger.warning(
                        "gateway top_up denied (%s) — committing at "
                        "est-cap; caller=%s job=%s actual=%s est=%s",
                        top.get("outcome"), caller, job_name,
                        actual_credits, est_credits,
                    )
                    actual_credits = est_credits
                    await self.budget._audit(
                        "budget_overage_blocked",
                        caller=caller, job_name=job_name,
                        actual_credits=actual_credits,
                        estimated_credits=est_credits,
                        intent_id=intent_id,
                    )
            await self.budget.commit(intent_id,
                                       actual_credits=actual_credits,
                                       response_metadata={
                                           "http_status": http_status,
                                           "endpoint_type": endpoint_type,
                                       })
        except Exception:  # pragma: no cover
            pass

        result_summary = {
            "ok": response_data is not None,
            "http_status": http_status,
            "actual_credits": actual_credits,
        }
        if response_data is not None:
            await self.flight.complete(rk, owner_token or "",
                                         result_summary=result_summary)
        else:
            await self.flight.fail(rk, owner_token or "",
                                    error=str(upstream_error)[:200])

        # ── Log ─────────────────────────────────────────────────────
        await self._log_paid_request(
            endpoint_type=endpoint_type,
            url=url, params=param_view,
            caller=caller, reason=reason, job_name=job_name,
            sport_key=s_key, event_id=event_id, markets=markets,
            request_key=rk, cache_outcome="miss" if response_data is not None else "miss_failed",
            duplicate_suppressed=False,
            reservation_id=intent_id,
            estimated_credits=est_credits, actual_credits=actual_credits,
            http_status=http_status, retry_reason=upstream_error,
            emergency_used=bool(reservation.get("emergency")),
            duration_ms=duration_ms, upstream_called=True,
            quota_headers=quota_headers,
        )

        return GatewayResult(
            ok=response_data is not None,
            data=response_data,
            reason=upstream_error or "ok",
            endpoint_type=endpoint_type,
            sport_key=s_key, event_id=event_id,
            request_key=rk,
            actual_credits=actual_credits,
            estimated_credits=est_credits,
            http_status=http_status,
        )

    # ─────────────────────────────────────────────────────────
    # Request-log writer (extends odds_api_request_log)
    # ─────────────────────────────────────────────────────────
    async def _log_paid_request(self, *,
        endpoint_type: str,
        url: str, params: dict,
        caller: str, reason: str, job_name: Optional[str],
        sport_key: Optional[str], event_id: Optional[str],
        markets: Optional[str], request_key: str,
        cache_outcome: str, duplicate_suppressed: bool,
        reservation_id: Optional[str],
        estimated_credits: int, actual_credits: int,
        http_status: Optional[int], retry_reason: Optional[str],
        emergency_used: bool, duration_ms: int,
        upstream_called: bool,
        budget_outcome: Optional[str] = None,
        quota_headers: Optional[dict] = None,
    ) -> None:
        rec = {
            "ts":                datetime.now(timezone.utc).isoformat(),
            "endpoint_type":     endpoint_type,
            "endpoint_path":     urlparse(url).path,
            "url":               url,
            "params":            params,
            "caller":            caller,
            "reason":            reason,
            "job_name":          job_name,
            "sport_key":         sport_key,
            "sport":             sport_key,
            "event_id":          event_id,
            "markets":           markets,
            "request_key":       request_key,
            "cache_outcome":     cache_outcome,
            "cache_status":      cache_outcome,     # legacy field name
            "duplicate_suppressed": duplicate_suppressed,
            "budget_reservation_id": reservation_id,
            "budget_outcome":    budget_outcome,
            "estimated_credits": estimated_credits,
            "actual_credits":    actual_credits,
            "upstream_status":   http_status,
            "http_status":       http_status,
            "retry_reason":      retry_reason,
            "emergency_used":    emergency_used,
            "duration_ms":       duration_ms,
            "upstream_called":   upstream_called,
            "gateway":           True,
        }
        if quota_headers:
            rec["quota_headers"] = {k: v for k, v in quota_headers.items()}
        try:
            await self.db.odds_api_request_log.insert_one(rec)
        except Exception as e:  # pragma: no cover
            logger.debug("gateway log write err: %s", e)


__all__ = [
    "OddsApiGateway", "GatewayResult",
    "ODDS_API_BASE",
    "ENDPOINT_SPORTS_LIST", "ENDPOINT_EVENTS_LIST",
    "ENDPOINT_BULK_ODDS", "ENDPOINT_EVENT_ODDS",
    "ENDPOINT_SCORES", "ENDPOINT_ALT_LINES", "ENDPOINT_GENERIC",
    "APPROVED_422_ENDPOINTS",
    "MAX_422_RETRY_REQUESTS", "MAX_422_RETRY_CREDITS",
    "_gateway_enabled", "_global_refresh_mode",
    "_classify_endpoint", "_sport_key_from_url", "_event_id_from_url",
]
