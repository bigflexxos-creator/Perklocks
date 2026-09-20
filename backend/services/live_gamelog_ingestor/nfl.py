"""NFL live game-log ingestor — free ESPN public API.

Endpoint:
    GET https://site.web.api.espn.com/apis/common/v3/sports/football/nfl/
        athletes/{athleteId}/gamelog

Same response shape as NBA — the column labels differ per position:
    QB rows:  CMP · ATT · YDS · CMP% · AVG · TD · INT · LNG · SACK · RTG · QBR
    RB rows:  CAR · YDS · AVG · LNG · TD
    WR/TE:    REC · TGTS · YDS · AVG · LNG · TD
We normalise by looking up column offsets from the response's own
`labels`/`names` array so downstream code stays position-agnostic.
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


_BASE = "https://site.web.api.espn.com/apis/common/v3/sports/football/nfl"
_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
_SEM = asyncio.Semaphore(10)


# ESPN labels → canonical actuals keys.  We inspect BOTH passing and
# rushing/receiving stat blocks so multi-role players (RB with a rare
# passing TD, etc.) still get every stat captured.
_LABEL_MAP = {
    # Passing
    "CMP":  "completions",
    "ATT":  "attempts",
    "YDS":  None,        # yards column — position-dependent; resolved below
    "TD":   None,        # td column — position-dependent
    "INT":  "interceptions",
    # Rushing (RB carries)
    "CAR":  "rush_attempts",
    # Receiving
    "REC":  "receptions",
    "TGTS": "targets",
    "TAR":  "targets",
}


def _decode_labels(labels: list[str]) -> dict[str, int]:
    """Return a label→index map for a stat row.  For ambiguous
    labels ("YDS", "TD") — ESPN emits them once per category so we
    default to first-occurrence and let the caller pass the same
    row to the passing/rushing/receiving mapper as needed."""
    return {lab: i for i, lab in enumerate(labels)}


def _stats_to_actuals(stats_arr: list, labels: list[str]) -> dict:
    """Best-effort stat extraction across position rows.

    Since ESPN returns one row per game per stat GROUP (passing OR
    rushing OR receiving), a single-game entry may only carry
    completions/attempts/pass_yds/pass_tds OR rushing OR receiving.
    We produce a merged actuals dict — missing stats stay None.
    """
    lidx = _decode_labels(labels)
    def g(k: str) -> Optional[float]:
        i = lidx.get(k)
        return _f(stats_arr[i]) if (i is not None and i < len(stats_arr)) else None

    actuals: dict[str, Optional[float]] = {
        "pass_yds":       None, "pass_tds":     None,
        "completions":    g("CMP"), "attempts":  g("ATT"),
        "interceptions":  g("INT"),
        "rush_yds":       None, "rush_attempts": g("CAR"),
        "rush_tds":       None,
        "rec_yds":        None, "receptions":  g("REC"),
        "rec_tds":        None, "targets":     g("TGTS") or g("TAR"),
    }

    # Heuristic resolution for YDS / TD ambiguity — check which
    # position row we're in by inspecting the co-occurring labels.
    yds = g("YDS")
    tds = g("TD")
    is_passing   = "CMP" in lidx and "ATT" in lidx
    is_rushing   = "CAR" in lidx and "ATT" not in lidx
    is_receiving = ("REC" in lidx) or ("TGTS" in lidx) or ("TAR" in lidx)
    if is_passing and yds is not None:
        actuals["pass_yds"] = yds
        actuals["pass_tds"] = tds
    elif is_rushing and yds is not None:
        actuals["rush_yds"] = yds
        actuals["rush_tds"] = tds
    elif is_receiving and yds is not None:
        actuals["rec_yds"] = yds
        actuals["rec_tds"] = tds
    return actuals


def _merge_actuals(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in (b or {}).items():
        if out.get(k) is None and v is not None:
            out[k] = v
    return out


def _parse_events(data: dict) -> dict[str, dict]:
    events_meta = (data or {}).get("events") or {}
    top_labels = (data or {}).get("labels") or (data or {}).get("names") or []

    out: dict[str, dict] = {}
    for st in (data or {}).get("seasonTypes") or []:
        season_year = st.get("year") or (st.get("displayName") or "")
        try:
            season_year = int(season_year) if season_year else None
        except Exception:
            season_year = None
        for cat in st.get("categories") or []:
            cat_labels = cat.get("labels") or top_labels
            for ev in cat.get("events") or []:
                event_id = str(ev.get("eventId") or "")
                if not event_id:
                    continue
                meta = events_meta.get(event_id) or {}
                stats_arr = ev.get("stats") or []
                new_actuals = _stats_to_actuals(stats_arr, cat_labels)
                existing = out.get(event_id, {})
                merged = (_merge_actuals(existing.get("actuals") or {},
                                          new_actuals)
                          if existing else new_actuals)
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
                    "actuals": merged,
                }
    return out


async def _get(client: httpx.AsyncClient, path: str,
               params: Optional[dict] = None) -> Optional[dict]:
    async with _SEM:
        try:
            r = await client.get(f"{_BASE}{path}", params=params, timeout=_TIMEOUT)
            if r.status_code == 200:
                return r.json()
            logger.warning("NFL gamelog %s → HTTP %d", path, r.status_code)
        except Exception as e:
            logger.warning("NFL gamelog %s exception: %s", path, e)
        return None


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
            "sport": "nfl",
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
            "source": "live_gamelog_nfl_v1",
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
    players = await iter_active_players(db, "nfl")
    if not players:
        logger.warning("NFL gamelog: 0 active players in db.players — "
                       "have you run espn_public.refresh_nfl yet?")
        return {"ok": False, "reason": "no_players", "elapsed_sec": 0}
    priority = await pick_priority_ids(db, "NFL")
    players = sort_players_by_priority(players, priority, "espn_id")
    if max_players:
        players = players[:max_players]

    tally = {"players_processed": 0, "splits": 0, "inserted": 0,
             "updated": 0, "skipped": 0, "errors": 0}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        # P0 STABILITY (2026-06) — bounded fan-out prevents thousands
        # of coroutine frames from being allocated simultaneously.
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
    tally["elapsed_sec"] = round(time.time() - started, 1)
    logger.info("NFL live gamelog refresh: %s", tally)
    return tally


__all__ = ["refresh"]
