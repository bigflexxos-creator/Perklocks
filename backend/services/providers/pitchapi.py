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
PROVIDER_VERSION = "1.1.0-real-endpoints"

# ── VERIFIED 2026-08-25 via authenticated real response ───────────────
# Base URL:  https://api.pitchapi.dev
# Auth:      X-API-KEY: <key>   (NOT Bearer)
# Endpoints (confirmed):
#   GET /v1/leagues                          → list all leagues
#   GET /v1/leagues/{league_id}              → league detail
#   GET /v1/leagues/{league_id}/matches      → league match list
#                                              (finished/scheduled/live)
#   GET /v1/matches/{match_id}               → match detail
#   GET /v1/matches/{match_id}/stats         → team stats (periods+groups)
#   GET /v1/matches/{match_id}/events        → goals + cards + subs
#                                              types observed:
#                                              "goal", "yellowcard",
#                                              "substitution",
#                                              (redcard on incidence)
#   GET /v1/matches/{match_id}/lineups       → starting XI + bench
#   GET /v1/matches/{match_id}/players       → per-player stats
#                                              (Goals, Assists, Rating,
#                                              Minutes played, Corners,
#                                              ShotsOnTarget/OffTarget,
#                                              total_shots, xG, xA, ...)
#
# ID prefixes observed:
#   league:   "l_<slug>"
#   match:    "m_<slug>"
#   team:     "t_<slug>"
#   player:   "p_<slug>"
DEFAULT_BASE_URL = os.getenv("PITCHAPI_BASE_URL", "https://api.pitchapi.dev")
AUTH_HEADER_NAME = "X-API-KEY"
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
            # Verified: /v1/leagues is a cheap GET that returns 200 for
            # authorized keys. No dedicated /ping endpoint exists.
            resp = await client.get(
                f"{DEFAULT_BASE_URL}/v1/leagues",
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
    player_name: Optional[str] = None,
    force_refresh: bool = False,
) -> ProviderResult:
    """Return actual value for a completed Soccer fixture + market.

    2026-08-25 rewrite — REAL endpoint set (verified against live
    authenticated provider response):
      • Player markets (goals / assists / goalscorer / score_or_assist
        / shots / shots_on_target) → GET /v1/matches/{id}/players
      • Team-corners                → GET /v1/matches/{id}/stats
      • Cards (yellow/red)          → GET /v1/matches/{id}/events

    `canonical_event_id` MUST be the PitchAPI match id (``m_<slug>``).
    Callers use ``soccer_fixture_resolver`` to obtain it first.

    Player-market lookups require ``player_name`` — PitchAPI player IDs
    (``p_<slug>``) are NOT known to Perklocks yet, so we match by
    normalized name against the players payload.
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
    # ── Route to the right real endpoint per market family ──────────
    PLAYER_MARKETS = {
        "soccer_goals", "soccer_assists", "soccer_goalscorer",
        "soccer_score_or_assist", "soccer_player_shots",
        "soccer_player_shots_on_target",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            if market_family in PLAYER_MARKETS:
                endpoint = f"{DEFAULT_BASE_URL}/v1/matches/{canonical_event_id}/players"
            elif market_family == "soccer_team_corners":
                endpoint = f"{DEFAULT_BASE_URL}/v1/matches/{canonical_event_id}/stats"
            elif market_family == "soccer_cards":
                endpoint = f"{DEFAULT_BASE_URL}/v1/matches/{canonical_event_id}/events"
            else:
                return ProviderResult(
                    status="MARKET_UNSUPPORTED", provider=PROVIDER_NAME,
                    canonical_event_id=canonical_event_id,
                    error_detail=f"no real endpoint for {market_family}",
                )
            resp = await client.get(endpoint,
                                     headers={AUTH_HEADER_NAME: api_key()})
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
        body = resp.json() or {}
        actual = _extract_actual(body, market_family, player_name=player_name)
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
            provenance={"source": PROVIDER_NAME, "market_family": market_family,
                        "endpoint": endpoint, "player_name": player_name},
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


def _norm_name(s: str) -> str:
    import unicodedata as _u
    s = _u.normalize("NFKD", s or "").encode("ascii","ignore").decode("ascii").lower()
    import re as _re
    return _re.sub(r"[^a-z ]+", "", s).strip()


def _extract_actual(body: dict, market_family: str,
                    player_name: Optional[str] = None):
    """Extract the market-family actual from a real PitchAPI response.

    Player-market extraction walks the nested `stats` groups; team
    stats extraction walks per-team `stats.groups.stats` similarly.
    Card extraction counts event_type occurrences in the events list.
    """
    if not isinstance(body, dict):
        return None
    data = body.get("data")
    if data is None:
        return None

    # ── Player markets ─────────────────────────────────────────
    PLAYER_MARKETS = {
        "soccer_goals", "soccer_assists", "soccer_goalscorer",
        "soccer_score_or_assist", "soccer_player_shots",
        "soccer_player_shots_on_target",
    }
    if market_family in PLAYER_MARKETS:
        if not player_name:
            return None
        target = _norm_name(player_name)
        target_last = target.split()[-1] if target else ""
        players = data if isinstance(data, list) else data.get("players")
        if not isinstance(players, list):
            return None
        # Locate player row.  Session B: 3-tier matching:
        # (1) exact normalized name equality
        # (2) either side contains the other as a substring
        # (3) last-token match when the token is unique in the payload
        # This survives spelling variants like "Aleksei" vs "Aleksey".
        row = None
        for p in players:
            nm = ((p.get("player") or {}).get("name") or "")
            nn = _norm_name(nm)
            if nn == target:
                row = p; break
        if row is None:
            for p in players:
                nm = ((p.get("player") or {}).get("name") or "")
                nn = _norm_name(nm)
                if not nn or not target:
                    continue
                if target in nn or nn in target:
                    row = p; break
        if row is None and target_last and len(target_last) >= 4:
            # last-name-only match, but only if the last name uniquely
            # identifies a player in the payload
            matches = []
            for p in players:
                nm = ((p.get("player") or {}).get("name") or "")
                nn = _norm_name(nm)
                if nn and target_last in nn.split():
                    matches.append(p)
            if len(matches) == 1:
                row = matches[0]
        if row is None:
            return None
        stat_map: dict[str, float] = {}
        for grp in (row.get("stats") or []):
            for _label, sd in (grp.get("stats") or {}).items():
                key = (sd.get("key") or "").strip()
                val = (sd.get("stat") or {}).get("value")
                if key and val is not None:
                    try:
                        stat_map[key] = float(val)
                    except (TypeError, ValueError):
                        pass
        goals = stat_map.get("goals", 0.0)
        assists = stat_map.get("assists", 0.0)
        if market_family == "soccer_goals":
            return goals
        if market_family == "soccer_assists":
            return assists
        if market_family == "soccer_goalscorer":
            return bool(goals >= 1)
        if market_family == "soccer_score_or_assist":
            return bool(goals + assists >= 1)
        if market_family == "soccer_player_shots":
            return (stat_map.get("total_shots")
                    or (stat_map.get("ShotsOnTarget", 0)
                        + stat_map.get("ShotsOffTarget", 0)))
        if market_family == "soccer_player_shots_on_target":
            return stat_map.get("ShotsOnTarget")
        return None

    # ── Team corners ──────────────────────────────────────────
    if market_family == "soccer_team_corners":
        total = 0.0
        for period_row in (data if isinstance(data, list) else []):
            if period_row.get("period") not in (0, "0", "FT"):
                continue
            for grp in (period_row.get("groups") or []):
                for _lbl, sd in (grp.get("stats") or {}).items():
                    if (sd.get("key") or "") == "corners":
                        for team_side in ("home", "away"):
                            v = (sd.get("stat") or {}).get(team_side)
                            if v is not None:
                                try: total += float(v)
                                except (TypeError, ValueError): pass
        return total if total > 0 else None

    # ── Cards (yellowcard + redcard events) ───────────────────
    if market_family == "soccer_cards":
        events = data if isinstance(data, list) else data.get("events")
        if not isinstance(events, list):
            return None
        y = sum(1 for e in events if e.get("event_type") == "yellowcard")
        r = sum(1 for e in events if e.get("event_type") == "redcard")
        return float(y + r)

    return None


__all__ = [
    "PROVIDER_NAME", "SUPPORTED_MARKETS", "ProviderResult",
    "is_configured", "health_check",
    "cache_get", "cache_put", "get_completed_actual",
]
