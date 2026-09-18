"""NBA live game-log ingestor — free ESPN public API.

Endpoint:
    GET https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba/
        athletes/{athleteId}/gamelog

Response shape (abbreviated):
    events: { "<eventId>": { "id", "week", "opponent": {abbreviation,displayName,homeAwaySymbol}, "gameDate", ... } }
    seasonTypes: [
        { categories: [ { events: [ { eventId, stats: [ ... ] } ] } ] }
    ]
    names: [ "MIN", "REB", "AST", "3PM", "STL", "BLK", "TO", "PTS", ... ] (column order)
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from .common import (
    BACKFILL_VERSION, _f, _iso, iter_active_players, logger, now_iso,
    pick_priority_ids, sort_players_by_priority, upsert_one,
)


_BASE = "https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba"
_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
_SEM = asyncio.Semaphore(10)

# Column labels we care about in the ESPN gamelog stat rows.
# ESPN sometimes returns them in slightly different orders per season —
# we normalise by looking up the label list from the response.
_STAT_LABEL_MAP = {
    "PTS": "points",
    "REB": "rebounds",
    "AST": "assists",
    "3PM": "threes_made",
    "STL": "steals",
    "BLK": "blocks",
    "TO":  "turnovers",
    "TOV": "turnovers",
}


async def _get(client: httpx.AsyncClient, path: str,
               params: Optional[dict] = None) -> Optional[dict]:
    async with _SEM:
        try:
            r = await client.get(f"{_BASE}{path}", params=params, timeout=_TIMEOUT)
            if r.status_code == 200:
                return r.json()
            logger.warning("NBA gamelog %s → HTTP %d", path, r.status_code)
        except Exception as e:
            logger.warning("NBA gamelog %s exception: %s", path, e)
        return None


def _parse_events(data: dict) -> dict[str, dict]:
    """Parse ESPN gamelog response → {event_id: {actuals, event_time,
    opponent, home_away, season, week}}."""
    events_meta = (data or {}).get("events") or {}
    labels = (data or {}).get("labels") or (data or {}).get("names") or []
    label_idx = {lab: i for i, lab in enumerate(labels)}

    out: dict[str, dict] = {}
    for st in (data or {}).get("seasonTypes") or []:
        season_year = st.get("year") or (st.get("displayName") or "")
        try:
            season_year = int(season_year) if season_year else None
        except Exception:
            season_year = None
        for cat in st.get("categories") or []:
            for ev in cat.get("events") or []:
                event_id = str(ev.get("eventId") or "")
                if not event_id:
                    continue
                meta = events_meta.get(event_id) or {}
                stats_arr = ev.get("stats") or []
                actuals: dict[str, Optional[float]] = {}
                for label, ak in _STAT_LABEL_MAP.items():
                    i = label_idx.get(label)
                    if i is None or i >= len(stats_arr):
                        continue
                    v = _f(stats_arr[i])
                    if actuals.get(ak) is None:
                        actuals[ak] = v
                opp_obj = meta.get("opponent") or {}
                opp_name = (opp_obj.get("displayName")
                             or opp_obj.get("abbreviation"))
                hs = (opp_obj.get("homeAwaySymbol") or "").lower()
                home_away = "home" if hs == "vs" else "away" if hs == "@" else None
                out[event_id] = {
                    "event_id": event_id,
                    "event_time": _iso(meta.get("gameDate") or meta.get("date")),
                    "canonical_opponent_id": opp_name,
                    "opponent": opp_name,
                    "home_away": home_away,
                    "season": season_year,
                    "week": meta.get("week"),
                    "actuals": actuals,
                }
    return out


async def _ingest_one_player(client: httpx.AsyncClient, db,
                              player: dict) -> dict:
    stats = {"player": None, "splits": 0, "inserted": 0,
             "updated": 0, "skipped": 0}
    athlete_id = player.get("espn_id") or player.get("player_id")
    try:
        athlete_id = int(athlete_id)
    except (TypeError, ValueError):
        return stats
    stats["player"] = athlete_id
    data = await _get(client, f"/athletes/{athlete_id}/gamelog")
    if not data:
        return stats
    events = _parse_events(data)
    stats["splits"] = len(events)
    for event_id, row in events.items():
        actuals = row.get("actuals") or {}
        if all(v is None for v in actuals.values()):
            stats["skipped"] += 1
            continue
        doc = {
            "sport": "nba",
            "canonical_player_id": str(athlete_id),
            "player_id": athlete_id,
            "player_name": player.get("name"),
            "team": player.get("team_name") or player.get("team"),
            "opponent": row.get("opponent"),
            "canonical_team_id": player.get("team_name") or player.get("team"),
            "canonical_opponent_id": row.get("canonical_opponent_id"),
            "home_away": row.get("home_away"),
            "event_id": event_id,
            "canonical_event_id": event_id,
            "event_time": row.get("event_time"),
            "season": row.get("season"),
            "week": row.get("week"),
            "surface": None,
            "actuals": actuals,
            "source": "live_gamelog_nba_v1",
            "source_record_id": event_id,
            "source_player_id": athlete_id,
            "backfill_version": BACKFILL_VERSION,
            "ingested_at": now_iso(),
        }
        r = await upsert_one(db, doc)
        stats[r] += 1
    return stats


async def refresh(db, *, max_players: Optional[int] = None) -> dict:
    started = time.time()
    players = await iter_active_players(db, "nba")
    if not players:
        logger.warning("NBA gamelog: 0 active players in db.players — "
                       "have you run espn_public.refresh_nba yet?")
        return {"ok": False, "reason": "no_players", "elapsed_sec": 0}
    priority = await pick_priority_ids(db, "NBA")
    players = sort_players_by_priority(players, priority, "espn_id")
    if max_players:
        players = players[:max_players]

    tally = {"players_processed": 0, "splits": 0, "inserted": 0,
             "updated": 0, "skipped": 0, "errors": 0}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        results = await asyncio.gather(
            *[_ingest_one_player(client, db, p) for p in players],
            return_exceptions=True,
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
    tally["elapsed_sec"] = round(time.time() - started, 1)
    logger.info("NBA live gamelog refresh: %s", tally)
    return tally


__all__ = ["refresh"]
