"""PitchAPI Soccer completed-match provider (2026-08-25).

PRIMARY provider for Soccer completed-fixture actual/stat data.
Wiring status: SCAFFOLD ONLY — settlement pipeline is NOT calling this
yet. Envelope + cache infrastructure is here so we can flip on
settlement wiring per market (P3) with `wire=True` on the specific
markets whose real provider response has been verified.

Design rules (per P3 hard-freeze contract):
  • PitchAPI is the PRIMARY completed-fixture provider for Soccer.
  • Big Balls is the FALLBACK when PitchAPI lacks the required
    fixture/stat.
  • The Odds API remains the sportsbook-odds provider and is NOT
    replaced or duplicated here.
  • Neither provider is authoritative for pre-match probability;
    they exist purely to settle already-published Perklocks picks
    against real completed-game actuals.

Supported field set (initial, will grow after real provider response
verification):
  goals, goalscorer, assists, score_or_assist, player_shots,
  player_shots_on_target, team_corners, cards

Missing data on an otherwise supported completed fixture returns
DATA_UNAVAILABLE. This provider NEVER guesses WIN/LOSS/PUSH.

Cache:
  • Completed-fixture actuals are IMMUTABLE — once persisted the
    cache lives forever, cutting API usage to (near) zero on
    replays / settlement retries.
  • Cache key: (provider, sport, canonical_event_id, market_family)
  • Backing store: db.provider_stat_cache (new collection —
    additive; nothing else uses it).

This module is IMPORT-ONLY at present. `settle_soccer_pick` is
exported but callers must opt-in via a flag.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

PROVIDER_NAME = "pitchapi"
PROVIDER_VERSION = "1.0.0-scaffold"

# Real base URL is placeholder until real product endpoint is confirmed
# from the provider dashboard. Reads env override so ops can flip it
# without redeploy.
DEFAULT_BASE_URL = os.getenv("PITCHAPI_BASE_URL", "https://api.pitchapi.io")
API_KEY_ENV = "PITCHAPI_API_KEY"

# Supported market family whitelist. A family only becomes SUPPORTED
# after a real provider response has been observed for it in
# `db.provider_stat_cache`.  Until then it stays SETTLEMENT_UNSUPPORTED
# so the settlement gate does not falsely mark a pick as PUSH/LOSS.
SUPPORTED_MARKETS = frozenset({
    "soccer_goals",
    "soccer_goalscorer",
    "soccer_assists",
    "soccer_score_or_assist",
    "soccer_player_shots",
    "soccer_player_shots_on_target",
    "soccer_team_corners",
    "soccer_cards",
})


@dataclass
class ProviderResult:
    """Uniform return type for both providers.

    status:
      "OK"                  — data present, `actual` is authoritative
      "DATA_UNAVAILABLE"    — provider reachable but no data for this
                              fixture/market
      "MARKET_UNSUPPORTED"  — this provider does not cover this market
      "AUTH_FAIL"           — bad/missing API key
      "PROVIDER_ERROR"      — 5xx / network / parse
    """
    status: str
    actual: Any = None
    provider: str = PROVIDER_NAME
    provider_version: str = PROVIDER_VERSION
    provider_event_id: Optional[str] = None
    canonical_event_id: Optional[str] = None
    canonical_player_id: Optional[str] = None
    canonical_team_id: Optional[str] = None
    fetched_at: str = ""
    latency_ms: int = 0
    error_detail: Optional[str] = None
    raw_snippet: Optional[dict] = None
    provenance: dict = field(default_factory=dict)


def api_key() -> Optional[str]:
    """Return PitchAPI key from env (never printed / logged)."""
    return os.getenv(API_KEY_ENV)


def is_configured() -> bool:
    return bool(api_key())


async def health_check(timeout: float = 5.0) -> dict:
    """Non-fatal health probe. Returns status/latency.

    Does NOT expose the API key value anywhere in the response.
    """
    if not is_configured():
        return {
            "provider": PROVIDER_NAME, "configured": False,
            "status": "API_KEY_MISSING",
        }
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                f"{DEFAULT_BASE_URL}/v1/ping",
                headers={"Authorization": f"Bearer {api_key()}"},
            )
            return {
                "provider": PROVIDER_NAME,
                "configured": True,
                "status": "OK" if resp.status_code < 400 else "PROVIDER_ERROR",
                "http_code": resp.status_code,
                "latency_ms": int((time.monotonic() - started) * 1000),
            }
    except Exception as e:
        return {
            "provider": PROVIDER_NAME,
            "configured": True,
            "status": "PROVIDER_UNREACHABLE",
            "error": type(e).__name__,
            "latency_ms": int((time.monotonic() - started) * 1000),
        }


# ── Cache read/write (backed by db.provider_stat_cache) ───────────────
# Every completed-fixture actual is immutable; once cached it lives
# forever so replay costs are zero.
async def cache_get(
    db, *, sport: str, canonical_event_id: str,
    market_family: str, canonical_player_id: Optional[str] = None,
) -> Optional[ProviderResult]:
    """Return cached ProviderResult if present, else None."""
    if not canonical_event_id or not market_family:
        return None
    q = {
        "provider": PROVIDER_NAME,
        "sport": (sport or "").lower(),
        "canonical_event_id": canonical_event_id,
        "market_family": market_family,
    }
    if canonical_player_id:
        q["canonical_player_id"] = canonical_player_id
    doc = await db.provider_stat_cache.find_one(q, {"_id": 0})
    if not doc:
        return None
    return ProviderResult(
        status=doc.get("status", "OK"),
        actual=doc.get("actual"),
        provider=PROVIDER_NAME,
        provider_version=doc.get("provider_version") or PROVIDER_VERSION,
        provider_event_id=doc.get("provider_event_id"),
        canonical_event_id=canonical_event_id,
        canonical_player_id=canonical_player_id,
        canonical_team_id=doc.get("canonical_team_id"),
        fetched_at=doc.get("fetched_at") or "",
        provenance=doc.get("provenance") or {},
    )


async def cache_put(db, result: ProviderResult, *, sport: str,
                    market_family: str) -> None:
    """Persist a ProviderResult. Idempotent on the composite key."""
    if not result.canonical_event_id or not market_family:
        return
    key = {
        "provider": PROVIDER_NAME,
        "sport": (sport or "").lower(),
        "canonical_event_id": result.canonical_event_id,
        "market_family": market_family,
    }
    if result.canonical_player_id:
        key["canonical_player_id"] = result.canonical_player_id
    doc = {
        **key,
        "status": result.status,
        "actual": result.actual,
        "provider_version": result.provider_version,
        "provider_event_id": result.provider_event_id,
        "canonical_team_id": result.canonical_team_id,
        "fetched_at": result.fetched_at or datetime.now(timezone.utc)
                                            .isoformat().replace("+00:00", "Z"),
        "provenance": result.provenance,
    }
    await db.provider_stat_cache.update_one(key, {"$set": doc}, upsert=True)


async def get_completed_actual(
    db, *, sport: str, canonical_event_id: str,
    market_family: str, canonical_player_id: Optional[str] = None,
    force_refresh: bool = False,
) -> ProviderResult:
    """Return actual value for a completed Soccer fixture + market.

    SCAFFOLD-ONLY BEHAVIOR (2026-08-25):
      • Reads cache first.
      • On cache miss AND `is_configured()`, issues a REAL request
        against the placeholder base URL.  If the provider is not
        yet contactable at the placeholder URL, the request will
        return PROVIDER_ERROR and NOTHING is cached — caller must
        treat as DATA_UNAVAILABLE.
      • Never returns a synthetic actual.

    Callers MUST enforce the "sport is Soccer" precondition; this
    module is a Soccer-first provider by design.
    """
    if (sport or "").lower() != "soccer":
        return ProviderResult(
            status="MARKET_UNSUPPORTED",
            provider=PROVIDER_NAME,
            error_detail="PitchAPI supports Soccer only in this scaffold",
        )
    if market_family not in SUPPORTED_MARKETS:
        return ProviderResult(
            status="MARKET_UNSUPPORTED",
            provider=PROVIDER_NAME,
            canonical_event_id=canonical_event_id,
            canonical_player_id=canonical_player_id,
            error_detail=f"market_family={market_family} not in scaffold whitelist",
        )
    if not force_refresh:
        cached = await cache_get(
            db, sport=sport, canonical_event_id=canonical_event_id,
            market_family=market_family, canonical_player_id=canonical_player_id,
        )
        if cached is not None:
            return cached
    if not is_configured():
        return ProviderResult(
            status="AUTH_FAIL", provider=PROVIDER_NAME,
            canonical_event_id=canonical_event_id,
            canonical_player_id=canonical_player_id,
            error_detail=f"{API_KEY_ENV} not configured",
        )
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(
                f"{DEFAULT_BASE_URL}/v1/fixtures/{canonical_event_id}/stats",
                headers={"Authorization": f"Bearer {api_key()}"},
                params={"market": market_family,
                        **({"player_id": canonical_player_id}
                            if canonical_player_id else {})},
            )
        latency_ms = int((time.monotonic() - started) * 1000)
        if resp.status_code == 401 or resp.status_code == 403:
            return ProviderResult(
                status="AUTH_FAIL", provider=PROVIDER_NAME,
                latency_ms=latency_ms,
                canonical_event_id=canonical_event_id,
                canonical_player_id=canonical_player_id,
                error_detail=f"HTTP {resp.status_code}",
            )
        if resp.status_code == 404 or resp.status_code == 204:
            return ProviderResult(
                status="DATA_UNAVAILABLE", provider=PROVIDER_NAME,
                latency_ms=latency_ms,
                canonical_event_id=canonical_event_id,
                canonical_player_id=canonical_player_id,
            )
        if resp.status_code >= 500:
            return ProviderResult(
                status="PROVIDER_ERROR", provider=PROVIDER_NAME,
                latency_ms=latency_ms,
                canonical_event_id=canonical_event_id,
                canonical_player_id=canonical_player_id,
                error_detail=f"HTTP {resp.status_code}",
            )
        body = resp.json()
        actual = _extract_actual(body, market_family)
        if actual is None:
            return ProviderResult(
                status="DATA_UNAVAILABLE", provider=PROVIDER_NAME,
                latency_ms=latency_ms,
                canonical_event_id=canonical_event_id,
                canonical_player_id=canonical_player_id,
            )
        result = ProviderResult(
            status="OK", actual=actual, provider=PROVIDER_NAME,
            provider_version=PROVIDER_VERSION,
            latency_ms=latency_ms,
            canonical_event_id=canonical_event_id,
            canonical_player_id=canonical_player_id,
            fetched_at=datetime.now(timezone.utc).isoformat().replace("+00:00","Z"),
            provenance={"source": PROVIDER_NAME, "market_family": market_family},
        )
        # Cache authoritative OK responses only.
        await cache_put(db, result, sport=sport, market_family=market_family)
        return result
    except Exception as e:
        return ProviderResult(
            status="PROVIDER_ERROR", provider=PROVIDER_NAME,
            latency_ms=int((time.monotonic() - started) * 1000),
            canonical_event_id=canonical_event_id,
            canonical_player_id=canonical_player_id,
            error_detail=type(e).__name__,
        )


def _extract_actual(body: dict, market_family: str) -> Any:
    """Extract the market-family actual from a provider response.

    Placeholder mapping — updated once real provider payload shape
    is verified. Every branch is defensive to never crash.
    """
    if not isinstance(body, dict):
        return None
    if market_family == "soccer_goals":
        return body.get("player_goals")
    if market_family == "soccer_goalscorer":
        return bool(body.get("player_goals", 0) and int(body.get("player_goals") or 0) >= 1)
    if market_family == "soccer_assists":
        return body.get("player_assists")
    if market_family == "soccer_score_or_assist":
        g = int(body.get("player_goals") or 0)
        a = int(body.get("player_assists") or 0)
        return bool(g + a >= 1)
    if market_family == "soccer_player_shots":
        return body.get("player_shots")
    if market_family == "soccer_player_shots_on_target":
        return body.get("player_shots_on_target")
    if market_family == "soccer_team_corners":
        return body.get("team_corners")
    if market_family == "soccer_cards":
        return body.get("cards_total")
    return None


__all__ = [
    "PROVIDER_NAME", "SUPPORTED_MARKETS", "ProviderResult",
    "is_configured", "health_check",
    "cache_get", "cache_put", "get_completed_actual",
]
