"""MLB Stats API ingestor — free, no-key, official MLB.com data.

Pulls:
  • All 30 teams → all active rosters → ~1,200 active players
  • Per-player season stats (batting OR pitching depending on position)
  • Current 7/10/15/60-day injured-list status

Usage: call `refresh_all()` once per day. Idempotent (upserts).
Quota: zero — MLB Stats API has no rate limit for our volume
(<2k requests per refresh, completes in ~90 seconds).
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger("lockscore.player_db.mlb")

_BASE = "https://statsapi.mlb.com/api/v1"
_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
# Bounded concurrency so we don't open 1,200 sockets at once.
_SEM = asyncio.Semaphore(20)


async def _get(client: httpx.AsyncClient, path: str, params: dict | None = None) -> dict | None:
    """GET with bounded concurrency. Returns parsed JSON or None on error
    (never raises — ingestion is best-effort)."""
    async with _SEM:
        try:
            r = await client.get(f"{_BASE}{path}", params=params, timeout=_TIMEOUT)
            if r.status_code == 200:
                return r.json()
            logger.warning("MLB Stats API %s → HTTP %d", path, r.status_code)
        except Exception as e:
            logger.warning("MLB Stats API %s exception: %s", path, e)
        return None


def _canonical(name: str) -> str:
    """Lowercased, whitespace-trimmed name used as the join key against
    pick.player. Mirrors the canonicalization in sports_engine so the
    same canonical_name finds the same player no matter the source."""
    return (name or "").strip().lower()


async def _fetch_teams(client: httpx.AsyncClient) -> list[dict]:
    """All 30 MLB teams. Tiny payload, called once per refresh."""
    data = await _get(client, "/teams", params={"sportId": 1})
    return (data or {}).get("teams", []) if data else []


async def _fetch_roster(client: httpx.AsyncClient, team_id: int) -> list[dict]:
    """Active roster for one team. Returns 25-40 players per team."""
    data = await _get(
        client,
        f"/teams/{team_id}/roster",
        params={"rosterType": "active"},
    )
    return (data or {}).get("roster", []) if data else []


async def _fetch_person(client: httpx.AsyncClient, person_id: int) -> dict | None:
    """Full bio for one player — birthDate, height, weight, position,
    photo URL, current team, injuredList status."""
    data = await _get(client, f"/people/{person_id}")
    people = (data or {}).get("people") or []
    return people[0] if people else None


async def _fetch_season_stats(client: httpx.AsyncClient, person_id: int, season: int) -> dict | None:
    """Season aggregates. Returns batting + pitching for the requested
    season. Pitchers will have empty batting (NL has DH now). Hitters
    return empty pitching."""
    # group=hitting,pitching gets both in one call
    data = await _get(
        client,
        f"/people/{person_id}/stats",
        params={
            "stats": "season",
            "season": season,
            "group": "hitting,pitching",
        },
    )
    out = {"batting": None, "pitching": None}
    for grp in (data or {}).get("stats") or []:
        split_type = (grp.get("group") or {}).get("displayName", "").lower()
        splits = grp.get("splits") or []
        if not splits:
            continue
        stat = splits[0].get("stat") or {}
        if "hit" in split_type:
            out["batting"] = stat
        elif "pitch" in split_type:
            out["pitching"] = stat
    return out


async def _ingest_one_player(
    client: httpx.AsyncClient,
    db: AsyncIOMotorDatabase,
    team: dict,
    roster_row: dict,
    season: int,
) -> tuple[bool, str]:
    """Upsert one player + their season stats. Returns (ok, status_label)."""
    person_id = (roster_row.get("person") or {}).get("id")
    name = (roster_row.get("person") or {}).get("fullName") or ""
    if not person_id or not name:
        return False, "missing_person"
    bio = await _fetch_person(client, person_id)
    if not bio:
        return False, "no_bio"

    # Position — prefer detailed position from the roster row; fall back
    # to bio.primaryPosition.
    pos_obj = roster_row.get("position") or bio.get("primaryPosition") or {}
    position = pos_obj.get("abbreviation") or pos_obj.get("code")

    # Status — "Active" / "Injured List 10-Day" / etc.
    status_obj = bio.get("currentTeamStatus") or {}
    status_desc = (
        (roster_row.get("status") or {}).get("description")
        or status_obj.get("description")
        or "Active"
    )

    player_doc = {
        "sport": "mlb",
        # Set both mlb_id and player_id — legacy Player Intelligence module
        # already enforces a unique index on (player_id, sport); reusing
        # the same field keeps both writers compatible on the same docs.
        "mlb_id": person_id,
        "player_id": person_id,
        "name": name,
        "canonical_name": _canonical(name),
        "first_name": bio.get("firstName"),
        "last_name": bio.get("lastName"),
        "team": team.get("abbreviation") or team.get("name"),
        "team_id": team.get("id"),
        "team_name": team.get("name"),
        "position": position,
        "bats": bio.get("batSide", {}).get("code"),
        "throws": bio.get("pitchHand", {}).get("code"),
        "birth_date": bio.get("birthDate"),
        "height": bio.get("height"),
        "weight_lb": bio.get("weight"),
        "photo_url": f"https://midfield.mlbstatic.com/v1/people/{person_id}/spots/120",
        "status": status_desc,
        "active": (roster_row.get("status") or {}).get("code", "A") == "A",
        "source": "mlb_stats_api",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    # Upsert by (sport, player_id) so we merge with any legacy
    # Player Intelligence doc rather than triggering the unique-index
    # 11000 collision. player_id and mlb_id carry the same value
    # (MLB Stats API person_id) for MLB.
    await db.players.update_one(
        {"sport": "mlb", "player_id": person_id},
        {"$set": player_doc, "$setOnInsert": {"created_at": datetime.now(timezone.utc).isoformat()}},
        upsert=True,
    )

    # Season stats — separate collection so the players doc stays small.
    stats = await _fetch_season_stats(client, person_id, season)
    if stats and (stats.get("batting") or stats.get("pitching")):
        await db.player_stats.update_one(
            {"sport": "mlb", "mlb_id": person_id, "season": season},
            {"$set": {
                "sport": "mlb",
                "mlb_id": person_id,
                "canonical_name": _canonical(name),
                "season": season,
                "batting": stats.get("batting"),
                "pitching": stats.get("pitching"),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }},
            upsert=True,
        )

    # Injury surface — promote the IL status into the injuries collection
    # so client.find_injury() can resolve it in one query.
    is_injured = "injured list" in (status_desc or "").lower()
    if is_injured:
        await db.injuries.update_one(
            {"sport": "mlb", "mlb_id": person_id},
            {"$set": {
                "sport": "mlb",
                "mlb_id": person_id,
                "canonical_name": _canonical(name),
                "status": status_desc,
                "team": team.get("abbreviation"),
                "source": "mlb_stats_api",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }},
            upsert=True,
        )
    else:
        # Clear stale IL doc if the player is now active again.
        await db.injuries.delete_one({"sport": "mlb", "mlb_id": person_id})

    return True, ("injured" if is_injured else "active")


async def refresh_all(db: AsyncIOMotorDatabase, season: int | None = None) -> dict:
    """Full refresh: all 30 teams → ~1,200 players → bios + season stats
    + injury status. Returns counters for logging."""
    season = season or datetime.now(timezone.utc).year
    started = time.time()

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        teams = await _fetch_teams(client)
        if not teams:
            return {"ok": False, "reason": "teams_fetch_failed"}

        # Pull every team's roster in parallel (30 calls)
        roster_pairs: list[tuple[dict, list[dict]]] = []
        roster_results = await asyncio.gather(
            *[_fetch_roster(client, t["id"]) for t in teams],
            return_exceptions=False,
        )
        for team, roster in zip(teams, roster_results):
            roster_pairs.append((team, roster))

        # Then upsert every player + their stats in parallel (bounded by _SEM)
        tasks = []
        for team, roster in roster_pairs:
            for row in roster:
                tasks.append(_ingest_one_player(client, db, team, row, season))
        results = await asyncio.gather(*tasks, return_exceptions=True)

    ok = sum(1 for r in results if isinstance(r, tuple) and r[0])
    injured = sum(1 for r in results if isinstance(r, tuple) and r[1] == "injured")
    errors = sum(1 for r in results if isinstance(r, Exception))

    # Make sure our hot indexes exist (cheap; no-op if already present).
    await _ensure_indexes(db)

    elapsed = round(time.time() - started, 1)
    summary = {
        "ok": True,
        "season": season,
        "teams": len(teams),
        "players_upserted": ok,
        "injured": injured,
        "errors": errors,
        "elapsed_sec": elapsed,
    }
    logger.info("MLB player_db refresh: %s", summary)
    return summary


async def _ensure_indexes(db: AsyncIOMotorDatabase) -> None:
    """Hot-path indexes for find_player / find_injury / enrich_profile.
    `mlb_id` uniqueness is partial — the same Mongo collection is also
    used by the legacy Player Intelligence module which may store docs
    without an mlb_id (e.g. soccer/tennis), and a plain unique index
    would collide on multiple `null` mlb_id values."""
    await db.players.create_index([("sport", 1), ("canonical_name", 1)])
    await db.players.create_index(
        [("sport", 1), ("mlb_id", 1)],
        unique=True,
        partialFilterExpression={"mlb_id": {"$type": "number"}},
        name="sport_1_mlb_id_1_partial",
    )
    await db.players.create_index([("sport", 1), ("last_name", 1)])
    await db.player_stats.create_index(
        [("sport", 1), ("mlb_id", 1), ("season", 1)], unique=True,
    )
    await db.injuries.create_index([("sport", 1), ("canonical_name", 1)])
