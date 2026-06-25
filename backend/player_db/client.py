"""Drop-in replacement for `player_intel.sportsdataio_client` powered by
the local free-source player_db (MLB Stats API for now; NBA/NFL/Tennis
to follow in Phase 2+).

Shape-compatible — exposes `enrich_profile(profile)`, `find_player()`,
`find_injury()` so existing callers don't change.

When the local DB doesn't have a row for the requested sport/player,
we fall back to the legacy SportsDataIO client so coverage degrades
gracefully during the cut-over period (and so NBA/NFL keep working
until their ingestor lands).
"""
from __future__ import annotations

import logging
import time
from typing import Any

from deps import db

logger = logging.getLogger("lockscore.player_db.client")


def _canonical(name: str) -> str:
    return (name or "").strip().lower()


async def find_player(sport: str, name: str) -> dict | None:
    """Local-first lookup by canonical name. Last-name fallback for
    half-name picks ('Mahomes' → 'Patrick Mahomes')."""
    s = (sport or "").lower()
    target = _canonical(name)
    if not target:
        return None
    # Exact canonical match
    row = await db.players.find_one(
        {"sport": s, "canonical_name": target}, {"_id": 0}
    )
    if row:
        return row
    # Last-name fallback
    last = target.split()[-1] if target else ""
    if last:
        row = await db.players.find_one(
            {"sport": s, "last_name": {"$regex": f"^{last}$", "$options": "i"}},
            {"_id": 0},
        )
        if row:
            return row
    return None


async def find_injury(sport: str, name: str) -> dict | None:
    s = (sport or "").lower()
    target = _canonical(name)
    if not target:
        return None
    return await db.injuries.find_one(
        {"sport": s, "canonical_name": target}, {"_id": 0}
    )


async def enrich_profile(profile: dict) -> dict:
    """Shape-compatible with the SportsDataIO enrich_profile. Fills in
    position, team, photo, injury status from the local DB. Falls back
    to SportsDataIO for sports we haven't ingested yet (NBA/NFL until
    Phase 2 ships)."""
    sport = (profile.get("sport") or "").lower()
    name = profile.get("canonical_name") or profile.get("name") or ""
    if not sport or not name:
        return profile

    # Phase 1: MLB has a local source (MLB Stats API).
    # Phase 2: NBA + NFL now also resolve locally (ESPN public).
    # Remaining sports (soccer, tennis, etc.) fall back to legacy
    # SportsDataIO until their ingestors land.
    if sport not in ("mlb", "nba", "nfl"):
        try:
            from player_intel.sportsdataio_client import enrich_profile as legacy
            return await legacy(profile)
        except Exception as e:
            logger.debug("legacy SDIO enrich skipped for %s: %s", sport, e)
            return profile

    try:
        row = await find_player(sport, name)
        if row:
            # Position — only overwrite when seed didn't pin one.
            if row.get("position") and (
                not profile.get("position") or profile.get("source") != "seed"
            ):
                profile["position"] = row["position"]
            if row.get("team"):
                profile["team"] = row["team"]
            if row.get("team_name"):
                profile["team_name"] = row["team_name"]
            if row.get("height"):
                profile["height"] = row["height"]
            if row.get("weight_lb"):
                profile["weight_lb"] = row["weight_lb"]
            if row.get("birth_date"):
                profile["birth_date"] = row["birth_date"]
            if row.get("photo_url"):
                profile["photo_url"] = row["photo_url"]
            if row.get("jersey"):
                profile["jersey"] = row["jersey"]
            # MLB-only handedness fields
            if sport == "mlb":
                if row.get("bats"):
                    profile["bats"] = row["bats"]
                if row.get("throws"):
                    profile["throws"] = row["throws"]
                profile["mlb_id"]    = row.get("mlb_id") or row.get("player_id")
            else:
                profile["espn_id"]   = row.get("espn_id") or row.get("player_id")
            profile["player_db_status"]      = row.get("status")
            profile["player_db_source"]      = row.get("source") or "player_db"
            profile["player_db_enriched_at"] = int(time.time())
        injury = await find_injury(sport, name)
        if injury:
            profile["injury_status"]      = injury.get("status") or "questionable"
            profile["injury_description"] = injury.get("description") or injury.get("status")
            profile["injury_updated"]     = injury.get("updated_at")
        else:
            profile["injury_status"] = profile.get("injury_status") or "healthy"
    except Exception as e:
        logger.warning("player_db enrich_profile(%s, %s) failed: %s", sport, name, e)
    return profile


async def get_season_stats(sport: str, name: str, season: int | None = None) -> dict | None:
    """Return the latest cached season stats row for a player. Used by
    the usage_intensity bucket and BvP enrichment."""
    s = (sport or "").lower()
    target = _canonical(name)
    if not target:
        return None
    q: dict[str, Any] = {"sport": s, "canonical_name": target}
    if season:
        q["season"] = season
    return await db.player_stats.find_one(q, {"_id": 0}, sort=[("season", -1)])
