"""Tennis player DB ingestor — Sackmann-format match data via the
publicly-mirrored Tennismylife/TML-Database repo on GitHub.

What this gives us (FREE, no key):
  • Every ATP player who has played a tour-level match since 1968
  • Per-player: hand, height, IOC nationality, last-known rank, last
    age, match count, surface splits (Hard / Clay / Grass / Carpet)
  • Recent match history (configurable window — default last 24 months)
  • Head-to-head data derivable from `tennis_matches` collection

Strategy:
  1. One-time bulk load: fetch last N years of CSVs in parallel
  2. Stream-parse each row → players + tennis_matches upserts
  3. Idempotent — re-runs just refresh in place
  4. Monthly refresh loop keeps everything fresh

Sackmann's ATP CSVs are also mirrored monthly so even though we can't
hit JeffSackmann/tennis_atp directly from this container, the data is
identical.
"""
from __future__ import annotations

import asyncio
import csv
import io
import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger("lockscore.player_db.tennis")

_BASE = "https://raw.githubusercontent.com/Tennismylife/TML-Database/master"
_HEADERS = {"User-Agent": "Mozilla/5.0 (PerksLocks/1.0)"}
_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_SEM = asyncio.Semaphore(6)             # one CSV per file — be polite


def _canonical(name: str) -> str:
    return (name or "").strip().lower()


def _safe_int(v: Any) -> int | None:
    try:
        if v is None or v == "":
            return None
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _safe_float(v: Any) -> float | None:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


async def _fetch_year_csv(client: httpx.AsyncClient, year: int) -> str | None:
    """Download one year's match CSV. Returns text or None on error."""
    async with _SEM:
        try:
            url = f"{_BASE}/{year}.csv"
            r = await client.get(url, timeout=_TIMEOUT)
            if r.status_code == 200:
                return r.text
            logger.warning("TML-Database %d → HTTP %d", year, r.status_code)
        except Exception as e:
            logger.warning("TML-Database %d exception: %s", year, e)
        return None


def _parse_year(text: str, year: int) -> list[dict]:
    """Stream-parse a year CSV into a list of normalised match dicts.
    Each row yields one match record we can derive player + match data from."""
    rows: list[dict] = []
    reader = csv.DictReader(io.StringIO(text))
    for r in reader:
        try:
            tdate = r.get("tourney_date") or ""
            iso_date = None
            if tdate and len(tdate) == 8:
                try:
                    iso_date = datetime.strptime(tdate, "%Y%m%d").date().isoformat()
                except ValueError:
                    iso_date = None
            rows.append({
                "tour": "atp",
                "year": year,
                "tourney_id":    r.get("tourney_id"),
                "tourney_name":  r.get("tourney_name"),
                "tourney_level": r.get("tourney_level"),
                "tourney_date":  iso_date,
                "surface":       r.get("surface"),
                "draw_size":     _safe_int(r.get("draw_size")),
                "match_num":     _safe_int(r.get("match_num")),
                "round":         r.get("round"),
                "best_of":       _safe_int(r.get("best_of")),
                "winner_id":     r.get("winner_id") or None,
                "winner_name":   r.get("winner_name"),
                "winner_hand":   r.get("winner_hand"),
                "winner_ht":     _safe_int(r.get("winner_ht")),
                "winner_ioc":    r.get("winner_ioc"),
                "winner_age":    _safe_float(r.get("winner_age")),
                "winner_rank":   _safe_int(r.get("winner_rank")),
                "winner_rank_points": _safe_int(r.get("winner_rank_points")),
                "loser_id":      r.get("loser_id") or None,
                "loser_name":    r.get("loser_name"),
                "loser_hand":    r.get("loser_hand"),
                "loser_ht":      _safe_int(r.get("loser_ht")),
                "loser_ioc":     r.get("loser_ioc"),
                "loser_age":     _safe_float(r.get("loser_age")),
                "loser_rank":    _safe_int(r.get("loser_rank")),
                "loser_rank_points": _safe_int(r.get("loser_rank_points")),
                "score":         r.get("score"),
                "minutes":       _safe_int(r.get("minutes")),
            })
        except Exception as e:
            logger.debug("row parse failed for %s/%s: %s", year, r.get("match_num"), e)
    return rows


async def _upsert_match(db: AsyncIOMotorDatabase, m: dict) -> None:
    """Idempotent match upsert keyed by (tour, tourney_id, match_num)."""
    key = {
        "tour":       m.get("tour"),
        "tourney_id": m.get("tourney_id"),
        "match_num":  m.get("match_num"),
    }
    await db.tennis_matches.update_one(key, {"$set": m}, upsert=True)


def _player_doc_from_rows(player_rows: list[dict]) -> dict:
    """Aggregate every match-row touching a player into a single profile
    doc with surface splits + latest-known metadata."""
    name = player_rows[0]["name"]
    sport = "tennis"

    # Latest known metadata wins — sort by date descending
    sorted_rows = sorted(player_rows, key=lambda r: r.get("date") or "", reverse=True)
    latest = sorted_rows[0]

    surfaces: dict[str, dict[str, int]] = {}
    wins = losses = 0
    for r in player_rows:
        s = (r.get("surface") or "Unknown")
        slot = surfaces.setdefault(s, {"wins": 0, "losses": 0})
        if r["won"]:
            wins += 1
            slot["wins"] += 1
        else:
            losses += 1
            slot["losses"] += 1

    return {
        "sport":          sport,
        "tour":           "atp",
        "player_id":      latest.get("player_id"),
        "name":           name,
        "canonical_name": _canonical(name),
        "first_name":     name.split()[0] if name else None,
        "last_name":      name.split()[-1] if name else None,
        "hand":           latest.get("hand"),
        "ioc":            latest.get("ioc"),
        "height":         latest.get("ht"),
        "age":            latest.get("age"),
        "rank":           latest.get("rank"),
        "rank_points":    latest.get("rank_points"),
        "matches":        wins + losses,
        "wins":           wins,
        "losses":         losses,
        "win_pct":        round(wins / max(1, wins + losses), 3),
        "surface_splits": surfaces,
        "last_match_at":  latest.get("date"),
        "source":         "tml_sackmann_mirror",
        "updated_at":     datetime.now(timezone.utc).isoformat(),
    }


async def refresh_atp(
    db: AsyncIOMotorDatabase,
    *,
    years: int = 10,
) -> dict:
    """Bulk-load last `years` years of ATP match data and rebuild the
    player profiles. Default 10y is enough to cover every active player
    while keeping the row count manageable (~3-5k matches/year ≈ 40k)."""
    started = time.time()
    current_year = datetime.now(timezone.utc).year
    year_list = list(range(current_year - years + 1, current_year + 1))

    matches_written = 0
    player_rows_by_id: dict[str, list[dict]] = {}
    csv_errors = 0

    async with httpx.AsyncClient(headers=_HEADERS) as client:
        # Fetch all years in parallel
        texts = await asyncio.gather(
            *[_fetch_year_csv(client, y) for y in year_list],
            return_exceptions=False,
        )

    for year, text in zip(year_list, texts):
        if not text:
            csv_errors += 1
            continue
        rows = _parse_year(text, year)
        # Persist matches (in modestly-sized batches; idempotent)
        for r in rows:
            await _upsert_match(db, r)
            matches_written += 1
            # Build per-player aggregation feed (winner)
            wid = r.get("winner_id")
            if wid:
                player_rows_by_id.setdefault(wid, []).append({
                    "player_id": wid,
                    "name":      r.get("winner_name"),
                    "hand":      r.get("winner_hand"),
                    "ht":        r.get("winner_ht"),
                    "ioc":       r.get("winner_ioc"),
                    "age":       r.get("winner_age"),
                    "rank":      r.get("winner_rank"),
                    "rank_points": r.get("winner_rank_points"),
                    "date":      r.get("tourney_date"),
                    "surface":   r.get("surface"),
                    "won":       True,
                })
            # And loser
            lid = r.get("loser_id")
            if lid:
                player_rows_by_id.setdefault(lid, []).append({
                    "player_id": lid,
                    "name":      r.get("loser_name"),
                    "hand":      r.get("loser_hand"),
                    "ht":        r.get("loser_ht"),
                    "ioc":       r.get("loser_ioc"),
                    "age":       r.get("loser_age"),
                    "rank":      r.get("loser_rank"),
                    "rank_points": r.get("loser_rank_points"),
                    "date":      r.get("tourney_date"),
                    "surface":   r.get("surface"),
                    "won":       False,
                })

    # Build and upsert every player profile
    players_upserted = 0
    for pid, rows in player_rows_by_id.items():
        # Pick the most recent name spelling (canonical) for the profile.
        valid_rows = [r for r in rows if r.get("name")]
        if not valid_rows:
            continue
        doc = _player_doc_from_rows(valid_rows)
        await db.players.update_one(
            {"sport": "tennis", "player_id": pid},
            {"$set": doc, "$setOnInsert": {"created_at": datetime.now(timezone.utc).isoformat()}},
            upsert=True,
        )
        players_upserted += 1

    await _ensure_indexes(db)

    summary = {
        "ok": True,
        "tour": "atp",
        "years_pulled": [y for y, t in zip(year_list, texts) if t],
        "csv_errors": csv_errors,
        "matches_written": matches_written,
        "players_upserted": players_upserted,
        "elapsed_sec": round(time.time() - started, 1),
    }
    logger.info("Tennis ATP player_db refresh: %s", summary)
    return summary


async def _ensure_indexes(db: AsyncIOMotorDatabase) -> None:
    """Hot-path indexes for the tennis player + match collections."""
    await db.players.create_index([("sport", 1), ("canonical_name", 1)])
    await db.players.create_index(
        [("sport", 1), ("player_id", 1)],
        unique=True,
        partialFilterExpression={"player_id": {"$type": "string"}},
        name="sport_tennis_player_id_partial",
    )
    await db.tennis_matches.create_index(
        [("tour", 1), ("tourney_id", 1), ("match_num", 1)],
        unique=True, name="tour_tourney_match_unique",
    )
    await db.tennis_matches.create_index([("winner_name", 1), ("tourney_date", -1)])
    await db.tennis_matches.create_index([("loser_name", 1), ("tourney_date", -1)])
    await db.tennis_matches.create_index([("surface", 1), ("tourney_date", -1)])
