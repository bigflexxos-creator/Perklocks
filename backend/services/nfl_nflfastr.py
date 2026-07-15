"""NFL nflfastR / nflverse ingester — Phase 4 pre-season prep.

Adds three deep NFL signals ready for kickoff (September):
  1) **Snap count %**       — offensive workload share; a WR at 25% is
                              a rotational player, at 85% is a bell-cow.
                              Directly caps target and rush volume.
  2) **Target share**       — % of team targets that go to this player;
                              structural feature that persists week-over-week.
  3) **aDOT (avg depth of target)** & **YPRR** (yards per route run) —
                              downfield vs underneath role + efficiency;
                              matters for receiving-yards Overs.
  4) **WOPR** (weighted opportunity rating) — combined target-share +
                              air-yards-share metric; empirically the
                              strongest single leading indicator of
                              fantasy points for WRs / TEs.

Data source: **nflverse** GitHub Releases — public, no auth, permanent
CDN. Two parquet files per season:
    snap_counts/snap_counts_YYYY.parquet     — per-game snap %
    player_stats/player_stats_season.parquet — season-aggregated (all seasons)

Verified 2026-07 that both endpoints return HTTP 200 for 2024 and 2025.
Since the 2026 season doesn't start until September, we seed with 2024
+ 2025 so the signal is populated the day preseason opens.

Storage:
    nfl_player_usage — one document per (player, season) with:
        {
          player_id, player, position, team, season,
          games, snap_pct_avg,
          receiving: {targets, target_share, air_yards_share, wopr,
                       receptions, receiving_yards, receiving_air_yards,
                       adot, yprr_est},
          rushing:   {carries, rushing_yards, rushing_epa,
                      rushing_first_downs},
          source, updated_at,
        }

Public API:
    from services.nfl_nflfastr import (
        refresh_nfl_seasons,                 # bulk ingest (seasons=(2024,2025))
        get_nfl_player_usage,                # lookup by name
        enrich_picks_with_nfl_usage_bulk,    # on-read enrichment
    )
"""
from __future__ import annotations

import asyncio
import io
import logging
import urllib.request
from datetime import datetime, timezone
from typing import Iterable, Optional

logger = logging.getLogger("lockscore.services.nfl_nflfastr")

_NFLVERSE_BASE = "https://github.com/nflverse/nflverse-data/releases/download"
_HTTP_TIMEOUT = 60
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; LockScore/1.0)"}

# Focus on offensive skill positions — snap % / YPRR are only meaningful
# for WR / TE / RB / QB.
_SKILL_POSITIONS = {"QB", "RB", "WR", "TE", "FB", "HB"}


# ── HTTP helpers ────────────────────────────────────────────────────
def _download_parquet(url: str) -> "pyarrow.Table":  # type: ignore  # noqa: F821
    """Download a parquet file from nflverse. Blocking. Called in an
    asyncio thread executor (see _fetch_parquet_async)."""
    import pyarrow.parquet as pq
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
        raw = resp.read()
    return pq.read_table(io.BytesIO(raw))


async def _fetch_parquet_async(url: str):
    """Async wrapper — parquet reads are CPU-bound, run in thread pool."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _download_parquet, url)


# ── Aggregation math ────────────────────────────────────────────────
def _safe_div(a, b) -> Optional[float]:
    try:
        a = float(a) if a is not None else None
        b = float(b) if b is not None else None
    except (TypeError, ValueError):
        return None
    if a is None or b is None or b == 0:
        return None
    return round(a / b, 3)


def _normalize_name(name: str) -> str:
    """Case-fold + strip whitespace. NFL names are stable across
    nflverse and TheSportsBook feeds so no complex normalization is
    needed here (e.g. "Ja'Marr Chase" → "ja'marr chase")."""
    return (name or "").strip().lower()


# ── Ingest ────────────────────────────────────────────────────────
async def _ingest_snap_counts(db, season: int) -> int:
    """Pull snap_counts_YYYY.parquet, aggregate per player, upsert."""
    url = f"{_NFLVERSE_BASE}/snap_counts/snap_counts_{season}.parquet"
    try:
        tbl = await _fetch_parquet_async(url)
    except Exception as e:
        logger.debug("snap_counts %d fetch failed: %s", season, e)
        return 0
    df = tbl.to_pandas()
    # Keep only skill positions and regular-season games (nflverse tags
    # postseason as game_type='POST').
    if "position" in df.columns:
        df = df[df["position"].isin(_SKILL_POSITIONS)]
    if "game_type" in df.columns:
        df = df[df["game_type"] == "REG"]
    if df.empty:
        return 0
    # Group by player + team + position; compute avg snap %.
    grouped = df.groupby(
        ["pfr_player_id", "player", "position", "team"],
        dropna=False,
    ).agg(
        games=("game_id", "count"),
        offense_snaps_sum=("offense_snaps", "sum"),
        offense_pct_avg=("offense_pct", "mean"),
        st_pct_avg=("st_pct", "mean"),
    ).reset_index()
    upserted = 0
    now_iso = datetime.now(timezone.utc).isoformat()
    for _, row in grouped.iterrows():
        name = _normalize_name(row["player"])
        if not name:
            continue
        doc_update = {
            "$set": {
                "player_id":       str(row.get("pfr_player_id") or ""),
                "player":          name,
                "position":        row.get("position") or "",
                "team":            row.get("team") or "",
                "season":          season,
                "games":           int(row.get("games") or 0),
                "snap_pct_avg":    round(float(row.get("offense_pct_avg") or 0), 3),
                "special_teams_pct_avg": round(float(row.get("st_pct_avg") or 0), 3),
                "offense_snaps_sum": int(row.get("offense_snaps_sum") or 0),
                "source":          "nflverse_snap_counts",
                "updated_at":      now_iso,
            }
        }
        await db.nfl_player_usage.update_one(
            {"player": name, "season": season}, doc_update, upsert=True,
        )
        upserted += 1
    logger.info("nflverse snap_counts %d → %d players", season, upserted)
    return upserted


async def _ingest_player_stats_season(db, seasons: Iterable[int]) -> int:
    """Pull the single all-seasons player_stats_season parquet, filter
    to requested seasons, merge with existing docs, upsert."""
    url = f"{_NFLVERSE_BASE}/player_stats/player_stats_season.parquet"
    try:
        tbl = await _fetch_parquet_async(url)
    except Exception as e:
        logger.debug("player_stats_season fetch failed: %s", e)
        return 0
    df = tbl.to_pandas()
    seasons = tuple(seasons)
    df = df[df["season"].isin(seasons)]
    if df.empty:
        return 0
    if "position" in df.columns:
        df = df[df["position"].isin(_SKILL_POSITIONS)]
    # Filter regular season only
    if "season_type" in df.columns:
        df = df[df["season_type"] == "REG"]
    if df.empty:
        return 0

    upserted = 0
    now_iso = datetime.now(timezone.utc).isoformat()
    for _, row in df.iterrows():
        name = _normalize_name(row.get("player_display_name") or row.get("player_name") or "")
        if not name:
            continue
        season = int(row["season"])
        targets = float(row.get("targets") or 0)
        rec_air = float(row.get("receiving_air_yards") or 0)
        rec_yds = float(row.get("receiving_yards") or 0)
        adot = _safe_div(rec_air, targets)
        # YPRR estimate: without pff-style route counts, we approximate
        # from receiving_yards / (targets * 1.6) — the ~1.6 factor is
        # the league-avg route-per-target ratio (Fantasy Points, 2023).
        yprr_est = _safe_div(rec_yds, targets * 1.6) if targets else None
        payload = {
            "receiving.targets":           int(targets),
            "receiving.target_share":      round(float(row.get("target_share") or 0), 3),
            "receiving.air_yards_share":   round(float(row.get("air_yards_share") or 0), 3),
            "receiving.wopr":              round(float(row.get("wopr") or 0), 3),
            "receiving.receptions":        int(row.get("receptions") or 0),
            "receiving.receiving_yards":   round(float(row.get("receiving_yards") or 0), 1),
            "receiving.receiving_tds":     int(row.get("receiving_tds") or 0),
            "receiving.receiving_air_yards": round(float(row.get("receiving_air_yards") or 0), 1),
            "receiving.racr":              round(float(row.get("racr") or 0), 3),
            "receiving.receiving_epa":     round(float(row.get("receiving_epa") or 0), 2),
            "receiving.adot":              adot,
            "receiving.yprr_est":          yprr_est,
            "rushing.carries":             int(row.get("carries") or 0),
            "rushing.rushing_yards":       round(float(row.get("rushing_yards") or 0), 1),
            "rushing.rushing_tds":         int(row.get("rushing_tds") or 0),
            "rushing.rushing_epa":         round(float(row.get("rushing_epa") or 0), 2),
            "rushing.rushing_first_downs": int(row.get("rushing_first_downs") or 0),
            "passing.attempts":            int(row.get("attempts") or 0),
            "passing.completions":         int(row.get("completions") or 0),
            "passing.passing_yards":       round(float(row.get("passing_yards") or 0), 1),
            "passing.passing_tds":         int(row.get("passing_tds") or 0),
            "passing.passing_epa":         round(float(row.get("passing_epa") or 0), 2),
            "position":                    row.get("position") or "",
            "team":                        row.get("recent_team") or "",
            "player":                      name,
            "season":                      season,
            "games_recorded":              int(row.get("games") or 0),
            "source_stats":                "nflverse_player_stats_season",
            "updated_at":                  now_iso,
        }
        await db.nfl_player_usage.update_one(
            {"player": name, "season": season},
            {"$set": payload},
            upsert=True,
        )
        upserted += 1
    logger.info("nflverse player_stats_season → %d rows across %s",
                upserted, seasons)
    return upserted


async def refresh_nfl_seasons(db, seasons: Iterable[int] = (2024, 2025)) -> dict:
    """Bulk ingest snap counts + season stats for the given seasons.
    Idempotent — safe to run repeatedly."""
    result = {"seasons": list(seasons), "snap_count_docs": 0, "stat_docs": 0}
    for season in seasons:
        result["snap_count_docs"] += await _ingest_snap_counts(db, season)
    result["stat_docs"] = await _ingest_player_stats_season(db, seasons)
    return result


# ── Lookups ─────────────────────────────────────────────────────────
async def get_nfl_player_usage(db, name: str,
                                season: Optional[int] = None) -> Optional[dict]:
    """Case-insensitive lookup. Falls back to most recent available
    season if current one isn't cached yet."""
    if not name:
        return None
    lname = _normalize_name(name)
    if season:
        doc = await db.nfl_player_usage.find_one(
            {"player": lname, "season": season}, {"_id": 0},
        )
        if doc:
            return doc
    # Fallback: most recent season for this player
    return await db.nfl_player_usage.find_one(
        {"player": lname}, {"_id": 0}, sort=[("season", -1)],
    )


# ── Enrichment ──────────────────────────────────────────────────────
_NFL_RECEIVER_MARKETS = (
    "receiving yards", "receptions", "longest reception",
    "receiving tds", "anytime td", "first td",
)
_NFL_RUSHER_MARKETS = (
    "rushing yards", "rush attempts", "carries",
    "longest rush", "rushing tds", "rush + rec",
)
_NFL_QB_MARKETS = (
    "passing yards", "passing tds", "passing attempts",
    "passing completions", "interceptions thrown",
)


def _is_nfl_skill_prop(pick: dict) -> bool:
    if (pick.get("sport") or "").upper() != "NFL":
        return False
    market = (pick.get("market") or "").lower()
    return any(m in market for m in
               _NFL_RECEIVER_MARKETS + _NFL_RUSHER_MARKETS + _NFL_QB_MARKETS)


def _pick_player(pick: dict) -> Optional[str]:
    """Extract player name from an NFL pick. Skill props store the
    player in `selection` (e.g. selection='Ja\\'Marr Chase' for a
    receiving-yards Over)."""
    selection = (pick.get("selection") or "").strip()
    if selection.lower() in ("over", "under", "yes", "no"):
        return None
    return selection or None


async def enrich_picks_with_nfl_usage_bulk(db, picks: list[dict]) -> int:
    """Attach `nfl_usage` block to every NFL skill-position prop.
    Dedupes lookups per unique (player, season)."""
    if not picks:
        return 0
    nfl_picks = [p for p in picks if _is_nfl_skill_prop(p)]
    if not nfl_picks:
        return 0
    # Determine target season — nflverse `player_stats_season` includes
    # all completed seasons; for regular-season games in October we'd
    # want the current year, but if the season hasn't started we fall
    # back to the most recent available.
    year = datetime.now(timezone.utc).year
    cache: dict[str, Optional[dict]] = {}
    touched = 0
    for p in nfl_picks:
        name = _pick_player(p)
        if not name:
            continue
        key = _normalize_name(name)
        if key not in cache:
            cache[key] = await get_nfl_player_usage(db, name, year)
        doc = cache[key]
        if not doc:
            continue
        rec = doc.get("receiving") or {}
        rush = doc.get("rushing") or {}
        p["nfl_usage"] = {
            "season":         doc.get("season"),
            "games":          doc.get("games_recorded") or doc.get("games"),
            "snap_pct":       doc.get("snap_pct_avg"),
            "position":       doc.get("position"),
            "team":           doc.get("team"),
            "target_share":   rec.get("target_share"),
            "air_yards_share": rec.get("air_yards_share"),
            "wopr":           rec.get("wopr"),
            "adot":           rec.get("adot"),
            "yprr_est":       rec.get("yprr_est"),
            "receiving_yards": rec.get("receiving_yards"),
            "receiving_epa":  rec.get("receiving_epa"),
            "rushing_yards":  rush.get("rushing_yards"),
            "carries":        rush.get("carries"),
            "rushing_epa":    rush.get("rushing_epa"),
            "source":         "nflverse",
        }
        touched += 1
    return touched


# ── CLI ─────────────────────────────────────────────────────────────
async def _main():
    import os
    from motor.motor_asyncio import AsyncIOMotorClient  # type: ignore
    cli = AsyncIOMotorClient(os.getenv("MONGO_URL"))
    db = cli[os.getenv("DB_NAME", "lockscore_db")]
    result = await refresh_nfl_seasons(db)
    print("NFL nflverse refresh:", result)
    cli.close()


if __name__ == "__main__":
    asyncio.run(_main())


__all__ = [
    "refresh_nfl_seasons",
    "get_nfl_player_usage",
    "enrich_picks_with_nfl_usage_bulk",
    "_is_nfl_skill_prop",
]
