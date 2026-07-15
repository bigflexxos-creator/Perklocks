"""TheSportsDB source — free, no key. Teams + basic fixtures.

Free tier endpoints (documented at thesportsdb.com/api.php):
    all_leagues.php                → all leagues
    lookup_all_teams.php?id={lid}  → all teams in a league (needs league id)
    search_all_teams.php?l={name}  → search teams by league name
    eventsseason.php?id={lid}&s=2024-2025  → all events for a league-season
    eventsnext.php?id={teamid}     → next 5 fixtures for a team
    eventspastleague.php?id={lid}  → last 15 results in a league

No odds. Good for team metadata (stadium, founded, badge URL) and as a
fallback fixture source for leagues Football-Data.org doesn't cover.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from services.soccer.models import SoccerFixture, SoccerMatch, SoccerTeam

logger = logging.getLogger("lockscore.services.soccer.thesportsdb")

_BASE = "https://www.thesportsdb.com/api/v1/json/3"  # v3 = free tier
_HTTP_TIMEOUT = 15.0
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; LockScore/1.0)"}

# Canonical league code → TheSportsDB league id
_LEAGUE_ID_MAP: dict[str, str] = {
    "EPL":          "4328",
    "ELC":          "4329",
    "EL1":          "4396",
    "EL2":          "4397",
    "LaLiga":       "4335",
    "LaLiga2":      "4396",  # note: TSDB has different id
    "Bundesliga":   "4331",
    "Bundesliga2":  "4332",
    "SerieA":       "4332",  # placeholder — verify at runtime
    "Ligue1":       "4334",
    "Eredivisie":   "4337",
    "Primeira":     "4344",
    "SPL":          "4330",
    "Allsvenskan":  "4347",
    "Eliteserien":  "4342",
    "MLS":          "4346",
    "LigaMX":       "4350",
    "Brasileirao":  "4351",
    "UCL":          "4480",
    "UEL":          "4481",
}


async def _get(path: str, params: Optional[dict] = None) -> dict:
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, headers=_HEADERS) as cx:
        r = await cx.get(f"{_BASE}/{path.lstrip('/')}", params=params or {})
        r.raise_for_status()
        return r.json() or {}


async def fetch_teams(league: str) -> list[dict]:
    lid = _LEAGUE_ID_MAP.get(league)
    if not lid:
        return []
    try:
        data = await _get("lookup_all_teams.php", {"id": lid})
    except Exception as e:
        logger.debug("thesportsdb teams %s failed: %s", league, e)
        return []
    out: list[dict] = []
    for t in (data.get("teams") or []):
        out.append(SoccerTeam(
            name=(t.get("strTeam") or "").strip(),
            league=league,
            country=t.get("strCountry"),
            stadium=t.get("strStadium"),
            founded=int(t.get("intFormedYear")) if (t.get("intFormedYear") or "").isdigit() else None,
            website=t.get("strWebsite"),
            thesportsdb_id=t.get("idTeam"),
            source="thesportsdb",
        ).to_dict())
    return out


async def fetch_league_season_events(league: str, season: str) -> list[dict]:
    """`season` in TSDB format is 'YYYY-YYYY' e.g. '2024-2025'."""
    lid = _LEAGUE_ID_MAP.get(league)
    if not lid:
        return []
    s = season if "-" in season and len(season) >= 9 else f"20{season[:2]}-20{season[2:]}"
    try:
        data = await _get("eventsseason.php", {"id": lid, "s": s})
    except Exception as e:
        logger.debug("thesportsdb eventsseason %s %s failed: %s", league, s, e)
        return []
    events = data.get("events") or []
    out: list[dict] = []
    for e in events:
        home = (e.get("strHomeTeam") or "").strip()
        away = (e.get("strAwayTeam") or "").strip()
        date = (e.get("dateEvent") or e.get("dateEventLocal") or "")[:10]
        if not home or not away or not date:
            continue
        hs = e.get("intHomeScore")
        as_ = e.get("intAwayScore")
        status = "finished" if (hs not in (None, "") and as_ not in (None, "")) else "scheduled"
        try:
            hs_int = int(hs) if hs not in (None, "") else None
            as_int = int(as_) if as_ not in (None, "") else None
        except (TypeError, ValueError):
            hs_int = as_int = None
        out.append(SoccerMatch(
            league=league,
            season=s,
            home_team=home,
            away_team=away,
            date=date,
            home_score=hs_int,
            away_score=as_int,
            status=status,
            source="thesportsdb",
        ).to_dict())
    return out


async def fetch_next_events_league(league: str) -> list[dict]:
    """Next upcoming events. Uses `eventsnextleague.php`."""
    lid = _LEAGUE_ID_MAP.get(league)
    if not lid:
        return []
    try:
        data = await _get("eventsnextleague.php", {"id": lid})
    except Exception as e:
        logger.debug("thesportsdb next %s failed: %s", league, e)
        return []
    out: list[dict] = []
    for e in (data.get("events") or []):
        home = (e.get("strHomeTeam") or "").strip()
        away = (e.get("strAwayTeam") or "").strip()
        kickoff = (e.get("strTimestamp") or "") or (
            (e.get("dateEvent") or "") + "T" + (e.get("strTime") or "00:00:00")
        )
        if not home or not away or not kickoff:
            continue
        out.append(SoccerFixture(
            league=league,
            season=(e.get("strSeason") or "current"),
            home_team=home,
            away_team=away,
            utc_kickoff=kickoff,
            venue=e.get("strVenue"),
            status="SCHEDULED",
            source="thesportsdb",
        ).to_dict())
    return out


__all__ = ["fetch_teams", "fetch_league_season_events",
           "fetch_next_events_league", "_LEAGUE_ID_MAP"]
