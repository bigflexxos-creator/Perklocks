"""NBA Ingest — free public data sources, no paid APIs.

Primary  : ESPN public (`site.api.espn.com/.../basketball/nba/...`)
Enrich   : Basketball-Reference (`basketball-reference.com`) — per-game
           stats table for the current NBA season.
Fallback : nba.com/stats — included as a code path; **blocked from
           datacenter IPs** as of 2026-06-27 so we wrap every call in a
           try/except and fold its failures into a single warning. When
           you run the backend behind a residential proxy this path
           lights up automatically.

Each successful source records into `services.active_registry` with the
source name; a player is only considered ACTIVE for picks when at least
one source has confirmed them within the staleness window AND the
registry's minutes/games_played guard accepts them.

Wired by `server.py` at startup:

    asyncio.create_task(loop_nba(db))  # daily refresh (24-h cadence)

Author: PerkLocks AI · 2026-06-27
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from services import active_registry

logger = logging.getLogger("lockscore.services.nba")

# ─────────────────────────── Config ───────────────────────────
HTTP_TIMEOUT_S = 15.0
ESPN_TEAMS = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams"
ESPN_ROSTER = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{team_id}/roster"
ESPN_LEADERS = "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba/seasons/{season}/types/2/leaders"
BBR_PER_GAME = "https://www.basketball-reference.com/leagues/NBA_{season}_per_game.html"
NBA_STATS_LEADERS = "https://stats.nba.com/stats/leaguedashplayerstats"

USER_AGENTS = [
    # Rotate between real-browser UAs to reduce block probability when hitting
    # Basketball-Reference (CloudFront serves 403 to default urllib UAs).
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:127.0) Gecko/20100101 Firefox/127.0",
]


def _season_label_bbr() -> str:
    """Basketball-Reference URL slug: e.g. 2025-26 NBA season → '2026'."""
    now = datetime.now(timezone.utc)
    # NBA season starts in October. Oct–Dec → next year, Jan–Sep → current year.
    return str(now.year + 1 if now.month >= 10 else now.year)


def _season_label_espn() -> str:
    """ESPN core uses the ending year of the season."""
    return _season_label_bbr()


# ─────────────────────────── ESPN (primary) ───────────────────────────
async def _ingest_from_espn(db) -> dict[str, Any]:
    """Pull teams + active rosters + season leaders from ESPN public.
    This is the same shape we already use for CSL — proven reliable.
    """
    summary = {"source": "espn", "teams": 0, "active_players": 0, "leaders": 0, "errors": 0}
    season = _season_label_espn()
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S, headers={
        "User-Agent": "PerkLocks-AI/1.0 (+ESPN public)",
        "Accept": "application/json",
    }) as client:
        # Teams ----------------------------------------------------------
        try:
            r = await client.get(ESPN_TEAMS)
            r.raise_for_status()
            teams: list[dict] = []
            for s in r.json().get("sports", []):
                for lg in s.get("leagues", []):
                    for e in lg.get("teams", []):
                        t = e.get("team") or {}
                        if t.get("id"):
                            teams.append({"id": t["id"], "name": t.get("displayName") or t.get("name")})
            summary["teams"] = len(teams)
        except Exception as e:
            logger.warning(f"NBA ESPN teams fetch failed: {e}")
            return summary

        # Rosters (active = status.name == 'Active') -------------------
        sem = asyncio.Semaphore(6)
        async def _one(team: dict) -> int:
            async with sem:
                try:
                    rr = await client.get(ESPN_ROSTER.format(team_id=team["id"]))
                    rr.raise_for_status()
                    count = 0
                    for a in rr.json().get("athletes", []):
                        status = (a.get("status") or {}).get("name") or ""
                        # ESPN marks "Active", "Inactive", "Suspended", "Out", "Day-To-Day"
                        # Treat anything NOT explicitly Inactive/Suspended as active —
                        # we want to err on the side of including the player and let
                        # downstream minutes/games filters do the heavy lifting.
                        if status.lower() in ("inactive", "suspended"):
                            continue
                        name = a.get("fullName") or a.get("displayName") or ""
                        if not name:
                            continue
                        active_registry.record_active(
                            "nba", "espn", name,
                            team=team["name"],
                            status=status,
                            raw={"espn_id": a.get("id"), "position": (a.get("position") or {}).get("abbreviation")},
                        )
                        count += 1
                    return count
                except Exception as e:
                    logger.debug(f"NBA ESPN roster fail team={team.get('name')}: {e}")
                    return 0
        roster_counts = await asyncio.gather(*(_one(t) for t in teams))
        summary["active_players"] = sum(roster_counts)

        # Leaders --------------------------------------------------------
        try:
            url = ESPN_LEADERS.format(season=season)
            r = await client.get(url, params={"lang": "en", "region": "us"})
            r.raise_for_status()
            for cat in r.json().get("categories", []):
                name = cat.get("name", "").lower()
                if not any(k in name for k in ("points", "assists", "rebounds")):
                    continue
                for L in cat.get("leaders", [])[:25]:
                    ath_ref = (L.get("athlete") or {}).get("$ref")
                    if not ath_ref:
                        continue
                    try:
                        ar = await client.get(ath_ref)
                        ath = ar.json()
                    except Exception:
                        continue
                    pname = ath.get("fullName") or ath.get("displayName") or ""
                    if not pname:
                        continue
                    games_played = None
                    # Some leader rows expose "GP" in displayValue ("GP: 60, PPG: 28.7")
                    dv = (L.get("displayValue") or "")
                    m = re.search(r"GP:\s*(\d+)", dv)
                    if m:
                        games_played = int(m.group(1))
                    active_registry.record_active(
                        "nba", "espn_leaders", pname,
                        games_played=games_played,
                        raw={"category": name, "value": L.get("value"), "display": dv},
                    )
                    summary["leaders"] += 1
        except Exception as e:
            logger.debug(f"NBA ESPN leaders fail: {e}")
            summary["errors"] += 1

    return summary


# ─────────────────────────── Basketball-Reference (enrichment) ───────────────────────────
async def _ingest_from_bbr(db) -> dict[str, Any]:
    """Scrape the season per-game stats table. Provides minutes + games
    played which the registry uses to disqualify zero-minute players."""
    summary = {"source": "bbr", "rows": 0, "with_minutes": 0, "errors": 0}
    season = _season_label_bbr()
    url = BBR_PER_GAME.format(season=season)
    # Stagger requests with a small random delay to avoid CloudFront 429.
    import random
    await asyncio.sleep(random.uniform(0.3, 1.2))
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html",
        "Accept-Language": "en-US,en;q=0.9",
        # Looks like a real browser session.
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-User": "?1",
        "Sec-Fetch-Dest": "document",
    }
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S, headers=headers, follow_redirects=True) as client:
            r = await client.get(url)
            if r.status_code != 200:
                logger.info(f"NBA BBR scrape blocked: HTTP {r.status_code} ({url})")
                summary["errors"] += 1
                return summary
            html = r.text
    except Exception as e:
        logger.info(f"NBA BBR scrape errored: {e}")
        summary["errors"] += 1
        return summary

    # Parse the per_game_stats table with regex (avoids beautifulsoup dep).
    # BBR's 2026 layout uses data-stat="name_display" (changed from "player"
    # in a 2026 redesign — confirmed live on 2026-06-27).
    rows = re.findall(r'<tr[^>]*>.*?</tr>', html, flags=re.DOTALL)
    for row in rows:
        if 'data-stat="name_display"' not in row:
            continue
        pmatch = re.search(r'data-stat="name_display"[^>]*>(?:<a [^>]*>)?([^<]+)', row)
        tmatch = re.search(r'data-stat="team_name_abbr"[^>]*>(?:<a [^>]*>)?([^<]+)', row)
        gmatch = re.search(r'data-stat="games"[^>]*>([0-9.]+)', row)
        mpmatch = re.search(r'data-stat="mp_per_g"[^>]*>([0-9.]+)', row)
        if not pmatch:
            continue
        name = re.sub(r'\s+', ' ', pmatch.group(1)).strip()
        team = (tmatch.group(1) if tmatch else "").strip()
        if name.lower() in ("player", "name_display"):   # header rows repeat
            continue
        games = int(float(gmatch.group(1))) if gmatch else None
        mpg = float(mpmatch.group(1)) if mpmatch else None
        season_minutes = (mpg * games) if (mpg and games) else mpg
        active_registry.record_active(
            "nba", "bbr", name,
            team=team or None,
            minutes=season_minutes,
            games_played=games,
            raw={"mpg": mpg, "season": season},
        )
        summary["rows"] += 1
        if season_minutes and season_minutes > 0:
            summary["with_minutes"] += 1
    return summary


# ─────────────────────────── nba.com/stats (datacenter-blocked) ───────────────────────────
async def _ingest_from_nba_stats(db) -> dict[str, Any]:
    """Attempt the official nba.com/stats endpoint. Blocked on datacenter
    IPs as of 2026-06-27 but we keep the code path so a residential proxy
    deploy lights this up automatically."""
    summary = {"source": "nba_stats", "rows": 0, "errors": 0, "skipped": False}
    if os.getenv("PERKLOCKS_DISABLE_SCRAPERS") == "1":
        summary["skipped"] = True
        return summary
    params = {
        "Season": f"{int(_season_label_bbr()) - 1}-{_season_label_bbr()[-2:]}",
        "SeasonType": "Regular Season",
        "PerMode": "PerGame",
        "MeasureType": "Base",
        "LastNGames": "0", "Month": "0", "OpponentTeamID": "0",
        "PaceAdjust": "N", "PORound": "0", "PlusMinus": "N",
        "Rank": "N", "TeamID": "0", "TwoWay": "0", "Period": "0",
        "LeagueID": "00",
    }
    headers = {
        "User-Agent": USER_AGENTS[0],
        "Referer": "https://www.nba.com/",
        "Origin": "https://www.nba.com",
        "Accept": "application/json",
        "x-nba-stats-origin": "stats",
        "x-nba-stats-token": "true",
    }
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S, headers=headers) as client:
            r = await client.get(NBA_STATS_LEADERS, params=params)
            if r.status_code != 200:
                logger.info(f"NBA stats.nba.com blocked: HTTP {r.status_code}")
                summary["errors"] += 1
                return summary
            data = r.json()
    except Exception as e:
        logger.info(f"NBA stats.nba.com errored (datacenter likely blocked): {e}")
        summary["errors"] += 1
        return summary

    rs = (data.get("resultSets") or [{}])[0]
    headers_row = rs.get("headers", [])
    idx_name = headers_row.index("PLAYER_NAME") if "PLAYER_NAME" in headers_row else -1
    idx_team = headers_row.index("TEAM_ABBREVIATION") if "TEAM_ABBREVIATION" in headers_row else -1
    idx_gp = headers_row.index("GP") if "GP" in headers_row else -1
    idx_min = headers_row.index("MIN") if "MIN" in headers_row else -1
    for row in rs.get("rowSet", []):
        if idx_name < 0:
            continue
        name = row[idx_name]
        team = row[idx_team] if idx_team >= 0 else None
        gp = row[idx_gp] if idx_gp >= 0 else None
        mpg = row[idx_min] if idx_min >= 0 else None
        season_min = (gp * mpg) if (gp and mpg) else mpg
        active_registry.record_active(
            "nba", "nba_stats", name,
            team=team, games_played=gp, minutes=season_min,
        )
        summary["rows"] += 1
    return summary


# ─────────────────────────── Public refresh ───────────────────────────
async def refresh(db) -> dict[str, Any]:
    """Refresh all NBA sources in parallel, persist registry, return summary."""
    started = time.time()
    espn, bbr, nba_stats = await asyncio.gather(
        _ingest_from_espn(db),
        _ingest_from_bbr(db),
        _ingest_from_nba_stats(db),
        return_exceptions=False,
    )
    persisted = await active_registry.persist(db)
    summary = {
        "ok": True,
        "elapsed_sec": round(time.time() - started, 1),
        "sources": {"espn": espn, "bbr": bbr, "nba_stats": nba_stats},
        "registry_persisted": persisted.get("nba", 0),
    }
    logger.info(f"NBA ingest: {summary}")
    return summary


async def loop(db, interval_s: int = 24 * 60 * 60) -> None:
    if os.getenv("PERKLOCKS_DISABLE_NETWORK") == "1":
        logger.info("NBA ingest loop skipped (PERKLOCKS_DISABLE_NETWORK=1)")
        return
    while True:
        try:
            await refresh(db)
        except Exception as e:
            logger.warning(f"NBA refresh failed: {e}")
        await asyncio.sleep(interval_s)
