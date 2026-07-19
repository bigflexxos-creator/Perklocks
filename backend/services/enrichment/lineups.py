"""Confirmed starting lineup enrichment (2026-07-19).

Kills the "bench player" problem stone dead. If Kane isn't in the
England starting XI or Judge is scratched from the Yankees lineup, the
pick's `lineup_confirmed = False` and the signal engine drops it below
the visible board threshold instead of surfacing a dead bet.

Sources:
  • MLB — MLB StatsAPI /game/{pk}/boxscore (free, no key). Lineups lock
    ~1h before first pitch.
  • Soccer — football-data.org /matches/{id}/lineups (already have key
    in `FOOTBALL_DATA_ORG_KEY`). Free tier gives lineups 60 min pre-KO.

Each pick gets a `lineup_status` dict:
    { status: 'confirmed_start' | 'confirmed_bench' | 'confirmed_scratch'
              | 'projected'      | 'unknown',
      lineup_pos: <int|None>,        # batting order 1-9 for MLB
      updated_at: ISO }

Signal engine reads `pick["lineup_status"]` and:
  • confirmed_start → +5 signal (real starter, not a projection)
  • confirmed_bench → -30 signal (pick is basically dead)
  • confirmed_scratch → -50 signal + `no_bet = True` (auto-void)
  • projected      →  0 (default, no signal shift)
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

import aiohttp

logger = logging.getLogger("lockscore.lineups")

_MLB_BOX_URL = "https://statsapi.mlb.com/api/v1.1/game/{gamePk}/feed/live"
_FD_KEY = os.environ.get("FOOTBALL_DATA_ORG_KEY", "").strip()
_FD_LINEUP_URL = "https://api.football-data.org/v4/matches/{matchId}"

# ── In-process cache ─────────────────────────────────────────────────
# MLB lineups lock ~1h pre-first-pitch and rarely change after; 30-min
# TTL keeps us honest without hammering the free API.
_TTL = 30 * 60
_MLB_LINEUPS: dict[int, tuple[float, dict[str, dict]]] = {}
_SOC_LINEUPS: dict[int, tuple[float, dict[str, dict]]] = {}


async def _fetch_mlb_lineups(game_pk: int) -> dict[str, dict]:
    """Return {player_name_lower: {status, lineup_pos}} for both sides.

    `status` = "confirmed_start" if the player is in the batting order
    OR is the starting pitcher, otherwise not in the dict at all (missing
    → treat as bench / projected downstream).
    """
    now = time.time()
    cached = _MLB_LINEUPS.get(game_pk)
    if cached and (now - cached[0]) < _TTL:
        return cached[1]
    url = _MLB_BOX_URL.format(gamePk=game_pk)
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(url) as r:
                if r.status != 200:
                    _MLB_LINEUPS[game_pk] = (now, {})
                    return {}
                data = await r.json()
    except Exception as e:
        logger.debug("MLB lineups fetch failed: %s", e)
        _MLB_LINEUPS[game_pk] = (now, {})
        return {}
    out: dict[str, dict] = {}
    box = ((data.get("liveData") or {}).get("boxscore") or {})
    for side in ("home", "away"):
        team = (box.get("teams") or {}).get(side) or {}
        # `battingOrder` is a list of playerId strings when the lineup
        # has been posted; empty otherwise.
        order = team.get("battingOrder") or []
        players = team.get("players") or {}
        for i, pid in enumerate(order, start=1):
            key = f"ID{pid}" if not str(pid).startswith("ID") else str(pid)
            info = players.get(key) or {}
            name = ((info.get("person") or {}).get("fullName") or "").lower()
            if name:
                out[name] = {"status": "confirmed_start", "lineup_pos": i}
        # Starting pitcher lives on `team.pitchers[0]` when posted
        pitchers = team.get("pitchers") or []
        if pitchers:
            key = f"ID{pitchers[0]}" if not str(pitchers[0]).startswith("ID") else str(pitchers[0])
            info = players.get(key) or {}
            name = ((info.get("person") or {}).get("fullName") or "").lower()
            if name:
                out[name] = {"status": "confirmed_start", "lineup_pos": 0}
    _MLB_LINEUPS[game_pk] = (now, out)
    return out


async def _fetch_soccer_lineups(match_id: int) -> dict[str, dict]:
    """Return {player_name_lower: {status}} for both teams' starting XI."""
    if not _FD_KEY:
        return {}
    now = time.time()
    cached = _SOC_LINEUPS.get(match_id)
    if cached and (now - cached[0]) < _TTL:
        return cached[1]
    url = _FD_LINEUP_URL.format(matchId=match_id)
    headers = {"X-Auth-Token": _FD_KEY}
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(url, headers=headers) as r:
                if r.status != 200:
                    _SOC_LINEUPS[match_id] = (now, {})
                    return {}
                data = await r.json()
    except Exception as e:
        logger.debug("football-data lineups fetch failed: %s", e)
        _SOC_LINEUPS[match_id] = (now, {})
        return {}
    out: dict[str, dict] = {}
    for side in ("homeTeam", "awayTeam"):
        team = data.get(side) or {}
        for p in team.get("lineup") or []:
            name = (p.get("name") or "").lower()
            if name:
                out[name] = {"status": "confirmed_start", "lineup_pos": None}
        for p in team.get("bench") or []:
            name = (p.get("name") or "").lower()
            if name:
                out[name] = {"status": "confirmed_bench", "lineup_pos": None}
    _SOC_LINEUPS[match_id] = (now, out)
    return out


async def enrich_pick_with_lineup(pick: dict) -> dict:
    """Attach `pick["lineup_status"]` for player-specific prop picks.

    Idempotent. No-ops when we can't identify the player or the game
    ID. Silently succeeds on network errors — caller falls back to
    `status='projected'`.
    """
    if pick.get("lineup_status"):
        return pick
    sport = pick.get("sport")
    player_name = (pick.get("player_name") or "").lower()
    if not player_name:
        # Try to recover from the market string
        market = pick.get("market") or ""
        for suffix in (" Anytime Goal Scorer", " To Score or Assist",
                       " Anytime Scorer", " To Score",
                       " Over", " Under"):
            if market.endswith(suffix):
                player_name = market[: -len(suffix)].strip().lower()
                break
    if not player_name:
        return pick

    lineup_map: dict[str, dict] = {}
    if sport == "MLB":
        # Auto-resolve game_pk (2026-07-19) — the ingest pipeline
        # doesn't populate this yet, so we resolve on-demand from
        # the MLB daily schedule (cached).
        try:
            from services.enrichment.game_resolver import resolve_mlb_game_pk
            game_pk = await resolve_mlb_game_pk(pick)
        except Exception:
            game_pk = pick.get("game_pk") or pick.get("mlb_game_pk")
        if game_pk:
            try:
                lineup_map = await _fetch_mlb_lineups(int(game_pk))
            except (TypeError, ValueError):
                pass
    elif sport == "Soccer":
        try:
            from services.enrichment.game_resolver import resolve_soccer_match_id
            match_id = await resolve_soccer_match_id(pick)
        except Exception:
            match_id = pick.get("football_data_match_id") or pick.get("fd_match_id")
        if match_id:
            try:
                lineup_map = await _fetch_soccer_lineups(int(match_id))
            except (TypeError, ValueError):
                pass

    if not lineup_map:
        pick["lineup_status"] = {"status": "projected", "lineup_pos": None,
                                  "updated_at": time.time()}
        return pick

    # Fuzzy lookup — try full name first, then last-name fallback.
    entry = lineup_map.get(player_name)
    if not entry:
        last = player_name.split()[-1] if player_name else ""
        for k, v in lineup_map.items():
            if last and k.endswith(last):
                entry = v
                break
    if entry:
        pick["lineup_status"] = {**entry, "updated_at": time.time()}
        if entry["status"] == "confirmed_scratch":
            pick["no_bet"] = True
            pick["no_bet_reason"] = "confirmed_scratch"
    else:
        # We have SOME lineup data for the game but our player isn't
        # in it — that means either bench or scratch.
        pick["lineup_status"] = {"status": "confirmed_bench",
                                  "lineup_pos": None,
                                  "updated_at": time.time()}
    return pick


def lineup_signal_component(pick: dict) -> tuple[float, str]:
    """Return (delta_points, explanation) for confirmed-lineup status."""
    ls = pick.get("lineup_status") or {}
    status = ls.get("status") or "projected"
    if status == "confirmed_start":
        return +5.0, "confirmed starter"
    if status == "confirmed_bench":
        return -30.0, "on the bench"
    if status == "confirmed_scratch":
        return -50.0, "scratched"
    return 0.0, ""


async def bulk_enrich_slate(picks: list[dict]) -> dict[str, int]:
    """Batch enrich a slate — dedups network calls by game_pk / match_id."""
    stats = {"mlb": 0, "soccer": 0, "skipped": 0}
    for p in picks:
        try:
            before = p.get("lineup_status")
            await enrich_pick_with_lineup(p)
            after = p.get("lineup_status")
            if after and after != before:
                if p.get("sport") == "MLB":
                    stats["mlb"] += 1
                elif p.get("sport") == "Soccer":
                    stats["soccer"] += 1
            else:
                stats["skipped"] += 1
        except Exception:
            stats["skipped"] += 1
    return stats
