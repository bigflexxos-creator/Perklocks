"""OpenLigaDB source — free, no key. German soccer authoritative.

Endpoints:
    /getavailableleagues                          → all leagues
    /getmatchdata/{leagueShortcut}/{leagueSeason} → all matches
    /getavailablegroups/{leagueShortcut}/{leagueSeason} → matchdays
    /getcurrentgroup/{leagueShortcut}             → current matchday

leagueShortcut: 'bl1' = Bundesliga, 'bl2' = 2.Bundesliga, 'bl3' = 3.Liga,
                'dfb' = DFB-Pokal, 'em' = European Championship,
                'wm' = World Cup

Season is the START year (2024 = 2024/25). No odds.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from services.soccer.models import SoccerMatch

logger = logging.getLogger("lockscore.services.soccer.openligadb")

_BASE = "https://api.openligadb.de"
_HTTP_TIMEOUT = 15.0
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; LockScore/1.0)",
            "Accept": "application/json"}

_LEAGUE_SHORTCUT_MAP: dict[str, str] = {
    "Bundesliga":  "bl1",
    "Bundesliga2": "bl2",
    "Bundesliga3": "bl3",   # 3. Liga
}


async def _get(path: str) -> dict | list:
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, headers=_HEADERS) as cx:
        r = await cx.get(f"{_BASE}/{path.lstrip('/')}")
        r.raise_for_status()
        return r.json()


async def fetch_league_season(league: str, season_start_year: int) -> list[dict]:
    shortcut = _LEAGUE_SHORTCUT_MAP.get(league)
    if not shortcut:
        return []
    try:
        data = await _get(f"getmatchdata/{shortcut}/{season_start_year}")
    except Exception as e:
        logger.debug("openligadb %s %d failed: %s", league, season_start_year, e)
        return []
    out: list[dict] = []
    season = f"{season_start_year}-{str(season_start_year+1)[-2:]}"
    for m in (data or []):
        team1 = ((m.get("team1") or {}).get("teamName") or "").strip()
        team2 = ((m.get("team2") or {}).get("teamName") or "").strip()
        date = (m.get("matchDateTime") or "")[:10]
        if not team1 or not team2 or not date:
            continue
        # Final score comes from `matchResults` list — pick the entry with
        # resultTypeID == 2 (final). Otherwise the pre-half-time result.
        final = next(
            (r for r in (m.get("matchResults") or [])
             if r.get("resultTypeID") == 2 or (r.get("resultName") or "").lower().startswith("endergebnis")),
            None,
        )
        hs = final.get("pointsTeam1") if final else None
        as_ = final.get("pointsTeam2") if final else None
        out.append(SoccerMatch(
            league=league,
            season=season,
            home_team=team1,
            away_team=team2,
            date=date,
            home_score=hs,
            away_score=as_,
            status="finished" if m.get("matchIsFinished") else "scheduled",
            source="openligadb",
        ).to_dict())
    return out


__all__ = ["fetch_league_season", "_LEAGUE_SHORTCUT_MAP"]
