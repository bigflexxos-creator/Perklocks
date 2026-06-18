"""MLB Live Data Layer — pulls game schedules + final scores from the
free, official MLB Stats API (statsapi.mlb.com).

Purpose: replace The Odds API as the score source for MLB settlement
so we stop burning Odds API credits on score polls. MLB Stats API is:
  • Free, unlimited, no API key
  • Official MLB.com / Statcast data
  • Updates every few seconds during live games
  • Includes pre-game / live / final status + linescore

The returned payloads are shape-compatible with what the existing
settlement_engine expects from `_fetch_scores()`:
    {
        "id":            "<gamePk>",          # MLB's unique game ID
        "home_team":     "Baltimore Orioles",
        "away_team":     "Seattle Mariners",
        "commence_time": "2026-06-18T23:05:00Z",
        "completed":     True,                # only True for Final status
        "scores": [
            {"name": "Baltimore Orioles", "score": 4},
            {"name": "Seattle Mariners",  "score": 3},
        ],
    }

This keeps `settle_pick()` / `_match_score_for_pick()` 100% reusable
without touching their logic.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

import httpx

logger = logging.getLogger("lockscore.mlb_live")

MLB_BASE = "https://statsapi.mlb.com/api/v1"
CACHE_TTL_SECONDS = 15  # matches user's spec
HTTP_TIMEOUT_SECONDS = 8

# Minimal in-memory cache: { url: (expires_at_unix, parsed_json) }
_cache: dict[str, tuple[float, Any]] = {}


async def _safe_get(url: str, params: Optional[dict] = None) -> Optional[dict]:
    """GET with 15-second in-memory cache. Returns parsed JSON or None.

    All errors swallowed → caller falls back to Odds API. We never raise
    because MLB settlement is a best-effort optimisation; the old Odds
    API path remains the safety net.
    """
    # Stable cache key includes params so e.g. /schedule?date=X is cached
    # per-date.
    key = url + (f"|{sorted(params.items())}" if params else "")
    now = time.time()
    cached = _cache.get(key)
    if cached and cached[0] > now:
        return cached[1]
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as cx:
            r = await cx.get(url, params=params or {},
                             headers={"Accept": "application/json",
                                      "User-Agent": "PerksLocks/1.0"})
            if r.status_code != 200:
                logger.warning("MLB Stats API non-200 %s for %s", r.status_code, url)
                return None
            data = r.json()
    except Exception as e:
        logger.warning("MLB Stats API error %s: %s", url, e)
        return None
    _cache[key] = (now + CACHE_TTL_SECONDS, data)
    return data


def _fmt_iso_z(dt: datetime) -> str:
    """Render a UTC datetime as the same ISO-Z string the Odds API uses."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _convert_game(game: dict) -> Optional[dict]:
    """Convert one MLB Stats API `dates[].games[]` entry to the Odds-API
    score shape that settlement_engine expects.

    Returns None for games that aren't useful for settlement (postponed,
    cancelled, etc. where there's no resolvable score).
    """
    status = (game.get("status") or {})
    detailed = status.get("detailedState") or ""
    abstract = status.get("abstractGameState") or ""
    teams = game.get("teams") or {}
    home = teams.get("home") or {}
    away = teams.get("away") or {}
    home_team_name = (home.get("team") or {}).get("name")
    away_team_name = (away.get("team") or {}).get("name")
    if not home_team_name or not away_team_name:
        return None

    game_pk = game.get("gamePk")
    game_date = game.get("gameDate")  # ISO string
    commence = ""
    if game_date:
        try:
            # MLB returns e.g. "2026-06-18T23:05:00Z" already — normalise just
            # in case it's "+00:00".
            dt = datetime.fromisoformat(game_date.replace("Z", "+00:00"))
            commence = _fmt_iso_z(dt)
        except Exception:
            commence = game_date

    # A game is "completed" for settlement purposes only when MLB marks
    # it Final or Game Over. We deliberately exclude Postponed / Suspended
    # / Cancelled — those have no final score and would mis-grade picks.
    completed_states = {"Final", "Game Over", "Completed Early"}
    completed = detailed in completed_states or abstract == "Final"

    home_score_val = home.get("score")
    away_score_val = away.get("score")

    scores_payload: list[dict] = []
    # Only emit scores when both sides have a numeric score (covers
    # in-progress games too, useful later for "live status" badges).
    if isinstance(home_score_val, (int, float)) and isinstance(away_score_val, (int, float)):
        scores_payload = [
            {"name": home_team_name, "score": int(home_score_val)},
            {"name": away_team_name, "score": int(away_score_val)},
        ]

    return {
        # gamePk uniquely identifies a game (including each leg of a
        # doubleheader). Prefix with mlb_ so it can't collide with an
        # Odds API event_id in any future hybrid match logic.
        "id": f"mlb_{game_pk}" if game_pk else None,
        "home_team": home_team_name,
        "away_team": away_team_name,
        "commence_time": commence,
        "completed": completed,
        "status": detailed,           # bonus field — useful for live badges
        "abstract_status": abstract,  # "Preview" | "Live" | "Final"
        "scores": scores_payload,
    }


async def fetch_mlb_scores(days_back: int = 3) -> list[dict]:
    """Fetch all MLB games over the trailing `days_back` days.

    Mirrors the Odds API `/sports/{sport}/scores?daysFrom=N` contract so
    the existing `_match_score_for_pick()` function can consume the output
    unchanged. Single HTTP call thanks to MLB Stats API's date-range
    `schedule` endpoint.

    Returns a list of game dicts (Odds-API shaped). On any failure
    returns [] — settlement falls back to Odds API automatically.
    """
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=max(0, days_back))
    end = today + timedelta(days=1)  # include today's late games
    url = f"{MLB_BASE}/schedule"
    params = {
        "sportId": 1,                # 1 == MLB
        "startDate": start.isoformat(),
        "endDate":   end.isoformat(),
        "hydrate":   "team,linescore",
    }
    data = await _safe_get(url, params=params)
    if not data:
        return []
    out: list[dict] = []
    for day in (data.get("dates") or []):
        for game in (day.get("games") or []):
            conv = _convert_game(game)
            if conv:
                out.append(conv)
    if out:
        completed_count = sum(1 for g in out if g.get("completed"))
        logger.info(
            "MLB Stats API: fetched %d games (%d Final) over %d days — 0 Odds credits",
            len(out), completed_count, days_back,
        )
    return out


async def fetch_today_live_mlb() -> list[dict]:
    """Fetch only TODAY's MLB games with current linescore + status.

    Used for live in-game features (live score badges, "Pick Status"
    indicators) — not for settlement. Cached for 15 s so a tab full of
    MLB cards only triggers one HTTP call per cache window.
    """
    url = f"{MLB_BASE}/schedule"
    params = {
        "sportId": 1,
        "hydrate": "team,linescore,liveLookin",
    }
    data = await _safe_get(url, params=params)
    if not data:
        return []
    out: list[dict] = []
    for day in (data.get("dates") or []):
        for game in (day.get("games") or []):
            conv = _convert_game(game)
            if conv:
                out.append(conv)
    return out
