"""MLS Player Stats Fetcher — American Soccer Analysis (ASA) integration.

Fixes user report (2026-07-22 / Message 633): MLS player-prop picks
land at Lock Score = 55.0 because the ``soccer_player_form`` collection
carries ZERO MLS players — every MLS scorer / assist market falls to
the "no evidence" branch in ``real_line_scorer_ingest`` which then
maps ``model_prob = book_impl`` and lands at LOW_LOCK_SCORE.

This fetcher populates ``soccer_player_form`` with real MLS xG / xA /
per-90 stats via the public ASA API
(https://app.americansocceranalysis.com/api/v1/mls/players/xgoals) —
no auth required, free, respectful of freeze-after-publish (12h TTL).

Design:
  * Pull once at server startup (once ~60s in), then every 12h.
  * Season autodetected: try current calendar year first, then year - 1
    when the current-season endpoint yields < 100 rows.
  * Store canonical form-row shape so `soccer_feature_resolver` step 1
    (soccer_player_form) hits FIRST — bypasses the goals-only ESPN
    fallback that produced xG=0 for Pep Biel and friends.
  * MLS-specific team-name canonicalization so cross-referencing with
    The Odds API game rows just works.
"""
from __future__ import annotations

import asyncio
import logging
import os
import unicodedata
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

logger = logging.getLogger("lockscore.mls_player_stats")

_ASA_BASE = "https://app.americansocceranalysis.com/api/v1"
_REFRESH_INTERVAL_S = 12 * 60 * 60   # 12h — freeze-after-publish safe
_MIN_MINUTES = 90                     # gates thin sample noise
_HTTP_TIMEOUT = 20.0


def _norm_name(name: str) -> str:
    """Normalise for matching: lowercase + strip accents + trim."""
    if not name:
        return ""
    nk = unicodedata.normalize("NFKD", name)
    return "".join(c for c in nk if not unicodedata.combining(c)).lower().strip()


def _form_label(form_score: float) -> str:
    if form_score >= 70:
        return "HOT"
    if form_score >= 55:
        return "WARM"
    if form_score <= 30:
        return "COLD"
    if form_score <= 45:
        return "COOL"
    return "NEUTRAL"


def _general_position_to_soccer_form(gp: str) -> str:
    """ASA general_position → soccer_player_form position tag.

    Understat uses: F, M, D, GK (sometimes multi like 'F M').
    ASA general_position: ST, W, AM, CM, DM, FB, CB, GK
    """
    if not gp:
        return ""
    gp = gp.upper()
    if gp == "ST":
        return "F S"
    if gp == "W":
        return "F W"
    if gp == "AM":
        return "M AM"
    if gp == "CM":
        return "M"
    if gp == "DM":
        return "M DM"
    if gp == "FB":
        return "D FB"
    if gp == "CB":
        return "D CB"
    if gp == "GK":
        return "GK"
    return gp


async def _fetch_json(cx: httpx.AsyncClient, path: str,
                       params: Optional[dict] = None) -> Any:
    """GET ``{_ASA_BASE}{path}`` and return parsed JSON, or None on fail."""
    try:
        r = await cx.get(f"{_ASA_BASE}{path}", params=params or {})
        if r.status_code != 200:
            logger.warning("ASA %s HTTP %d", path, r.status_code)
            return None
        return r.json()
    except Exception as e:
        logger.warning("ASA %s failed: %s", path, e)
        return None


async def _load_players_map(cx: httpx.AsyncClient) -> dict[str, dict]:
    """player_id → {player_name, birth_date, nationality}."""
    data = await _fetch_json(cx, "/mls/players")
    if not isinstance(data, list):
        return {}
    out = {}
    for row in data:
        pid = row.get("player_id")
        pname = row.get("player_name")
        if pid and pname:
            out[pid] = {
                "player_name": pname,
                "birth_date": row.get("birth_date"),
                "nationality": row.get("nationality"),
            }
    return out


async def _load_teams_map(cx: httpx.AsyncClient) -> dict[str, str]:
    """team_id → team_name."""
    data = await _fetch_json(cx, "/mls/teams")
    if not isinstance(data, list):
        return {}
    out = {}
    for row in data:
        tid = row.get("team_id")
        tname = row.get("team_name")
        if tid and tname:
            out[tid] = tname
    return out


async def _load_xgoals_rows(cx: httpx.AsyncClient, season: str,
                             minimum_minutes: int = _MIN_MINUTES
                             ) -> list[dict]:
    """Load per-player xG / xA / shots / key_passes rows."""
    data = await _fetch_json(cx, "/mls/players/xgoals", {
        "season_name": season,
        "minimum_minutes": minimum_minutes,
    })
    return data if isinstance(data, list) else []


def _team_from_row(row: dict, teams_map: dict[str, str]) -> str:
    """Extract single team name; ASA returns list for mid-season transfers."""
    tid = row.get("team_id")
    if isinstance(tid, list):
        # Most-recent team is typically LAST in the ASA list.
        tid = tid[-1] if tid else ""
    return teams_map.get(tid or "", "") if isinstance(tid, str) else ""


def _build_form_doc(row: dict, players_map: dict[str, dict],
                     teams_map: dict[str, str], season: str) -> Optional[dict]:
    """Convert ASA xgoals row → soccer_player_form document."""
    pid = row.get("player_id")
    if not pid:
        return None
    pmeta = players_map.get(pid, {})
    pname = pmeta.get("player_name")
    if not pname:
        return None
    team = _team_from_row(row, teams_map)
    minutes = int(row.get("minutes_played") or 0)
    if minutes < _MIN_MINUTES:
        return None
    matches90 = minutes / 90.0 if minutes else 1.0

    goals = int(row.get("goals") or 0)
    xg = float(row.get("xgoals") or 0.0)
    xa = float(row.get("xassists") or 0.0)
    assists = int(row.get("primary_assists") or 0)
    shots = int(row.get("shots") or 0)
    sot = int(row.get("shots_on_target") or 0)
    key_passes = int(row.get("key_passes") or 0)

    # ASA doesn't split penalty xG; npxg ≈ xg is a safe conservative
    # proxy for anytime-scorer purposes (penalty share averages ~5-10%
    # across MLS).
    npxg = max(0.0, xg - (goals * 0.05))   # tiny conservative shave

    games_est = max(1, int(round(minutes / 65.0)))   # ~65 min/appearance

    # Form score derived from goals - xG (finishing gap).  Positive gap
    # (converting above expected) → hotter form.  ~35 gap = +30 pts.
    goals_over_xg = goals - xg
    finishing_pct = (goals - xg) / max(1.0, xg) if xg > 0 else 0.0
    form_score = max(10, min(90, 50 + finishing_pct * 40))

    name_canonical = _norm_name(pname)
    doc_id = f"{name_canonical}__MLS__{season}"

    return {
        "_id": doc_id,
        "name_canonical": name_canonical,
        "player_name": pname,
        "team": team,
        "league": "MLS",
        "season": str(season),
        "games": games_est,
        "minutes": minutes,
        "goals": goals,
        "assists": assists,
        "shots": shots,
        "sot": sot,
        "key_passes": key_passes,
        "xg": round(xg, 3),
        "xa": round(xa, 3),
        "npxg": round(npxg, 3),
        # Per-90 (goals / shots / xg / xa / npxg / key_passes)
        "goals_per_90":     round(goals * 90.0 / minutes, 3),
        "assists_per_90":   round(assists * 90.0 / minutes, 3),
        "shots_per_90":     round(shots * 90.0 / minutes, 3),
        "sot_per_90":       round(sot * 90.0 / minutes, 3),
        "xg_per_90":        round(xg * 90.0 / minutes, 3),
        "xa_per_90":        round(xa * 90.0 / minutes, 3),
        "npxg_per_90":      round(npxg * 90.0 / minutes, 3),
        "key_passes_per_90": round(key_passes * 90.0 / minutes, 3),
        "goals_over_xg":    round(goals_over_xg, 3),
        "form_score":       round(form_score, 1),
        "form_label":       _form_label(form_score),
        "position":         _general_position_to_soccer_form(
                                str(row.get("general_position") or "")),
        "source":           "asa",
        "asa_player_id":    pid,
        "updated_at":       datetime.now(timezone.utc),
    }


def _pick_active_season() -> str:
    """Return the season the ASA endpoint will most likely populate.

    MLS season runs Feb–Nov calendar year.  In Jan/Feb the previous
    year's endpoint is still authoritative; in Nov onward the current
    year is the source of truth.
    """
    now = datetime.now(timezone.utc)
    if now.month <= 2:
        return str(now.year - 1)
    return str(now.year)


async def run_once() -> dict:
    """Single ASA fetch + upsert pass.  Returns a small summary dict."""
    from deps import db
    from pymongo import ReplaceOne

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as cx:
        primary_season = _pick_active_season()
        fallback_season = str(int(primary_season) - 1)

        players_map = await _load_players_map(cx)
        teams_map = await _load_teams_map(cx)

        rows = await _load_xgoals_rows(cx, primary_season)
        season_used = primary_season
        if len(rows) < 80:
            # Season likely not started yet — fall back to prior season
            # so we always have data on file.
            logger.info("MLS ASA %s thin (%d rows) — falling back to %s",
                        primary_season, len(rows), fallback_season)
            fb = await _load_xgoals_rows(cx, fallback_season)
            if len(fb) > len(rows):
                rows = fb
                season_used = fallback_season

    if not rows:
        return {"ok": False, "reason": "asa_empty", "season": season_used}

    ops = []
    written = 0
    skipped_thin = 0
    for row in rows:
        doc = _build_form_doc(row, players_map, teams_map, season_used)
        if not doc:
            skipped_thin += 1
            continue
        ops.append(ReplaceOne({"_id": doc["_id"]}, doc, upsert=True))
        written += 1

    if ops:
        try:
            await db.soccer_player_form.bulk_write(ops, ordered=False)
        except Exception as e:
            logger.warning("MLS ASA bulk_write failed: %s", e)
            return {"ok": False, "reason": "bulk_write_error",
                    "error": str(e), "attempted": written}

    logger.info(
        "MLS ASA sync complete: season=%s written=%d skipped_thin=%d "
        "(players_map=%d teams_map=%d)",
        season_used, written, skipped_thin,
        len(players_map), len(teams_map),
    )
    return {
        "ok": True, "season": season_used,
        "written": written, "skipped_thin": skipped_thin,
        "players_map": len(players_map),
        "teams_map": len(teams_map),
    }


async def loop() -> None:
    """Fire-and-forget background refresher (every 12h).

    After each successful ASA fetch, trigger the in-place MLS prop
    repair so any stale picks (from before ASA data existed) upgrade
    to their honest Lock Score immediately — no waiting for the next
    scheduled odds ingest.
    """
    # Wait a short warm-up so DB / other startup tasks finish first.
    await asyncio.sleep(60)
    while True:
        try:
            summary = await run_once()
            logger.info("MLS ASA player-stats cycle: %s", summary)
            # Trigger in-place prop repair when ASA sync succeeded.
            if summary.get("ok") and summary.get("written", 0) > 0:
                try:
                    from services.mls_prop_repair import repair_mls_props
                    from deps import db
                    repair = await repair_mls_props(db)
                    logger.info(
                        "MLS prop repair applied: on_board_new=%d "
                        "updated_up=%d updated_down=%d off_board_new=%d",
                        repair.get("on_board_new", 0),
                        repair.get("updated_up", 0),
                        repair.get("updated_down", 0),
                        repair.get("off_board_new", 0),
                    )
                except Exception as _rp_err:
                    logger.warning("MLS prop repair post-ASA failed: %s",
                                    _rp_err)
        except Exception as e:
            logger.warning("MLS ASA cycle failed: %s", e)
        await asyncio.sleep(_REFRESH_INTERVAL_S)


__all__ = ["run_once", "loop"]
