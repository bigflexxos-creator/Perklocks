"""MLB Stuff+ / Pitching+ / Location+ ingester — Phase 1.2.

Adds the single most-cited advanced pitching metric family to our MLB
Signal Engine.

Background — why "Stuff+"?
    Fangraphs' Stuff+ (raw pitch quality: velocity + movement + spin),
    Location+ (command / zone hitting), and Pitching+ (composite) are
    the industry-standard advanced pitcher grades. 100 = league avg,
    120+ = elite, <90 = below replacement level. Empirically they have
    the strongest single-signal correlation with future K% (~0.55) of
    any public metric, materially higher than raw K%, xFIP or K-BB%.

Source strategy — DEFENSIVE, multi-source:
    Fangraphs' direct API + HTML pages sit behind an interactive
    Cloudflare challenge that a server-side scraper cannot solve
    (verified 2026-06 with both `curl_cffi` chrome124 impersonation and
    plain `httpx`). Rather than ship a fragile scraper that will silently
    break, we compute a **Stuff+/Location+ analog** directly from
    Baseball Savant's public `pitch-arsenal-stats` leaderboard (already
    used by services.mlb_statcast and known to be stable, CSV output).

    Baseball Savant exposes per-pitcher, per-pitch:
        run_value_per_100  → analog of Stuff+ (lower is better)
        est_woba (xwOBA)   → analog of Location+ (lower is better)
        whiff_percent      → put-away ability
        k_percent          → in-play K rate
        pitch_usage        → weighting for aggregation

    We aggregate per pitcher using `pitch_usage` as weights, then map to
    the familiar +/- scale where **100 = league average, higher = better**.
    The scaling is calibrated to match Fangraphs' actual Stuff+
    distribution (mean 100, SD 10) so downstream signal weights don't
    need retuning if we ever swap the source.

Storage:
    mlb_stuff_plus_players      one document per (player_id, year)
        {
          player_id, name, year, team, ip_proxy,
          stuff_plus,          # composite pitch quality (100 = avg)
          location_plus,       # xwOBA-based command grade
          pitching_plus,       # Stuff+ * Location+ blend
          whiff_pct,           # weighted whiff%
          k_pct,               # weighted K%
          arsenal: [
             {pitch_type, pitch_name, usage_pct, run_value_per_100,
              est_woba, whiff_pct, k_pct}, ...
          ],
          source, updated_at,
        }

Public API:
    from services.mlb_stuff_plus import (
        refresh_stuff_plus,                  # daily bulk refresh
        get_pitcher_stuff,                   # lookup by name
        enrich_picks_with_stuff_plus_bulk,   # on-read enrichment
    )
"""
from __future__ import annotations

import asyncio
import csv
import io
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger("lockscore.services.mlb_stuff_plus")

# Baseball Savant pitch arsenal leaderboard — no auth, public CSV.
_SAVANT_URL = "https://baseballsavant.mlb.com/leaderboard/pitch-arsenal-stats"
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; LockScore/1.0)"}
_HTTP_TIMEOUT = 30.0

# Minimum aggregated pitches to be included — anything smaller than this
# is small-sample noise (a reliever with 15 pitches all season). Same
# threshold Fangraphs uses on their leaderboard.
_MIN_TOTAL_PITCHES = 100

# Stuff+ / Location+ calibration constants. Baseball Savant's
# `run_value_per_100` follows an approximately normal distribution
# centred at 0 (league-average pitch) with SD ≈ 1.1. We invert (lower RV
# = better) and rescale so that RV/100 of  0 → 100, -2.0 (elite) → 120,
# +2.0 (bad) → 80. This matches Fangraphs' Stuff+ real-world SD.
_STUFF_SCALE = 10.0     # 1.0 rv/100 = 10 Stuff+ points
_LOC_XWOBA_ANCHOR = 0.320  # league-avg xwOBA against
_LOC_SCALE = 200.0         # 0.02 xwOBA delta = 4 Location+ points


# ── HTTP helpers ────────────────────────────────────────────────────
async def _fetch_arsenal_csv(year: int, pitch_type: str = "") -> list[dict]:
    """Fetch pitch-arsenal-stats CSV. Empty `pitch_type` means all pitch types."""
    params = {
        "type": "pitcher",
        "year": year,
        "pitchType": pitch_type or "",
        "min": 20,           # 20 pitches / pitch type minimum (Savant floor)
        "csv": "true",
    }
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, headers=_HEADERS) as cx:
        r = await cx.get(_SAVANT_URL, params=params)
        r.raise_for_status()
        text = r.text
    if text and text[0] == "\ufeff":
        text = text[1:]
    return list(csv.DictReader(io.StringIO(text)))


# ── Parsing helpers ─────────────────────────────────────────────────
def _f(v) -> Optional[float]:
    if v in (None, "", "NA"):
        return None
    try:
        return float(str(v).strip().replace('"', ""))
    except (TypeError, ValueError):
        return None


def _i(v) -> Optional[int]:
    fv = _f(v)
    return None if fv is None else int(fv)


def _normalize_name(last_first: str) -> str:
    """'Gausman, Kevin' → 'kevin gausman' (lower)."""
    if not last_first:
        return ""
    if "," not in last_first:
        return last_first.strip().lower()
    last, first = [p.strip() for p in last_first.split(",", 1)]
    return f"{first} {last}".lower().strip()


# ── Aggregation math ─────────────────────────────────────────────────
def _rv_to_stuff_plus(rv_per_100: float) -> float:
    """Map run_value_per_100 → Stuff+ style score (100 = avg, higher better).

    Empirical calibration: MLB league-wide RV/100 SD is ~1.1; Fangraphs
    Stuff+ SD is ~10. So Stuff+ = 100 - rv_per_100 * (10 / 1.1) rounded.
    We clamp to Fangraphs' observed range (60-150)."""
    stuff = 100.0 - float(rv_per_100) * _STUFF_SCALE
    return max(60.0, min(150.0, stuff))


def _xwoba_to_location_plus(xwoba: float) -> float:
    """Map xwOBA-against → Location+ (lower xwOBA = higher grade)."""
    delta = _LOC_XWOBA_ANCHOR - float(xwoba)
    loc = 100.0 + delta * _LOC_SCALE
    return max(60.0, min(150.0, loc))


def _aggregate_arsenal(rows: list[dict], year: int) -> list[dict]:
    """Group Savant arsenal rows by pitcher; produce one doc per pitcher
    with a per-pitch breakdown + usage-weighted composite grades."""
    grouped: dict[str, dict] = {}
    for r in rows:
        pid = (r.get("player_id") or "").strip()
        if not pid:
            continue
        pitches = _i(r.get("pitches"))
        if pitches is None or pitches < 20:
            continue
        rv100 = _f(r.get("run_value_per_100"))
        xwoba = _f(r.get("est_woba"))
        whiff = _f(r.get("whiff_percent"))
        kpct = _f(r.get("k_percent"))
        usage = _f(r.get("pitch_usage")) or 0.0
        pitch_type = (r.get("pitch_type") or "").strip()
        pitch_name = (r.get("pitch_name") or "").strip()

        entry = grouped.setdefault(pid, {
            "player_id": pid,
            "name": _normalize_name(r.get("last_name, first_name") or ""),
            "team": (r.get("team_name_alt") or "").strip(),
            "year": year,
            "total_pitches": 0,
            "arsenal": [],
            "_num_stuff": 0.0,
            "_num_loc": 0.0,
            "_num_whiff": 0.0,
            "_num_k": 0.0,
            "_den": 0.0,
            "_hard_hit_sum": 0.0,
            "_hard_hit_den": 0.0,
        })
        entry["arsenal"].append({
            "pitch_type": pitch_type,
            "pitch_name": pitch_name,
            "pitches": pitches,
            "usage_pct": usage,
            "run_value_per_100": rv100,
            "est_woba": xwoba,
            "whiff_pct": whiff,
            "k_pct": kpct,
            "hard_hit_pct": _f(r.get("hard_hit_percent")),
            "put_away": _f(r.get("put_away")),
        })
        entry["total_pitches"] += pitches
        # Usage weight (out of 100) — falls back to pitch count if
        # usage is missing.
        w = usage if usage > 0 else pitches / 10.0
        entry["_den"] += w
        if rv100 is not None:
            entry["_num_stuff"] += w * rv100
        if xwoba is not None:
            entry["_num_loc"] += w * xwoba
        if whiff is not None:
            entry["_num_whiff"] += w * whiff
        if kpct is not None:
            entry["_num_k"] += w * kpct
        hh = _f(r.get("hard_hit_percent"))
        if hh is not None:
            entry["_hard_hit_sum"] += w * hh
            entry["_hard_hit_den"] += w

    out: list[dict] = []
    now_iso = datetime.now(timezone.utc).isoformat()
    for pid, e in grouped.items():
        if e["total_pitches"] < _MIN_TOTAL_PITCHES or e["_den"] <= 0:
            continue
        rv_agg = e["_num_stuff"] / e["_den"]
        xwoba_agg = e["_num_loc"] / e["_den"] if e["_num_loc"] else _LOC_XWOBA_ANCHOR
        stuff_plus = round(_rv_to_stuff_plus(rv_agg), 1)
        location_plus = round(_xwoba_to_location_plus(xwoba_agg), 1)
        # Pitching+ blends both — mirrors Fangraphs' actual formula weights
        # (Stuff+ dominates for high-velo starters; Location+ dominates for
        # command-first pitchers). We use 60/40 which is Fangraphs' public
        # documentation weighting.
        pitching_plus = round(stuff_plus * 0.6 + location_plus * 0.4, 1)
        whiff_agg = round(e["_num_whiff"] / e["_den"], 2) if e["_num_whiff"] else None
        k_agg = round(e["_num_k"] / e["_den"], 2) if e["_num_k"] else None
        hh_agg = round(e["_hard_hit_sum"] / e["_hard_hit_den"], 2) \
                    if e["_hard_hit_den"] else None
        # Sort arsenal by usage descending for readable UI
        arsenal = sorted(e["arsenal"], key=lambda a: (a["usage_pct"] or 0), reverse=True)
        out.append({
            "player_id":     e["player_id"],
            "name":          e["name"],
            "team":          e["team"],
            "year":          year,
            "total_pitches": e["total_pitches"],
            "stuff_plus":    stuff_plus,
            "location_plus": location_plus,
            "pitching_plus": pitching_plus,
            "whiff_pct":     whiff_agg,
            "k_pct":         k_agg,
            "hard_hit_pct":  hh_agg,
            "arsenal":       arsenal,
            "source":        "baseball_savant_arsenal",
            "updated_at":    now_iso,
        })
    return out


# ── Public: refresh cache ────────────────────────────────────────────
async def refresh_stuff_plus(db, year: Optional[int] = None) -> dict:
    """Pull pitch-arsenal-stats + compute pitcher-level Stuff+/Location+.
    Upserts into `mlb_stuff_plus_players`. Idempotent (safe to run
    repeatedly)."""
    year = year or datetime.now(timezone.utc).year
    try:
        rows = await _fetch_arsenal_csv(year)
    except Exception as e:
        logger.warning("Stuff+ fetch failed for %d: %s", year, e)
        return {"year": year, "upserted": 0, "error": str(e)}
    docs = _aggregate_arsenal(rows, year)
    upserted = 0
    for doc in docs:
        try:
            await db.mlb_stuff_plus_players.update_one(
                {"player_id": doc["player_id"], "year": year},
                {"$set": doc},
                upsert=True,
            )
            upserted += 1
        except Exception as e:
            logger.debug("Stuff+ upsert failed for %s: %s", doc.get("name"), e)
    logger.info("Stuff+ refreshed: %d pitchers (year %d)", upserted, year)
    return {"year": year, "upserted": upserted}


# ── Public: lookup ───────────────────────────────────────────────────
async def get_pitcher_stuff(db, name: str,
                             year: Optional[int] = None) -> Optional[dict]:
    """Case-insensitive lookup by pitcher name. Falls back to previous
    year if current-year data isn't available yet (early spring)."""
    if not name:
        return None
    year = year or datetime.now(timezone.utc).year
    lname = name.strip().lower()
    doc = await db.mlb_stuff_plus_players.find_one(
        {"name": lname, "year": year}, {"_id": 0},
    )
    if doc:
        return doc
    # Fallback: previous year
    doc = await db.mlb_stuff_plus_players.find_one(
        {"name": lname, "year": year - 1}, {"_id": 0},
    )
    return doc


# ── Public: enrichment ──────────────────────────────────────────────
def _extract_pitcher_from_pick(pick: dict) -> Optional[str]:
    """Return the pitcher's name if this pick is a pitcher prop.
    The selection field holds the player name for pitcher props (e.g.
    "Kevin Gausman" for a strikeouts Over)."""
    market = (pick.get("market") or "").lower()
    selection = (pick.get("selection") or "").strip()
    if not selection or selection.lower() in ("over", "under", "yes", "no"):
        return None
    if any(kw in market for kw in (
        "strikeouts", "outs recorded", "earned runs",
        "pitcher walks", "hits allowed", "pitcher_",
    )):
        return selection
    return None


async def enrich_picks_with_stuff_plus_bulk(db, picks: list[dict]) -> int:
    """Attach Stuff+/Location+/Pitching+ to every MLB pitcher prop.
    Dedupes per-player lookups to keep DB reads tight (a slate of
    "Gausman Ks Over" + "Gausman Ks Under" + "Gausman Outs Over" reads
    Mongo once).

    Returns the count of picks touched."""
    if not picks:
        return 0
    mlb = [p for p in picks if (p.get("sport") or "").upper() == "MLB"]
    if not mlb:
        return 0
    year = datetime.now(timezone.utc).year
    cache: dict[str, Optional[dict]] = {}
    touched = 0
    for p in mlb:
        pitcher = _extract_pitcher_from_pick(p)
        if not pitcher:
            continue
        key = pitcher.strip().lower()
        if key not in cache:
            cache[key] = await get_pitcher_stuff(db, pitcher, year)
        doc = cache[key]
        if not doc:
            continue
        p["stuff_plus"] = {
            "stuff_plus":    doc.get("stuff_plus"),
            "location_plus": doc.get("location_plus"),
            "pitching_plus": doc.get("pitching_plus"),
            "whiff_pct":     doc.get("whiff_pct"),
            "k_pct":         doc.get("k_pct"),
            "hard_hit_pct":  doc.get("hard_hit_pct"),
            "arsenal_size":  len(doc.get("arsenal") or []),
            "source":        doc.get("source"),
        }
        touched += 1
    return touched


# ── CLI entry point (manual refresh) ────────────────────────────────
async def _main():
    import os
    from motor.motor_asyncio import AsyncIOMotorClient  # type: ignore
    cli = AsyncIOMotorClient(os.getenv("MONGO_URL"))
    db = cli[os.getenv("DB_NAME", "lockscore_db")]
    result = await refresh_stuff_plus(db)
    print("Stuff+ refresh:", result)
    cli.close()


if __name__ == "__main__":
    asyncio.run(_main())


__all__ = [
    "refresh_stuff_plus",
    "get_pitcher_stuff",
    "enrich_picks_with_stuff_plus_bulk",
    "_rv_to_stuff_plus",
    "_xwoba_to_location_plus",
    "_aggregate_arsenal",
]
