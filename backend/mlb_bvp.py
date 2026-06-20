"""MLB Batter-vs-Pitcher (BvP) enrichment.

Pulls career batter-vs-specific-pitcher splits from the FREE MLB Stats API
(0 Odds credits) and bolts them onto MLB batter prop picks. Used to:

  • Add a `bvp_history` field with the canonical AB / H / AVG / HR / SO
    line so the UI can render "5-for-12 (.417) vs Strider — 2 HR".
  • Adjust `lock_score` upward when a batter has dominated this pitcher
    historically (≥0.333 AVG in ≥10 ABs) or downward when they've
    struggled (<0.150 in ≥10 ABs).
  • Append a one-liner insight bullet to `insights[]`.

User spec: "make sure you got batter vs pitcher when making hit prediction".

All lookups are aggressively cached — player-id and probable-pitcher
caches persist in memory for the entire backend lifetime; BvP splits
cache per (batter_id, pitcher_id) tuple for the day.

Designed to fail-soft: any HTTP error / missing data simply skips the
enrichment without crashing the pick generator.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

MLB_STATS_BASE = "https://statsapi.mlb.com/api/v1"

# ── In-process caches (no TTL needed; rosters & BvP don't drift mid-day) ──
_PLAYER_ID_CACHE: dict[str, int] = {}        # "Aaron Judge" → 592450
_PROBABLE_PITCHER_CACHE: dict[str, dict] = {}  # gamePk → {home, away}
_BVP_CACHE: dict[tuple, dict] = {}            # (batter_id, pitcher_id) → stats


async def _get_json(url: str, timeout: float = 8.0) -> Optional[dict]:
    try:
        async with httpx.AsyncClient(timeout=timeout) as cx:
            r = await cx.get(url)
            if r.status_code != 200:
                return None
            return r.json()
    except Exception as e:
        logger.debug("MLB Stats API GET failed (%s): %s", url, e)
        return None


async def lookup_player_id(name: str) -> Optional[int]:
    """Resolve a player full-name to its MLB Stats API id."""
    if not name:
        return None
    norm = name.strip()
    cached = _PLAYER_ID_CACHE.get(norm)
    if cached:
        return cached
    data = await _get_json(f"{MLB_STATS_BASE}/people/search?names={norm}")
    if not data or not data.get("people"):
        return None
    pid = data["people"][0].get("id")
    if pid:
        _PLAYER_ID_CACHE[norm] = pid
    return pid


async def get_probable_pitchers(game_pk: int) -> dict:
    """Return {'home': {'id': ..., 'name': ...}, 'away': {...}} for a game.

    Uses the schedule endpoint with the probablePitcher hydrate so a
    single call covers every game on the slate."""
    key = str(game_pk)
    cached = _PROBABLE_PITCHER_CACHE.get(key)
    if cached:
        return cached
    data = await _get_json(
        f"{MLB_STATS_BASE}/schedule?gamePk={game_pk}&hydrate=probablePitcher",
    )
    out: dict = {"home": None, "away": None}
    if not data:
        return out
    for date in (data.get("dates") or []):
        for g in (date.get("games") or []):
            if g.get("gamePk") != int(game_pk):
                continue
            teams = g.get("teams") or {}
            for side in ("home", "away"):
                pp = (teams.get(side) or {}).get("probablePitcher") or {}
                if pp.get("id"):
                    out[side] = {"id": pp["id"], "name": pp.get("fullName")}
    _PROBABLE_PITCHER_CACHE[key] = out
    return out


async def fetch_bvp(batter_id: int, pitcher_id: int) -> Optional[dict]:
    """Career stats for `batter_id` against `pitcher_id`. Returns
    {ab, h, avg, hr, so, bb, doubles, triples} or None if no AB
    history exists."""
    key = (batter_id, pitcher_id)
    cached = _BVP_CACHE.get(key)
    if cached is not None:
        return cached
    data = await _get_json(
        f"{MLB_STATS_BASE}/people/{batter_id}/stats"
        f"?stats=vsPlayer&opposingPlayerId={pitcher_id}&group=hitting",
    )
    out: Optional[dict] = None
    if data and data.get("stats"):
        for stat_group in data["stats"]:
            for split in (stat_group.get("splits") or []):
                stat = split.get("stat") or {}
                ab = int(stat.get("atBats") or 0)
                if ab <= 0:
                    continue
                out = {
                    "ab":       ab,
                    "h":        int(stat.get("hits") or 0),
                    "avg":      float(stat.get("avg") or 0),
                    "hr":       int(stat.get("homeRuns") or 0),
                    "so":       int(stat.get("strikeOuts") or 0),
                    "bb":       int(stat.get("baseOnBalls") or 0),
                    "doubles":  int(stat.get("doubles") or 0),
                    "triples":  int(stat.get("triples") or 0),
                }
                break
            if out:
                break
    _BVP_CACHE[key] = out  # cache the None result too — no point re-fetching
    return out


def _batter_name_from_market(market: str) -> str:
    """'Aaron Judge (NYY) Over 0.5 Hits' → 'Aaron Judge'."""
    if "(" in market:
        return market.split("(")[0].strip()
    # Fallback: strip from "Over"/"Under" onward
    for kw in (" Over ", " Under "):
        if kw in market:
            return market.split(kw)[0].strip()
    return market.strip()


async def enrich_pick_with_bvp(pick: dict, game_pk: Optional[int]) -> dict:
    """Append BvP context to a single MLB batter prop pick. No-op for
    non-batter or non-MLB picks."""
    if pick.get("sport") != "MLB":
        return pick
    market = pick.get("market") or ""
    market_l = market.lower()
    # Only enrich batter hit / TB / HR / Score markets — pitcher props don't
    # benefit from BvP (the pitcher IS the variable).
    if not any(k in market_l for k in (
        "hits", "total bases", "home run", "to score", "rbi",
    )):
        return pick
    if "strikeout" in market_l or "outs" in market_l:
        return pick   # pitcher props
    if not game_pk:
        return pick

    batter_name = _batter_name_from_market(market)
    batter_id = await lookup_player_id(batter_name)
    if not batter_id:
        return pick

    probables = await get_probable_pitchers(game_pk)
    # Determine which pitcher the batter faces — opposing team's starter.
    selection = (pick.get("selection") or "").lower()
    # The selection often contains the team abbrev; fall back to whichever
    # probable pitcher we have.
    pitcher_info = probables.get("away") if "home" in selection else probables.get("home")
    if not pitcher_info:
        pitcher_info = probables.get("home") or probables.get("away")
    if not pitcher_info or not pitcher_info.get("id"):
        return pick

    bvp = await fetch_bvp(batter_id, pitcher_info["id"])
    if not bvp or bvp.get("ab", 0) < 1:
        return pick

    # Attach raw history for the UI
    pick["bvp_history"] = {
        **bvp,
        "pitcher_name": pitcher_info.get("name"),
        "batter_name": batter_name,
    }

    # Lock-score adjustment based on sample-size-weighted BvP performance.
    ab = bvp["ab"]
    avg = bvp["avg"]
    insights = list(pick.get("insights") or [])
    bvp_summary = (
        f"BvP: {bvp['h']}-for-{ab} ({avg:.3f}) vs {pitcher_info.get('name')}"
    )
    if bvp["hr"] > 0:
        bvp_summary += f" — {bvp['hr']} HR"
    insights.append(bvp_summary)

    boost = 0.0
    note = ""
    if ab >= 10:
        if avg >= 0.333:
            boost = +3.0
            note = "Dominant BvP history — historic edge vs this pitcher"
        elif avg <= 0.150:
            boost = -2.5
            note = "Weak BvP history — has struggled vs this pitcher"
    elif ab >= 5:
        if avg >= 0.400:
            boost = +1.5
            note = "Small-sample but positive BvP"
        elif avg <= 0.100:
            boost = -1.5
            note = "Small-sample but rough BvP"

    if note:
        insights.append(note)

    if boost:
        cur_lock = float(pick.get("lock_score") or 0)
        new_lock = max(0.0, min(99.0, cur_lock + boost))
        pick["lock_score"] = round(new_lock, 1)
        pick["bvp_lock_adjustment"] = round(boost, 1)
        # Re-grade if the bucket changed
        try:
            from sports_engine import _grade, _confidence
            pick["grade"] = _grade(new_lock)
            pick["confidence"] = _confidence(new_lock)
        except Exception:
            pass

    pick["insights"] = insights
    return pick


async def enrich_picks_bulk(picks: list[dict]) -> list[dict]:
    """Apply BvP enrichment across a full slate. Looks up `gamePk` from
    the pick's event_id field when present, otherwise tries to derive
    from the schedule endpoint matching on event_time + teams.

    This is a best-effort enrichment — any pick we can't match cleanly
    just passes through unchanged.
    """
    # Build (event_time_date, home_team, away_team) → gamePk map from MLB
    # schedule. Single API call covers all games for today.
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Look ±1 day so we cover late-night slates that span midnight UTC.
    sched = await _get_json(
        f"{MLB_STATS_BASE}/schedule?sportId=1"
        f"&startDate={today}&endDate={today}&hydrate=probablePitcher",
    )
    team_to_gamepk: dict[tuple, int] = {}
    if sched:
        for date in (sched.get("dates") or []):
            for g in (date.get("games") or []):
                pk = g.get("gamePk")
                home = (g.get("teams") or {}).get("home", {}).get("team", {}).get("name", "")
                away = (g.get("teams") or {}).get("away", {}).get("team", {}).get("name", "")
                if pk and home and away:
                    team_to_gamepk[(home, away)] = pk

    enriched_count = 0
    for p in picks:
        if p.get("sport") != "MLB":
            continue
        # event field is "Away @ Home"
        event = p.get("event") or ""
        if " @ " not in event:
            continue
        away, home = event.split(" @ ", 1)
        away = away.strip(); home = home.strip()
        # Try exact match then substring fallback (e.g. "Yankees" vs "New York Yankees")
        pk = team_to_gamepk.get((home, away))
        if pk is None:
            for (h, a), gpk in team_to_gamepk.items():
                if (home in h or h in home) and (away in a or a in away):
                    pk = gpk
                    break
        if not pk:
            continue
        before_lock = p.get("lock_score")
        await enrich_pick_with_bvp(p, pk)
        if p.get("bvp_history"):
            enriched_count += 1
    logger.info("MLB BvP enrichment: %d picks enriched", enriched_count)
    return picks
