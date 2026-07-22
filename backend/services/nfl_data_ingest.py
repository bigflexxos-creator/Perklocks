"""NFL data ingestion — Phase 3 (2026-07-22).

Pulls per-player weekly stats from the NFLverse GitHub releases and
stores them in MongoDB. Zero placeholders, zero RNG — only real
historical stats.

Sources (all parquet, ~1 MB each, ~7 seasons × ~19k rows = ~130k
player-weeks total):

    https://github.com/nflverse/nflverse-data/releases/download/
        stats_player/stats_player_week_{year}.parquet

Storage:
    Collection: nfl_player_weekly
    Doc shape (one per player-week):
        {
          "_id":            f"{player_id}_{season}_{week}",
          "player_id":       "00-0034857",       # nflverse GSIS
          "player_name":     "Josh Allen",
          "position":        "QB",
          "position_group":  "QB",
          "team":            "BUF",
          "opponent_team":   "ARI",
          "season":          2024,
          "week":            1,
          "game_id":         "2024_01_ARI_BUF",
          # ── Passing ────────────────
          "completions":     18,
          "attempts":        23,
          "passing_yards":   232,
          "passing_tds":     2,
          "passing_ints":    0,
          "passing_air_yards":  166,
          "passing_yac":     125,
          "passing_epa":     9.16,
          "passing_cpoe":    9.89,
          # ── Rushing ────────────────
          "carries":         9,
          "rushing_yards":   39,
          "rushing_tds":     2,
          "rushing_first_downs": 4,
          "rushing_epa":     8.56,
          # ── Receiving ──────────────
          "receptions":      0,
          "targets":         0,
          "receiving_yards": 0,
          "receiving_tds":   0,
          "receiving_air_yards": 0,
          "receiving_yac":   0,
          "receiving_epa":   None,
          "target_share":    0.0,
          "air_yards_share": 0.0,
          "wopr":            0.0,
          "racr":            None,
          # ── Fantasy / Composite ────
          "fantasy_points":     ...,
          "fantasy_points_ppr": ...,
        }
"""

from __future__ import annotations

import io
import logging
import os
import urllib.request
from datetime import datetime, timezone
from typing import Iterable, Optional

logger = logging.getLogger("lockscore.services.nfl_data_ingest")

_NFLVERSE_URL_TMPL = (
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "stats_player/stats_player_week_{year}.parquet"
)
_COLLECTION = "nfl_player_weekly"
_DEFAULT_YEARS = tuple(range(2019, 2026))  # 2019 through 2025 inclusive


# Whitelist columns we actually care about. Everything else (defensive
# stat rows, kicking, punting) stays out of Mongo to keep the collection
# small and query-fast. If we later want defensive props we'll extend.
_COLUMNS_TO_KEEP = {
    # Identity
    "player_id", "player_name", "player_display_name",
    "position", "position_group",
    "team", "opponent_team",
    "season", "week", "season_type", "game_id",
    # Passing
    "completions", "attempts", "passing_yards", "passing_tds",
    "passing_interceptions", "passing_air_yards",
    "passing_yards_after_catch", "passing_first_downs",
    "passing_epa", "passing_cpoe", "passing_2pt_conversions",
    "sacks_suffered", "sack_yards_lost",
    # Rushing
    "carries", "rushing_yards", "rushing_tds",
    "rushing_first_downs", "rushing_epa",
    # Receiving
    "receptions", "targets", "receiving_yards", "receiving_tds",
    "receiving_air_yards", "receiving_yards_after_catch",
    "receiving_first_downs", "receiving_epa",
    "target_share", "air_yards_share", "wopr", "racr",
    # Fantasy
    "fantasy_points", "fantasy_points_ppr",
}

# Fields to rename for clarity
_RENAME = {
    "passing_interceptions":       "passing_ints",
    "passing_yards_after_catch":   "passing_yac",
    "receiving_yards_after_catch": "receiving_yac",
}


def _download_parquet(year: int) -> bytes:
    """Download the raw parquet bytes for a season. Raises on non-200."""
    url = _NFLVERSE_URL_TMPL.format(year=year)
    req = urllib.request.Request(url, headers={"User-Agent": "PerkLocks/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        if resp.status != 200:
            raise RuntimeError(f"NFLverse {year}: HTTP {resp.status}")
        return resp.read()


def _rows_from_parquet(data: bytes) -> Iterable[dict]:
    """Parse parquet bytes into filtered player-week dicts."""
    import pandas as pd
    df = pd.read_parquet(io.BytesIO(data))
    # Filter to columns we keep
    cols = [c for c in df.columns if c in _COLUMNS_TO_KEEP]
    df = df[cols].copy()
    # Filter out preseason if desired — keep REG + POST only
    if "season_type" in df.columns:
        df = df[df["season_type"].isin(["REG", "POST"])]
    # Drop rows without a player_id (nflverse occasionally emits totals rows)
    if "player_id" in df.columns:
        df = df.dropna(subset=["player_id"])

    for row in df.to_dict(orient="records"):
        # Coerce NaN → None for Mongo cleanliness.
        clean = {}
        for k, v in row.items():
            try:
                if v != v:  # NaN
                    v = None
            except Exception:
                pass
            k2 = _RENAME.get(k, k)
            clean[k2] = v
        pid = clean.get("player_id")
        season = clean.get("season")
        week = clean.get("week")
        if not (pid and season and week):
            continue
        clean["_id"] = f"{pid}_{int(season)}_{int(week)}"
        yield clean


async def refresh_nfl_weekly(db, years: Optional[Iterable[int]] = None) -> dict:
    """Idempotent bulk import of NFL weekly player stats.

    Uses ordered=False bulk upserts so partial failures don't block
    the rest of the seasons. Returns a summary dict with counts.
    """
    years = tuple(years) if years else _DEFAULT_YEARS
    coll = db[_COLLECTION]
    total_upserts = 0
    per_year_counts = {}

    # Ensure index on season/week/player_id and on game_id for future joins.
    try:
        await coll.create_index(
            [("season", 1), ("week", 1)], name="season_week"
        )
        await coll.create_index(
            [("player_id", 1), ("season", -1), ("week", -1)],
            name="player_season_week",
        )
        await coll.create_index(
            [("team", 1), ("season", 1), ("week", 1)], name="team_season_week"
        )
        await coll.create_index(
            [("opponent_team", 1), ("season", 1), ("week", 1)],
            name="opp_season_week",
        )
    except Exception as e:
        logger.debug("nfl_player_weekly index create: %s", e)

    from pymongo import UpdateOne

    for year in years:
        try:
            logger.info("NFLverse ingest: fetching %s season", year)
            raw = _download_parquet(year)
        except Exception as e:
            logger.warning("NFLverse ingest %s: download failed: %s", year, e)
            per_year_counts[str(year)] = {"error": str(e), "rows": 0}
            continue

        ops = []
        for row in _rows_from_parquet(raw):
            ops.append(
                UpdateOne({"_id": row["_id"]}, {"$set": row}, upsert=True)
            )
            if len(ops) >= 1000:
                try:
                    res = await coll.bulk_write(ops, ordered=False)
                    total_upserts += (res.upserted_count or 0) + (res.modified_count or 0)
                except Exception as e:
                    logger.warning("bulk_write batch failed: %s", e)
                ops = []
        if ops:
            try:
                res = await coll.bulk_write(ops, ordered=False)
                total_upserts += (res.upserted_count or 0) + (res.modified_count or 0)
            except Exception as e:
                logger.warning("bulk_write final failed: %s", e)

        year_count = await coll.count_documents({"season": year})
        per_year_counts[str(year)] = {"rows": year_count}
        logger.info("NFLverse ingest %s done: %d rows in DB", year, year_count)

    # Metadata doc for freshness display in UI / admin
    await db["nfl_ingest_meta"].replace_one(
        {"_id": "nfl_player_weekly"},
        {
            "_id": "nfl_player_weekly",
            "last_refreshed": datetime.now(timezone.utc),
            "years": list(years),
            "total_upserts_this_run": total_upserts,
            "per_year_counts": per_year_counts,
        },
        upsert=True,
    )

    return {
        "years": list(years),
        "total_upserts": total_upserts,
        "per_year_counts": per_year_counts,
    }


__all__ = [
    "refresh_nfl_weekly",
]
