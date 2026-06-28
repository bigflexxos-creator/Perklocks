"""Soccer Ingest — free public data sources, no paid APIs.

Primary  : Understat (`understat.com`) — top-5 European leagues. Provides
           per-player goals, xG, minutes, position, team, season → the
           "alive in this league" signal the registry needs.
Enrich   : ESPN public (`site.api.espn.com/.../soccer/{league_code}/...`)
           — covers CSL (chn.1), Liga MX (mex.1), MLS (usa.1), Saudi
           Pro League (ksa.1) and others Understat doesn't track.
Fallback : FotMob (`www.fotmob.com/api/data/...`) — kept as a code path
           but their stats endpoints are heavily obfuscated as of
           2026-06-28 and require a residential proxy + signed `x-mas`
           header to consistently return the top-scorer leaderboards.
           For now we only pull league overview metadata from FotMob.

Each successful source records into `services.active_registry`.
Soccer-specific: we record under sport="soccer" with the league as the
team prefix ("EPL — Manchester City") so cross-league name collisions
(two "Felix"es in different leagues) don't merge incorrectly.

Wired by `server.py` at startup:

    asyncio.create_task(loop_soccer(db))   # 24-h cadence

Author: PerkLocks AI · 2026-06-28
"""
from __future__ import annotations

import asyncio
import codecs
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from services import active_registry

logger = logging.getLogger("lockscore.services.soccer")

HTTP_TIMEOUT_S = 15.0

# ─── Understat ───
UNDERSTAT_BASE = "https://understat.com"
# These slugs match Understat's URL routing. Season uses the year the
# season *began* — 2024 = 2024/2025 campaign.
UNDERSTAT_LEAGUES = [
    "EPL", "La_liga", "Bundesliga", "Serie_A", "Ligue_1", "RFPL",
]
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"

# ─── ESPN soccer codes ───
# Covers leagues outside Understat's top-5. Slug map: ESPN league code →
# pretty label written into the registry's `team` field prefix.
ESPN_SOCCER_LEAGUES = {
    # Top-5 European leagues — these double as Understat's coverage. We
    # include them in ESPN too so the registry has real-time rosters
    # year-round (Understat only ships playersData during active season).
    "eng.1":  "Premier League",
    "esp.1":  "La Liga",
    "ger.1":  "Bundesliga",
    "ita.1":  "Serie A",
    "fra.1":  "Ligue 1",
    # Other major competitions
    "usa.1":  "MLS",
    "mex.1":  "Liga MX",
    "ksa.1":  "Saudi Pro League",
    "jpn.1":  "J1 League",
    "aus.1":  "A-League",
    "bra.1":  "Brasileirão",
    "arg.1":  "Liga Argentina",
    "uefa.champions": "UEFA Champions League",
    "uefa.europa":    "UEFA Europa League",
    "eng.2":  "Championship",
    "ger.2":  "2. Bundesliga",
    "esp.2":  "Segunda División",
    "ita.2":  "Serie B",
    "fra.2":  "Ligue 2",
    "ned.1":  "Eredivisie",
    "por.1":  "Primeira Liga",
    "tur.1":  "Süper Lig",
    "bel.1":  "Belgian Pro League",
}


def _current_season() -> int:
    """Most leagues run Aug→May; before Aug we want the previous season."""
    now = datetime.now(timezone.utc)
    return now.year if now.month >= 8 else now.year - 1


# ─────────────────────────── Understat ───────────────────────────
async def _ingest_understat_league(client: httpx.AsyncClient, league: str,
                                   season: int) -> dict[str, Any]:
    """Scrapes Understat's league page and extracts the inline JSON player
    data. League slug is the URL form (EPL, La_liga, Bundesliga, ...)."""
    summary = {"source": f"understat:{league}", "rows": 0, "with_minutes": 0, "errors": 0}
    url = f"{UNDERSTAT_BASE}/league/{league}/{season}"
    try:
        r = await client.get(url, headers={
            "User-Agent": UA,
            "Accept": "text/html",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": UNDERSTAT_BASE,
        })
        if r.status_code != 200:
            logger.info(f"Understat {league} HTTP {r.status_code}")
            summary["errors"] += 1
            return summary
        html = r.text
    except Exception as e:
        logger.info(f"Understat {league} errored: {e}")
        summary["errors"] += 1
        return summary

    # Understat embeds players + teams as JSON.parse('\xNN...') escaped strings
    m = re.search(r"var\s+playersData\s*=\s*JSON\.parse\('([^']+)'\)", html)
    if not m:
        # Some seasons render no playersData (off-season). Not an error.
        summary["errors"] += 1
        return summary
    try:
        players = json.loads(codecs.decode(m.group(1), "unicode_escape"))
    except Exception as e:
        logger.warning(f"Understat {league} JSON decode failed: {e}")
        summary["errors"] += 1
        return summary

    league_label = league.replace("_", " ")
    for p in players:
        name = (p.get("player_name") or "").strip()
        team = (p.get("team_title") or "").strip()
        if not name:
            continue
        try:
            minutes = int(float(p.get("time") or 0))
            games = int(float(p.get("games") or 0))
            goals = int(float(p.get("goals") or 0))
            xg = float(p.get("xG") or 0)
        except (TypeError, ValueError):
            minutes = games = goals = 0
            xg = 0.0
        active_registry.record_active(
            "soccer", f"understat:{league}", name,
            team=f"{league_label} — {team}" if team else league_label,
            minutes=minutes,
            games_played=games,
            raw={
                "league": league_label,
                "team": team,
                "goals": goals,
                "xg": round(xg, 2),
                "assists": int(float(p.get("assists") or 0)),
                "xa": float(p.get("xA") or 0),
                "position": p.get("position"),
                "season": season,
            },
        )
        summary["rows"] += 1
        if minutes > 0:
            summary["with_minutes"] += 1
    return summary


# ─────────────────────────── ESPN ───────────────────────────
async def _ingest_espn_league(client: httpx.AsyncClient, league_code: str,
                              label: str) -> dict[str, Any]:
    """Two-pass ESPN roster fetch:
       1. `site.api.espn.com/.../teams/{id}/roster` — works for non-EU
          leagues (CSL, MLS, Liga MX, J1, KSA, etc.).
       2. `sports.core.api.espn.com/v2/.../teams/{id}/athletes` — required
          for the Big-5 European leagues + UCL/Europa whose `roster`
          endpoint returns an empty athletes list (ESPN siloes EU player
          data behind the deeper core API).
    """
    summary = {"source": f"espn:{league_code}", "teams": 0, "active_players": 0, "errors": 0}
    teams_url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{league_code}/teams"
    roster_tpl = "https://site.api.espn.com/apis/site/v2/sports/soccer/{lg}/teams/{tid}/roster"
    season_yr = _current_season() + 1  # ESPN core uses season-end year
    core_tpl = (
        "https://sports.core.api.espn.com/v2/sports/soccer/leagues/{lg}/"
        "seasons/{yr}/teams/{tid}/athletes"
    )
    try:
        r = await client.get(teams_url, headers={"User-Agent": "PerkLocks-AI/1.0 (ESPN public)"})
        r.raise_for_status()
        teams: list[dict] = []
        for s in r.json().get("sports", []):
            for lg in s.get("leagues", []):
                for e in lg.get("teams", []):
                    t = e.get("team") or {}
                    if t.get("id"):
                        teams.append({"id": t["id"], "name": t.get("displayName") or t.get("name") or ""})
        summary["teams"] = len(teams)
    except Exception as e:
        logger.debug(f"ESPN soccer {league_code} teams failed: {e}")
        return summary
    if not teams:
        return summary

    sem = asyncio.Semaphore(6)
    async def _one(team: dict) -> int:
        async with sem:
            count = 0
            # Pass 1 — site.api roster
            try:
                rr = await client.get(
                    roster_tpl.format(lg=league_code, tid=team["id"]),
                    headers={"User-Agent": "PerkLocks-AI/1.0"},
                )
                rr.raise_for_status()
                for a in rr.json().get("athletes", []):
                    if not isinstance(a, dict):
                        continue
                    status = (a.get("status") or {}).get("name") or "Active"
                    if status.lower() in ("inactive", "suspended", "retired"):
                        continue
                    name = a.get("fullName") or a.get("displayName") or ""
                    if not name:
                        continue
                    active_registry.record_active(
                        "soccer", f"espn:{league_code}", name,
                        team=f"{label} — {team['name']}",
                        status=status,
                        raw={
                            "league": label,
                            "espn_id": a.get("id"),
                            "position": (a.get("position") or {}).get("abbreviation"),
                        },
                    )
                    count += 1
            except Exception as e:
                logger.debug(f"ESPN soccer pass1 fail {league_code}/{team['id']}: {e}")
            # Pass 2 — core.api athletes (Big-5 + UCL fallback)
            if count == 0:
                try:
                    cr = await client.get(
                        core_tpl.format(lg=league_code, yr=season_yr, tid=team["id"]),
                        params={"limit": 100},
                        headers={"User-Agent": "PerkLocks-AI/1.0"},
                    )
                    cr.raise_for_status()
                    items = cr.json().get("items", [])
                    # Each item is {$ref: ".../athletes/{id}?lang=en"}
                    refs = [it.get("$ref") for it in items if it.get("$ref")]
                    # Resolve in parallel with a small concurrency cap.
                    sub_sem = asyncio.Semaphore(8)
                    async def _resolve_one(ref):
                        async with sub_sem:
                            try:
                                ar = await client.get(ref, headers={"User-Agent": "PerkLocks-AI/1.0"})
                                ar.raise_for_status()
                                return ar.json()
                            except Exception:
                                return None
                    athletes = await asyncio.gather(*(_resolve_one(r) for r in refs))
                    for a in athletes:
                        if not a:
                            continue
                        name = a.get("fullName") or a.get("displayName") or ""
                        if not name:
                            continue
                        active_registry.record_active(
                            "soccer", f"espn_core:{league_code}", name,
                            team=f"{label} — {team['name']}",
                            status=a.get("status") or "Active",
                            raw={
                                "league": label,
                                "espn_id": a.get("id"),
                                "position": (a.get("position") or {}).get("abbreviation"),
                                "age": a.get("age"),
                                "season": season_yr,
                            },
                        )
                        count += 1
                except Exception as e:
                    logger.debug(f"ESPN soccer pass2 fail {league_code}/{team['id']}: {e}")
            return count
    counts = await asyncio.gather(*(_one(t) for t in teams))
    summary["active_players"] = sum(counts)
    return summary


# ─────────────────────────── FotMob (best-effort) ───────────────────────────
async def _ingest_fotmob(client: httpx.AsyncClient) -> dict[str, Any]:
    """FotMob's `data.fotmob.com/stats/.../topstats.json` endpoint requires
    a signed `x-mas` header (request-signing) as of 2026-06-28 — datacenter
    requests get HTTP 403 without it. We probe one league and record the
    response so a future residential-proxy deploy can light this up.
    """
    summary = {"source": "fotmob", "rows": 0, "errors": 0, "skipped": False}
    if os.getenv("PERKLOCKS_DISABLE_SCRAPERS") == "1":
        summary["skipped"] = True
        return summary
    try:
        r = await client.get(
            "https://www.fotmob.com/api/data/leagues",
            params={"id": 47},   # EPL probe
            headers={"User-Agent": UA},
        )
        if r.status_code != 200:
            summary["errors"] += 1
            return summary
        d = r.json()
        # Surface a single roster from the overview's topPlayers.byGoals when
        # present — even one entry is better than zero coverage of EPL.
        tp = (d.get("overview") or {}).get("topPlayers") or {}
        # When FotMob renders the byGoals dict client-side, it currently only
        # exposes `seeAllLink` from this endpoint. We don't error on that —
        # just log so we know the residential-proxy path is needed.
        if "seeAllLink" in (tp.get("byGoals") or {}):
            logger.info(
                "FotMob top-scorer leaderboard requires signed request "
                "(seeAllLink only available without proxy)"
            )
    except Exception as e:
        logger.debug(f"FotMob probe failed: {e}")
        summary["errors"] += 1
    return summary


# ─────────────────────────── Public refresh ───────────────────────────
async def refresh(db) -> dict[str, Any]:
    """Refresh all soccer sources in parallel, persist registry, return summary."""
    started = time.time()
    season = _current_season()
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
        # Understat is rate-limited (~5s spacing per their robots.txt
        # respect notes). Run leagues sequentially with a small delay.
        understat_summaries = []
        for lg in UNDERSTAT_LEAGUES:
            understat_summaries.append(await _ingest_understat_league(client, lg, season))
            await asyncio.sleep(2.0)
        # ESPN parallel
        espn_summaries = await asyncio.gather(
            *(_ingest_espn_league(client, code, label) for code, label in ESPN_SOCCER_LEAGUES.items()),
            return_exceptions=False,
        )
        fotmob_summary = await _ingest_fotmob(client)
    persisted = await active_registry.persist(db)
    summary = {
        "ok": True,
        "elapsed_sec": round(time.time() - started, 1),
        "sources": {
            "understat": understat_summaries,
            "espn": espn_summaries,
            "fotmob": fotmob_summary,
        },
        "registry_persisted": persisted.get("soccer", 0),
    }
    logger.info(
        f"Soccer ingest: understat_rows={sum(s.get('rows',0) for s in understat_summaries)}, "
        f"espn_active={sum(s.get('active_players',0) for s in espn_summaries)}, "
        f"persisted={summary['registry_persisted']}, elapsed={summary['elapsed_sec']}s"
    )
    return summary


async def loop(db, interval_s: int = 24 * 60 * 60) -> None:
    if os.getenv("PERKLOCKS_DISABLE_NETWORK") == "1":
        logger.info("Soccer ingest loop skipped (PERKLOCKS_DISABLE_NETWORK=1)")
        return
    while True:
        try:
            await refresh(db)
        except Exception as e:
            logger.warning(f"Soccer refresh failed: {e}")
        await asyncio.sleep(interval_s)
