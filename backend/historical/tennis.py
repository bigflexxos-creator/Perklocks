"""Tennis historical client — uses Tennismylife's free GitHub mirror of
the Jeff Sackmann ATP schema. Jeff Sackmann's `tennis_atp` repo was
delisted from the public GitHub API as of mid-2026; the Tennismylife
mirror (https://github.com/Tennismylife/TML-Database) preserves the
exact same column schema and goes back to 1968.

Data source (CC-BY, no key required):
  • ATP: https://raw.githubusercontent.com/Tennismylife/TML-Database/master/{YYYY}.csv
    1968 → 2026+ available, one file per calendar year.

WTA: not currently mirrored on a free GitHub source we can access. The
existing `player_db/ingestors/tennis_sackmann.py` (ESPN public) handles
current-year WTA player intel; multi-year WTA backfill is deferred to
Phase 3 of the historical ingestion plan.

Each ATP year file contains ~3,000 main-draw matches with per-match
stats: aces, double_faults, service points, break points, etc. Enough
to derive Aces / Double-Faults / Total Games props at the match level
(no per-set logs).

This client populates:
  • `players` (sport='tennis')
  • `games`   — one row per match (sport='tennis')
  • `player_game_logs` — TWO rows per match (winner + loser perspective)
    with fields: aces, double_faults, service_points, first_serves_in,
    break_points_saved, total_games_match (sum of all games).
"""
from __future__ import annotations

import asyncio
import csv
import io
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger("lockscore.historical.tennis")

# Primary source: Tennismylife mirror — same schema as Sackmann, one
# file per year named `{YYYY}.csv` at the repo root.
_TML_BASE = "https://raw.githubusercontent.com/Tennismylife/TML-Database/master"
_TIMEOUT = 60.0
_PACE = 1.0  # 1 req/sec — GitHub raw is generous

_CURRENT_YEAR = datetime.now(timezone.utc).year


async def _get_csv(cx: httpx.AsyncClient, url: str) -> Optional[list[dict]]:
    try:
        r = await cx.get(url)
        if r.status_code == 200:
            text = r.text
            reader = csv.DictReader(io.StringIO(text))
            return list(reader)
        logger.warning("Tennis %s → %s", url, r.status_code)
    except Exception as e:
        logger.warning("Tennis %s exception: %s", url, e)
    return None


def _to_int(v) -> Optional[int]:
    try:
        if v in (None, "", "NA"):
            return None
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _parse_score_total_games(score: str) -> Optional[int]:
    """Count total games from a score string like '6-4 7-6(3) 6-7(5) 6-2'.

    Handles tiebreaks (the (n) annotation is ignored), retirements (RET),
    and walkovers (W/O). Returns None if we can't parse anything.
    """
    if not score:
        return None
    score_clean = str(score).strip()
    if not score_clean or score_clean.upper() in ("W/O", "WALKOVER", "DEF."):
        return None
    total = 0
    found_any = False
    for set_part in score_clean.split():
        # Strip tiebreak annotation "(7)" etc.
        sp = set_part.split("(")[0]
        # Common formats: "6-4", "7-6", "10-8" (super-tiebreak)
        if "-" not in sp:
            continue
        try:
            a, b = sp.split("-", 1)
            ai = int(a)
            bi = int(b)
            total += ai + bi
            found_any = True
        except (ValueError, IndexError):
            continue
    return total if found_any else None


async def _ingest_tour_year(
    cx: httpx.AsyncClient, db, *, tour: str, year: int,
) -> dict:
    """Pull and parse one (tour, year) CSV. Stores matches + player logs.

    Currently only `tour='atp'` is wired (via TML mirror). WTA is
    deferred until a free WTA Sackmann-schema mirror is available.
    """
    if tour == "atp":
        url = f"{_TML_BASE}/{year}.csv"
        sport_id_prefix = "atp"
    elif tour == "wta":
        return {
            "tour": tour,
            "year": year,
            "skipped": "wta_no_free_multi_year_mirror",
            "note": ("Sackmann tennis_wta repo is not publicly listed; "
                     "current-year WTA player intel is handled by "
                     "player_db/ingestors/tennis_sackmann.py."),
        }
    else:
        return {"error": f"unknown tour {tour}"}

    rows = await _get_csv(cx, url)
    await asyncio.sleep(_PACE)
    if not rows:
        return {"tour": tour, "year": year, "matches": 0, "skipped": "no_data"}

    matches_inserted = logs_inserted = 0
    for r in rows:
        try:
            match_id = (r.get("match_num") or "").strip() or (r.get("tourney_id") or "")
            tourney_date = (r.get("tourney_date") or "").strip()
            # tourney_date is YYYYMMDD; normalize to ISO
            try:
                date_iso = datetime.strptime(tourney_date, "%Y%m%d").replace(
                    tzinfo=timezone.utc).isoformat()
            except ValueError:
                date_iso = None

            winner_id = (r.get("winner_id") or "").strip()
            loser_id = (r.get("loser_id") or "").strip()
            winner_name = (r.get("winner_name") or "").strip()
            loser_name = (r.get("loser_name") or "").strip()
            if not winner_id or not loser_id:
                continue

            game_id = f"sackmann_{tour}_{year}_{r.get('tourney_id','')}_{match_id}"
            total_games = _parse_score_total_games(r.get("score") or "")

            await db.games.update_one(
                {"game_id": game_id, "sport": "tennis"},
                {"$set": {
                    "sport": "tennis",
                    "game_id": game_id,
                    "tour": tour,
                    "season": int(year),
                    "date": date_iso,
                    "tourney_name": (r.get("tourney_name") or "").strip(),
                    "surface": (r.get("surface") or "").strip(),
                    "round": (r.get("round") or "").strip(),
                    "winner_name": winner_name,
                    "loser_name": loser_name,
                    "winner_id": f"{sport_id_prefix}_{winner_id}",
                    "loser_id": f"{sport_id_prefix}_{loser_id}",
                    "score": r.get("score"),
                    "total_games_match": total_games,
                    "status": "Final",
                }},
                upsert=True,
            )
            matches_inserted += 1

            # Upsert both players
            for side in ("winner", "loser"):
                pid_raw = r.get(f"{side}_id")
                pname = r.get(f"{side}_name")
                if not pid_raw:
                    continue
                pid = f"{sport_id_prefix}_{pid_raw}"
                await db.players.update_one(
                    {"player_id": pid, "sport": "tennis"},
                    {"$set": {
                        "player_id": pid,
                        "sport": "tennis",
                        "name": pname,
                        "tour": tour.upper(),
                        "hand": (r.get(f"{side}_hand") or "").strip() or None,
                        "country": (r.get(f"{side}_ioc") or "").strip() or None,
                    }},
                    upsert=True,
                )
                # Per-side stat block — Sackmann uses w_ace / l_ace etc.
                p = "w" if side == "winner" else "l"
                aces = _to_int(r.get(f"{p}_ace"))
                df = _to_int(r.get(f"{p}_df"))
                svpt = _to_int(r.get(f"{p}_svpt"))
                first_in = _to_int(r.get(f"{p}_1stIn"))
                first_won = _to_int(r.get(f"{p}_1stWon"))
                second_won = _to_int(r.get(f"{p}_2ndWon"))
                bp_saved = _to_int(r.get(f"{p}_bpSaved"))
                bp_faced = _to_int(r.get(f"{p}_bpFaced"))

                log_doc = {
                    "player_id": pid,
                    "game_id": game_id,
                    "sport": "tennis",
                    "season": int(year),
                    "date": date_iso,
                    "tour": tour,
                    "name": pname,
                    "outcome": "W" if side == "winner" else "L",
                    "aces": aces,
                    "double_faults": df,
                    "service_points": svpt,
                    "first_serves_in": first_in,
                    "first_serve_won": first_won,
                    "second_serve_won": second_won,
                    "break_points_saved": bp_saved,
                    "break_points_faced": bp_faced,
                    "total_games_match": total_games,
                }
                await db.player_game_logs.update_one(
                    {"player_id": pid, "game_id": game_id, "sport": "tennis"},
                    {"$set": log_doc},
                    upsert=True,
                )
                logs_inserted += 1
        except Exception as e:
            logger.warning("Tennis row parse failed: %s", e)
            continue

    return {
        "tour": tour,
        "year": year,
        "rows_in_csv": len(rows),
        "matches_inserted": matches_inserted,
        "player_logs_inserted": logs_inserted,
    }


async def backfill_season(db, season: int) -> dict:
    """Ingest one season of ATP + WTA matches from Sackmann GitHub.

    `season` is a calendar year. Returns combined ATP + WTA summary.
    """
    season = int(season)
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers={"User-Agent": "PerksLocks/1.0"}) as cx:
        atp = await _ingest_tour_year(cx, db, tour="atp", year=season)
        wta = await _ingest_tour_year(cx, db, tour="wta", year=season)
    return {
        "season": season,
        "atp": atp,
        "wta": wta,
        "matches_inserted": (atp.get("matches_inserted") or 0) + (wta.get("matches_inserted") or 0),
        "player_logs_inserted": (atp.get("player_logs_inserted") or 0) + (wta.get("player_logs_inserted") or 0),
    }


async def backfill_current_season(db) -> dict:
    """Backward-compatible wrapper — backfills the current calendar year."""
    return await backfill_season(db, _CURRENT_YEAR)


async def incremental_sync(db, since=None) -> dict:
    """Tennis CSVs are released continuously through the year — re-pull
    the current year file, which is idempotent. Past years rarely change."""
    return await backfill_season(db, _CURRENT_YEAR)
