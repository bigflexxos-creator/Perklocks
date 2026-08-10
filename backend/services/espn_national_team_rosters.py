"""P0-E (2026-08-11) — Dedicated current national-team roster ingest.

Fetches ACTUAL squad rosters (call-ups) for every national team from
ESPN's public confederation/friendly endpoints — the strongest signal
for "who is currently playing for this country."

This replaces the P0-D behaviour of inferring national-team membership
from ESPN's `citizenship` field (which reflects passport and can be
wrong — e.g. Endrick shows citizenship="Portugal" while he plays for
Brazil).  Instead:

  * ``current_national_team`` is now written ONLY by this module and
    the P0-C curated bootstrap (fallback).
  * The P0-D live club-roster ingester writes ONLY ``nationality``
    from ESPN citizenship — the validator treats it as WEAK evidence
    that never causes a hard team_mismatch on its own.

Confederations covered:
  * fifa.worldq.uefa      — European WC qualifying squads
  * fifa.worldq.conmebol  — South American WC qualifying squads
  * fifa.worldq.concacaf  — North/Central America + Caribbean
  * fifa.worldq.afc       — Asia
  * fifa.worldq.caf       — Africa
  * fifa.worldq.ofc       — Oceania
  * fifa.friendly         — global fallback (fills any gaps)

Callable from server startup + periodic refresh loop.  All writes
flow through the P0-A race-safe ``persist_identity`` layer.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

logger = logging.getLogger("lockscore.espn_national_team_rosters")

CONFEDERATION_SLUGS: list[str] = [
    "fifa.worldq.uefa",
    "fifa.worldq.conmebol",
    "fifa.worldq.concacaf",
    "fifa.worldq.afc",
    "fifa.worldq.caf",
    "fifa.worldq.ofc",
    "fifa.friendly",       # fallback — global
]

_TEAMS_URL_TMPL  = ("https://site.api.espn.com/apis/site/v2/sports/soccer/"
                    "{slug}/teams")
_ROSTER_URL_TMPL = ("https://site.api.espn.com/apis/site/v2/sports/soccer/"
                    "{slug}/teams/{tid}/roster")

_LIVE_NT_SOURCE = "espn_national_team_roster"


async def _get_json(cx: httpx.AsyncClient, url: str) -> Optional[dict]:
    try:
        r = await cx.get(url)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        logger.debug("ESPN NT GET %s failed: %s", url, e)
    return None


async def _fetch_confed_teams(
    cx: httpx.AsyncClient, slug: str,
) -> list[tuple[str, str]]:
    blob = await _get_json(cx, _TEAMS_URL_TMPL.format(slug=slug))
    if not blob:
        return []
    lgs = (blob.get("sports") or [{}])[0].get("leagues") or [{}]
    teams = lgs[0].get("teams", []) if lgs else []
    out = []
    for t in teams:
        tm = t.get("team") or {}
        tid = tm.get("id")
        name = tm.get("displayName") or tm.get("name")
        if tid and name:
            out.append((str(tid), name))
    return out


async def _fetch_nt_roster(
    cx: httpx.AsyncClient, slug: str, tid: str,
) -> list[dict]:
    blob = await _get_json(cx, _ROSTER_URL_TMPL.format(slug=slug, tid=tid))
    if not blob:
        return []
    return blob.get("athletes", []) or []


async def _upsert_national_team_player(
    db, *, country: str, athlete: dict, now_iso: str,
) -> bool:
    from services.player_identity import (
        upsert_player, persist_identity, _norm as _pnorm,
    )
    espn_id = athlete.get("id")
    display = (athlete.get("displayName")
                or athlete.get("fullName")
                or f"{athlete.get('firstName','')} "
                    f"{athlete.get('lastName','')}").strip()
    if not display:
        return False
    ident = upsert_player(
        name=display, sport="Soccer", league="International",
        provider="espn",
        provider_id=(str(espn_id) if espn_id
                     else f"nt:{_pnorm(display)}:{_pnorm(country)}"),
        current_team=country,           # → current_national_team
        affiliation_type="national_team",
        source=_LIVE_NT_SOURCE,
        observed_at=now_iso,
        roster_status="active",
        nationality=country,
    )
    outcome = await persist_identity(db, ident.to_dict())
    return outcome in ("inserted", "advanced", "merged_only")


async def refresh_national_team_rosters(
    db, *,
    confederations: Optional[list[str]] = None,
    max_concurrency: int = 4,
    request_timeout: float = 20.0,
) -> dict[str, Any]:
    """Fetch every confederation's national teams + rosters and
    upsert authoritative ``current_national_team`` records.

    Returns a diagnostic dict:
    ``{observed_at, confederations: {slug: {teams, rosters_with_players}},
       nations_covered, rosters_written}``
    """
    from services.player_identity import ensure_identity_indexes
    await ensure_identity_indexes(db)

    slugs = confederations or CONFEDERATION_SLUGS
    now_iso = datetime.now(timezone.utc).isoformat()

    stats: dict[str, Any] = {
        "observed_at": now_iso,
        "confederations": {}, "nations_covered": 0,
        "rosters_written": 0,
    }

    seen_countries: set[str] = set()
    # Phase 5.2.1 (2026-08-11) — the earlier "skip once seen" logic
    # excluded fifa.friendly players who weren't in the WCQ squad
    # (e.g. Lawrence Shankland was called up in a friendly but not
    # UEFA WCQ).  We now MERGE across confederations so friendly
    # players are added on top of WCQ players — persist_identity
    # is idempotent so duplicate writes are safe.
    merge_across_confederations = True
    sem = asyncio.Semaphore(max_concurrency)

    async with httpx.AsyncClient(timeout=request_timeout) as cx:
        for slug in slugs:
            teams = await _fetch_confed_teams(cx, slug)
            cs = {"teams": len(teams), "rosters_with_players": 0,
                  "writes": 0}
            if not teams:
                stats["confederations"][slug] = cs
                continue

            async def _proc(tid_country):
                tid, country = tid_country
                # Phase 5.2.1 — MERGE across confederations.  Earlier
                # slug (WCQ) writes are the strongest signal but a
                # later slug (fifa.friendly) may include players not
                # in the WCQ squad — those must be added, not skipped.
                if not merge_across_confederations \
                        and country.lower() in seen_countries:
                    return {"players": 0, "writes": 0}
                async with sem:
                    roster = await _fetch_nt_roster(cx, slug, tid)
                if not roster:
                    return {"players": 0, "writes": 0}
                writes = 0
                for a in roster:
                    ok = await _upsert_national_team_player(
                        db, country=country, athlete=a, now_iso=now_iso,
                    )
                    if ok: writes += 1
                # Mark covered so subsequent confederations don't
                # re-fetch.
                if writes:
                    seen_countries.add(country.lower())
                return {"players": len(roster), "writes": writes}

            results = await asyncio.gather(*(_proc(t) for t in teams))
            for r in results:
                if r["players"] > 0:
                    cs["rosters_with_players"] += 1
                    cs["writes"] += r["writes"]
            stats["confederations"][slug] = cs
            stats["nations_covered"] += cs["rosters_with_players"]
            stats["rosters_written"] += cs["writes"]
            logger.info(
                "ESPN NT roster ingest: confed=%s teams=%d "
                "rosters=%d writes=%d",
                slug, cs["teams"], cs["rosters_with_players"], cs["writes"],
            )
    return stats


__all__ = [
    "refresh_national_team_rosters",
    "CONFEDERATION_SLUGS",
]
