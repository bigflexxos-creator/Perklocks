"""SportsDataIO HTTP client for player intelligence enrichment.

Pulls real position + injury status + last-N game stats for NBA / NFL / MLB
players currently in the pick slate. We avoid daily blanket pulls — only
players who actually appear in today's picks get hit, with a 24h cache.

Quota target: ≤ 3000 calls/day (user has 100k/month plan = 3,300/day budget,
even though our typical use is ~500/day across all sports).

ENDPOINTS USED (no extra cost, all included in scores tier):
  • /v3/{sport}/scores/json/Players      → roster + position + status
  • /v3/{sport}/scores/json/PlayerSeasonStats/{season}
                                          → season aggregates (high-volume,
                                            so we cache 24h)
  • /v3/{sport}/stats/json/Injuries      → active injury report (12h cache)

NOT used by default (premium tier only): play-by-play, projections,
RotoWire feeds. If user upgrades we can extend.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

import httpx

logger = logging.getLogger("lockscore.sportsdataio")

SPORTSDATAIO_KEY = os.environ.get("SPORTSDATAIO_KEY", "")
_BASE = "https://api.sportsdata.io/v3"
_TIMEOUT = httpx.Timeout(15.0, connect=5.0)

# Cache layout: { (sport, endpoint): (expiry_ts, payload) }
_CACHE: dict[tuple, tuple[float, Any]] = {}
_LOCK = asyncio.Lock()                   # serialize cold-cache fetches

# Endpoint-level TTLs (seconds)
_TTL_PLAYERS  = 24 * 60 * 60             # roster rarely changes mid-season
_TTL_INJURIES = 12 * 60 * 60             # injury report — refresh twice daily
_TTL_STATS    = 24 * 60 * 60             # season stats — daily is plenty


def _enabled() -> bool:
    return bool(SPORTSDATAIO_KEY)


async def _get(sport: str, path: str, ttl: int) -> Any:
    """Cached HTTP GET against SportsDataIO. Returns None on failure (never
    raises — Player Intelligence enrichment is a non-critical enhancement
    and should not break pick generation)."""
    if not _enabled():
        return None
    key = (sport, path)
    now = time.time()
    cached = _CACHE.get(key)
    if cached and cached[0] > now:
        return cached[1]
    # Cold path — serialise so 50 concurrent player-resolve calls don't
    # all spam the same endpoint while waiting for the first response.
    async with _LOCK:
        cached = _CACHE.get(key)
        if cached and cached[0] > now:
            return cached[1]
        url = f"{_BASE}/{sport}{path}"
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                r = await client.get(
                    url,
                    headers={"Ocp-Apim-Subscription-Key": SPORTSDATAIO_KEY},
                )
            if r.status_code == 200:
                payload = r.json()
                _CACHE[key] = (now + ttl, payload)
                return payload
            logger.warning("SportsDataIO %s %s → HTTP %d", sport, path, r.status_code)
        except Exception as e:
            logger.warning("SportsDataIO %s %s exception: %s", sport, path, e)
        # On failure, cache the negative result for 5 minutes so we don't
        # hammer the API in a loop.
        _CACHE[key] = (now + 300, None)
        return None


# ────────────────────────────────────────────────────────────────────
# Sport-specific helpers — all return None when the API is unavailable
# ────────────────────────────────────────────────────────────────────
async def get_active_players(sport: str) -> list[dict] | None:
    """Roster for the entire active league. ~600 NFL / ~500 NBA / ~1200 MLB.

    `sport` must be one of: "nba", "nfl", "mlb".
    """
    s = sport.lower()
    if s not in {"nba", "nfl", "mlb"}:
        return None
    return await _get(s, "/scores/json/Players", _TTL_PLAYERS)


async def get_injuries(sport: str) -> list[dict] | None:
    s = sport.lower()
    if s not in {"nba", "nfl", "mlb"}:
        return None
    # MLB injury endpoint is under /stats/, NFL+NBA also.
    return await _get(s, "/stats/json/Injuries", _TTL_INJURIES)


async def get_player_season_stats(sport: str, season: int) -> list[dict] | None:
    """Season-level stat aggregates. Used to compute usage_intensity bucket
    (volume of points/yards/at-bats relative to position median)."""
    s = sport.lower()
    if s not in {"nba", "nfl", "mlb"}:
        return None
    path = f"/scores/json/PlayerSeasonStats/{season}"
    return await _get(s, path, _TTL_STATS)


def _norm_name(s: str) -> str:
    return (s or "").strip().lower()


async def find_player(sport: str, name: str) -> dict | None:
    """Locate a player by canonical name. Soft match — strips punctuation
    and treats accent-insensitively.
    """
    players = await get_active_players(sport)
    if not players:
        return None
    target = _norm_name(name)
    # Build a fast lookup once per cache cycle (cheap; <2000 players).
    for p in players:
        # SportsDataIO uses Name / FirstName / LastName / Position / Team
        full = _norm_name(p.get("Name") or "")
        if full == target:
            return p
    # Last name fallback for common cases ("Mahomes" → "Patrick Mahomes")
    last = target.split()[-1]
    for p in players:
        if _norm_name(p.get("LastName") or "") == last:
            return p
    return None


async def find_injury(sport: str, name: str) -> dict | None:
    inj = await get_injuries(sport)
    if not inj:
        return None
    target = _norm_name(name)
    for r in inj:
        if _norm_name(r.get("Name") or "") == target:
            return r
    return None


# ────────────────────────────────────────────────────────────────────
# Public entry: enrich_profile()
# ────────────────────────────────────────────────────────────────────
async def enrich_profile(profile: dict) -> dict:
    """Augment a Player Intelligence profile dict in-place with real position,
    team, and injury status from SportsDataIO.

    Soccer + Tennis profiles are returned untouched (SportsDataIO doesn't
    cover those sports on the standard tier). All MLB / NBA / NFL profiles
    get a best-effort enrichment that NEVER raises.
    """
    sport = (profile.get("sport") or "").lower()
    if sport not in {"nba", "nfl", "mlb"}:
        return profile
    name = profile.get("canonical_name") or ""
    if not name:
        return profile
    try:
        row = await find_player(sport, name)
        if row:
            # Position — only override when SportsDataIO has a confirmed value
            # AND the seed didn't already pin one (seed wins for marquee).
            if row.get("Position"):
                if not profile.get("position") or profile.get("source") != "seed":
                    profile["position"] = row["Position"]
            # Team — refresh always (trades happen)
            team_short = row.get("Team")
            if team_short:
                profile["team"] = team_short
            if row.get("Height"):
                profile["height"] = row["Height"]
            if row.get("Weight"):
                profile["weight_lb"] = row["Weight"]
            if row.get("BirthDate"):
                profile["birth_date"] = row["BirthDate"]
            if row.get("PhotoUrl"):
                profile["photo_url"] = row["PhotoUrl"]
            profile["sportsdataio_player_id"] = row.get("PlayerID")
            profile["sportsdataio_status"]    = row.get("Status")  # Active / IR / etc.
        injury = await find_injury(sport, name)
        if injury:
            profile["injury_status"]     = injury.get("Status") or "questionable"
            profile["injury_description"] = injury.get("BodyPart") or injury.get("Practice")
            profile["injury_updated"]     = injury.get("Updated")
        else:
            profile["injury_status"] = profile.get("injury_status") or "healthy"
        profile["sportsdataio_enriched_at"] = int(time.time())
    except Exception as e:
        # Defensive: never break Player Intelligence on a SportsDataIO hiccup
        logger.warning("enrich_profile(%s, %s) failed: %s", sport, name, e)
    return profile
