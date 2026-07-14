"""MLB Statcast ingester — Phase 1.1 of the data-gap roadmap.

Fetches Baseball Savant expected-statistics + Statcast batted-ball CSVs
and caches per-player season aggregates in MongoDB. Provides lookup
helpers used at pick-enrichment time to attach Statcast context to
every MLB hitter/pitcher pick.

Data captured (batters):
    xwoba        — expected wOBA (batted-ball quality on contact)
    xba          — expected batting average
    xslg         — expected slugging
    ba, woba, slg— actual season stats (for luck-vs-quality delta)
    barrel_pct   — barrel%
    hard_hit_pct — hard-hit% (95+ mph)
    avg_ev       — average exit velocity
    launch_angle — average launch angle
    sweet_spot   — sweet-spot%

Data captured (pitchers):
    xwoba_against— expected wOBA allowed on contact
    xba_against, xslg_against
    xera         — expected ERA
    era          — actual season ERA

Source: Baseball Savant public CSV endpoints (no auth, free, ~50KB per
CSV). Refreshed daily; snapshots are keyed by (year, type) so we can
audit historical changes if needed.

Why this signal matters:
  Empirically the single biggest lift for MLB props (Fangraphs +3-5%
  AUC on hitter Overs). xwOBA/xBA decouple TRUE batting quality from
  short-run luck — the Cardinals hitter who's batting .180 but with a
  .310 xBA is a strong regression buy on Overs; the .310/.240 hitter
  is a fade.
"""
from __future__ import annotations

import asyncio
import csv
import io
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger("lockscore.services.mlb_statcast")

_SAVANT_BASE = "https://baseballsavant.mlb.com/leaderboard"
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; LockScore/1.0)"}
_HTTP_TIMEOUT = 20.0

# Minimum PA / batters-faced thresholds — anything smaller than this is
# usually noise (small-sample xwOBA on 40 PA swings wildly).
_MIN_PA_BATTER   = 80
_MIN_BF_PITCHER  = 60


# ── HTTP layer ───────────────────────────────────────────────────────
async def _fetch_csv(url: str, params: dict) -> list[dict]:
    """Fetch a Baseball Savant CSV and return list of row dicts."""
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, headers=_HEADERS) as cx:
        r = await cx.get(url, params=params)
        r.raise_for_status()
        text = r.text
    # Baseball Savant sometimes serves BOM'd UTF-8; strip if present.
    if text and text[0] == "\ufeff":
        text = text[1:]
    reader = csv.DictReader(io.StringIO(text))
    return list(reader)


# ── Row → normalized document ────────────────────────────────────────
def _parse_float(value) -> Optional[float]:
    """Parse "0.279" or "104.4" → float, tolerating quotes and blanks."""
    if value is None or value == "":
        return None
    try:
        return float(str(value).strip().replace('"', ""))
    except (ValueError, TypeError):
        return None


def _normalize_name(last_first: str) -> str:
    """Baseball Savant returns 'Wood, James' — convert to 'James Wood'
    for case-insensitive lookups. Handles suffixes like 'Jr.'."""
    if not last_first or "," not in last_first:
        return (last_first or "").strip().lower()
    last, first = [p.strip() for p in last_first.split(",", 1)]
    return f"{first} {last}".lower().strip()


def _row_to_batter_doc(row: dict, year: int) -> Optional[dict]:
    pa = _parse_float(row.get("pa")) or 0
    if pa < _MIN_PA_BATTER:
        return None
    player_id = row.get("player_id") or ""
    name_key = _normalize_name(row.get("last_name, first_name") or "")
    if not player_id or not name_key:
        return None
    return {
        "player_id": str(player_id),
        "name":      name_key,
        "year":      year,
        "type":      "batter",
        "pa":        int(pa),
        "ba":        _parse_float(row.get("ba")),
        "xba":       _parse_float(row.get("est_ba")),
        "slg":       _parse_float(row.get("slg")),
        "xslg":      _parse_float(row.get("est_slg")),
        "woba":      _parse_float(row.get("woba")),
        "xwoba":     _parse_float(row.get("est_woba")),
        "xba_diff":  _parse_float(row.get("est_ba_minus_ba_diff")),
        "xwoba_diff": _parse_float(row.get("est_woba_minus_woba_diff")),
        "xslg_diff": _parse_float(row.get("est_slg_minus_slg_diff")),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _row_to_pitcher_doc(row: dict, year: int) -> Optional[dict]:
    pa = _parse_float(row.get("pa")) or 0
    if pa < _MIN_BF_PITCHER:
        return None
    player_id = row.get("player_id") or ""
    name_key = _normalize_name(row.get("last_name, first_name") or "")
    if not player_id or not name_key:
        return None
    return {
        "player_id":     str(player_id),
        "name":          name_key,
        "year":          year,
        "type":          "pitcher",
        "batters_faced": int(pa),
        "ba_against":    _parse_float(row.get("ba")),
        "xba_against":   _parse_float(row.get("est_ba")),
        "slg_against":   _parse_float(row.get("slg")),
        "xslg_against":  _parse_float(row.get("est_slg")),
        "woba_against":  _parse_float(row.get("woba")),
        "xwoba_against": _parse_float(row.get("est_woba")),
        "era":           _parse_float(row.get("era")),
        "xera":          _parse_float(row.get("xera")),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _row_to_batted_ball(row: dict, year: int) -> Optional[dict]:
    """Batted-ball metrics come from the /statcast leaderboard endpoint.
    Keyed by player_id so we can merge into the expected-stats doc."""
    player_id = row.get("player_id") or ""
    name_key = _normalize_name(row.get("last_name, first_name") or "")
    if not player_id or not name_key:
        return None
    return {
        "player_id":    str(player_id),
        "name":         name_key,
        "year":         year,
        "attempts":     int(_parse_float(row.get("attempts")) or 0),
        "avg_ev":       _parse_float(row.get("avg_hit_speed")),
        "launch_angle": _parse_float(row.get("avg_hit_angle")),
        "sweet_spot":   _parse_float(row.get("anglesweetspotpercent")),
        "hard_hit":     _parse_float(row.get("ev95percent")),
        "barrel_pct":   _parse_float(row.get("brl_percent")),
        "brl_pa":       _parse_float(row.get("brl_pa")),
        "max_distance": _parse_float(row.get("max_distance")),
        "avg_distance": _parse_float(row.get("avg_distance")),
    }


# ── Public: refresh cache ────────────────────────────────────────────
async def refresh_batters(db, year: Optional[int] = None) -> dict:
    """Pull batter expected-stats + batted-ball metrics, merge, upsert
    into `mlb_statcast_players`."""
    year = year or datetime.now(timezone.utc).year
    exp_rows   = await _fetch_csv(f"{_SAVANT_BASE}/expected_statistics",
                                   {"type": "batter", "year": year,
                                    "min": _MIN_PA_BATTER, "csv": "true"})
    bb_rows    = await _fetch_csv(f"{_SAVANT_BASE}/statcast",
                                   {"type": "batter", "year": year,
                                    "min": _MIN_PA_BATTER, "csv": "true"})

    # Index batted-ball by player_id for merge
    bb_index: dict[str, dict] = {}
    for r in bb_rows:
        doc = _row_to_batted_ball(r, year)
        if doc:
            bb_index[doc["player_id"]] = doc

    upserted = 0
    for r in exp_rows:
        base = _row_to_batter_doc(r, year)
        if not base:
            continue
        bb = bb_index.get(base["player_id"]) or {}
        merged = {**base}
        for k in ("avg_ev", "launch_angle", "sweet_spot", "hard_hit",
                  "barrel_pct", "brl_pa", "max_distance", "avg_distance"):
            if bb.get(k) is not None:
                merged[k] = bb[k]
        await db.mlb_statcast_players.update_one(
            {"player_id": merged["player_id"], "year": year, "type": "batter"},
            {"$set": merged},
            upsert=True,
        )
        upserted += 1
    logger.info("Statcast batters refreshed: %d players (year %d)", upserted, year)
    return {"type": "batter", "year": year, "upserted": upserted}


async def refresh_pitchers(db, year: Optional[int] = None) -> dict:
    year = year or datetime.now(timezone.utc).year
    rows = await _fetch_csv(f"{_SAVANT_BASE}/expected_statistics",
                             {"type": "pitcher", "year": year,
                              "min": _MIN_BF_PITCHER, "csv": "true"})
    upserted = 0
    for r in rows:
        doc = _row_to_pitcher_doc(r, year)
        if not doc:
            continue
        await db.mlb_statcast_players.update_one(
            {"player_id": doc["player_id"], "year": year, "type": "pitcher"},
            {"$set": doc},
            upsert=True,
        )
        upserted += 1
    logger.info("Statcast pitchers refreshed: %d players (year %d)", upserted, year)
    return {"type": "pitcher", "year": year, "upserted": upserted}


async def refresh_all(db, year: Optional[int] = None) -> dict:
    b = await refresh_batters(db, year)
    p = await refresh_pitchers(db, year)
    return {"batters": b, "pitchers": p}


# ── Public: lookup at pick-enrichment time ───────────────────────────
async def get_batter_statcast(db, name: str,
                              year: Optional[int] = None) -> Optional[dict]:
    """Case-insensitive batter lookup. Returns None if the player wasn't
    in the season leaderboard (usually means <80 PA)."""
    if not name:
        return None
    year = year or datetime.now(timezone.utc).year
    return await db.mlb_statcast_players.find_one({
        "name": name.strip().lower(),
        "year": year,
        "type": "batter",
    }, {"_id": 0})


async def get_pitcher_statcast(db, name: str,
                               year: Optional[int] = None) -> Optional[dict]:
    if not name:
        return None
    year = year or datetime.now(timezone.utc).year
    return await db.mlb_statcast_players.find_one({
        "name": name.strip().lower(),
        "year": year,
        "type": "pitcher",
    }, {"_id": 0})


# ── Public: enrich picks ─────────────────────────────────────────────
def _is_hitter_market(pick: dict) -> bool:
    market = (pick.get("market") or "").lower()
    selection = (pick.get("selection") or "").lower()
    if any(t in market for t in (
        "team total", "spread", "moneyline", "run line", "run-line",
        "1st inning", "nrfi", "yrfi",
    )):
        return False
    if selection in ("over", "under", "yes", "no", ""):
        return False
    return any(kw in market for kw in (
        "hits", "home run", "total bases", "rbi", "runs scored",
        "hit + run", "hits+run", "singles", "doubles", "triples",
    ))


def _is_pitcher_market(pick: dict) -> bool:
    market = (pick.get("market") or "").lower()
    selection = (pick.get("selection") or "").lower()
    if not selection or selection in ("over", "under", "yes", "no"):
        return False
    return any(m in market for m in (
        "strikeouts", "outs recorded", "earned runs", "pitcher walks",
        "hits allowed",
    ))


async def enrich_picks_with_statcast_bulk(db, picks: list[dict]) -> int:
    """Attach Statcast context to every MLB batter/pitcher pick. Uses a
    cached lookup so per-slate cost is O(distinct-players) DB reads.
    Returns the count of picks touched."""
    if not picks:
        return 0
    mlb = [p for p in picks if (p.get("sport") or "").upper() == "MLB"]
    if not mlb:
        return 0
    year = datetime.now(timezone.utc).year
    # Dedupe player lookups per slate
    cache_bat: dict[str, Optional[dict]] = {}
    cache_pit: dict[str, Optional[dict]] = {}
    touched = 0
    for p in mlb:
        if _is_hitter_market(p):
            name = (p.get("selection") or "").strip().lower()
            if not name:
                continue
            if name not in cache_bat:
                cache_bat[name] = await get_batter_statcast(db, name, year)
            sc = cache_bat[name]
            if sc:
                p["statcast_batter"] = {
                    k: sc.get(k) for k in (
                        "xba", "xslg", "xwoba", "ba", "slg", "woba",
                        "xba_diff", "xwoba_diff", "xslg_diff",
                        "barrel_pct", "hard_hit", "avg_ev",
                        "launch_angle", "sweet_spot",
                    )
                }
                touched += 1
        elif _is_pitcher_market(p):
            name = (p.get("selection") or "").strip().lower()
            if not name:
                continue
            if name not in cache_pit:
                cache_pit[name] = await get_pitcher_statcast(db, name, year)
            sc = cache_pit[name]
            if sc:
                p["statcast_pitcher"] = {
                    k: sc.get(k) for k in (
                        "xba_against", "xslg_against", "xwoba_against",
                        "ba_against", "slg_against", "woba_against",
                        "xera", "era",
                    )
                }
                touched += 1
    return touched


# ── CLI entry point (for manual refresh from cron) ───────────────────
async def _main():
    import os
    from motor.motor_asyncio import AsyncIOMotorClient  # type: ignore
    cli = AsyncIOMotorClient(os.getenv("MONGO_URL"))
    db = cli[os.getenv("DB_NAME", "lockscore_db")]
    result = await refresh_all(db)
    print("Statcast refresh:", result)
    cli.close()


if __name__ == "__main__":
    asyncio.run(_main())


__all__ = [
    "refresh_batters",
    "refresh_pitchers",
    "refresh_all",
    "get_batter_statcast",
    "get_pitcher_statcast",
    "enrich_picks_with_statcast_bulk",
]
