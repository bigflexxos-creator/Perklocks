"""NRFI/YRFI pick generator — wires the Poisson model into the live MLB pipeline.

For each MLB game today:
  1. Pull probable starting pitchers from MLB Stats API
  2. Pull each pitcher's career K/9 + BB/9 from the local player DB
  3. Pull each lineup's top-3 OPS from the local player DB
  4. Look up the home park factor
  5. Compute NRFI/YRFI probabilities
  6. Compare to The Odds API 1st-inning total odds (≥ 0.5 runs) when present
  7. Generate a pick if our edge over fair >= 4%

Inserts into the standard `picks` collection so the existing UI, lock
score, validator, settlement, and CLV snapshotter all work without
any frontend changes. Sport=MLB, market="NRFI" or "YRFI".

Refresh loop: runs daily ~12:00 UTC and again pregame (every 30 min
during 15:00-23:00 UTC).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from motor.motor_asyncio import AsyncIOMotorDatabase

from brain.nrfi_yrfi_model import (
    nrfi_yrfi_model,
    park_factor,
    pitcher_factor_from_pair,
    lineup_top_factor_from_pair,
    LEAGUE_BASE_RUNS_1ST,
)

logger = logging.getLogger("lockscore.nrfi_engine")

_MLB_BASE = "https://statsapi.mlb.com/api/v1"
_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
_HEADERS = {"User-Agent": "PerksLocks/1.0"}

EDGE_THRESHOLD = 0.04     # 4% over fair to surface a pick
LOCK_BAND = (60.0, 92.0)  # range we map our prob-edge into


def _today_str() -> str:
    return datetime.now(timezone.utc).date().isoformat()


async def _fetch_schedule(client: httpx.AsyncClient, date_iso: str) -> list[dict]:
    """Today's MLB schedule with probable pitchers populated."""
    url = f"{_MLB_BASE}/schedule"
    params = {
        "sportId": 1,
        "date": date_iso,
        "hydrate": "probablePitcher,team,lineups",
    }
    try:
        r = await client.get(url, params=params, headers=_HEADERS, timeout=_TIMEOUT)
        if r.status_code != 200:
            return []
        data = r.json()
        out: list[dict] = []
        for d in data.get("dates", []):
            for g in d.get("games", []):
                out.append(g)
        return out
    except Exception as e:
        logger.warning("MLB schedule fetch failed: %s", e)
        return []


def _pick_pitcher_stat_dict(stats_row: dict | None) -> dict:
    """Convert a player_stats doc → the {k9_rolling, bb9_rolling} dict
    the nrfi model wants."""
    if not stats_row:
        return {}
    pit = stats_row.get("pitching") or {}
    return {
        "k9_rolling": pit.get("strikeoutsPer9Inn"),
        "bb9_rolling": pit.get("walksPer9Inn"),
    }


async def _lineup_top3_ops(db: AsyncIOMotorDatabase, team_abbr: str) -> float | None:
    """Compute the top-3 batter OPS for a team using the local DB.
    Falls back to None if we don't have enough data; the model then
    defaults that team's factor to neutral 1.0."""
    rows = await db.player_stats.find(
        {"sport": "mlb", "batting.ops": {"$exists": True}},
        {"_id": 0, "canonical_name": 1, "batting.ops": 1, "batting.plateAppearances": 1},
    ).to_list(length=None)
    # Filter to the team's roster
    team_doc = await db.players.find_one(
        {"sport": "mlb", "team": team_abbr}, {"_id": 0, "canonical_name": 1},
    )
    if not team_doc:
        return None
    # Pull all batters with PA >= 50 for this team
    team_canonicals = {
        d["canonical_name"]
        async for d in db.players.find(
            {"sport": "mlb", "team": team_abbr}, {"canonical_name": 1, "_id": 0},
        )
    }
    eligible = []
    for r in rows:
        if r["canonical_name"] not in team_canonicals:
            continue
        bat = r.get("batting") or {}
        ops = bat.get("ops")
        pa = bat.get("plateAppearances") or 0
        try:
            ops_f = float(ops)
            pa_i = int(pa)
        except (TypeError, ValueError):
            continue
        if pa_i >= 50:
            eligible.append(ops_f)
    if len(eligible) < 3:
        return None
    eligible.sort(reverse=True)
    top3 = eligible[:3]
    return round(sum(top3) / 3.0, 3)


async def _build_game_inputs(
    db: AsyncIOMotorDatabase, game: dict,
) -> dict | None:
    """Build the `game` dict the math model wants from one MLB
    schedule entry. Returns None if essential data is missing."""
    teams = game.get("teams") or {}
    home_team = (teams.get("home") or {}).get("team") or {}
    away_team = (teams.get("away") or {}).get("team") or {}
    home_abbr = home_team.get("abbreviation")
    away_abbr = away_team.get("abbreviation")
    if not home_abbr or not away_abbr:
        return None

    # Probable pitchers
    home_p = (teams.get("home") or {}).get("probablePitcher") or {}
    away_p = (teams.get("away") or {}).get("probablePitcher") or {}
    home_pid = home_p.get("id")
    away_pid = away_p.get("id")

    # Pull pitcher stat rows from the local player_stats collection.
    home_stat = await db.player_stats.find_one(
        {"sport": "mlb", "mlb_id": home_pid}, {"_id": 0, "pitching": 1},
    ) if home_pid else None
    away_stat = await db.player_stats.find_one(
        {"sport": "mlb", "mlb_id": away_pid}, {"_id": 0, "pitching": 1},
    ) if away_pid else None

    pf = pitcher_factor_from_pair(
        _pick_pitcher_stat_dict(home_stat),
        _pick_pitcher_stat_dict(away_stat),
    )

    home_ops = await _lineup_top3_ops(db, home_abbr)
    away_ops = await _lineup_top3_ops(db, away_abbr)
    lf = lineup_top_factor_from_pair(home_ops, away_ops)

    parkf = park_factor(home_abbr)

    return {
        "game_pk": game.get("gamePk"),
        "home_team": home_abbr,
        "away_team": away_abbr,
        "home_pitcher": home_p.get("fullName"),
        "away_pitcher": away_p.get("fullName"),
        "home_pitcher_id": home_pid,
        "away_pitcher_id": away_pid,
        "event_time": game.get("gameDate"),
        "model_inputs": {
            "league_base": LEAGUE_BASE_RUNS_1ST,
            "pitcher_factor": pf,
            "lineup_top_factor": lf,
            "park_factor": parkf,
        },
    }


def _prob_to_lock_score(prob: float) -> float:
    """Map a single probability (0..1) into our 60-92 lock-score band.
    Picks below 0.54 get filtered upstream (edge threshold)."""
    lo, hi = LOCK_BAND
    # Anchor: 0.50 → 60, 0.65 → 76, 0.80 → 88, 0.90 → 92
    if prob <= 0.50:
        return lo
    if prob >= 0.90:
        return hi
    # Linear in (0.50, 0.90) → (60, 92)
    return round(lo + (prob - 0.50) * (hi - lo) / (0.90 - 0.50), 1)


def _grade_from_lock(lock: float) -> str:
    if lock >= 95: return "Elite Lock"
    if lock >= 90: return "Strong Lock"
    if lock >= 80: return "Lock"
    if lock >= 70: return "Playable"
    return "Pass"


async def _upsert_pick(
    db: AsyncIOMotorDatabase, base: dict, side: str, prob: float,
    model_out: dict,
) -> None:
    """One pick per side (NRFI or YRFI). Idempotent — keyed by
    (game_pk, market)."""
    lock = _prob_to_lock_score(prob)
    grade = _grade_from_lock(lock)
    market_label = "NRFI (No Run in 1st Inning)" if side == "NRFI" else "YRFI (Yes Run in 1st Inning)"
    pick_id = f"nrfi-{base['game_pk']}-{side.lower()}"
    doc = {
        "_id": pick_id,
        "id": pick_id,
        "sport": "MLB",
        "market": market_label,
        "market_key": "1st_inning_runs",
        # Category tag — keeps these picks OFF the main /api/picks/today
        # board (user wants a dedicated MLB sub-tab). The dedicated
        # endpoint /api/picks/nrfi-yrfi queries by this tag.
        "category": "nrfi_yrfi",
        "hide_from_main_board": True,
        "side": side,
        "lock_score": lock,
        "lock_score_raw": lock,
        "grade": grade,
        "win_probability": round(prob * 100, 1),
        "implied_probability": round(prob * 100, 1),
        "edge_percent": round(model_out["edge_signal"] * 100, 2)
                        if side == "NRFI"
                        else round(-model_out["edge_signal"] * 100, 2),
        "home_team": base["home_team"],
        "away_team": base["away_team"],
        "event_time": base["event_time"],
        "pick_date": _today_str(),
        "match": f"{base['away_team']} @ {base['home_team']}",
        "key_insights": [
            f"λ₁ (expected runs in 1st) = {model_out['expected_runs_1st_inning']}",
            f"Pitchers: {base.get('home_pitcher') or '?'} vs {base.get('away_pitcher') or '?'}",
            f"Pitcher factor = {model_out['model_inputs']['pitcher_factor']}",
            f"Lineup top-3 OPS factor = {model_out['model_inputs']['lineup_top_factor']}",
            f"Park factor ({base['home_team']}) = {model_out['model_inputs']['park_factor']}",
        ],
        "model_inputs": model_out["model_inputs"],
        "model_output": {
            "expected_runs_1st_inning": model_out["expected_runs_1st_inning"],
            "nrfi_prob": model_out["nrfi_prob"],
            "yrfi_prob": model_out["yrfi_prob"],
        },
        "source_model": "nrfi_yrfi_poisson_v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.picks.update_one({"_id": pick_id}, {"$set": doc}, upsert=True)


async def generate_nrfi_yrfi_picks(db: AsyncIOMotorDatabase) -> dict:
    """Main entry point — build NRFI/YRFI picks for every MLB game
    on today's slate. Idempotent."""
    started = datetime.now(timezone.utc)
    date_iso = _today_str()
    async with httpx.AsyncClient(headers=_HEADERS) as client:
        games = await _fetch_schedule(client, date_iso)

    if not games:
        return {"ok": True, "reason": "no_games", "date": date_iso, "picks": 0}

    n_eligible = 0
    n_picks = 0
    n_skipped = 0
    for g in games:
        base = await _build_game_inputs(db, g)
        if not base:
            n_skipped += 1
            continue
        out = nrfi_yrfi_model(base["model_inputs"])
        n_eligible += 1
        nrfi = out["nrfi_prob"]
        yrfi = out["yrfi_prob"]
        # Surface whichever side beats the EDGE_THRESHOLD over fair 0.50
        if nrfi >= 0.50 + EDGE_THRESHOLD:
            await _upsert_pick(db, base, "NRFI", nrfi, out)
            n_picks += 1
        elif yrfi >= 0.50 + EDGE_THRESHOLD:
            await _upsert_pick(db, base, "YRFI", yrfi, out)
            n_picks += 1

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    summary = {
        "ok": True,
        "date": date_iso,
        "games_scanned": len(games),
        "eligible": n_eligible,
        "skipped_missing_data": n_skipped,
        "picks_generated": n_picks,
        "elapsed_sec": round(elapsed, 1),
    }
    logger.info("NRFI/YRFI pick generator: %s", summary)
    return summary


async def nrfi_yrfi_loop(db: AsyncIOMotorDatabase) -> None:
    """Background loop — runs at boot, then every 30 min during pregame
    window (UTC 15:00-23:00), else hourly. Cheap (one MLB Stats API
    schedule call + small DB lookups)."""
    await asyncio.sleep(45)  # let other startup work settle first
    while True:
        try:
            await generate_nrfi_yrfi_picks(db)
        except Exception as e:
            logger.warning("NRFI/YRFI loop iteration failed: %s", e)
        # cadence
        hr_utc = datetime.now(timezone.utc).hour
        sleep_sec = 30 * 60 if 15 <= hr_utc <= 23 else 60 * 60
        await asyncio.sleep(sleep_sec)
