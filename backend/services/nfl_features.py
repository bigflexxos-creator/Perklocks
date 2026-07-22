"""NFL Player Feature Engineering — Phase 3 (2026-07-22).

Consumes the `nfl_player_weekly` Mongo collection populated by
`services.nfl_data_ingest` and derives real-data features per player.

Every function is deterministic — no RNG, no placeholders. Returns
None when insufficient data instead of guessing.

Feature families surfaced:
  Recent-form rolling averages (L3 / L5 / season)
  Volume:   targets/game, carries/game, target share, air-yards share
  Efficiency: rec/tgt, yds/carry, YPT, YAC/rec, aDOT, WOPR, RACR
  Usage:    red-zone touches, first-downs share, snap proxy via touches
  Splits:   home vs away, per opponent history
  Trend:    L3 vs season delta (heating up / cooling down)
"""

from __future__ import annotations

import logging
from statistics import mean
from typing import Any, Optional

logger = logging.getLogger("lockscore.services.nfl_features")

_COLL = "nfl_player_weekly"


async def _recent_games(db, player_key: str, current_season: int,
                        current_week: int, limit: int = 5,
                        season_type: str = "REG") -> list[dict]:
    """Return the most recent `limit` game logs strictly before the
    given (season, week). Cross-season if the player is early in the
    current season (< limit games logged yet).

    player_key may be either the player_id (00-0034857) or the full
    display name — both are indexed and used.
    """
    coll = db[_COLL]
    q_id = {"player_id": player_key} if len(player_key) > 5 and "-" in player_key \
           else {"player_display_name": player_key}
    q_id["$or"] = [
        {"season": {"$lt": current_season}},
        {"season": current_season, "week": {"$lt": current_week}},
    ]
    q_id["season_type"] = {"$in": ["REG", "POST"]}
    cursor = coll.find(q_id).sort([("season", -1), ("week", -1)]).limit(limit)
    return [r async for r in cursor]


def _safe_mean(vals: list) -> Optional[float]:
    nums = [v for v in vals if isinstance(v, (int, float))]
    if not nums:
        return None
    return round(mean(nums), 3)


# ── Rolling averages by prop family ──────────────────────────────────

async def player_recent_averages(db, player: str, season: int, week: int,
                                 windows: tuple[int, ...] = (3, 5)) -> dict:
    """Return L3 / L5 rolling averages for every stat we track.

    Output shape:
        {
          "l3": {"passing_yards": 262.7, "carries": 10, ...},
          "l5": {"passing_yards": 228.2, "carries": 9.6, ...},
          "season_avg": {...},
          "n_games_l3": 3,
          "n_games_l5": 5,
        }
    """
    out: dict[str, Any] = {}
    max_w = max(windows)
    games = await _recent_games(db, player, season, week, limit=max_w)
    for w in windows:
        subset = games[:w]
        key = f"l{w}"
        out[key] = {}
        for stat in (
            "passing_yards", "passing_tds", "passing_ints", "attempts", "completions",
            "carries", "rushing_yards", "rushing_tds",
            "receptions", "targets", "receiving_yards", "receiving_tds",
            "target_share", "air_yards_share", "wopr",
            "fantasy_points_ppr",
        ):
            out[key][stat] = _safe_mean([g.get(stat) for g in subset])
        out[f"n_games_{key}"] = len(subset)

    # Season-to-date average (all games this season prior to `week`)
    coll = db[_COLL]
    q = {
        "season": season,
        "week": {"$lt": week},
        "season_type": "REG",
    }
    if "-" in player and len(player) >= 8:
        q["player_id"] = player
    else:
        q["player_display_name"] = player
    season_games = [r async for r in coll.find(q)]
    out["season_avg"] = {}
    for stat in (
        "passing_yards", "passing_tds", "passing_ints", "attempts",
        "carries", "rushing_yards", "rushing_tds",
        "receptions", "targets", "receiving_yards", "receiving_tds",
        "target_share", "air_yards_share", "wopr",
        "fantasy_points_ppr",
    ):
        out["season_avg"][stat] = _safe_mean(
            [g.get(stat) for g in season_games]
        )
    out["n_games_season"] = len(season_games)
    return out


# ── Efficiency ratios ────────────────────────────────────────────────

def compute_efficiency_ratios(avgs: dict) -> dict:
    """Given a `l5`/`season_avg` dict from player_recent_averages,
    return derived efficiency ratios.
    """
    out = {}
    for window_key in ("l3", "l5", "season_avg"):
        w = avgs.get(window_key) or {}
        recs = w.get("receptions") or 0
        tgts = w.get("targets") or 0
        rec_y = w.get("receiving_yards") or 0
        car = w.get("carries") or 0
        rush_y = w.get("rushing_yards") or 0
        att = w.get("attempts") or 0
        pass_y = w.get("passing_yards") or 0

        eff = {}
        if tgts:
            eff["catch_rate"] = round(recs / tgts, 3)
            eff["yards_per_target"] = round(rec_y / tgts, 2)
        if recs:
            eff["yards_per_reception"] = round(rec_y / recs, 2)
        if car:
            eff["yards_per_carry"] = round(rush_y / car, 2)
        if att:
            eff["yards_per_attempt"] = round(pass_y / att, 2)
            eff["completion_pct"] = round((w.get("completions") or 0) / att, 3)
        out[window_key] = eff
    return out


# ── Volume trend (heating up / cooling down) ─────────────────────────

def compute_volume_trend(avgs: dict) -> dict:
    """Delta of L3 vs season for the big volume markers.
    Positive = trending up (buy on Overs), negative = trending down.
    """
    l3 = avgs.get("l3") or {}
    season = avgs.get("season_avg") or {}
    out = {}
    for stat in ("passing_yards", "carries", "targets", "rushing_yards",
                 "receiving_yards", "receptions"):
        a, b = l3.get(stat), season.get(stat)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)) and b > 0:
            out[stat] = round((a - b) / b, 3)  # % delta as decimal
    return out


# ── Home vs Away splits ──────────────────────────────────────────────

async def home_away_splits(db, player: str, seasons_back: int = 2,
                           current_season: int = 2025) -> dict:
    """Return home/away splits over the last `seasons_back` seasons.

    Uses game_id parsing (`YYYY_WW_AWAY_HOME`) — if the player's team
    matches the HOME abbrev in game_id, that's a home game.
    """
    coll = db[_COLL]
    q = {
        "season": {"$gte": current_season - seasons_back, "$lte": current_season},
        "season_type": {"$in": ["REG", "POST"]},
    }
    if "-" in player and len(player) >= 8:
        q["player_id"] = player
    else:
        q["player_display_name"] = player
    games = [g async for g in coll.find(q)]

    home_stats, away_stats = [], []
    for g in games:
        gid = g.get("game_id") or ""
        parts = gid.split("_")
        if len(parts) < 4:
            continue
        home_team = parts[-1]
        my_team = g.get("team")
        if my_team == home_team:
            home_stats.append(g)
        else:
            away_stats.append(g)

    def _agg(bucket):
        return {
            "games": len(bucket),
            "passing_yards": _safe_mean([g.get("passing_yards") for g in bucket]),
            "rushing_yards": _safe_mean([g.get("rushing_yards") for g in bucket]),
            "receiving_yards": _safe_mean([g.get("receiving_yards") for g in bucket]),
            "receptions": _safe_mean([g.get("receptions") for g in bucket]),
            "targets": _safe_mean([g.get("targets") for g in bucket]),
            "fantasy_points_ppr": _safe_mean([g.get("fantasy_points_ppr") for g in bucket]),
        }

    return {"home": _agg(home_stats), "away": _agg(away_stats)}


# ── Opponent matchup history ─────────────────────────────────────────

async def player_vs_opponent(db, player: str, opponent: str,
                             lookback_years: int = 3,
                             current_season: int = 2025) -> dict:
    """Historical performance vs a specific opponent team abbrev."""
    coll = db[_COLL]
    q = {
        "season": {"$gte": current_season - lookback_years, "$lte": current_season},
        "opponent_team": opponent,
        "season_type": {"$in": ["REG", "POST"]},
    }
    if "-" in player and len(player) >= 8:
        q["player_id"] = player
    else:
        q["player_display_name"] = player
    games = [g async for g in coll.find(q).sort([("season", -1), ("week", -1)])]
    return {
        "n_games": len(games),
        "avg_passing_yards":    _safe_mean([g.get("passing_yards") for g in games]),
        "avg_rushing_yards":    _safe_mean([g.get("rushing_yards") for g in games]),
        "avg_receiving_yards":  _safe_mean([g.get("receiving_yards") for g in games]),
        "avg_receptions":       _safe_mean([g.get("receptions") for g in games]),
        "avg_targets":          _safe_mean([g.get("targets") for g in games]),
        "avg_fantasy_ppr":      _safe_mean([g.get("fantasy_points_ppr") for g in games]),
    }


# ── PROP HIT-RATE vs OPPONENT (career-long, all seasons in DB) ────────
#
# 2026-07-22 — this is the "Burrow is 100% for 200+ vs ATL" query.
# Answers: "How often has this player hit line X for stat Y vs
# opponent Z across their whole career?" Full career (2019-onward
# for anyone in nflverse). Powers the "Why this pick" rationale.

async def player_prop_hit_rate_vs_opponent(
    db,
    player: str,
    stat_field: str,          # e.g. "passing_yards", "rushing_yards"
    line: float,               # e.g. 199.5 for "Over 199.5 pass yds"
    opponent: str,             # e.g. "ATL"
    side: str = "over",        # "over" or "under"
) -> Optional[dict]:
    """Return the historical hit-rate for this player, stat, line, opponent.

    Output shape:
        {
          "opponent": "ATL",
          "stat": "passing_yards",
          "line": 199.5,
          "side": "over",
          "games": 1,
          "hits": 1,
          "hit_rate": 1.0,
          "avg_stat": 481.0,
          "rationale": "Joe Burrow has cleared 200+ passing yards in
                        1/1 (100%) career games vs ATL (avg 481.0)."
        }
    """
    coll = db[_COLL]
    q = {
        "opponent_team": opponent,
        "season_type": {"$in": ["REG", "POST"]},
    }
    if "-" in player and len(player) >= 8:
        q["player_id"] = player
    else:
        q["player_display_name"] = player
    games = [g async for g in coll.find(q)]
    if not games:
        return None

    vals = [g.get(stat_field) for g in games if isinstance(g.get(stat_field), (int, float))]
    if not vals:
        return None

    cmp_fn = (lambda v: v > line) if side.lower().startswith("over") else (lambda v: v < line)
    hits = sum(1 for v in vals if cmp_fn(v))
    n = len(vals)
    hit_rate = round(hits / n, 3) if n else None
    avg = round(sum(vals) / len(vals), 1)

    label = "cleared" if side.lower().startswith("over") else "stayed under"
    stat_label = stat_field.replace("_", " ")
    rationale = (
        f"{player} has {label} {line:g} {stat_label} in "
        f"{hits}/{n} ({hit_rate*100:.0f}%) career games vs {opponent} "
        f"(avg {avg:.1f})."
    )
    return {
        "opponent": opponent,
        "stat": stat_field,
        "line": float(line),
        "side": side.lower(),
        "games": n,
        "hits": hits,
        "hit_rate": hit_rate,
        "avg_stat": avg,
        "rationale": rationale,
    }


async def player_prop_hit_rate_all_opponents(
    db,
    player: str,
    stat_field: str,
    line: float,
    side: str = "over",
) -> list[dict]:
    """Full breakdown of hit-rate for this stat vs EVERY opponent
    the player has ever faced. Used for scouting reports and the
    "100% vs 20 teams" style rationale.
    """
    coll = db[_COLL]
    q = {"season_type": {"$in": ["REG", "POST"]}}
    if "-" in player and len(player) >= 8:
        q["player_id"] = player
    else:
        q["player_display_name"] = player
    cmp_op = "$gte" if side.lower().startswith("over") else "$lt"
    pipe = [
        {"$match": q},
        {"$group": {
            "_id": "$opponent_team",
            "games": {"$sum": 1},
            "hits": {"$sum": {"$cond": [{cmp_op: [f"${stat_field}", line]}, 1, 0]}},
            "avg": {"$avg": f"${stat_field}"},
        }},
        {"$project": {
            "opponent": "$_id",
            "games": 1,
            "hits": 1,
            "avg_stat": {"$round": ["$avg", 1]},
            "hit_rate": {"$round": [{"$divide": ["$hits", "$games"]}, 3]},
            "_id": 0,
        }},
        {"$sort": {"hit_rate": -1, "games": -1}},
    ]
    return [r async for r in coll.aggregate(pipe)]


# ── Composite: full feature bundle for one player-game ───────────────

async def build_player_features(db, player: str, opponent: str,
                                season: int, week: int,
                                prop_stat: Optional[str] = None,
                                prop_line: Optional[float] = None,
                                prop_side: str = "over") -> dict:
    """Return the ALL-IN feature bundle for a single upcoming game.

    If `prop_stat` + `prop_line` are provided, ALSO computes the
    career hit-rate vs this specific opponent for that line (e.g.
    "how often has Burrow hit 199.5+ pass yds vs ATL?").

    Consumed by nfl_feature_engine.build_nfl_prop_factors() in the
    picking pipeline.
    """
    avgs = await player_recent_averages(db, player, season, week)
    eff = compute_efficiency_ratios(avgs)
    trend = compute_volume_trend(avgs)
    splits = await home_away_splits(db, player, current_season=season)
    matchup = await player_vs_opponent(db, player, opponent,
                                       current_season=season)

    out = {
        "player": player,
        "opponent": opponent,
        "season": season,
        "week": week,
        "rolling_avg": avgs,
        "efficiency": eff,
        "volume_trend": trend,
        "home_away_splits": splits,
        "vs_opponent": matchup,
    }

    if prop_stat and isinstance(prop_line, (int, float)):
        out["prop_hit_rate_vs_opp"] = await player_prop_hit_rate_vs_opponent(
            db, player, prop_stat, prop_line, opponent, prop_side
        )

    return out


__all__ = [
    "player_recent_averages",
    "compute_efficiency_ratios",
    "compute_volume_trend",
    "home_away_splits",
    "player_vs_opponent",
    "player_prop_hit_rate_vs_opponent",
    "player_prop_hit_rate_all_opponents",
    "build_player_features",
]
