"""Big Balls FALLBACK completed-match provider (2026-08-25).

Wiring status: SCAFFOLD ONLY — settlement pipeline is NOT calling this
yet. Envelope + cache infrastructure is here so we can flip on
settlement wiring per market/sport (P3) with `wire=True` on the
specific markets whose real provider response has been verified.

Design rules (per P3 hard-freeze contract):
  • Big Balls is the FALLBACK when PitchAPI lacks the required
    fixture/stat for Soccer.
  • Big Balls MAY additionally fill genuine missing actuals for
    MLB/NBA/NFL/NHL/CFB, but ONLY as a fallback behind an existing
    authoritative source. Its output must never overwrite an
    authoritative primary actual.
  • Neither provider is authoritative for pre-match probability;
    they exist purely to settle already-published Perklocks picks
    against real completed-game actuals.
  • Neither provider averages conflicting values.  If Big Balls
    disagrees with PitchAPI, the pick is DATA_CONFLICT and does
    NOT settle.

The API surface intentionally mirrors ``pitchapi.py`` (same
``ProviderResult`` shape, same cache semantics) so downstream
settlement can consume both through a single flow.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from services.providers.pitchapi import ProviderResult, SUPPORTED_MARKETS as SOCCER_MARKETS

PROVIDER_NAME = "bigballs"
PROVIDER_VERSION = "1.1.0-real-endpoints"

# ── VERIFIED 2026-08-25 via authenticated real response ───────────────
# Base URL:  https://api.bigballsdata.com
# Auth:      x-api-key: <key>
# Docs:      https://bigballsdata.com/docs
# OpenAPI:   https://api.bigballsdata.com/openapi.json  (115 endpoints)
#
# Endpoints of interest for settlement (verified reachable):
#   GET /v1/matches                          → match list (soccer)
#   GET /v1/matches/{id}                     → match detail
#   GET /v1/matches/{id}/statistics          → match stats
#   GET /v1/matches/{id}/events              → goals, cards, subs
#   GET /v1/nba/games                        → NBA cross-sport fallback
#   GET /v1/nfl/games                        → NFL cross-sport fallback
#   GET /v1/nhl/games/{id}/matchup           → NHL cross-sport fallback
#   GET /v1/players/{id}/game-log            → player game log
#   GET /v1/players/{id}/stats               → season stats
#   GET /v1/live-stats/{sport}/{matchId}/players → live player stats
#   GET /v1/leagues?sport=football           → soccer league list
#
# NOTE: `/v1/fixtures` does NOT exist here (route_not_found);
# `/v1/matches` is the canonical entry.  Documented via provider's
# suggested_fix response.
DEFAULT_BASE_URL = os.getenv("BIGBALLS_BASE_URL", "https://api.bigballsdata.com")
AUTH_HEADER_NAME = "x-api-key"
API_KEY_ENV = "BIGBALLS_API_KEY"

# Cross-sport supported market families. Populated incrementally as
# each family is proven against a real provider response.  Until then
# each family stays SETTLEMENT_UNSUPPORTED at the settlement gate.
SUPPORTED_MARKETS_CROSS_SPORT = frozenset(SOCCER_MARKETS | {
    # MLB
    "mlb_hits", "mlb_home_runs", "mlb_strikeouts", "mlb_total_bases",
    "mlb_outs_recorded", "mlb_earned_runs",
    # NFL
    "nfl_passing_yards", "nfl_rushing_yards", "nfl_receiving_yards",
    "nfl_receptions", "nfl_anytime_td",
    # NBA
    "nba_points", "nba_rebounds", "nba_assists", "nba_threes", "nba_pra",
    # NHL
    "nhl_goals", "nhl_assists", "nhl_points", "nhl_shots_on_goal",
    # CFB
    "cfb_passing_yards", "cfb_rushing_yards", "cfb_receiving_yards",
})


def api_key() -> Optional[str]:
    return os.getenv(API_KEY_ENV)


def is_configured() -> bool:
    return bool(api_key())


async def health_check(timeout: float = 5.0) -> dict:
    if not is_configured():
        return {
            "provider": PROVIDER_NAME, "configured": False,
            "status": "API_KEY_MISSING",
        }
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            # Verified: /v1/health is a cheap 200-return endpoint.
            resp = await client.get(
                f"{DEFAULT_BASE_URL}/v1/health",
                headers={AUTH_HEADER_NAME: api_key()},
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


async def cache_get(
    db, *, sport: str, canonical_event_id: str,
    market_family: str, canonical_player_id: Optional[str] = None,
) -> Optional[ProviderResult]:
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
    """Return actual value for a completed fixture + market.

    Semantically identical to `pitchapi.get_completed_actual` but
    covers the cross-sport whitelist. SCAFFOLD-ONLY: settlement
    wiring must opt-in per market.
    """
    if market_family not in SUPPORTED_MARKETS_CROSS_SPORT:
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
                f"{DEFAULT_BASE_URL}/v1/games/{canonical_event_id}/stats",
                headers={"Authorization": f"Bearer {api_key()}"},
                params={"market": market_family, "sport": (sport or "").lower(),
                        **({"player_id": canonical_player_id}
                            if canonical_player_id else {})},
            )
        latency_ms = int((time.monotonic() - started) * 1000)
        if resp.status_code in (401, 403):
            return ProviderResult(
                status="AUTH_FAIL", provider=PROVIDER_NAME,
                latency_ms=latency_ms,
                canonical_event_id=canonical_event_id,
                canonical_player_id=canonical_player_id,
                error_detail=f"HTTP {resp.status_code}",
            )
        if resp.status_code in (204, 404):
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
    """Placeholder mapping. Populated per family after real payload
    inspection.  Defensive on every branch.
    """
    if not isinstance(body, dict):
        return None
    # Soccer families — reuse PitchAPI mapping.
    if market_family == "soccer_goals":
        return body.get("player_goals")
    if market_family == "soccer_goalscorer":
        try:
            return bool(int(body.get("player_goals") or 0) >= 1)
        except (TypeError, ValueError):
            return None
    if market_family == "soccer_assists":
        return body.get("player_assists")
    if market_family == "soccer_score_or_assist":
        try:
            g = int(body.get("player_goals") or 0)
            a = int(body.get("player_assists") or 0)
            return bool(g + a >= 1)
        except (TypeError, ValueError):
            return None
    if market_family == "soccer_player_shots":
        return body.get("player_shots")
    if market_family == "soccer_player_shots_on_target":
        return body.get("player_shots_on_target")
    if market_family == "soccer_team_corners":
        return body.get("team_corners")
    if market_family == "soccer_cards":
        return body.get("cards_total")
    # MLB / NFL / NBA / NHL / CFB extraction is left as
    # per-family scaffolding — the settlement pipeline calls this
    # via a wrap that expects a `None` = DATA_UNAVAILABLE contract.
    key_map = {
        "mlb_hits": "hits", "mlb_home_runs": "home_runs",
        "mlb_strikeouts": "strikeouts", "mlb_total_bases": "total_bases",
        "mlb_outs_recorded": "outs_recorded", "mlb_earned_runs": "earned_runs",
        "nfl_passing_yards": "passing_yards",
        "nfl_rushing_yards": "rushing_yards",
        "nfl_receiving_yards": "receiving_yards",
        "nfl_receptions": "receptions",
        "nfl_anytime_td": "any_touchdown",
        "nba_points": "points", "nba_rebounds": "rebounds",
        "nba_assists": "assists", "nba_threes": "three_pointers",
        "nba_pra": "points_rebounds_assists",
        "nhl_goals": "goals", "nhl_assists": "assists",
        "nhl_points": "points", "nhl_shots_on_goal": "shots_on_goal",
        "cfb_passing_yards": "passing_yards",
        "cfb_rushing_yards": "rushing_yards",
        "cfb_receiving_yards": "receiving_yards",
    }
    return body.get(key_map.get(market_family, ""))


__all__ = [
    "PROVIDER_NAME", "SUPPORTED_MARKETS_CROSS_SPORT", "ProviderResult",
    "is_configured", "health_check",
    "cache_get", "cache_put", "get_completed_actual",
]
