"""football-data.org v4 async HTTP client.

Detected the user's key works with football-data.org (NOT api-sports.io).
This module wraps the v4 REST API:

  • Base URL:  https://api.football-data.org/v4
  • Auth:      X-Auth-Token: <SOCCER_API_KEY>
  • Free tier: 10 req/min, free competitions only (Premier League, Bundesliga,
               La Liga, Serie A, Ligue 1, Champions League, World Cup, etc.)
  • Rate-limit headers:
      X-Requests-Available-Minute, X-RequestCounter-Reset

Endpoints we use:
  • /matches                          → today's matches across all comps
  • /competitions/{code}/standings    → league tables
  • /competitions/{code}/scorers      → top scorers
  • /teams/{id}/matches               → historical
  • /matches/{id}                     → single match detail

(Lineups + Injuries aren't exposed by football-data.org — that's an
api-sports.io / Sportmonks feature. We'll skip those for the MVP and
revisit if the user upgrades the provider later.)
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, timedelta
from typing import Any

import httpx
from dotenv import load_dotenv

from .cache import PREGAME_TTL_SECONDS, cache

load_dotenv()
logger = logging.getLogger("lockscore.soccer.client")

BASE_URL    = "https://api.football-data.org/v4"
TIMEOUT_S   = 10
MAX_RETRIES = 3
BACKOFF_CAP = 30.0

_last_rate_limit: dict[str, Any] = {
    "limit": None, "remaining": None, "as_of": None, "reset_seconds": None,
}


class SoccerAPIError(Exception):
    """Raised when football-data.org returns an unrecoverable error or
    the retry budget is exhausted. Caller should degrade gracefully —
    the existing sports_engine soccer flow is the safety net."""


class FootballDataClient:
    def __init__(self) -> None:
        self._key = os.getenv("SOCCER_API_KEY", "").strip()
        if not self._key:
            logger.warning("SOCCER_API_KEY is empty — football-data.org client will fail on first request.")
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=TIMEOUT_S,
            headers={"Accept": "application/json"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, path: str, params: dict[str, Any] | None = None) -> dict:
        if not self._key:
            raise SoccerAPIError("SOCCER_API_KEY env var not set")
        headers = {"X-Auth-Token": self._key}
        backoff = 1.0
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                r = await self._client.get(path, params=params or {}, headers=headers)
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                logger.warning("football-data.org network error on %s (attempt %d): %s", path, attempt, e)
                if attempt == MAX_RETRIES:
                    raise SoccerAPIError(f"network error after {MAX_RETRIES} attempts: {e}")
                await asyncio.sleep(min(backoff, BACKOFF_CAP))
                backoff *= 2
                continue

            # Track quota every response.
            avail = r.headers.get("X-Requests-Available-Minute")
            reset = r.headers.get("X-RequestCounter-Reset")
            if avail is not None or reset is not None:
                _last_rate_limit.update({
                    "limit":         10,  # free-tier per-minute limit
                    "remaining":     _safe_int(avail),
                    "reset_seconds": _safe_int(reset),
                    "as_of":         _now_iso(),
                })

            if r.status_code == 200:
                return r.json()

            if r.status_code == 429:
                ra = r.headers.get("Retry-After") or reset or "30"
                wait = float(ra) if ra.replace(".", "").isdigit() else backoff
                logger.warning("football-data.org 429 on %s — sleeping %.1fs (attempt %d)", path, wait, attempt)
                if attempt == MAX_RETRIES:
                    raise SoccerAPIError(f"rate-limited (429) after {MAX_RETRIES} attempts")
                await asyncio.sleep(min(wait, BACKOFF_CAP))
                backoff *= 2
                continue

            if 500 <= r.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise SoccerAPIError(f"upstream {r.status_code} after {MAX_RETRIES} attempts")
                await asyncio.sleep(min(backoff, BACKOFF_CAP))
                backoff *= 2
                continue

            # 4xx other than 429 — don't retry.
            raise SoccerAPIError(f"football-data.org {r.status_code}: {r.text[:200]}")
        raise SoccerAPIError("exhausted retries")

    async def _cached(self, key: str, path: str, params: dict | None,
                      ttl: int = PREGAME_TTL_SECONDS) -> dict:
        hit = await cache.get(key)
        if hit is not None:
            return hit
        data = await self._request(path, params)
        await cache.set(key, data, ttl)
        return data

    # ---------- endpoint helpers ----------
    async def matches_by_date(self, d: date) -> dict:
        """All matches scheduled for date `d` across the user's plan.

        IMPORTANT: We've observed that football-data.org's bare
        /v4/matches endpoint ignores the dateFrom/dateTo filter on
        TIER_ONE plans (returns 0). The per-competition endpoint
        /v4/competitions/{code}/matches respects it correctly, so we
        iterate active competitions and union the results.

        Active competitions are tried in this priority order so the
        most-likely-to-have-matches league gets queried first if we
        bump into the 10 req/min rate limit mid-run:

          WC  → World Cup (active June/July 2026)
          CL  → Champions League
          EC  → Euros
          PL, BL1, PD, SA, FL1  → big-5 leagues
          BSA, ELC, DED, PPL, CLI → other tier-one + tier-four

        Free tier = 10 req/min. Pipeline caches each competition's
        response for 15 min so successive runs cost ~0.
        """
        ds = d.isoformat()
        cache_key = f"matches:{ds}"
        hit = await cache.get(cache_key)
        if hit is not None:
            return hit
        active_codes = ["WC", "CL", "EL", "ECL", "EC", "PL", "BL1", "PD", "SA", "FL1",
                        "BSA", "ELC", "DED", "PPL", "CLI"]
        combined: list[dict] = []
        errors: list[str] = []
        # Free-tier limit = 10 req/min. Pace at ~4s between calls (= 15
        # calls/min, but the 15-min cache means most runs hit ~0 fresh
        # calls). Earlier 7s pacing made the pipeline take 93s which
        # exceeded mobile HTTP timeouts on the manual refresh endpoint.
        REQ_INTERVAL_SECS = 4
        for i, code in enumerate(active_codes):
            try:
                r = await self._cached(
                    f"comp_matches:{code}:{ds}",
                    f"/competitions/{code}/matches",
                    {"dateFrom": ds, "dateTo": ds},
                )
                for m in (r or {}).get("matches") or []:
                    combined.append(m)
            except SoccerAPIError as e:
                errors.append(f"{code}: {e}")
                logger.warning("matches_by_date(%s): %s failed: %s", ds, code, e)
            # Sleep between requests EXCEPT after the last one.
            if i < len(active_codes) - 1:
                await asyncio.sleep(REQ_INTERVAL_SECS)
        # Shape the response so downstream consumers see the same
        # `{matches: [...]}` envelope as the original endpoint.
        out = {"matches": combined, "errors_per_comp": errors}
        await cache.set(cache_key, out, PREGAME_TTL_SECONDS)
        return out

    async def matches_window(self, days: int = 1) -> dict:
        """Trailing/leading window \u2014 same per-competition approach as
        matches_by_date(). Used by the daily scheduler."""
        today = date.today()
        to = today + timedelta(days=days)
        ds, de = today.isoformat(), to.isoformat()
        cache_key = f"matches_window:{ds}:{de}"
        hit = await cache.get(cache_key)
        if hit is not None:
            return hit
        active_codes = ["WC", "CL", "EL", "ECL", "EC", "PL", "BL1", "PD", "SA", "FL1",
                        "BSA", "ELC", "DED", "PPL", "CLI"]
        combined: list[dict] = []
        for code in active_codes:
            try:
                r = await self._cached(
                    f"comp_matches_w:{code}:{ds}:{de}",
                    f"/competitions/{code}/matches",
                    {"dateFrom": ds, "dateTo": de},
                )
                for m in (r or {}).get("matches") or []:
                    combined.append(m)
            except SoccerAPIError as e:
                logger.warning("matches_window(%s\u2192%s): %s failed: %s",
                               ds, de, code, e)
        out = {"matches": combined}
        await cache.set(cache_key, out, PREGAME_TTL_SECONDS)
        return out

    async def competitions(self) -> dict:
        return await self._cached("competitions", "/competitions", None)

    async def standings(self, competition_code: str) -> dict:
        """`competition_code` is the SHORT code, e.g. 'PL' (Premier League),
        'BL1' (Bundesliga), 'PD' (La Liga), 'SA' (Serie A), 'FL1' (Ligue 1),
        'CL' (Champions League)."""
        return await self._cached(
            f"standings:{competition_code}",
            f"/competitions/{competition_code}/standings", None,
        )

    async def scorers(self, competition_code: str, limit: int = 20) -> dict:
        return await self._cached(
            f"scorers:{competition_code}:{limit}",
            f"/competitions/{competition_code}/scorers",
            {"limit": limit},
        )

    async def team_matches(self, team_id: int, days_back: int = 30) -> dict:
        """Historical matches for a team — used for trend/feature
        engineering once we expand the model."""
        end = date.today()
        start = end - timedelta(days=days_back)
        params = {"dateFrom": start.isoformat(), "dateTo": end.isoformat()}
        return await self._cached(
            f"team_matches:{team_id}:{start}:{end}",
            f"/teams/{team_id}/matches", params,
        )

    async def h2h_matches(self, team_a_id: int, team_b_id: int,
                          limit: int = 10) -> list[dict]:
        """Return the last `limit` head-to-head matches between two teams.

        football-data.org doesn't have a dedicated H2H endpoint on
        TIER_ONE, so we fetch one team's history (longer window) and
        filter to matches that include the other team. Cached for 24h
        because historical results don't change.
        """
        # Use a 2-year window — should comfortably contain `limit` H2Hs
        # for any team pair in active competition.
        end = date.today()
        start = end - timedelta(days=730)
        ck = f"h2h:{team_a_id}:{team_b_id}:{limit}"
        hit = await cache.get(ck)
        if hit is not None:
            return hit
        try:
            r = await self._request(
                f"/teams/{team_a_id}/matches",
                {"dateFrom": start.isoformat(),
                 "dateTo":   end.isoformat(),
                 "status":   "FINISHED",
                 "limit":    100},
            )
        except SoccerAPIError as e:
            logger.warning("H2H fetch %d vs %d failed: %s", team_a_id, team_b_id, e)
            await cache.set(ck, [], 60 * 60)
            return []
        h2hs: list[dict] = []
        for m in (r or {}).get("matches") or []:
            home_id = ((m.get("homeTeam") or {}).get("id"))
            away_id = ((m.get("awayTeam") or {}).get("id"))
            if {home_id, away_id} == {team_a_id, team_b_id}:
                h2hs.append(m)
            if len(h2hs) >= limit:
                break
        # Cache for 24h — historical results are immutable.
        await cache.set(ck, h2hs, 24 * 60 * 60)
        return h2hs

    async def finished_matches_for_date(self, d: date) -> list[dict]:
        """All FINISHED matches across active competitions for date `d`.

        Used by the backfill loop to grade yesterday's soccer
        predictions. Same per-competition iteration as
        `matches_by_date` so it works on the user's TIER_ONE plan.
        Cached for 24h (historical = immutable).
        """
        ds = d.isoformat()
        ck = f"finished_matches:{ds}"
        hit = await cache.get(ck)
        if hit is not None:
            return hit
        active_codes = ["WC", "CL", "EC", "PL", "BL1", "PD", "SA", "FL1",
                        "BSA", "ELC", "DED", "PPL", "CLI"]
        combined: list[dict] = []
        REQ_INTERVAL_SECS = 7
        for i, code in enumerate(active_codes):
            try:
                r = await self._cached(
                    f"comp_finished:{code}:{ds}",
                    f"/competitions/{code}/matches",
                    {"dateFrom": ds, "dateTo": ds, "status": "FINISHED"},
                    ttl=24 * 60 * 60,
                )
                for m in (r or {}).get("matches") or []:
                    combined.append(m)
            except SoccerAPIError as e:
                logger.warning("finished_matches_for_date(%s): %s failed: %s", ds, code, e)
            if i < len(active_codes) - 1:
                await asyncio.sleep(REQ_INTERVAL_SECS)
        await cache.set(ck, combined, 24 * 60 * 60)
        return combined


# ---------- module helpers ----------
def _safe_int(v: Any) -> int | None:
    try: return int(v)
    except (TypeError, ValueError): return None


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def get_quota_snapshot() -> dict:
    return dict(_last_rate_limit)


# Module-singleton client.
client = FootballDataClient()
