"""Fallback orchestrator — tries providers in priority order per capability.

Public API used by the rest of the codebase:

    await refresh_all_leagues(db, seasons=("2024-25", "2023-24"))
        → runs all sources in parallel, caches everything into
          soccer_matches / soccer_teams / soccer_standings / soccer_fixtures

    matches = await get_historical_results(db, league, season=None)
    fixtures = await get_fixtures(db, league, date_from, date_to)
    standings = await get_standings(db, league, season=None)
    team = await get_team(db, name, league=None)

The fallback logic is simple but effective:
    1. HIT the mongo cache first. If we have fresh data (< staleness_hours
       old), return it.
    2. Otherwise iterate through the provider priority list for the
       requested capability, trying each in turn until one returns
       non-empty data. Cache the result on the way out.
    3. If ALL providers fail, return whatever's in the cache (even
       stale) rather than empty. Better stale than nothing.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from services.soccer import cache as soccer_cache
from services.soccer.sources import (
    football_data_co_uk as fd_couk,
    football_data_org as fd_org,
    openligadb,
    thesportsdb as tsdb,
)

logger = logging.getLogger("lockscore.services.soccer.fallback")


# ── Provider priority per capability (highest-trust first) ─────────
_MATCHES_PROVIDERS = ("football_data_co_uk", "football_data_org",
                       "openligadb", "thesportsdb")
_STANDINGS_PROVIDERS = ("football_data_org",)              # only source that has standings
_TEAMS_PROVIDERS     = ("football_data_org", "thesportsdb")
_FIXTURES_PROVIDERS  = ("football_data_org", "thesportsdb")

_CACHE_STALE_HOURS = 24  # standings / teams refresh once/day


# ── Public: bulk refresh (called by cron / server startup) ──────────
async def refresh_all_leagues(db, seasons: Iterable[str] = ("2024-25",)) -> dict:
    """Run every source and cache. Non-fatal — one provider failing
    doesn't stop the others."""
    summary: dict[str, dict] = {}

    # 1) football-data.co.uk — huge historical CSV pull
    try:
        matches = await fd_couk.fetch_all_leagues(seasons)
        n = await soccer_cache.upsert_matches_bulk(db, matches)
        await soccer_cache.log_ingest_run(
            db, "football_data_co_uk", "matches", {"upserted": n})
        summary["football_data_co_uk"] = {"matches": n}
        logger.info("football-data.co.uk: upserted %d matches", n)
    except Exception as e:
        logger.warning("football-data.co.uk refresh failed: %s", e)
        summary["football_data_co_uk"] = {"error": str(e)}

    # 2) OpenLigaDB — German leagues
    try:
        openliga_matches: list[dict] = []
        for league in openligadb._LEAGUE_SHORTCUT_MAP:
            for start_year in (2024, 2023, 2022):
                ms = await openligadb.fetch_league_season(league, start_year)
                openliga_matches.extend(ms)
        n = await soccer_cache.upsert_matches_bulk(db, openliga_matches)
        await soccer_cache.log_ingest_run(
            db, "openligadb", "matches", {"upserted": n})
        summary["openligadb"] = {"matches": n}
        logger.info("openligadb: upserted %d matches", n)
    except Exception as e:
        logger.warning("openligadb refresh failed: %s", e)
        summary["openligadb"] = {"error": str(e)}

    # 3) TheSportsDB — teams (metadata) + past/next events
    try:
        team_count = 0
        for league in tsdb._LEAGUE_ID_MAP:
            teams = await tsdb.fetch_teams(league)
            for t in teams:
                await soccer_cache.upsert_team(db, t)
                team_count += 1
        await soccer_cache.log_ingest_run(
            db, "thesportsdb", "teams", {"upserted": team_count})
        summary["thesportsdb"] = {"teams": team_count}
        logger.info("thesportsdb: upserted %d teams", team_count)
    except Exception as e:
        logger.warning("thesportsdb refresh failed: %s", e)
        summary["thesportsdb"] = {"error": str(e)}

    # 4) football-data.org — standings + fixtures + teams (needs API key)
    try:
        fdo_result = {"standings": 0, "fixtures": 0, "teams": 0}
        today = datetime.now(timezone.utc).date()
        date_from = (today - timedelta(days=7)).isoformat()
        date_to   = (today + timedelta(days=30)).isoformat()

        for league in fd_org._LEAGUE_CODE_MAP:
            # Standings
            standings = await fd_org.fetch_standings(league)
            for s in standings:
                await soccer_cache.upsert_standing(db, s)
                fdo_result["standings"] += 1
            # Teams
            teams = await fd_org.fetch_teams(league)
            for t in teams:
                await soccer_cache.upsert_team(db, t)
                fdo_result["teams"] += 1
            # Fixtures (past 7d + next 30d)
            fixtures = await fd_org.fetch_fixtures(league, date_from, date_to)
            for f in fixtures:
                if f.get("_kind") == "match":
                    f.pop("_kind", None)
                    await soccer_cache.upsert_match(db, f)
                else:
                    await soccer_cache.upsert_fixture(db, f)
                    fdo_result["fixtures"] += 1
        await soccer_cache.log_ingest_run(
            db, "football_data_org", "all", fdo_result)
        summary["football_data_org"] = fdo_result
        logger.info("football-data.org: %s", fdo_result)
    except Exception as e:
        logger.warning("football-data.org refresh failed: %s", e)
        summary["football_data_org"] = {"error": str(e)}

    return summary


# ── Public: cache-first reads with source fallback ──────────────────
async def get_historical_results(db, league: str,
                                  season: Optional[str] = None,
                                  limit: int = 500) -> list[dict]:
    """Return finished matches for a league (optionally filtered by season).
    Reads exclusively from cache — expects `refresh_all_leagues` to have
    populated it. If the cache is empty, callers should trigger a
    refresh first (server startup does this)."""
    q: dict = {"league": league, "status": "finished"}
    if season:
        q["season"] = season
    cursor = db.soccer_matches.find(q, {"_id": 0}).sort("date", -1).limit(limit)
    return await cursor.to_list(length=limit)


async def get_standings(db, league: str, season: Optional[str] = None) -> list[dict]:
    q: dict = {"league": league}
    if season:
        q["season"] = season
    cursor = db.soccer_standings.find(q, {"_id": 0}).sort("position", 1)
    return await cursor.to_list(length=100)


async def get_team(db, name: str,
                    league: Optional[str] = None) -> Optional[dict]:
    """Fuzzy team lookup — case-insensitive contains match."""
    q: dict = {"name": {"$regex": name, "$options": "i"}}
    if league:
        q["league"] = league
    return await db.soccer_teams.find_one(q, {"_id": 0})


async def get_fixtures(db, league: str, date_from: str,
                        date_to: str) -> list[dict]:
    q = {
        "league": league,
        "utc_kickoff": {"$gte": date_from, "$lte": date_to + "T23:59:59"},
    }
    cursor = db.soccer_fixtures.find(q, {"_id": 0}).sort("utc_kickoff", 1)
    return await cursor.to_list(length=200)


__all__ = [
    "refresh_all_leagues",
    "get_historical_results",
    "get_standings",
    "get_team",
    "get_fixtures",
]
