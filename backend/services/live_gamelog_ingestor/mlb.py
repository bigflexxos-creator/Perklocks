"""MLB live game-log ingestor — free, no-key MLB Stats API.

Endpoint:
    GET https://statsapi.mlb.com/api/v1/people/{personId}/stats
        ?stats=gameLog&group=hitting,pitching&season={season}

Behaviour:
* Pulls per-game splits for BOTH hitting and pitching groups so
  two-way players (Ohtani et al.) get both stat blocks.
* Uses ``gamePk`` as ``event_id`` — same string key that
  ``team_game_actuals`` uses, so the shared date-enrichment helper
  in ``services/historical_intelligence.py`` finds matches.
* Populates ``event_time`` directly from the split's ``date``
  (ISO YYYY-MM-DD → ISO with T00:00:00Z).
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from .common import (
    BACKFILL_VERSION, _f, _iso, _mlb_ip_to_outs, iter_active_players,
    logger, now_iso, pick_priority_ids, sort_players_by_priority,
    upsert_one,
)


_BASE = "https://statsapi.mlb.com/api/v1"
_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
_SEM = asyncio.Semaphore(15)


async def _get(client: httpx.AsyncClient, path: str,
               params: Optional[dict] = None) -> Optional[dict]:
    async with _SEM:
        try:
            r = await client.get(f"{_BASE}{path}", params=params, timeout=_TIMEOUT)
            if r.status_code == 200:
                return r.json()
            logger.warning("MLB gameLog %s → HTTP %d", path, r.status_code)
        except Exception as e:
            logger.warning("MLB gameLog %s exception: %s", path, e)
        return None


async def _fetch_player_gamelog(client: httpx.AsyncClient,
                                  person_id: int, season: int) -> list[dict]:
    """Return the ``splits`` list across both hitting and pitching
    groups for the requested season.  Empty list on failure."""
    data = await _get(
        client,
        f"/people/{person_id}/stats",
        params={
            "stats": "gameLog",
            "group": "hitting,pitching",
            "season": season,
        },
    )
    out: list[dict] = []
    for grp in (data or {}).get("stats") or []:
        group_name = ((grp.get("group") or {}).get("displayName") or "").lower()
        for sp in grp.get("splits") or []:
            sp["_group"] = "pitching" if "pitch" in group_name else "hitting"
            out.append(sp)
    return out


def _split_to_actuals(split: dict) -> dict:
    """Merge hitting + pitching stat fields into the canonical
    ``actuals`` sub-doc.  Missing stats stay None."""
    stat = split.get("stat") or {}
    group = split.get("_group") or "hitting"
    if group == "pitching":
        outs = _f(stat.get("outs")) or _mlb_ip_to_outs(stat.get("inningsPitched"))
        return {
            "h": None, "hr": None, "rbi": None, "r": None, "tb": None,
            "at_bats": None, "strikeouts": None,
            "k": _f(stat.get("strikeOuts")),
            "outs": outs,
        }
    # hitting
    return {
        "h":   _f(stat.get("hits")),
        "hr":  _f(stat.get("homeRuns")),
        "rbi": _f(stat.get("rbi")),
        "r":   _f(stat.get("runs")),
        "tb":  _f(stat.get("totalBases")),
        "at_bats":    _f(stat.get("atBats")),
        "strikeouts": _f(stat.get("strikeOuts")),
        "k":   None,
        "outs": None,
    }


def _merge_actuals(a: dict, b: dict) -> dict:
    """Prefer the non-None value between two actuals dicts (for
    two-way players who appear in both hitting and pitching splits
    on the same gamePk)."""
    out = dict(a)
    for k, v in (b or {}).items():
        if out.get(k) is None and v is not None:
            out[k] = v
    return out


async def _ingest_one_player(client: httpx.AsyncClient, db,
                              player: dict, season: int) -> dict:
    stats = {"player": None, "splits": 0, "inserted": 0,
             "updated": 0, "skipped": 0}
    person_id = player.get("mlb_id") or player.get("player_id")
    try:
        person_id = int(person_id)
    except (TypeError, ValueError):
        return stats
    stats["player"] = person_id
    splits = await _fetch_player_gamelog(client, person_id, season)
    if not splits:
        return stats

    # Group by gamePk so two-way players merge into a single doc
    by_game: dict[str, dict] = {}
    for sp in splits:
        game_pk = ((sp.get("game") or {}).get("gamePk"))
        if not game_pk:
            continue
        game_pk = str(game_pk)
        row = by_game.setdefault(game_pk, {})
        row["event_id"] = game_pk
        row["event_time"] = _iso(sp.get("date"))
        row["season"] = int(sp.get("season") or season)
        # Opponent + home/away — canonical against the team_game_actuals
        # opponent-enrichment pipeline (which stores full team names).
        opp = sp.get("opponent") or {}
        opp_name = opp.get("name")
        team = sp.get("team") or {}
        team_name = team.get("name")
        row["canonical_opponent_id"] = opp_name
        row["canonical_team_id"] = team_name
        row["opponent"] = opp_name
        row["team"] = team_name
        is_home = sp.get("isHome")
        row["home_away"] = ("home" if is_home is True
                            else "away" if is_home is False else None)
        # Merge actuals
        new_actuals = _split_to_actuals(sp)
        row["actuals"] = (_merge_actuals(row.get("actuals") or {}, new_actuals)
                          if row.get("actuals") else new_actuals)

    stats["splits"] = len(by_game)
    for game_pk, row in by_game.items():
        actuals = row.get("actuals") or {}
        if all(v is None for v in actuals.values()):
            stats["skipped"] += 1
            continue
        doc = {
            "sport": "mlb",
            "canonical_player_id": str(person_id),
            "player_id": person_id,
            "player_name": player.get("name"),
            "team": row.get("team"),
            "opponent": row.get("opponent"),
            "canonical_team_id": row.get("canonical_team_id"),
            "canonical_opponent_id": row.get("canonical_opponent_id"),
            "home_away": row.get("home_away"),
            "event_id": row["event_id"],
            "canonical_event_id": row["event_id"],
            "event_time": row.get("event_time"),
            "season": row.get("season"),
            "week": None,
            "surface": None,
            "actuals": actuals,
            "source": "live_gamelog_mlb_v1",
            "source_record_id": row["event_id"],
            "source_player_id": person_id,
            "backfill_version": BACKFILL_VERSION,
            "ingested_at": now_iso(),
        }
        r = await upsert_one(db, doc)
        stats[r] += 1
    return stats


async def refresh(db, *, season: Optional[int] = None,
                   max_players: Optional[int] = None) -> dict:
    """Refresh MLB game logs for every active rostered MLB player.

    Priority: players in the current MLB board go first so today's
    Pick Breakdown always sees fresh logs within one refresh cycle.
    """
    started = time.time()
    season = season or datetime.now(timezone.utc).year
    players = await iter_active_players(db, "mlb")
    if not players:
        logger.warning("MLB gamelog: 0 active players in db.players — "
                       "have you run mlb_stats_api.refresh_all yet?")
        return {"ok": False, "reason": "no_players", "elapsed_sec": 0}
    priority = await pick_priority_ids(db, "MLB")
    players = sort_players_by_priority(players, priority, "mlb_id")
    if max_players:
        players = players[:max_players]

    tally = {"players_processed": 0, "splits": 0, "inserted": 0,
             "updated": 0, "skipped": 0, "errors": 0}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        results = await asyncio.gather(
            *[_ingest_one_player(client, db, p, season) for p in players],
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
    tally["season"] = season
    tally["priority_hits"] = len(priority)
    tally["elapsed_sec"] = round(time.time() - started, 1)
    logger.info("MLB live gamelog refresh: %s", tally)
    return tally


__all__ = ["refresh"]
