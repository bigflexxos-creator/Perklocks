"""NHL live game-log ingestor — free NHL public API.

Endpoint:
    GET https://api-web.nhle.com/v1/player/{nhlId}/game-log/now
    GET https://api-web.nhle.com/v1/player/{nhlId}/game-log/{seasonId}/{gameType}

Response shape (per 2026-10-17 verification against C. McDavid 8478402):
    seasonId:            20262027           active season YYYYYYYY
    gameTypeId:          2                   2 = regular, 3 = playoffs
    playerStatsSeasons:  [{season, gameTypes}...]  prior seasons available
    gameLog[]:
        gameId, teamAbbrev, homeRoadFlag (H/R),
        gameDate ("YYYY-MM-DD"), opponentAbbrev, opponentCommonName.default,
        goals, assists, points, plusMinus,
        shots,                               ← SOG
        powerPlayGoals, powerPlayPoints,
        gameWinningGoals, otGoals,
        shorthandedGoals, shorthandedPoints,
        pim, shifts, toi ("MM:SS")

Design — strict mirror of ``services/live_gamelog_ingestor/nfl.py``:

* Writes to ``player_game_actuals`` with sport="nhl" and
  canonical_player_id="nhl_<nhlId>" — the exact scheme the existing
  NHL Historical Intelligence adapter already queries.
* Idempotent upsert on ``(sport, canonical_player_id, event_id)``.
* Season field is populated from the NHL seasonId prefix (YYYYYYYY →
  YYYY).  The universal freshness contract will trust that value.
* HONEST ZERO: when the API returns an empty gameLog, zero rows are
  written — no fabrication.
* Best-effort: HTTP / parse errors are counted, never propagated.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

import httpx

from .common import (
    BACKFILL_VERSION, _f, _iso, iter_active_players, logger, now_iso,
    pick_priority_ids, sort_players_by_priority, upsert_one,
)


_BASE = "https://api-web.nhle.com/v1"
_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
_SEM = asyncio.Semaphore(10)


def _parse_toi(toi: Any) -> Optional[float]:
    """"MM:SS" → seconds as float.  Missing / unparseable → None."""
    if not toi or not isinstance(toi, str):
        return None
    try:
        m, s = toi.split(":")
        return float(int(m) * 60 + int(s))
    except Exception:
        return None


def _season_year_from_season_id(sid: Any) -> Optional[int]:
    """NHL seasonId=YYYYYYYY (e.g. 20262027) → canonical season year
    for the sport's calendar (first half → 2026)."""
    if sid is None:
        return None
    try:
        s = str(int(sid))
        if len(s) == 8:
            return int(s[0:4])
    except Exception:
        pass
    return None


def _row_to_actuals(g: dict) -> dict:
    """NHL gameLog row → canonical actuals subdoc.

    HONEST contract:
    * All stats are passed through ``_f`` — strings or missing fields
      become None, never 0.
    * Points are preserved as-emitted; downstream computation can
      verify goals+assists if needed.
    """
    return {
        "goals":                 _f(g.get("goals")),
        "assists":               _f(g.get("assists")),
        "points":                _f(g.get("points")),
        "plus_minus":            _f(g.get("plusMinus")),
        "shots_on_goal":         _f(g.get("shots")),  # NHL "shots" = SOG
        "shots":                 _f(g.get("shots")),
        "pim":                   _f(g.get("pim")),
        "shifts":                _f(g.get("shifts")),
        "toi_seconds":           _parse_toi(g.get("toi")),
        "power_play_goals":      _f(g.get("powerPlayGoals")),
        "power_play_points":     _f(g.get("powerPlayPoints")),
        "shorthanded_goals":     _f(g.get("shorthandedGoals")),
        "shorthanded_points":    _f(g.get("shorthandedPoints")),
        "game_winning_goals":    _f(g.get("gameWinningGoals")),
        "ot_goals":              _f(g.get("otGoals")),
    }


def _parse_gamelog(data: dict) -> dict[str, dict]:
    """NHL /game-log response → {event_id: row_doc} for upsert."""
    out: dict[str, dict] = {}
    season = _season_year_from_season_id(data.get("seasonId"))
    for g in data.get("gameLog") or []:
        gid = g.get("gameId")
        if gid is None:
            continue
        event_id = f"nhl_{gid}"
        opp_name = (
            (g.get("opponentCommonName") or {}).get("default")
            if isinstance(g.get("opponentCommonName"), dict)
            else None
        )
        opp_abbrev = g.get("opponentAbbrev")
        home_road = (g.get("homeRoadFlag") or "").upper()
        home_away = ("home" if home_road == "H"
                     else "away" if home_road == "R"
                     else None)
        out[event_id] = {
            "event_id":              event_id,
            "canonical_event_id":    event_id,
            "event_time":            _iso(g.get("gameDate")),
            "canonical_opponent_id": f"nhl_team_{opp_abbrev}" if opp_abbrev else None,
            "opponent":              opp_name or opp_abbrev,
            "opponent_abbrev":       opp_abbrev,
            "home_away":             home_away,
            "season":                season,
            "actuals":               _row_to_actuals(g),
            "team_abbrev":           g.get("teamAbbrev"),
        }
    return out


async def _get(client: httpx.AsyncClient, path: str,
                params: Optional[dict] = None) -> Optional[dict]:
    async with _SEM:
        try:
            r = await client.get(f"{_BASE}{path}", params=params,
                                   timeout=_TIMEOUT,
                                   follow_redirects=True)
            if r.status_code == 200:
                return r.json()
            logger.warning("NHL gamelog %s → HTTP %d", path, r.status_code)
        except Exception as e:
            logger.warning("NHL gamelog %s exception: %s", path, e)
        return None


def _nhl_id_from_player(player: dict) -> Optional[int]:
    """Resolve the NHL API id for a player row.

    Preference order:
      1. ``nhl_id`` numeric field,
      2. ``player_id`` of shape "nhl_<id>" (our canonical scheme),
      3. ``canonical_player_id`` of shape "nhl_<id>".
    """
    for k in ("nhl_id",):
        v = player.get(k)
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                pass
    for k in ("player_id", "canonical_player_id"):
        v = player.get(k)
        if isinstance(v, str) and v.startswith("nhl_"):
            try:
                return int(v[4:])
            except (TypeError, ValueError):
                pass
    return None


async def _ingest_one_player(client: httpx.AsyncClient, db,
                              player: dict) -> dict:
    stats = {"player": None, "splits": 0, "inserted": 0,
             "updated": 0, "skipped": 0}
    nhl_id = _nhl_id_from_player(player)
    if nhl_id is None:
        return stats
    stats["player"] = nhl_id
    data = await _get(client, f"/player/{nhl_id}/game-log/now")
    if not data:
        return stats
    events = _parse_gamelog(data)
    stats["splits"] = len(events)
    cpid = f"nhl_{nhl_id}"
    team_display = player.get("team_name") or player.get("team")
    for event_id, row in events.items():
        actuals = row.get("actuals") or {}
        # HONEST ZERO guard — if every stat is None, the row is a
        # scheduled/postponed/scratch and shouldn't be stamped as
        # an "actual" observation.  Preserves missing-vs-zero
        # distinction per the common contract.
        if all(v is None for v in actuals.values()):
            stats["skipped"] += 1
            continue
        doc = {
            "sport":                   "nhl",
            "canonical_player_id":     cpid,
            "player_id":               cpid,
            "player_name":             player.get("name"),
            "team":                    team_display,
            "team_abbrev":             row.get("team_abbrev"),
            "canonical_team_id":       (f"nhl_team_{row.get('team_abbrev')}"
                                         if row.get("team_abbrev") else team_display),
            "opponent":                row.get("opponent"),
            "opponent_abbrev":         row.get("opponent_abbrev"),
            "canonical_opponent_id":   row.get("canonical_opponent_id"),
            "home_away":               row.get("home_away"),
            "event_id":                event_id,
            "canonical_event_id":      event_id,
            "event_time":              row.get("event_time"),
            "season":                  row.get("season"),
            "actuals":                 actuals,
            "source":                  "live_gamelog_nhl_v1",
            "source_record_id":        event_id,
            "source_player_id":        nhl_id,
            "backfill_version":        BACKFILL_VERSION,
            "ingested_at":             now_iso(),
        }
        r = await upsert_one(db, doc)
        stats[r] += 1
    return stats


async def refresh(db, *,
                   max_players: Optional[int] = None,
                   hot_list_only: bool = False) -> dict:
    """Refresh NHL player game logs.

    * ``max_players`` — cap the loop (useful for incremental or
      preview runs).  No cap in production.
    * ``hot_list_only`` — process ONLY players who appear in today's
      canonical NHL picks (fast surgical refresh, bounded cost).
      Preserves "no Production writes without proof" posture when
      first wiring up the ingestor — the hot list is a tiny population
      (today's picks only) so cost is predictable.
    """
    started = time.time()
    players = await _iter_nhl_players(db)
    if not players:
        logger.warning("NHL gamelog: 0 players in db.players sport=nhl — "
                       "cannot refresh.")
        return {"ok": False, "reason": "no_players", "elapsed_sec": 0}
    priority = await pick_priority_ids(db, "NHL")
    players = sort_players_by_priority(players, priority, "player_id")
    if hot_list_only and priority:
        players = [p for p in players
                   if str(p.get("player_id") or "") in priority
                   or str(p.get("canonical_player_id") or "") in priority]
    if max_players:
        players = players[:max_players]

    tally = {"players_processed": 0, "splits": 0, "inserted": 0,
             "updated": 0, "skipped": 0, "errors": 0}
    async with httpx.AsyncClient(timeout=_TIMEOUT,
                                   follow_redirects=True) as client:
        from services.bounded_async import bounded_gather
        results = await bounded_gather(
            players,
            lambda p: _ingest_one_player(client, db, p),
            limit=16,
        )
    for r in results:
        if isinstance(r, Exception):
            tally["errors"] += 1
            continue
        if not r:
            continue
        tally["players_processed"] += 1 if r.get("player") else 0
        for k in ("splits", "inserted", "updated", "skipped"):
            tally[k] += r.get(k, 0)

    tally["ok"] = True
    tally["priority_hits"] = len(priority)
    tally["hot_list_only"] = hot_list_only
    tally["elapsed_sec"] = round(time.time() - started, 1)
    logger.info("NHL live gamelog refresh: %s", tally)
    return tally


async def _iter_nhl_players(db) -> list[dict]:
    """NHL roster rows carry sport="nhl" and ``player_id`` of shape
    "nhl_<nhlid>".  No ``active`` flag today — treat the collection
    as the current roster.
    """
    cur = db.players.find(
        {"sport": "nhl",
         "$or": [
             {"nhl_id": {"$ne": None}},
             {"player_id": {"$regex": "^nhl_"}},
             {"canonical_player_id": {"$regex": "^nhl_"}},
         ]},
        {"player_id": 1, "nhl_id": 1, "canonical_player_id": 1,
         "name": 1, "canonical_name": 1, "position": 1,
         "team": 1, "team_name": 1},
    )
    return [p async for p in cur]


__all__ = ["refresh"]
