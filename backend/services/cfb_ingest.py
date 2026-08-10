"""College Football ingestion via CollegeFootballData.com (free tier).

WHY THIS EXISTS
---------------
The user explicitly asked (2026-06-28):
   "P1: Integrate CFB-specific data (CollegeFootballData.com API) for
    Returning Production, Portal, and Strength of Schedule analysis."

Every other sport (NBA/NFL/MLB/Soccer) now has a free-tier ingestion
+ active-registry + rationale pipeline. CFB is the last sport on the
betting board without one, so when CFB picks land they have no
"Why this pick?" rationale beyond raw win-prob math.

WHAT THIS DOES
--------------
Polls the public CFBD REST API (https://api.collegefootballdata.com)
and caches FOUR datasets into Mongo:

  cfb_returning_production
      Per-team % of last season's PPA returning. Quick proxy for
      "how much of last year's production is back?".

  cfb_portal
      Transfer portal entries: who left where, who went where,
      with star/rating where known. Lets us answer "did this team
      lose its top WR to the portal?".

  cfb_sp_ratings
      SP+ ratings — overall rank, offense rank, defense rank, SoS.
      The de-facto pre-season power metric. SoS is the answer to
      "did they beat anyone real?".

  cfb_teams
      Team metadata (id, conference, mascot, abbreviation) so the
      rationale builder can resolve "Ohio State" / "OSU" / "Buckeyes"
      to one identity.

Auth: Bearer token from `CFBD_API_KEY` env var (free tier — sign up at
https://collegefootballdata.com/key).

CALL PATTERN
------------
Caller (typically the daily refresh job in `server.py`) invokes::

    await cfb_ingest.refresh_all(db)

It's idempotent — re-running just rewrites the same cache. Indexes are
created lazily so first-run on a fresh Mongo works.

This module is INGESTION ONLY. Rationale generation lives in
`services.cfb_rationale` and the wire-up into the pick enrichment
pipeline lives in `pick_enrichment.py`.

Author: PerkLocks AI · 2026-06-28
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

logger = logging.getLogger("lockscore.cfb.ingest")

CFBD_BASE = "https://api.collegefootballdata.com"


def _current_cfb_year() -> int:
    """Phase 2 (2026-08-11) — dynamic season resolution.

    CFB seasons run August → January.  CFBD publishes upcoming-season
    pre-game data under the *previous* calendar year until the new
    season kicks off.  We resolve the current data year at call time
    so the pipeline doesn't stall on a hardcoded value every August.

    Rule of thumb:
      * Aug 1 or later → use current calendar year
      * Jan 1–Jul 31   → use previous calendar year (bowl/off-season)
    """
    now = datetime.now(timezone.utc)
    return now.year if now.month >= 8 else now.year - 1


# Backwards-compat: keep the CURRENT_YEAR symbol resolved dynamically
# at import time.  Any long-running process that survives a season
# rollover should also call `_current_cfb_year()` at fetch time to
# stay accurate.
CURRENT_YEAR = _current_cfb_year()


def _headers() -> dict[str, str]:
    key = os.environ.get("CFBD_API_KEY", "")
    if not key:
        raise RuntimeError(
            "CFBD_API_KEY not set in env — register a free key at "
            "https://collegefootballdata.com/key"
        )
    return {
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "User-Agent": "PerkLocks/1.0 (CFB ingest)",
    }


async def _get(client: httpx.AsyncClient, path: str) -> list[dict[str, Any]]:
    """Single-attempt GET with simple 5s retry on transient 5xx."""
    for attempt in (1, 2):
        try:
            r = await client.get(f"{CFBD_BASE}{path}", headers=_headers(), timeout=20)
            if r.status_code == 200:
                return r.json() or []
            if r.status_code in (502, 503, 504) and attempt == 1:
                logger.warning("CFBD %s -> %d, retrying", path, r.status_code)
                await asyncio.sleep(5)
                continue
            logger.warning("CFBD %s -> %d: %s", path, r.status_code, r.text[:200])
            return []
        except Exception as e:
            if attempt == 1:
                logger.warning("CFBD %s -> %s, retrying", path, e)
                await asyncio.sleep(5)
                continue
            logger.warning("CFBD %s failed twice: %s", path, e)
            return []
    return []


# ─── Ensure indexes ──────────────────────────────────────────────────
async def ensure_indexes(db) -> None:
    """Create the small set of unique / lookup indexes we rely on.
    Safe to re-run."""
    try:
        await db.cfb_returning_production.create_index(
            [("season", 1), ("team", 1)], unique=True, background=True)
        await db.cfb_portal.create_index(
            [("season", 1), ("player_key", 1), ("destination", 1)],
            unique=True, background=True)
        await db.cfb_sp_ratings.create_index(
            [("year", 1), ("team", 1)], unique=True, background=True)
        await db.cfb_teams.create_index(
            [("school", 1)], unique=True, background=True)
    except Exception as e:
        logger.debug("CFB index create failed (likely race): %s", e)


# ─── Returning production ────────────────────────────────────────────
async def fetch_returning_production(db, year: int = CURRENT_YEAR) -> int:
    """Returns # of teams written."""
    async with httpx.AsyncClient() as client:
        rows = await _get(client, f"/player/returning?year={year}")
    if not rows:
        return 0
    ops = []
    for r in rows:
        team = r.get("team")
        if not team:
            continue
        ops.append((
            {"season": year, "team": team},
            {"$set": {
                "season": year,
                "team": team,
                "conference": r.get("conference"),
                "total_ppa": r.get("totalPPA"),
                "percent_ppa": r.get("percentPPA"),                       # 0..1
                "percent_passing_ppa": r.get("percentPassingPPA"),
                "percent_receiving_ppa": r.get("percentReceivingPPA"),
                "percent_rushing_ppa": r.get("percentRushingPPA"),
                "usage": r.get("usage"),
                "passing_usage": r.get("passingUsage"),
                "receiving_usage": r.get("receivingUsage"),
                "rushing_usage": r.get("rushingUsage"),
                "_fetched_at": datetime.now(timezone.utc).isoformat(),
            }},
        ))
    for filt, upd in ops:
        await db.cfb_returning_production.update_one(filt, upd, upsert=True)
    logger.info("CFB returning_production: %d teams ingested for %d", len(ops), year)
    return len(ops)


# ─── Transfer portal ─────────────────────────────────────────────────
def _portal_key(row: dict) -> str:
    """Stable identity for a portal entry so we can upsert."""
    first = (row.get("firstName") or "").strip().lower()
    last = (row.get("lastName") or "").strip().lower()
    pos = (row.get("position") or "").strip().upper()
    origin = (row.get("origin") or "").strip()
    return f"{first}|{last}|{pos}|{origin}"


async def fetch_portal(db, year: int = CURRENT_YEAR) -> int:
    async with httpx.AsyncClient() as client:
        rows = await _get(client, f"/player/portal?year={year}")
    if not rows:
        return 0
    n = 0
    for r in rows:
        dest = r.get("destination")
        if not dest:
            continue
        key = _portal_key(r)
        first = (r.get("firstName") or "").strip()
        last = (r.get("lastName") or "").strip()
        await db.cfb_portal.update_one(
            {"season": year, "player_key": key, "destination": dest},
            {"$set": {
                "season": year,
                "player_key": key,
                "first_name": first,
                "last_name": last,
                "full_name": f"{first} {last}".strip(),
                "position": r.get("position"),
                "origin": r.get("origin"),
                "destination": dest,
                "transfer_date": r.get("transferDate"),
                "rating": r.get("rating"),
                "stars": r.get("stars"),
                "eligibility": r.get("eligibility"),
                "_fetched_at": datetime.now(timezone.utc).isoformat(),
            }},
            upsert=True,
        )
        n += 1
    logger.info("CFB portal: %d transfers ingested for %d", n, year)
    return n


# ─── SP+ ratings (incl. SoS) ─────────────────────────────────────────
async def fetch_sp_ratings(db, year: int = CURRENT_YEAR) -> int:
    """SP+ is CFBD's headline preseason metric — overall rank, offense
    rank, defense rank, second-order wins, and strength of schedule.

    NB: the field naming on the API is inconsistent: `sos` lives on the
    root object some years and inside `.specialTeams` on others. We
    flatten everything our rationale builder might need."""
    async with httpx.AsyncClient() as client:
        rows = await _get(client, f"/ratings/sp?year={year}")
    if not rows:
        return 0
    n = 0
    for r in rows:
        team = r.get("team")
        if not team:
            continue
        offense = r.get("offense") or {}
        defense = r.get("defense") or {}
        await db.cfb_sp_ratings.update_one(
            {"year": year, "team": team},
            {"$set": {
                "year": year,
                "team": team,
                "conference": r.get("conference"),
                "rating": r.get("rating"),
                "ranking": r.get("ranking"),
                "second_order_wins": r.get("secondOrderWins"),
                "sos": r.get("sos"),
                "offense_rank": offense.get("ranking"),
                "offense_rating": offense.get("rating"),
                "defense_rank": defense.get("ranking"),
                "defense_rating": defense.get("rating"),
                "_fetched_at": datetime.now(timezone.utc).isoformat(),
            }},
            upsert=True,
        )
        n += 1
    logger.info("CFB SP+ ratings: %d teams ingested for %d", n, year)
    return n


# ─── Team metadata ───────────────────────────────────────────────────
async def fetch_teams(db, year: int = CURRENT_YEAR) -> int:
    async with httpx.AsyncClient() as client:
        rows = await _get(client, f"/teams/fbs?year={year}")
    if not rows:
        return 0
    n = 0
    for r in rows:
        school = r.get("school")
        if not school:
            continue
        await db.cfb_teams.update_one(
            {"school": school},
            {"$set": {
                "school": school,
                "cfbd_id": r.get("id"),
                "mascot": r.get("mascot"),
                "abbreviation": r.get("abbreviation"),
                "alternate_names": r.get("alternateNames") or [],
                "conference": r.get("conference"),
                "division": r.get("division"),
                "classification": r.get("classification"),
                "color": r.get("color"),
                "_fetched_at": datetime.now(timezone.utc).isoformat(),
            }},
            upsert=True,
        )
        n += 1
    logger.info("CFB teams: %d FBS schools ingested for %d", n, year)
    return n


# ─── Public entry point ──────────────────────────────────────────────
async def refresh_all(db, year: Optional[int] = None) -> dict[str, int]:
    """Refresh every CFB cache. Returns per-dataset row counts.
    Safe to call from the daily scheduler or admin route."""
    if year is None:
        year = CURRENT_YEAR
    await ensure_indexes(db)
    counts = {"year": year}
    try:
        counts["teams"] = await fetch_teams(db, year)
    except Exception as e:
        logger.warning("CFB teams fetch failed: %s", e)
        counts["teams"] = 0
    try:
        counts["returning_production"] = await fetch_returning_production(db, year)
    except Exception as e:
        logger.warning("CFB returning_production fetch failed: %s", e)
        counts["returning_production"] = 0
    try:
        counts["portal"] = await fetch_portal(db, year)
    except Exception as e:
        logger.warning("CFB portal fetch failed: %s", e)
        counts["portal"] = 0
    try:
        counts["sp_ratings"] = await fetch_sp_ratings(db, year)
    except Exception as e:
        logger.warning("CFB sp_ratings fetch failed: %s", e)
        counts["sp_ratings"] = 0
    return counts


async def loop(db, interval_hours: int = 24) -> None:
    """Background scheduler loop. Sleeps a short cold-start, then runs
    `refresh_all` every `interval_hours`. Survives transient failures
    so a one-off CFBD outage doesn't take the loop down."""
    import asyncio
    await asyncio.sleep(45)  # cold-start grace period
    while True:
        try:
            counts = await refresh_all(db)
            logger.info("CFB ingest cycle: %s", counts)
        except Exception as e:
            logger.warning("CFB ingest cycle failed: %s", e)
        await asyncio.sleep(interval_hours * 3600)


# ─── Lookup helpers used by cfb_rationale.py ─────────────────────────
async def get_team_record(db, team: str, year: Optional[int] = None) -> dict[str, Any]:
    """Returns {'returning':..., 'sp':..., 'team':...} for a school
    name. Tries exact match first, then alternate_names. Empty dict
    on miss so the caller can degrade gracefully."""
    if year is None:
        year = CURRENT_YEAR
    out: dict[str, Any] = {}
    if not team:
        return out
    # Resolve canonical school name (handles "Ohio State"/"OSU"/"Buckeyes")
    school = team
    t_doc = await db.cfb_teams.find_one({"school": team})
    if not t_doc:
        t_doc = await db.cfb_teams.find_one({"alternate_names": team})
        if t_doc:
            school = t_doc.get("school", team)
    if t_doc:
        out["team"] = {k: v for k, v in t_doc.items() if k != "_id"}
    rp = await db.cfb_returning_production.find_one({"season": year, "team": school})
    if rp:
        out["returning"] = {k: v for k, v in rp.items() if k != "_id"}
    sp = await db.cfb_sp_ratings.find_one({"year": year, "team": school})
    if sp:
        out["sp"] = {k: v for k, v in sp.items() if k != "_id"}
    return out


async def get_portal_entry(
    db, full_name: str, year: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    """Looks up a player's portal entry by full name. Returns None if
    the player isn't in the portal."""
    if year is None:
        year = CURRENT_YEAR
    if not full_name or not full_name.strip():
        return None
    fn = full_name.strip()
    doc = await db.cfb_portal.find_one({"season": year, "full_name": fn})
    if doc:
        return {k: v for k, v in doc.items() if k != "_id"}
    # Case-insensitive fallback
    doc = await db.cfb_portal.find_one(
        {"season": year, "full_name": {"$regex": f"^{fn}$", "$options": "i"}}
    )
    return {k: v for k, v in doc.items() if k != "_id"} if doc else None
