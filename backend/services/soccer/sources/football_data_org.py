"""Football-Data.org source — free tier with API key.

Coverage: 13 competitions on the free tier (Bundesliga, Champions League,
Championship, Copa Libertadores, EURO, EPL, Ligue 1, MLS, Primeira Liga,
Serie A, La Liga, WC, Nations League). Provides fixtures, results,
standings, teams. No odds.

Free tier limits: 10 requests / minute. We add a 6-second sleep between
requests to stay comfortably under.

Auth: `X-Auth-Token: <key>` header. Key lives in env FOOTBALL_DATA_ORG_KEY.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

import httpx

from services.soccer.models import (
    SoccerFixture, SoccerMatch, SoccerStanding, SoccerTeam,
)

logger = logging.getLogger("lockscore.services.soccer.football_data_org")

_BASE = "https://api.football-data.org/v4"
_HTTP_TIMEOUT = 20.0
_RATE_LIMIT_DELAY = 6.5  # seconds — stay under 10 req/min free tier

# Canonical league code → football-data.org competition code
_LEAGUE_CODE_MAP: dict[str, str] = {
    "EPL":         "PL",
    "ELC":         "ELC",
    "LaLiga":      "PD",
    "Bundesliga":  "BL1",
    "SerieA":      "SA",
    "Ligue1":      "FL1",
    "Eredivisie":  "DED",
    "Primeira":    "PPL",
    "Brasileirao": "BSA",
    "MLS":         "MLS",   # sometimes tier-restricted
    "UCL":         "CL",
    "UEL":         "EL",
}


def _headers() -> dict:
    key = os.getenv("FOOTBALL_DATA_ORG_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "FOOTBALL_DATA_ORG_KEY not set in env — Football-Data.org "
            "free tier requires a key (register at football-data.org).")
    return {"X-Auth-Token": key}


async def _get(path: str, params: Optional[dict] = None,
               respect_rate_limit: bool = True) -> dict:
    """Hit the API; sleep 6.5s after each request to stay under the 10/min quota."""
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, headers=_headers()) as cx:
        r = await cx.get(f"{_BASE}/{path.lstrip('/')}", params=params or {})
    if respect_rate_limit:
        await asyncio.sleep(_RATE_LIMIT_DELAY)
    if r.status_code == 429:
        logger.warning("football_data_org 429 rate limited — retrying after 30s")
        await asyncio.sleep(30)
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, headers=_headers()) as cx:
            r = await cx.get(f"{_BASE}/{path.lstrip('/')}", params=params or {})
    r.raise_for_status()
    return r.json()


def _season_str(current_season: dict) -> str:
    """Football-Data.org exposes `startDate: 2024-08-16, endDate: 2025-05-25`
    Return 'YYYY-YY' style like '2024-25'."""
    start = (current_season.get("startDate") or "")[:4]
    end   = (current_season.get("endDate") or "")[:4]
    if start and end:
        return f"{start}-{end[-2:]}"
    return start or "current"


# ── Public API ───────────────────────────────────────────────────────
async def fetch_standings(league: str) -> list[dict]:
    code = _LEAGUE_CODE_MAP.get(league)
    if not code:
        return []
    try:
        data = await _get(f"competitions/{code}/standings")
    except Exception as e:
        logger.debug("football_data_org standings %s failed: %s", league, e)
        return []
    season = _season_str(data.get("season") or {})
    out: list[dict] = []
    for standing_block in data.get("standings", []):
        if (standing_block.get("type") or "").upper() != "TOTAL":
            continue
        for row in standing_block.get("table", []):
            team = (row.get("team") or {}).get("name", "")
            if not team:
                continue
            out.append(SoccerStanding(
                league=league,
                season=season,
                team=team,
                position=int(row.get("position", 0)),
                played=int(row.get("playedGames", 0)),
                won=int(row.get("won", 0)),
                drawn=int(row.get("draw", 0)),
                lost=int(row.get("lost", 0)),
                goals_for=int(row.get("goalsFor", 0)),
                goals_against=int(row.get("goalsAgainst", 0)),
                goal_diff=int(row.get("goalDifference", 0)),
                points=int(row.get("points", 0)),
                form=row.get("form"),
                source="football_data_org",
            ).to_dict())
    return out


async def fetch_teams(league: str) -> list[dict]:
    code = _LEAGUE_CODE_MAP.get(league)
    if not code:
        return []
    try:
        data = await _get(f"competitions/{code}/teams")
    except Exception as e:
        logger.debug("football_data_org teams %s failed: %s", league, e)
        return []
    out: list[dict] = []
    for t in data.get("teams", []):
        out.append(SoccerTeam(
            name=t.get("name") or "",
            league=league,
            country=(t.get("area") or {}).get("name"),
            stadium=t.get("venue"),
            founded=t.get("founded"),
            website=t.get("website"),
            football_data_id=t.get("id"),
            source="football_data_org",
        ).to_dict())
    return out


async def fetch_fixtures(league: str, date_from: str, date_to: str) -> list[dict]:
    code = _LEAGUE_CODE_MAP.get(league)
    if not code:
        return []
    try:
        data = await _get(f"competitions/{code}/matches",
                          params={"dateFrom": date_from, "dateTo": date_to})
    except Exception as e:
        logger.debug("football_data_org fixtures %s %s..%s failed: %s",
                     league, date_from, date_to, e)
        return []
    out: list[dict] = []
    season = _season_str(data.get("filters", {}))
    for m in data.get("matches", []):
        home = ((m.get("homeTeam") or {}).get("name") or "").strip()
        away = ((m.get("awayTeam") or {}).get("name") or "").strip()
        if not home or not away:
            continue
        kickoff = m.get("utcDate") or ""
        status = (m.get("status") or "").upper()
        score = m.get("score", {}).get("fullTime", {})
        # Include finished matches with scores AND upcoming fixtures.
        out.append(SoccerFixture(
            league=league,
            season=season or (m.get("season", {}).get("startDate", "")[:4]),
            home_team=home,
            away_team=away,
            utc_kickoff=kickoff,
            matchday=m.get("matchday"),
            venue=(m.get("venue") or None),
            referee=next(
                (r.get("name") for r in m.get("referees", [])
                 if (r.get("role") or "").lower() in ("main", "referee")),
                None,
            ),
            status=status or "SCHEDULED",
            source="football_data_org",
        ).to_dict())
        # Also record as a match doc if finished with a score
        if status == "FINISHED" and score.get("home") is not None:
            out_match = SoccerMatch(
                league=league,
                season=season or "current",
                home_team=home,
                away_team=away,
                date=kickoff[:10],
                home_score=score.get("home"),
                away_score=score.get("away"),
                status="finished",
                source="football_data_org",
            ).to_dict()
            out.append({"_kind": "match", **out_match})
    return out


__all__ = ["fetch_standings", "fetch_teams", "fetch_fixtures",
           "_LEAGUE_CODE_MAP"]
