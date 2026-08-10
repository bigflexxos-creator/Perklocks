"""Phase 5.1 (2026-08-11) — Universal Full-Roster Identity Ingest.

Populates ``db.player_identities`` with the FULL active-player universe
per sport, independent of which players happen to have picks today.

Sources used (all existing / already proven in the codebase):

    NFL  → ESPN ``site.api.espn.com/apis/site/v2/sports/football/nfl``
    NBA  → ESPN ``site.api.espn.com/apis/site/v2/sports/basketball/nba``
    MLB  → StatsAPI (statsapi.mlb.com — same source as
             services.mlb_hitter_intel / mlb_matchup_resolver)
    NHL  → ESPN ``site.api.espn.com/apis/site/v2/sports/hockey/nhl``
    CFB  → ESPN ``site.api.espn.com/apis/site/v2/sports/football/college-football``
    Soccer → unchanged — services.soccer_identity_ingest owns Soccer
    Tennis → services.tennis_identity (Sackmann DB)
    UFC  → ESPN ``site.api.espn.com/apis/site/v2/sports/mma/ufc``

Every non-Soccer sport writes via the P0-A race-safe `persist_identity`
path.  Soccer's existing pipeline is left completely untouched.

Persistence contract:

    canonical_player_id ← minted deterministically (sport, league,
                          name_norm, provider_id)
    provider_ids        ← {"espn": "<athlete_id>"} for ESPN-sourced;
                          {"mlb_stats": "<mlb_id>"} for MLB
    current_team        ← team from the roster observation
    observed_at         ← now (UTC ISO-8601)
    source              ← "espn_<sport>_athletes" / "statsapi_mlb"
    roster_status       ← ESPN "status.name" lowercased (usually
                          "active")
    position / role     ← ESPN athlete.position / athlete.role
    league              ← "nfl" / "nba" / "mlb" / "nhl" / "cfb" / ...

Race-safe writes via ``services.player_identity.persist_identity`` —
older observations NEVER overwrite fresher current-team fields even
under concurrent multi-replica writes.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

import httpx

from services import player_identity as _pi
from services.player_identity import (
    IDENTITY_COLLECTION, ensure_identity_indexes, persist_identity,
    upsert_player, _norm,
)


logger = logging.getLogger("lockscore.services.universal_identity_ingest")

HTTP_TIMEOUT_S = 20.0
ESPN_HEADERS = {
    "User-Agent": "PerkLocks-AI/1.0 (+ESPN public)",
    "Accept": "application/json",
}

# ── ESPN URL builders (5 team sports) ─────────────────────────
ESPN_TEAMS = {
    "NFL": "https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams?limit=100",
    "NBA": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams?limit=100",
    "NHL": "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/teams?limit=100",
    "CFB": "https://site.api.espn.com/apis/site/v2/sports/football/college-football/teams?limit=400",
}
ESPN_ROSTER = {
    "NFL": "https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams/{tid}/roster",
    "NBA": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{tid}/roster",
    "NHL": "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/teams/{tid}/roster",
    "CFB": "https://site.api.espn.com/apis/site/v2/sports/football/college-football/teams/{tid}/roster",
}
ESPN_MMA_ATHLETES = "https://sports.core.api.espn.com/v3/sports/mma/ufc/athletes"

# MLB StatsAPI — free, public, versioned.
MLB_TEAMS = "https://statsapi.mlb.com/api/v1/teams?sportId=1&activeStatus=Y"
MLB_ROSTER = "https://statsapi.mlb.com/api/v1/teams/{tid}/roster?rosterType=fullRoster"
MLB_PEOPLE = "https://statsapi.mlb.com/api/v1/people/{pid}?hydrate=team"


# ── Provider-id namespaces used per sport ─────────────────
PROVIDER_ESPN = "espn"
PROVIDER_MLB  = "mlb_stats"


async def _http() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=HTTP_TIMEOUT_S, headers=ESPN_HEADERS)


# ═════════════════════════════════════════════════════════
# NFL / NBA / NHL / CFB — shared ESPN roster ingester
# ═════════════════════════════════════════════════════════
async def _ingest_espn_league(
    db, *, sport: str, league_key: str,
) -> dict[str, Any]:
    teams_url = ESPN_TEAMS[sport]
    roster_url_tmpl = ESPN_ROSTER[sport]
    summary = {"sport": sport, "teams": 0, "athletes_seen": 0,
                "persisted": 0, "advanced": 0, "merged": 0,
                "errors": 0}
    now_iso = datetime.now(timezone.utc).isoformat()
    async with await _http() as client:
        try:
            r = await client.get(teams_url)
            r.raise_for_status()
        except Exception as e:
            logger.warning(f"{sport} teams fetch failed: {e}")
            summary["errors"] += 1
            return summary
        teams: list[dict] = []
        for s in r.json().get("sports", []):
            for lg in s.get("leagues", []):
                for e in lg.get("teams", []):
                    t = e.get("team") or {}
                    if t.get("id"):
                        teams.append({
                            "id": t["id"],
                            "name": t.get("displayName")
                                    or t.get("name"),
                        })
        summary["teams"] = len(teams)

        sem = asyncio.Semaphore(6)

        async def _one_team(team: dict) -> None:
            async with sem:
                try:
                    rr = await client.get(
                        roster_url_tmpl.format(tid=team["id"]))
                    rr.raise_for_status()
                except Exception as e:
                    logger.debug(
                        f"{sport} roster fetch fail {team.get('name')}: {e}")
                    summary["errors"] += 1
                    return
                # ESPN returns either `athletes: [ {items: [...]} ]`
                # (grouped by position) or `athletes: [ {...player...} ]`.
                payload = rr.json()
                players_iter = _flatten_espn_athletes(payload)
                for a in players_iter:
                    status = ((a.get("status") or {}).get("name")
                              or "").lower()
                    if status in ("inactive", "suspended"):
                        # We still create the identity — but mark
                        # non-fresh so barrier stays cautious.
                        pass
                    name = a.get("fullName") or a.get("displayName") or ""
                    espn_id = str(a.get("id") or "").strip()
                    if not name or not espn_id:
                        continue
                    position = ((a.get("position") or {}).get("abbreviation")
                                 or None)
                    dob = a.get("dateOfBirth")
                    ident = upsert_player(
                        name=name, sport=sport, league=league_key,
                        provider=PROVIDER_ESPN, provider_id=espn_id,
                        current_team=team["name"], position=position,
                        role=None,
                        roster_status="active" if status not in
                            ("inactive", "suspended") else status,
                        source=f"espn_{sport.lower()}_athletes",
                        observed_at=now_iso, dob=dob,
                        affiliation_type="club",
                    )
                    summary["athletes_seen"] += 1
                    res = await persist_identity(db, ident.to_dict())
                    if res == "inserted":
                        summary["persisted"] += 1
                    elif res == "advanced":
                        summary["advanced"] += 1
                    elif res == "merged_only":
                        summary["merged"] += 1

        await asyncio.gather(*(_one_team(t) for t in teams))
    return summary


def _flatten_espn_athletes(payload: dict) -> list[dict]:
    """ESPN roster shape varies by sport — flatten to a single list."""
    ath = payload.get("athletes") or []
    out: list[dict] = []
    for entry in ath:
        # Football/hockey/basketball: nested items[] by position group.
        items = entry.get("items")
        if isinstance(items, list) and items:
            out.extend(items)
        else:
            # Direct athlete row.
            if entry.get("id") or entry.get("fullName"):
                out.append(entry)
    return out


# ═════════════════════════════════════════════════════════
# MLB — StatsAPI ingester
# ═════════════════════════════════════════════════════════
async def _ingest_mlb(db) -> dict[str, Any]:
    summary = {"sport": "MLB", "teams": 0, "athletes_seen": 0,
                "persisted": 0, "advanced": 0, "merged": 0,
                "errors": 0}
    now_iso = datetime.now(timezone.utc).isoformat()
    async with await _http() as client:
        try:
            r = await client.get(MLB_TEAMS)
            r.raise_for_status()
        except Exception as e:
            logger.warning(f"MLB teams fetch failed: {e}")
            summary["errors"] += 1
            return summary
        teams = [{"id": t["id"], "name": t.get("name")}
                  for t in r.json().get("teams", [])]
        summary["teams"] = len(teams)

        sem = asyncio.Semaphore(4)

        async def _one_team(team: dict) -> None:
            async with sem:
                try:
                    rr = await client.get(
                        MLB_ROSTER.format(tid=team["id"]))
                    rr.raise_for_status()
                except Exception as e:
                    logger.debug(
                        f"MLB roster fetch fail {team['name']}: {e}")
                    summary["errors"] += 1
                    return
                for entry in rr.json().get("roster", []):
                    person = entry.get("person") or {}
                    pid = str(person.get("id") or "").strip()
                    name = person.get("fullName") or ""
                    if not pid or not name:
                        continue
                    position = ((entry.get("position") or {}).get(
                        "abbreviation") or None)
                    ident = upsert_player(
                        name=name, sport="MLB", league="mlb",
                        provider=PROVIDER_MLB, provider_id=pid,
                        current_team=team["name"], position=position,
                        role=None, roster_status="active",
                        source="statsapi_mlb",
                        observed_at=now_iso,
                        affiliation_type="club",
                    )
                    summary["athletes_seen"] += 1
                    res = await persist_identity(db, ident.to_dict())
                    if res == "inserted":
                        summary["persisted"] += 1
                    elif res == "advanced":
                        summary["advanced"] += 1
                    elif res == "merged_only":
                        summary["merged"] += 1

        await asyncio.gather(*(_one_team(t) for t in teams))
    return summary


# ═════════════════════════════════════════════════════════
# UFC — ESPN /athletes endpoint (list of fighters)
# ═════════════════════════════════════════════════════════
async def _ingest_ufc(db) -> dict[str, Any]:
    summary = {"sport": "UFC", "athletes_seen": 0,
                "persisted": 0, "advanced": 0, "merged": 0,
                "errors": 0}
    now_iso = datetime.now(timezone.utc).isoformat()
    async with await _http() as client:
        # V3 athletes endpoint — paginated 1000/page.
        page = 1
        while True:
            try:
                r = await client.get(
                    ESPN_MMA_ATHLETES,
                    params={"limit": "1000", "active": "true",
                             "page": str(page)})
                r.raise_for_status()
            except Exception as e:
                logger.warning(f"UFC athletes page={page} failed: {e}")
                summary["errors"] += 1
                break
            payload = r.json()
            items = payload.get("items") or []
            if not items:
                break
            for a in items:
                name = a.get("fullName") or a.get("displayName") or ""
                aid = str(a.get("id") or "").strip()
                if not name or not aid:
                    continue
                # v3 shape: weight / height are numeric; weightClass
                # is not present here — enrich lazily elsewhere.
                stance = None
                division = None
                if isinstance(a.get("weightClass"), dict):
                    division = a["weightClass"].get("name")
                if isinstance(a.get("stance"), dict):
                    stance = a["stance"].get("displayName")
                ident = upsert_player(
                    name=name, sport="UFC", league="ufc",
                    provider="espn_mma_id", provider_id=aid,
                    current_team=None, position=division,
                    role="fighter", roster_status="active",
                    source="espn_mma_athletes",
                    observed_at=now_iso,
                    affiliation_type="club",
                )
                summary["athletes_seen"] += 1
                doc = ident.to_dict()
                if stance:
                    doc["stance"] = stance
                if division:
                    doc["division"] = division
                res = await persist_identity(db, doc)
                if res == "inserted":
                    summary["persisted"] += 1
                elif res == "advanced":
                    summary["advanced"] += 1
                elif res == "merged_only":
                    summary["merged"] += 1
            # Stop after last page.
            page_count = payload.get("pageCount") or 1
            if page >= page_count:
                break
            page += 1
    return summary


# ═════════════════════════════════════════════════════════
# Tennis — Sackmann DB (already ingested by tennis_identity)
# ═════════════════════════════════════════════════════════
async def _ingest_tennis(db) -> dict[str, Any]:
    """Tennis identities: individual sport with fixture-participant
    resolution — Universal Barrier resolves Tennis picks by matching
    the pick's player against the event participants directly (see
    ``services.universal_publication_barrier._validate_individual_sport``).

    A full ATP/WTA registry pre-load is available if
    ``db.player_db_tennis`` has been seeded (Sackmann integer id
    corpus).  When present we snapshot those rows into
    ``player_identities`` so restart hydration is uniform across
    sports; when absent we report the participant-resolution mode.
    """
    summary = {"sport": "Tennis",
                "mode": "individual_sport_participant_resolution",
                "athletes_seen": 0,
                "persisted": 0, "advanced": 0, "merged": 0,
                "errors": 0}
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        n = await db.player_db_tennis.count_documents({})
    except Exception:
        n = 0
    if n == 0:
        # No Sackmann seed available — barrier still resolves Tennis
        # picks via participant matching.  Emit a note so the review
        # audit reflects the mode honestly.
        summary["notes"] = (
            "player_db_tennis is empty on this environment; Tennis "
            "identity resolution flows through fixture-participant "
            "matching in the Universal Barrier.  No pre-load performed."
        )
        return summary
    async for row in db.player_db_tennis.find(
        {}, {"_id": 0, "name": 1, "first_name": 1, "last_name": 1,
             "player_id": 1, "tour": 1, "nationality": 1, "hand": 1}):
        name = row.get("name") or " ".join(
            filter(None, [row.get("first_name"), row.get("last_name")]))
        pid = row.get("player_id")
        tour = (row.get("tour") or "").upper() or None
        nationality = row.get("nationality")
        handedness = row.get("hand")
        if not name or not pid:
            continue
        ident = upsert_player(
            name=name, sport="Tennis",
            league="atp" if tour == "ATP" else (
                "wta" if tour == "WTA" else "tennis"),
            provider="sackmann_id", provider_id=str(pid),
            current_team=None, position=None, role="player",
            roster_status="active",
            source="sackmann_player_db_tennis",
            observed_at=now_iso, nationality=nationality,
            affiliation_type="club",
        )
        doc = ident.to_dict()
        if tour:
            doc["tour"] = tour
        if handedness:
            doc["handedness"] = handedness
        summary["athletes_seen"] += 1
        res = await persist_identity(db, doc)
        if res == "inserted":
            summary["persisted"] += 1
        elif res == "advanced":
            summary["advanced"] += 1
        elif res == "merged_only":
            summary["merged"] += 1
    return summary


# ═════════════════════════════════════════════════════════
# Public façade
# ═════════════════════════════════════════════════════════
async def ingest_all(
    db, *, sports: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Populate ``db.player_identities`` for every requested sport.
    Idempotent + restart-safe.  Soccer is intentionally excluded — its
    P0-A..P0-E pipeline owns Soccer identities.
    """
    await ensure_identity_indexes(db)
    order = sports or ["NFL", "NBA", "NHL", "CFB", "MLB", "UFC", "Tennis"]
    result: dict[str, Any] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "sports": {},
    }
    for sp in order:
        try:
            if sp in ("NFL", "NBA", "NHL", "CFB"):
                result["sports"][sp] = await _ingest_espn_league(
                    db, sport=sp, league_key=sp.lower())
            elif sp == "MLB":
                result["sports"][sp] = await _ingest_mlb(db)
            elif sp == "UFC":
                result["sports"][sp] = await _ingest_ufc(db)
            elif sp == "Tennis":
                result["sports"][sp] = await _ingest_tennis(db)
        except Exception as e:
            logger.exception(f"{sp} ingest failed: {e}")
            result["sports"][sp] = {"sport": sp, "errors": 1,
                                     "error_message": str(e)}
    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    return result


__all__ = [
    "ingest_all",
    "_ingest_espn_league", "_ingest_mlb",
    "_ingest_ufc", "_ingest_tennis",
]
