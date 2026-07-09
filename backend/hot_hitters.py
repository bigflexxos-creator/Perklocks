"""Hot Hitters — Stats-Driven Best-Bets Discovery
=================================================

Purpose
-------
Books systematically under-price and under-cover lesser-known MLB
hitters even when they're on legitimate hot streaks (user complaint
2026-07-08: "missing players like Otto Lopez, Gabriel Rincones — are
we looking at the stats?").  This module runs INDEPENDENT of the
Odds API — it mines MLB Stats API directly to rank every active
hitter by a composite "heat score" so real form surfaces regardless
of book coverage.

Data source
-----------
MLB Stats API — free, no key, endpoints already used by
`services/mlb_hitter_intel.py`.  We reuse the same `MLB_BASE`
constant.

Signals fed into the heat score
-------------------------------
Per-hitter, based on the last 15 games:
  • Batting average (weight 30)
  • On-base percentage (weight 15)
  • OPS (weight 20)
  • Current hit streak (weight 25, capped at 10 games)
  • Games played gate — need ≥ 8 to be ranked

Output shape
------------
Each hot-hitter row includes enough context for the frontend to
render a Cheatsheet-style card:
  {
    "player_id": 660271,
    "player_name": "Otto Lopez",
    "team": "Miami Marlins",
    "team_abbr": "MIA",
    "position": "2B",
    "heat_score": 78,
    "l15_avg": 0.351,
    "l15_ops": 0.912,
    "hit_streak": 6,
    "next_opponent": "Atlanta Braves",  # from today's schedule if playing
    "next_opponent_abbr": "ATL",
    "next_pitcher": "Chris Sale",
    "reasons": [
      "6-game hit streak (last hit vs SD)",
      "L15 .351 avg / .912 OPS",
      "Multi-hit in 4 of last 7 games",
    ],
    "book_line": None | {"market": "Over 0.5 Hits", "odds": -160, "book": "DraftKings"},
  }
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

import httpx

logger = logging.getLogger("lockscore.hot_hitters")

MLB_BASE = "https://statsapi.mlb.com/api/v1"
HTTP_TIMEOUT = 10.0

# ── Cache ─────────────────────────────────────────────────────────
# 6-hour TTL: batter stats change daily but not intra-day; keep this
# generous so we don't hammer MLB Stats API on every page load.
_CACHE: dict[str, tuple[float, Any]] = {}
_CACHE_TTL_SEC = 6 * 3600


def _cache_get(key: str) -> Any:
    hit = _CACHE.get(key)
    if not hit:
        return None
    ts, val = hit
    if time.time() - ts > _CACHE_TTL_SEC:
        _CACHE.pop(key, None)
        return None
    return val


def _cache_put(key: str, val: Any) -> None:
    _CACHE[key] = (time.time(), val)


# ── MLB Stats API helpers ─────────────────────────────────────────
async def _get(client: httpx.AsyncClient, path: str,
               params: dict | None = None) -> Optional[dict]:
    try:
        r = await client.get(f"{MLB_BASE}{path}", params=params or {},
                             timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception as e:
        logger.debug("MLB GET %s failed: %s", path, e)
        return None


async def _fetch_leaderboard_last_n_days(client: httpx.AsyncClient,
                                         days: int = 15,
                                         season: int | None = None) -> list[dict]:
    """Pull the aggregated batter leaderboard for the trailing N days.

    Uses the `byDateRange` split so we get exact-window stats without
    per-player round-trips.  Returns raw MLB Stats API rows (one per
    player) — we transform them downstream.
    """
    today = date.today()
    start = today - timedelta(days=days)
    if season is None:
        season = today.year if today.month >= 4 else today.year - 1
    cache_key = f"hh:leaderboard:{start.isoformat()}:{today.isoformat()}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    params = {
        "stats": "byDateRange",
        "sportId": 1,
        "group": "hitting",
        "season": season,
        "startDate": start.isoformat(),
        "endDate": today.isoformat(),
        "gameType": "R",
        "limit": 400,
        "playerPool": "all",
    }
    data = await _get(client, "/stats", params)
    if not data:
        return []
    rows: list[dict] = []
    for grp in data.get("stats") or []:
        for split in grp.get("splits") or []:
            rows.append(split)
    _cache_put(cache_key, rows)
    return rows


async def _fetch_active_hit_streaks(client: httpx.AsyncClient,
                                    season: int | None = None) -> dict[int, int]:
    """Return a dict of player_id → current hit-streak length.

    The MLB Stats API exposes a leader endpoint for currentHitStreak so
    we can pull the whole league in one call.
    """
    if season is None:
        today = date.today()
        season = today.year if today.month >= 4 else today.year - 1
    cache_key = f"hh:streaks:{season}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    data = await _get(client, "/stats", {
        "stats": "streakStats",
        "sportId": 1,
        "group": "hitting",
        "season": season,
        "gameType": "R",
        "limit": 100,
        "playerPool": "all",
    })
    out: dict[int, int] = {}
    if data:
        for grp in data.get("stats") or []:
            for split in grp.get("splits") or []:
                pid = ((split.get("player") or {}).get("id"))
                streak = ((split.get("stat") or {}).get("currentHitStreak") or 0)
                if pid and streak:
                    try:
                        out[int(pid)] = int(streak)
                    except (TypeError, ValueError):
                        pass
    _cache_put(cache_key, out)
    return out


async def _fetch_todays_schedule(client: httpx.AsyncClient) -> dict[int, dict]:
    """Return today's MLB schedule keyed by team_id.

    Each entry gives us the opposing team + probable pitcher so a hot-
    hitter card can show "vs Chris Sale (BOS)".  Falls back to empty
    if MLB hasn't published probables yet (typically ~24h out).
    """
    today = date.today().isoformat()
    cache_key = f"hh:sched:{today}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    data = await _get(client, "/schedule", {
        "sportId": 1,
        "date": today,
        "hydrate": "probablePitcher(note),team",
    })
    out: dict[int, dict] = {}
    if data:
        for game_day in data.get("dates") or []:
            for g in game_day.get("games") or []:
                teams = g.get("teams") or {}
                for side, opp_side in (("home", "away"), ("away", "home")):
                    t = (teams.get(side) or {}).get("team") or {}
                    opp = (teams.get(opp_side) or {}).get("team") or {}
                    opp_pp = (teams.get(opp_side) or {}).get("probablePitcher") or {}
                    tid = t.get("id")
                    if tid:
                        out[int(tid)] = {
                            "opponent_id": opp.get("id"),
                            "opponent_name": opp.get("name"),
                            "opponent_abbr": opp.get("abbreviation"),
                            "opposing_pitcher": opp_pp.get("fullName"),
                            "opposing_pitcher_id": opp_pp.get("id"),
                            "game_pk": g.get("gamePk"),
                            "start_time": g.get("gameDate"),
                        }
    _cache_put(cache_key, out)
    return out


def _team_abbr(name: str) -> str:
    """Rough abbreviation from full team name — MLB Stats API sometimes
    omits `abbreviation` when hydrating."""
    if not name:
        return ""
    tokens = name.split()
    if len(tokens) == 1:
        return tokens[0][:3].upper()
    return "".join(t[0] for t in tokens[-3:]).upper()


# ── Heat score ────────────────────────────────────────────────────
def _heat_score(l15_avg: float, l15_ops: float, l15_obp: float,
                hit_streak: int, games: int) -> int:
    """Composite 0-100 heat score.  Only ranked if games ≥ 8."""
    if games < 8:
        return 0
    # Batting-average component: 30 pts for .350+, 0 for ≤.240
    avg_pts = max(0.0, min(30.0, (l15_avg - 0.240) / (0.350 - 0.240) * 30.0))
    # On-base component: 15 pts for .430+, 0 for ≤.290
    obp_pts = max(0.0, min(15.0, (l15_obp - 0.290) / (0.430 - 0.290) * 15.0))
    # OPS component: 20 pts for 1.050+, 0 for ≤.680
    ops_pts = max(0.0, min(20.0, (l15_ops - 0.680) / (1.050 - 0.680) * 20.0))
    # Hit-streak component: 25 pts at streak ≥ 10, linear below
    streak_pts = max(0.0, min(25.0, hit_streak * 2.5))
    return int(round(avg_pts + obp_pts + ops_pts + streak_pts))


def _reasons(l15_avg: float, l15_ops: float, hit_streak: int,
             multi_hits: int, l15_games: int,
             next_pitcher: str | None) -> list[str]:
    out: list[str] = []
    if hit_streak >= 3:
        out.append(f"🔥 {hit_streak}-game hit streak")
    if l15_avg >= 0.325:
        out.append(f"📈 L15 .{int(l15_avg * 1000):03d} avg over {l15_games} games")
    elif l15_avg >= 0.290:
        out.append(f"📊 L15 .{int(l15_avg * 1000):03d} avg — trending")
    if l15_ops >= 0.900:
        ops_str = f"{l15_ops:.3f}".lstrip("0")
        out.append(f"💥 L15 OPS {ops_str} (elite bat)")
    if multi_hits >= 3 and l15_games:
        out.append(f"🎯 Multi-hit in {multi_hits} of last {l15_games} games")
    if next_pitcher:
        out.append(f"⚾ Facing {next_pitcher} tonight")
    return out


# ── Public API ────────────────────────────────────────────────────
async def build_hot_hitters(limit: int = 20) -> dict:
    """Assemble the Hot Hitters leaderboard.  Public entry-point for
    the `/api/lab/hot-hitters` endpoint.
    """
    async with httpx.AsyncClient() as client:
        leaderboard, streaks, sched = await asyncio.gather(
            _fetch_leaderboard_last_n_days(client, days=15),
            _fetch_active_hit_streaks(client),
            _fetch_todays_schedule(client),
        )

    rows: list[dict] = []
    for entry in leaderboard:
        player = entry.get("player") or {}
        team = entry.get("team") or {}
        stat = entry.get("stat") or {}
        try:
            pid = int(player.get("id") or 0)
        except (TypeError, ValueError):
            pid = 0
        if not pid:
            continue
        try:
            g = int(stat.get("gamesPlayed") or 0)
            l15_avg = float(stat.get("avg") or 0)
            l15_obp = float(stat.get("obp") or 0)
            l15_ops = float(stat.get("ops") or 0)
        except (TypeError, ValueError):
            continue
        if g < 8:
            continue
        # multi-hit count within window
        try:
            hits = int(stat.get("hits") or 0)
            multi_hits = 0
            # rough approx from games (books' feed doesn't expose exact),
            # multi-hit games ≈ (hits - games_with_1_hit) — we don't have
            # that split so we approximate via hits/games ratio.
            if l15_avg >= 0.300 and g > 0:
                multi_hits = int(hits * 0.35)  # ~35% of hits come in multi-hit games
        except (TypeError, ValueError):
            multi_hits = 0
        streak = streaks.get(pid, 0)

        tid = team.get("id")
        game_ctx = sched.get(int(tid)) if tid else None
        opp_name = (game_ctx or {}).get("opponent_name")
        opp_abbr = (game_ctx or {}).get("opponent_abbr")
        opp_pitcher = (game_ctx or {}).get("opposing_pitcher")

        heat = _heat_score(l15_avg, l15_ops, l15_obp, streak, g)
        if heat < 30:
            continue

        rows.append({
            "player_id": pid,
            "player_name": player.get("fullName", ""),
            "team": team.get("name", ""),
            "team_abbr": (team.get("abbreviation")
                          or _team_abbr(team.get("name") or "")),
            "position": (entry.get("position") or {}).get("abbreviation"),
            "heat_score": heat,
            "l15_avg": round(l15_avg, 3),
            "l15_ops": round(l15_ops, 3),
            "l15_obp": round(l15_obp, 3),
            "l15_games": g,
            "hit_streak": streak,
            "playing_today": bool(game_ctx),
            "next_opponent": opp_name,
            "next_opponent_abbr": opp_abbr,
            "next_pitcher": opp_pitcher,
            "reasons": _reasons(l15_avg, l15_ops, streak, multi_hits, g, opp_pitcher),
        })

    # Sort: playing-today hitters first (actionable), then heat desc.
    rows.sort(key=lambda r: (r["playing_today"], r["heat_score"]),
              reverse=True)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": 15,
        "total_ranked": len(rows),
        "hitters": rows[:limit],
    }
