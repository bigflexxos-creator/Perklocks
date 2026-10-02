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


def _resolve_compound_label_indices(labels: list[str]) -> dict[str, int]:
    """2026-10-02 CORRECTNESS FIX — ESPN's compound WR/RB gamelog row
    emits a single stats array that contains BOTH rushing and
    receiving blocks concatenated:

        labels = ['CAR','YDS','AVG','TD','LNG',
                  'REC','TGTS','YDS','AVG','TD','LNG', 'FUM', ...]

    A naive dict comprehension collapses duplicate ``YDS`` / ``TD``
    labels onto the LAST occurrence (receiving block), silently
    corrupting the rushing columns.  Downstream this landed Terry
    McLaurin's 2026 Week 3 "6 catches, 85 rec yds" as
    ``rush_yds=0, rec_yds=None`` and Historical Intelligence stopped
    showing current-season NFL rows.

    This resolver walks the label array positionally and emits
    separate ``RUSH_YDS``, ``RUSH_TD``, ``REC_YDS``, ``REC_TD``,
    ``PASS_YDS``, ``PASS_TD`` keys so the caller can read each stat
    column deterministically regardless of row shape.
    """
    out: dict[str, int] = {}
    # First occurrence of a shared label belongs to whichever block
    # has already started.  We detect block boundaries via the
    # category-leading tokens: CAR (rushing), REC/TGTS/TAR (receiving),
    # CMP/ATT (passing).
    block = None
    for i, lab in enumerate(labels):
        if lab == "CAR":   block = "rush"
        elif lab in ("REC", "TGTS", "TAR"): block = "rec" if block != "rec" else block
        elif lab in ("CMP", "ATT") and block != "pass":  block = "pass"
        if lab == "YDS":
            if block == "rush":  out.setdefault("RUSH_YDS", i)
            elif block == "rec": out.setdefault("REC_YDS",  i)
            elif block == "pass":out.setdefault("PASS_YDS", i)
            else:                out.setdefault("YDS",      i)
        elif lab == "TD":
            if block == "rush":  out.setdefault("RUSH_TD", i)
            elif block == "rec": out.setdefault("REC_TD",  i)
            elif block == "pass":out.setdefault("PASS_TD", i)
            else:                out.setdefault("TD",      i)
        elif lab == "REC":  out.setdefault("REC",  i)
        elif lab == "TGTS": out.setdefault("TGTS", i)
        elif lab == "TAR":  out.setdefault("TAR",  i)
        elif lab == "CAR":  out.setdefault("CAR",  i)
        elif lab == "CMP":  out.setdefault("CMP",  i)
        elif lab == "ATT":  out.setdefault("ATT",  i)
        elif lab == "INT":  out.setdefault("INT",  i)
    return out


def _stats_to_actuals(stats_arr: list, labels: list[str]) -> dict:
    """Best-effort stat extraction across position rows.

    Since ESPN returns one row per game per stat GROUP (passing OR
    rushing OR receiving), a single-game entry may only carry
    completions/attempts/pass_yds/pass_tds OR rushing OR receiving.
    We produce a merged actuals dict — missing stats stay None.

    2026-10-02 — handles ESPN's compound WR/RB rows which emit
    rushing + receiving in one array with duplicate YDS/TD labels.
    """
    idx = _resolve_compound_label_indices(labels)
    def pick(key: str) -> Optional[float]:
        i = idx.get(key)
        if i is None or i >= len(stats_arr):
            return None
        return _f(stats_arr[i])

    actuals: dict[str, Optional[float]] = {
        "pass_yds":       pick("PASS_YDS"), "pass_tds":    pick("PASS_TD"),
        "completions":    pick("CMP"),       "attempts":    pick("ATT"),
        "interceptions":  pick("INT"),
        "rush_yds":       pick("RUSH_YDS"),  "rush_attempts": pick("CAR"),
        "rush_tds":       pick("RUSH_TD"),
        "rec_yds":        pick("REC_YDS"),   "receptions":  pick("REC"),
        "rec_tds":        pick("REC_TD"),    "targets":     pick("TGTS") or pick("TAR"),
    }

    # Compat fallback — a legacy passing-only or rushing-only row
    # that has a plain unqualified YDS/TD column (block never opened).
    plain_yds = idx.get("YDS")
    plain_td  = idx.get("TD")
    if plain_yds is not None and plain_yds < len(stats_arr):
        y = _f(stats_arr[plain_yds]); t = _f(stats_arr[plain_td]) if plain_td is not None else None
        if "CMP" in labels and "ATT" in labels and actuals["pass_yds"] is None:
            actuals["pass_yds"] = y; actuals["pass_tds"] = t
        elif "CAR" in labels and actuals["rush_yds"] is None:
            actuals["rush_yds"] = y; actuals["rush_tds"] = t
        elif any(l in labels for l in ("REC","TGTS","TAR")) and actuals["rec_yds"] is None:
            actuals["rec_yds"] = y; actuals["rec_tds"] = t
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
