"""P0-D (2026-08-11) — Live soccer roster ingestion.

Fetches the CURRENT roster of every team in every configured league
via ESPN's public site API and upserts each athlete into
`player_identities` as:

  * a CLUB identity — ``current_team = <team display name>``,
    ``source = "espn_live_roster"``,
    ``affiliation_type = "club"``.
  * a NATIONAL-TEAM identity when ESPN provides ``citizenship`` —
    ``current_national_team = <citizenship country>``,
    ``source = "espn_live_roster"``,
    ``affiliation_type = "national_team"``.

Both writes flow through the P0-A race-safe ``persist_identity`` layer,
so an older ``soccer_player_form`` observation or the P0-C curated
national-team bootstrap can NEVER overwrite the fresher live roster
observation (freshness gate on ``observed_at`` /
``national_team_observed_at``).

Endpoint (no key required):
    https://site.api.espn.com/apis/site/v2/sports/soccer/{league_slug}/teams
    https://site.api.espn.com/apis/site/v2/sports/soccer/{league_slug}/teams/{tid}/roster

League slugs configured:
    * eng.1  – English Premier League
    * esp.1  – Spanish La Liga
    * ita.1  – Italian Serie A
    * ger.1  – German Bundesliga
    * fra.1  – French Ligue 1
    * usa.1  – MLS
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

logger = logging.getLogger("lockscore.espn_live_soccer_rosters")


LEAGUE_SLUGS: dict[str, str] = {
    "eng.1": "EPL",
    "esp.1": "La Liga",
    "ita.1": "Serie A",
    "ger.1": "Bundesliga",
    "fra.1": "Ligue 1",
    "usa.1": "MLS",
}


_TEAMS_URL_TMPL  = ("https://site.api.espn.com/apis/site/v2/sports/soccer/"
                    "{slug}/teams")
_ROSTER_URL_TMPL = ("https://site.api.espn.com/apis/site/v2/sports/soccer/"
                    "{slug}/teams/{tid}/roster")

_LIVE_SOURCE = "espn_live_roster"


async def _get_json(cx: httpx.AsyncClient, url: str) -> Optional[dict]:
    try:
        r = await cx.get(url)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        logger.debug("ESPN GET %s failed: %s", url, e)
    return None


async def _fetch_league_teams(
    cx: httpx.AsyncClient, slug: str,
) -> list[tuple[str, str]]:
    """Return list of (team_id, display_name) for the league."""
    blob = await _get_json(cx, _TEAMS_URL_TMPL.format(slug=slug))
    if not blob:
        return []
    sports = blob.get("sports", [])
    if not sports:
        return []
    leagues = sports[0].get("leagues", [])
    if not leagues:
        return []
    teams = leagues[0].get("teams", [])
    out = []
    for t in teams:
        tm = t.get("team") or {}
        tid = tm.get("id")
        name = tm.get("displayName") or tm.get("name")
        if tid and name:
            out.append((str(tid), name))
    return out


async def _fetch_team_roster(
    cx: httpx.AsyncClient, slug: str, tid: str,
) -> list[dict]:
    """Return list of athlete dicts as ESPN returns them."""
    blob = await _get_json(cx, _ROSTER_URL_TMPL.format(slug=slug, tid=tid))
    if not blob:
        return []
    return blob.get("athletes", []) or []


async def _upsert_athlete_identity(
    db, *, canonical_league: str, team_name: str,
    athlete: dict, now_iso: str,
) -> tuple[bool, bool]:
    """Upsert both club + national-team identities for one athlete.

    Returns ``(club_persisted, nt_persisted)`` — booleans indicating
    whether Mongo was mutated for each stream.  Freshness gates in
    ``persist_identity`` decide whether an existing older observation
    is overridden.
    """
    from services.player_identity import (
        upsert_player, persist_identity, snapshot_registry,
        reset_registry_for_tests,
    )

    espn_id = athlete.get("id")
    display = (athlete.get("displayName")
               or athlete.get("fullName")
               or f"{athlete.get('firstName','')} "
                   f"{athlete.get('lastName','')}").strip()
    if not display:
        return False, False
    pos_dict = athlete.get("position") or {}
    position = (pos_dict.get("abbreviation") if isinstance(pos_dict, dict)
                 else None)
    citizenship = athlete.get("citizenship")

    # ── Club affiliation ──
    from services.player_identity import _norm as _pnorm
    cid_seed_name = display
    # Ingest into the in-memory registry to get a canonical id +
    # historical bookkeeping, then persist to Mongo via the
    # race-safe writer.
    club_ident = upsert_player(
        name=cid_seed_name, sport="Soccer", league=canonical_league,
        provider="espn",
        provider_id=(str(espn_id) if espn_id else f"live:{_pnorm(display)}"),
        current_team=team_name,
        position=position,
        roster_status="active",
        source=_LIVE_SOURCE,
        observed_at=now_iso,
        affiliation_type="club",
        nationality=citizenship,
    )
    club_outcome = await persist_identity(db, club_ident.to_dict())

    nt_persisted = False
    if citizenship:
        nt_ident = upsert_player(
            name=cid_seed_name, sport="Soccer",
            league=canonical_league,  # same canonical id as the club record
            provider="espn",
            provider_id=(str(espn_id) if espn_id else f"live:{_pnorm(display)}"),
            current_team=citizenship,
            affiliation_type="national_team",
            source=_LIVE_SOURCE,
            observed_at=now_iso,
            roster_status="active",
            nationality=citizenship,
        )
        nt_outcome = await persist_identity(db, nt_ident.to_dict())
        nt_persisted = nt_outcome in ("inserted", "advanced", "merged_only")

    club_persisted = club_outcome in ("inserted", "advanced", "merged_only")
    return club_persisted, nt_persisted


async def refresh_live_rosters(
    db, *, league_slugs: Optional[dict[str, str]] = None,
    max_concurrency: int = 6,
    request_timeout: float = 20.0,
) -> dict[str, Any]:
    """Fetch every team's roster for every configured league and
    upsert live identities.  Returns a diagnostic dict.

    ``league_slugs`` — override / subset (for tests).
    """
    from services.player_identity import ensure_identity_indexes
    await ensure_identity_indexes(db)

    slugs = league_slugs or LEAGUE_SLUGS
    now_iso = datetime.now(timezone.utc).isoformat()

    stats: dict[str, Any] = {
        "observed_at": now_iso,
        "leagues": {}, "teams_scanned": 0, "athletes_scanned": 0,
        "club_writes": 0, "national_team_writes": 0,
    }

    sem = asyncio.Semaphore(max_concurrency)

    async with httpx.AsyncClient(timeout=request_timeout) as cx:
        for slug, canonical_league in slugs.items():
            teams = await _fetch_league_teams(cx, slug)
            if not teams:
                stats["leagues"][canonical_league] = {
                    "teams": 0, "athletes": 0, "error": "teams_fetch_failed"}
                continue
            lg_stats = {"teams": len(teams), "athletes": 0,
                         "club_writes": 0, "nt_writes": 0}

            async def _proc(tid_name):
                tid, tname = tid_name
                async with sem:
                    roster = await _fetch_team_roster(cx, slug, tid)
                nonlocal_stats = {"athletes": 0, "club": 0, "nt": 0}
                for a in roster:
                    nonlocal_stats["athletes"] += 1
                    c_ok, nt_ok = await _upsert_athlete_identity(
                        db, canonical_league=canonical_league,
                        team_name=tname, athlete=a, now_iso=now_iso,
                    )
                    if c_ok:  nonlocal_stats["club"] += 1
                    if nt_ok: nonlocal_stats["nt"]   += 1
                return nonlocal_stats

            results = await asyncio.gather(*(_proc(t) for t in teams))
            for r in results:
                lg_stats["athletes"]     += r["athletes"]
                lg_stats["club_writes"]  += r["club"]
                lg_stats["nt_writes"]    += r["nt"]
            stats["leagues"][canonical_league] = lg_stats
            stats["teams_scanned"]        += lg_stats["teams"]
            stats["athletes_scanned"]     += lg_stats["athletes"]
            stats["club_writes"]          += lg_stats["club_writes"]
            stats["national_team_writes"] += lg_stats["nt_writes"]
            logger.info(
                "ESPN live roster ingest: league=%s teams=%d athletes=%d "
                "club_writes=%d nt_writes=%d",
                canonical_league, lg_stats["teams"], lg_stats["athletes"],
                lg_stats["club_writes"], lg_stats["nt_writes"],
            )
    return stats


__all__ = [
    "refresh_live_rosters",
    "LEAGUE_SLUGS",
]
