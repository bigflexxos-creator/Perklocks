"""NFL Ingest — free public data sources, no paid APIs.

Primary  : ESPN public (`site.api.espn.com/.../football/nfl/...`)
Enrich   : nfl.com/stats — current-season player stats HTML pages.
Fallback : Pro-Football-Reference (`pro-football-reference.com`) —
           blocked from datacenter IPs as of 2026-06-27 (HTTP 403). Code
           path kept for residential-proxy deploys.

Same architecture as `nba_ingest.py` — every source feeds into the
shared `services.active_registry`. The validator there enforces:

    * No minutes / snaps / appearances  → False (excluded from picks)
    * Status == 'Inactive' or 'Suspended' from any source → False
    * Player missing from registry      → False (retired / never existed)

Wired by `server.py` at startup:

    asyncio.create_task(loop_nfl(db))   # 24-h cadence

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

logger = logging.getLogger("lockscore.services.nfl")

HTTP_TIMEOUT_S = 15.0
ESPN_TEAMS = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams"
ESPN_ROSTER = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams/{team_id}/roster"
ESPN_LEADERS = "https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/seasons/{season}/types/2/leaders"

# nfl.com/stats — public stats pages. Categories: passing, rushing,
# receiving, defensive, special-teams, scoring. We hit the top 3 offensive
# categories which cover ~90% of player-prop usage.
NFL_STATS_URL = (
    "https://www.nfl.com/stats/player-stats/category/{category}/{season}/REG/"
    "all/{sort_field}/desc"
)
NFL_CATEGORIES = [
    ("passing", "passingyards"),
    ("rushing", "rushingyards"),
    ("receiving", "receivingreceptions"),
]

PFR_OFFENSE = "https://www.pro-football-reference.com/years/{season}/passing.htm"

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:127.0) Gecko/20100101 Firefox/127.0",
]


def _season_year() -> int:
    """NFL season starts in September; pick the year that the regular
    season began in. June 2026 → 2025 season is the most-recently
    completed; we want the upcoming 2026 season once August hits."""
    now = datetime.now(timezone.utc)
    return now.year if now.month >= 8 else now.year - 1


# ─────────────────────────── ESPN (primary) ───────────────────────────
async def _ingest_from_espn(db) -> dict[str, Any]:
    summary = {"source": "espn", "teams": 0, "active_players": 0, "leaders": 0, "errors": 0}
    season = _season_year()
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S, headers={
        "User-Agent": "PerkLocks-AI/1.0 (+ESPN public)",
        "Accept": "application/json",
    }) as client:
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
            logger.warning(f"NFL ESPN teams fetch failed: {e}")
            return summary

        sem = asyncio.Semaphore(6)
        async def _one(team: dict) -> int:
            async with sem:
                try:
                    rr = await client.get(ESPN_ROSTER.format(team_id=team["id"]))
                    rr.raise_for_status()
                    count = 0
                    # NFL rosters return list-of-list under "athletes" (positions)
                    for block in rr.json().get("athletes", []):
                        if isinstance(block, dict) and block.get("items"):
                            iterable = block["items"]
                        elif isinstance(block, dict) and block.get("fullName"):
                            iterable = [block]
                        else:
                            iterable = []
                        for a in iterable:
                            status = (a.get("status") or {}).get("name") or "Active"
                            if status.lower() in ("inactive", "suspended", "retired"):
                                continue
                            name = a.get("fullName") or a.get("displayName") or ""
                            if not name:
                                continue
                            active_registry.record_active(
                                "nfl", "espn", name,
                                team=team["name"],
                                status=status,
                                raw={"espn_id": a.get("id"), "position": (a.get("position") or {}).get("abbreviation")},
                            )
                            count += 1
                    return count
                except Exception as e:
                    logger.debug(f"NFL ESPN roster fail team={team.get('name')}: {e}")
                    return 0
        roster_counts = await asyncio.gather(*(_one(t) for t in teams))
        summary["active_players"] = sum(roster_counts)

        try:
            url = ESPN_LEADERS.format(season=season)
            r = await client.get(url, params={"lang": "en", "region": "us"})
            r.raise_for_status()
            for cat in r.json().get("categories", []):
                name = cat.get("name", "").lower()
                if not any(k in name for k in ("passing", "rushing", "receiving", "touchdown")):
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
                    dv = L.get("displayValue") or ""
                    m = re.search(r"GP:\s*(\d+)", dv)
                    games_played = int(m.group(1)) if m else None
                    active_registry.record_active(
                        "nfl", "espn_leaders", pname,
                        games_played=games_played,
                        raw={"category": name, "value": L.get("value"), "display": dv},
                    )
                    summary["leaders"] += 1
        except Exception as e:
            logger.debug(f"NFL ESPN leaders fail: {e}")
            summary["errors"] += 1

    return summary


# ─────────────────────────── nfl.com/stats (enrichment) ───────────────────────────
async def _ingest_from_nfl_com(db) -> dict[str, Any]:
    """Scrape nfl.com/stats player tables. Public HTML, no auth.
    Confirmed reachable from datacenter IPs (HTTP 200) as of 2026-06-27."""
    import random
    summary = {"source": "nfl_com", "rows": 0, "errors": 0}
    season = _season_year()
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html",
        "Accept-Language": "en-US,en;q=0.9",
    }
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S, headers=headers, follow_redirects=True) as client:
        for category, sort_field in NFL_CATEGORIES:
            url = NFL_STATS_URL.format(category=category, season=season, sort_field=sort_field)
            try:
                r = await client.get(url)
                if r.status_code != 200:
                    summary["errors"] += 1
                    continue
                html = r.text
            except Exception:
                summary["errors"] += 1
                continue
            # Extract player rows. NFL.com renders a Vue/React shell so the
            # public HTML uses <a href="/players/...">Player Name</a> inside
            # <td class="d3-o-club-fullname">...</td> patterns.
            # Best-effort regex parse — falls through if NFL changes layout.
            for m in re.finditer(
                r'<a[^>]*href="/players/[^"]+/?"[^>]*>([^<]+)</a>'
                r'.*?<td[^>]*>(\d+)</td>',  # first stat column often games
                html, re.DOTALL,
            ):
                name = re.sub(r"\s+", " ", m.group(1)).strip()
                try:
                    games = int(m.group(2))
                except ValueError:
                    games = None
                if not name or name.lower() == "player":
                    continue
                active_registry.record_active(
                    "nfl", "nfl_com", name,
                    games_played=games,
                    raw={"category": category, "season": season},
                )
                summary["rows"] += 1
            # polite delay between categories
            await asyncio.sleep(random.uniform(0.5, 1.2))
    return summary


# ─────────────────────────── PFR (datacenter-blocked) ───────────────────────────
async def _ingest_from_pfr(db) -> dict[str, Any]:
    """Best-effort scrape of Pro-Football-Reference. Returns immediately
    on 403 — kept as code path for residential-proxy deploys."""
    import random
    summary = {"source": "pfr", "rows": 0, "errors": 0, "skipped": False}
    if os.getenv("PERKLOCKS_DISABLE_SCRAPERS") == "1":
        summary["skipped"] = True
        return summary
    season = _season_year()
    url = PFR_OFFENSE.format(season=season)
    await asyncio.sleep(random.uniform(0.3, 1.0))
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-User": "?1",
        "Sec-Fetch-Dest": "document",
    }
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S, headers=headers, follow_redirects=True) as client:
            r = await client.get(url)
            if r.status_code != 200:
                logger.info(f"NFL PFR scrape blocked: HTTP {r.status_code}")
                summary["errors"] += 1
                return summary
            html = r.text
    except Exception as e:
        logger.info(f"NFL PFR scrape errored: {e}")
        summary["errors"] += 1
        return summary

    for m in re.finditer(
        r'<tr[^>]*>.*?data-stat="player"[^>]*>(?:<a [^>]*>)?([^<]+).*?'
        r'data-stat="team"[^>]*>(?:<a [^>]*>)?([^<]+).*?'
        r'data-stat="g"[^>]*>([0-9]+).*?</tr>',
        html, re.DOTALL,
    ):
        name = re.sub(r"\s+", " ", m.group(1)).strip()
        team = m.group(2).strip()
        try:
            games = int(m.group(3))
        except ValueError:
            games = None
        if not name or name.lower() == "player":
            continue
        active_registry.record_active(
            "nfl", "pfr", name,
            team=team or None,
            games_played=games,
            raw={"category": "passing", "season": season},
        )
        summary["rows"] += 1
    return summary


# ─────────────────────────── Public refresh ───────────────────────────
async def refresh(db) -> dict[str, Any]:
    started = time.time()
    espn, nfl_com, pfr = await asyncio.gather(
        _ingest_from_espn(db),
        _ingest_from_nfl_com(db),
        _ingest_from_pfr(db),
        return_exceptions=False,
    )
    persisted = await active_registry.persist(db)
    summary = {
        "ok": True,
        "elapsed_sec": round(time.time() - started, 1),
        "sources": {"espn": espn, "nfl_com": nfl_com, "pfr": pfr},
        "registry_persisted": persisted.get("nfl", 0),
    }
    logger.info(f"NFL ingest: {summary}")
    return summary


async def loop(db, interval_s: int = 24 * 60 * 60) -> None:
    if os.getenv("PERKLOCKS_DISABLE_NETWORK") == "1":
        logger.info("NFL ingest loop skipped (PERKLOCKS_DISABLE_NETWORK=1)")
        return
    while True:
        try:
            await refresh(db)
        except Exception as e:
            logger.warning(f"NFL refresh failed: {e}")
        await asyncio.sleep(interval_s)
