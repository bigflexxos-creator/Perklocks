"""MLS player vs opponent matchup history (ESPN core API).

User request 2026-07-22:
   > "Also should pick up [per-opponent scoring history]"
   > (Screenshot showing Messi 7G vs Nashville, Surridge vs Charlotte 2G etc.)

Purpose:
  Fetch every top-scorer's 2025 (and current 2026) game-by-game record
  from ESPN's public core API `/v2/sports/soccer/leagues/usa.1/athletes
  /{aid}/eventlog`, then for each event pull the per-player statistics
  (goals, assists, shots) + event competitors (opponent teamId).

  Aggregate per (player, opponent_team) tuple → career G/A/matches +
  last-N-games hot streak. Stored in `mls_player_matchup_history`
  collection.

Consumers:
  services.mls_scorer_gate → future extension: boost picks where the
  player has ≥ 1.0 goals/match vs the specific opponent tonight, and
  demote picks where the player has zero career G+A vs opponent.

Refresh cadence: weekly (Sunday 09:00 UTC), plus manual admin trigger.
"""
from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger("lockscore.mls_matchup_history")

_EVENTLOG_URL = (
    "https://sports.core.api.espn.com/v2/sports/soccer/leagues/usa.1/"
    "seasons/{year}/athletes/{aid}/eventlog?limit=50"
)


def _norm(name: str) -> str:
    if not name:
        return ""
    nk = unicodedata.normalize("NFKD", name)
    return "".join(c for c in nk if not unicodedata.combining(c)).lower().strip()


async def _get_json(cx: httpx.AsyncClient, url: str) -> Optional[dict]:
    try:
        r = await cx.get(url)
        if r.status_code == 200:
            return r.json()
    except Exception:
        return None
    return None


async def _fetch_player_events(cx: httpx.AsyncClient, aid: str,
                               seasons: tuple[int, ...] = (2025, 2026)) -> list[dict]:
    """Fetch player's event refs across the requested seasons.

    Returns list of {event_url, stats_url, teamId, played} dicts.
    """
    out = []
    for season in seasons:
        blob = await _get_json(cx, _EVENTLOG_URL.format(year=season, aid=aid))
        if not blob:
            continue
        items = blob.get("events", {}).get("items", []) or []
        for it in items:
            if not it.get("played"):
                continue
            ev_url = (it.get("event") or {}).get("$ref") or ""
            st_url = (it.get("statistics") or {}).get("$ref") or ""
            if not (ev_url and st_url):
                continue
            out.append({
                "event_url": ev_url,
                "stats_url": st_url,
                "teamId": it.get("teamId") or "",
                "season": season,
            })
    return out


def _extract_player_gs(stats_blob: dict) -> tuple[int, int, int]:
    """Return (goals, assists, shots) from ESPN per-match stats blob."""
    if not stats_blob:
        return 0, 0, 0
    cats = (stats_blob.get("splits") or {}).get("categories") or []
    off = next((c for c in cats if c.get("name") == "offensive"), {})
    stats = off.get("stats", [])
    g = a = sh = 0
    for s in stats:
        n = s.get("name")
        v = s.get("value") or 0
        if n == "totalGoals":
            g = int(v)
        elif n == "goalAssists":
            a = int(v)
        elif n == "totalShots":
            sh = int(v)
    return g, a, sh


async def _resolve_event_opponent(cx: httpx.AsyncClient, event_url: str,
                                  player_team_id: str) -> Optional[dict]:
    """Return {'opponent_id','date','name'} from event blob."""
    ev = await _get_json(cx, event_url)
    if not ev:
        return None
    comps = ev.get("competitions", []) or []
    if not comps:
        return None
    opp_id = None
    for c in comps[0].get("competitors", []) or []:
        cid = c.get("id")
        if cid and cid != player_team_id:
            opp_id = cid
            break
    if not opp_id:
        return None
    return {
        "opponent_id": opp_id,
        "date": ev.get("date") or "",
        "name": ev.get("name") or "",
    }


async def _resolve_team_name(cx: httpx.AsyncClient, team_id: str, season: int) -> str:
    url = (
        f"https://sports.core.api.espn.com/v2/sports/soccer/leagues/usa.1/"
        f"seasons/{season}/teams/{team_id}?lang=en&region=us"
    )
    d = await _get_json(cx, url)
    if not d:
        return team_id
    return d.get("displayName") or d.get("name") or d.get("shortDisplayName") or team_id


async def refresh_one_player(cx: httpx.AsyncClient, aid: str,
                              player_name: str) -> dict:
    """Ingest all games for one player, aggregate per opponent, upsert."""
    from deps import db
    events = await _fetch_player_events(cx, aid)
    if not events:
        return {"aid": aid, "ok": False, "reason": "no_events"}

    # Per-opponent aggregates.
    by_opp: dict[str, dict] = {}
    per_game_rows = []

    sem = asyncio.Semaphore(6)

    async def _process(ev_entry: dict):
        async with sem:
            # Fetch player stats + event details in parallel.
            stats_task = _get_json(cx, ev_entry["stats_url"])
            event_task = _resolve_event_opponent(
                cx, ev_entry["event_url"], ev_entry["teamId"],
            )
            stats_blob, evinfo = await asyncio.gather(stats_task, event_task)
            if not evinfo:
                return
            g, a, sh = _extract_player_gs(stats_blob or {})
            opp = evinfo["opponent_id"]
            rec = by_opp.setdefault(opp, {"opponent_id": opp,
                                          "matches": 0, "goals": 0, "assists": 0,
                                          "shots": 0, "scored_matches": 0,
                                          "assist_matches": 0,
                                          "recent": []})
            rec["matches"] += 1
            rec["goals"] += g
            rec["assists"] += a
            rec["shots"] += sh
            if g > 0:
                rec["scored_matches"] += 1
            if a > 0:
                rec["assist_matches"] += 1
            rec["recent"].append({
                "date": evinfo["date"], "goals": g, "assists": a,
                "shots": sh, "season": ev_entry["season"],
            })
            per_game_rows.append({
                "aid": aid, "opponent_id": opp,
                "date": evinfo["date"], "goals": g, "assists": a,
                "shots": sh, "season": ev_entry["season"],
                "event_name": evinfo["name"],
            })

    await asyncio.gather(*[_process(e) for e in events])

    # Resolve team names (once per unique opp).
    team_ids = list(by_opp.keys())
    team_names = await asyncio.gather(
        *[_resolve_team_name(cx, tid, 2025) for tid in team_ids]
    )
    for tid, tname in zip(team_ids, team_names):
        by_opp[tid]["opponent_name"] = tname
        # Keep only last 5 recent games per opponent (most recent first).
        recent = sorted(by_opp[tid]["recent"],
                        key=lambda r: r.get("date", ""), reverse=True)
        by_opp[tid]["recent"] = recent[:5]

    doc = {
        "_id": aid,
        "player_id": aid,
        "player_name": player_name,
        "player_name_norm": _norm(player_name),
        "by_opponent": list(by_opp.values()),
        "total_events": len(events),
        "refreshed_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.mls_player_matchup_history.replace_one(
        {"_id": aid}, doc, upsert=True,
    )
    return {"aid": aid, "ok": True, "opponents": len(by_opp),
            "events": len(events)}


async def refresh_all_top_scorers(limit: int = 100) -> dict:
    """Refresh matchup history for every player in `espn_mls_stats`.

    Bounded concurrency (2 players at a time) so we don't hammer ESPN.
    Full run ~2-4 minutes for 80-100 players. Called weekly.
    """
    from deps import db
    scorers = await db.espn_mls_stats.find({}, {"_id": 1, "name": 1}).to_list(length=limit)
    if not scorers:
        return {"ok": False, "reason": "no_scorers_in_espn_mls_stats"}

    async with httpx.AsyncClient(timeout=15) as cx:
        sem = asyncio.Semaphore(2)

        async def _wrap(sc: dict):
            async with sem:
                try:
                    return await refresh_one_player(cx, sc["_id"], sc.get("name", ""))
                except Exception as e:
                    logger.warning("Matchup history for %s failed: %s", sc.get("name"), e)
                    return {"aid": sc.get("_id"), "ok": False, "reason": str(e)[:80]}

        results = await asyncio.gather(*[_wrap(sc) for sc in scorers])
    ok = sum(1 for r in results if r.get("ok"))
    logger.info(
        "MLS matchup history refresh: %d/%d players ingested (%.1fk events)",
        ok, len(results),
        sum(r.get("events", 0) for r in results if r.get("ok")) / 1000,
    )
    return {"ok": True, "players_ok": ok, "players_total": len(results)}


async def get_player_vs_opponent(player_name: str, opponent_team: str
                                  ) -> Optional[dict]:
    """Public lookup used by the picks pipeline.

    Returns aggregated stats for the given player vs the given team, or
    None if we have no history. Matching uses accent-stripped
    lowercased name for player and case-insensitive `team_name` OR
    espn_team_id for opponent.
    """
    from deps import db
    pname_n = _norm(player_name)
    doc = await db.mls_player_matchup_history.find_one(
        {"player_name_norm": pname_n},
    )
    if not doc:
        return None
    opp_l = (opponent_team or "").lower()
    for rec in doc.get("by_opponent", []):
        if (rec.get("opponent_name", "").lower() == opp_l
                or rec.get("opponent_id") == str(opponent_team)):
            return rec
    return None
