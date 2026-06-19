"""MLB Stats API extensions for game-log + player-search lookups.

Reuses the same free statsapi.mlb.com host as `mlb_live.py` so we don't
burn The Odds API credits. All responses cached in a tiny in-memory
TTL dict.

Endpoints wrapped:
  • /people/search?names=…       — player ID lookup by name
  • /people/{id}/stats?stats=gameLog…  — per-game hitting log
  • /teams/{id}/roster           — current 26-man roster for teammate scan

Game-log records are immutable historical data — we cache for 12h.
Player-ID lookups are cached for 24h (rarely change).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger("lockscore.survival.client")

BASE = "https://statsapi.mlb.com/api/v1"
TIMEOUT_S = 10
MAX_RETRIES = 2

# { key: (expires_at_unix, value) }
_cache: dict[str, tuple[float, Any]] = {}
_lock = asyncio.Lock()


async def _safe_get(url: str, params: dict | None = None,
                    ttl_seconds: int = 12 * 3600) -> dict | None:
    """GET with retry + TTL cache. Returns None on failure (caller falls back)."""
    key = url + (f"|{sorted((params or {}).items())}")
    now = time.time()
    async with _lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
    backoff = 0.5
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_S) as cx:
                r = await cx.get(url, params=params or {},
                                 headers={"Accept": "application/json",
                                          "User-Agent": "PerksLocks/Survival/1.0"})
        except Exception as e:
            logger.warning("MLB survival GET %s attempt %d failed: %s", url, attempt, e)
            if attempt == MAX_RETRIES:
                return None
            await asyncio.sleep(backoff); backoff *= 2
            continue
        if r.status_code == 200:
            try:
                data = r.json()
            except Exception:
                return None
            async with _lock:
                _cache[key] = (now + ttl_seconds, data)
            return data
        if r.status_code in (429, 500, 502, 503):
            if attempt == MAX_RETRIES:
                return None
            await asyncio.sleep(backoff); backoff *= 2
            continue
        # 4xx other than 429 — don't retry.
        return None
    return None


async def find_player_id(name: str) -> int | None:
    """Resolve a player display name (e.g. 'Juan Soto') to MLB person ID.
    Returns None if no exact match."""
    if not name or not name.strip():
        return None
    data = await _safe_get(
        f"{BASE}/people/search",
        params={"names": name.strip(), "sportIds": 1},
        ttl_seconds=24 * 3600,
    )
    if not data:
        return None
    people = data.get("people") or []
    # Prefer exact full-name match; fall back to first result.
    name_l = name.strip().lower()
    for p in people:
        full = (p.get("fullName") or "").strip().lower()
        if full == name_l:
            return p.get("id")
    if people:
        return people[0].get("id")
    return None


async def hitting_game_log(person_id: int, season: int) -> list[dict]:
    """Return a per-game hitting log for the player and season, sorted
    most-recent-first. Each entry has at least `gameDate` and stats
    `{hits, atBats, plateAppearances}`.
    """
    data = await _safe_get(
        f"{BASE}/people/{person_id}/stats",
        params={"stats": "gameLog", "group": "hitting", "season": season,
                "sportId": 1},
    )
    if not data:
        return []
    out: list[dict] = []
    for block in data.get("stats") or []:
        for split in block.get("splits") or []:
            stat = split.get("stat") or {}
            try:
                hits = int(stat.get("hits") or 0)
                abs_ = int(stat.get("atBats") or 0)
                pa   = int(stat.get("plateAppearances") or 0)
            except (TypeError, ValueError):
                continue
            game_date = split.get("date")
            if not game_date:
                continue
            out.append({
                "date":  game_date,            # "YYYY-MM-DD"
                "hits":  hits,
                "ab":    abs_,
                "pa":    pa,
                "qualifying": pa >= 1,         # appeared in the game
            })
    # Sort newest first.
    out.sort(key=lambda r: r["date"], reverse=True)
    return out


async def team_roster(team_id: int) -> list[dict]:
    """Return the active 26-man roster for `team_id`. Used to scope
    candidate teammates."""
    data = await _safe_get(
        f"{BASE}/teams/{team_id}/roster",
        params={"rosterType": "active"},
        ttl_seconds=12 * 3600,
    )
    if not data:
        return []
    out: list[dict] = []
    for r in data.get("roster") or []:
        person = r.get("person") or {}
        pos = r.get("position") or {}
        pos_code = (pos.get("abbreviation") or "").upper()
        # Hitters only — skip pitchers (P) from teammate coverage scan.
        if pos_code == "P":
            continue
        pid = person.get("id")
        name = person.get("fullName")
        if pid and name:
            out.append({"id": pid, "name": name, "position": pos_code})
    return out
