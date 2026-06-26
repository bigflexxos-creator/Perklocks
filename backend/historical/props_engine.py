"""Player Props Derivation Engine.

Reads from the unified `player_game_logs` collection (populated by the
per-sport historical clients) and derives prop hit-rates, rolling
averages, and consistency signals for every supported prop market across
all sports.

This is the READ-side of the Player Props pipeline:

    SportsDB / MLB Stats / ESPN / Sackmann
                  │
                  ▼
        player_game_logs (raw per-game stats)
                  │
                  ▼ (this module)
        props_history (per-player, per-stat hit-rate snapshots)
                  │
                  ▼
        Lock Engine / Evidence Engine / UI

Design rules:
  • ZERO HTTP calls. Pure read + aggregate from MongoDB.
  • Idempotent — `recompute_all_props()` can be re-run safely.
  • Catalog-driven — adding a new prop = adding one row to
    PLAYER_PROPS_CATALOG. No code changes needed elsewhere.
  • Cheap windows — L5 / L10 / L20 / season computed from a single
    fetch of the last 25 games.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("lockscore.historical.props")

# ─────────────────────────── Catalog ───────────────────────────
# Each entry defines a supported player-prop market that the engine can
# derive hit-rates for. `stat` is the field name inside player_game_logs
# (so adding a prop requires the upstream ingestor to populate that field).
# `default_lines` is the sportsbook-style lines we always pre-compute for
# (e.g. K props → 4.5/5.5/6.5; HR → 0.5; points → 14.5/19.5/24.5).
#
# Shape per entry:
#   key                — stable identifier used in API + storage
#   sport              — sport key (mlb, nba, nfl, nhl, soccer, tennis, cfb)
#   label              — human-readable
#   stat               — field name in player_game_logs
#   default_lines      — [float] common book lines we pre-compute
#   direction          — "over" (player needs ≥ line) — every player prop today
#                        works this way; structured as a field so future
#                        "under-only" props are easy to add.
#   role_filter        — optional position/role gate (e.g. pitcher props
#                        only apply to pitchers).

PLAYER_PROPS_CATALOG: list[dict[str, Any]] = [
    # ── MLB Batter ──
    {"key": "mlb_hits",          "sport": "mlb", "label": "Hits",            "stat": "hits",           "default_lines": [0.5, 1.5, 2.5], "direction": "over"},
    {"key": "mlb_home_runs",     "sport": "mlb", "label": "Home Runs",       "stat": "home_runs",      "default_lines": [0.5, 1.5],      "direction": "over"},
    {"key": "mlb_rbi",           "sport": "mlb", "label": "RBI",             "stat": "rbi",            "default_lines": [0.5, 1.5, 2.5], "direction": "over"},
    {"key": "mlb_total_bases",   "sport": "mlb", "label": "Total Bases",     "stat": "total_bases",    "default_lines": [1.5, 2.5, 3.5], "direction": "over"},
    {"key": "mlb_runs",          "sport": "mlb", "label": "Runs Scored",     "stat": "runs",           "default_lines": [0.5, 1.5],      "direction": "over"},
    {"key": "mlb_stolen_bases",  "sport": "mlb", "label": "Stolen Bases",    "stat": "stolen_bases",   "default_lines": [0.5],           "direction": "over"},
    {"key": "mlb_walks",         "sport": "mlb", "label": "Walks",           "stat": "walks",          "default_lines": [0.5, 1.5],      "direction": "over"},
    # ── MLB Pitcher ──
    {"key": "mlb_pitcher_ks",    "sport": "mlb", "label": "Pitcher Strikeouts", "stat": "pitcher_strikeouts", "default_lines": [4.5, 5.5, 6.5, 7.5, 8.5], "direction": "over", "role_filter": "pitcher"},
    {"key": "mlb_pitcher_outs",  "sport": "mlb", "label": "Pitcher Outs",    "stat": "outs_recorded",  "default_lines": [15.5, 17.5, 18.5], "direction": "over", "role_filter": "pitcher"},
    {"key": "mlb_pitcher_hits_allowed", "sport": "mlb", "label": "Hits Allowed", "stat": "hits_allowed", "default_lines": [4.5, 5.5, 6.5], "direction": "over", "role_filter": "pitcher"},
    # ── NBA ──
    {"key": "nba_points",        "sport": "nba", "label": "Points",          "stat": "points",         "default_lines": [9.5, 14.5, 19.5, 24.5, 29.5], "direction": "over"},
    {"key": "nba_rebounds",      "sport": "nba", "label": "Rebounds",        "stat": "rebounds",       "default_lines": [4.5, 6.5, 8.5, 10.5], "direction": "over"},
    {"key": "nba_assists",       "sport": "nba", "label": "Assists",         "stat": "assists",        "default_lines": [3.5, 5.5, 7.5, 9.5], "direction": "over"},
    {"key": "nba_threes",        "sport": "nba", "label": "3-Pointers Made", "stat": "threes_made",    "default_lines": [1.5, 2.5, 3.5], "direction": "over"},
    {"key": "nba_steals",        "sport": "nba", "label": "Steals",          "stat": "steals",         "default_lines": [0.5, 1.5],      "direction": "over"},
    {"key": "nba_blocks",        "sport": "nba", "label": "Blocks",          "stat": "blocks",         "default_lines": [0.5, 1.5],      "direction": "over"},
    {"key": "nba_pra",           "sport": "nba", "label": "Pts+Reb+Ast",     "stat": "pra",            "default_lines": [19.5, 24.5, 29.5, 34.5, 39.5], "direction": "over"},
    # ── NFL ──
    {"key": "nfl_passing_yds",   "sport": "nfl", "label": "Passing Yards",   "stat": "passing_yards",  "default_lines": [199.5, 224.5, 249.5, 274.5, 299.5], "direction": "over", "role_filter": "qb"},
    {"key": "nfl_passing_tds",   "sport": "nfl", "label": "Passing TDs",     "stat": "passing_tds",    "default_lines": [0.5, 1.5, 2.5], "direction": "over", "role_filter": "qb"},
    {"key": "nfl_rushing_yds",   "sport": "nfl", "label": "Rushing Yards",   "stat": "rushing_yards",  "default_lines": [29.5, 49.5, 69.5, 89.5], "direction": "over"},
    {"key": "nfl_receiving_yds", "sport": "nfl", "label": "Receiving Yards", "stat": "receiving_yards", "default_lines": [29.5, 49.5, 69.5, 89.5], "direction": "over"},
    {"key": "nfl_receptions",    "sport": "nfl", "label": "Receptions",      "stat": "receptions",     "default_lines": [2.5, 3.5, 4.5, 5.5, 6.5], "direction": "over"},
    {"key": "nfl_anytime_td",    "sport": "nfl", "label": "Anytime TD",      "stat": "any_td",         "default_lines": [0.5],           "direction": "over"},
    # ── CFB (same shape as NFL, separate so calibration can diverge) ──
    {"key": "cfb_passing_yds",   "sport": "cfb", "label": "Passing Yards",   "stat": "passing_yards",  "default_lines": [199.5, 224.5, 249.5, 274.5], "direction": "over", "role_filter": "qb"},
    {"key": "cfb_rushing_yds",   "sport": "cfb", "label": "Rushing Yards",   "stat": "rushing_yards",  "default_lines": [49.5, 79.5, 99.5], "direction": "over"},
    {"key": "cfb_receiving_yds", "sport": "cfb", "label": "Receiving Yards", "stat": "receiving_yards", "default_lines": [39.5, 59.5, 79.5], "direction": "over"},
    {"key": "cfb_anytime_td",    "sport": "cfb", "label": "Anytime TD",      "stat": "any_td",         "default_lines": [0.5],           "direction": "over"},
    # ── NHL ──
    {"key": "nhl_shots",         "sport": "nhl", "label": "Shots on Goal",   "stat": "shots",          "default_lines": [1.5, 2.5, 3.5, 4.5], "direction": "over"},
    {"key": "nhl_points",        "sport": "nhl", "label": "Points (G+A)",    "stat": "points",         "default_lines": [0.5, 1.5, 2.5], "direction": "over"},
    {"key": "nhl_goals",         "sport": "nhl", "label": "Goals",           "stat": "goals",          "default_lines": [0.5],           "direction": "over"},
    {"key": "nhl_assists",       "sport": "nhl", "label": "Assists",         "stat": "assists",        "default_lines": [0.5, 1.5],      "direction": "over"},
    {"key": "nhl_saves",         "sport": "nhl", "label": "Goalie Saves",    "stat": "saves",          "default_lines": [22.5, 26.5, 29.5, 32.5], "direction": "over", "role_filter": "goalie"},
    # ── Soccer ──
    {"key": "soccer_anytime_goal", "sport": "soccer", "label": "Anytime Goalscorer", "stat": "goals",  "default_lines": [0.5],           "direction": "over"},
    {"key": "soccer_assists",    "sport": "soccer", "label": "Assists",      "stat": "assists",        "default_lines": [0.5],           "direction": "over"},
    {"key": "soccer_shots",      "sport": "soccer", "label": "Shots",        "stat": "shots",          "default_lines": [0.5, 1.5, 2.5], "direction": "over"},
    {"key": "soccer_shots_on_target", "sport": "soccer", "label": "Shots on Target", "stat": "shots_on_target", "default_lines": [0.5, 1.5], "direction": "over"},
    # ── Tennis ──
    {"key": "tennis_aces",       "sport": "tennis", "label": "Aces",         "stat": "aces",           "default_lines": [3.5, 5.5, 7.5, 9.5], "direction": "over"},
    {"key": "tennis_double_faults", "sport": "tennis", "label": "Double Faults", "stat": "double_faults", "default_lines": [1.5, 2.5, 3.5], "direction": "over"},
    {"key": "tennis_total_games", "sport": "tennis", "label": "Total Games (Match)", "stat": "total_games_match", "default_lines": [19.5, 21.5, 23.5], "direction": "over"},
]

# Index for fast lookup
_CATALOG_BY_KEY: dict[str, dict] = {p["key"]: p for p in PLAYER_PROPS_CATALOG}
_CATALOG_BY_SPORT: dict[str, list[dict]] = {}
for _p in PLAYER_PROPS_CATALOG:
    _CATALOG_BY_SPORT.setdefault(_p["sport"], []).append(_p)


def get_catalog(sport: Optional[str] = None) -> list[dict]:
    if not sport:
        return list(PLAYER_PROPS_CATALOG)
    return list(_CATALOG_BY_SPORT.get((sport or "").lower(), []))


def get_prop(key: str) -> Optional[dict]:
    return _CATALOG_BY_KEY.get(key)


# ─────────────────────────── Hit-rate computation ───────────────────────────


def _hits_over_line(values: list[float], line: float) -> int:
    """Count values that strictly exceed (or equal threshold) the line.

    Sportsbook standard: a "0.5" line is "needs ≥ 1". We treat every line
    as the over: hit iff value > line.
    """
    return sum(1 for v in values if v is not None and float(v) > float(line))


def _avg(values: list[float]) -> float:
    if not values:
        return 0.0
    return round(sum(float(v or 0) for v in values) / len(values), 3)


async def compute_player_hitrate(
    db,
    *,
    player_id: Any,
    sport: str,
    stat: str,
    line: float,
    window: int = 10,
) -> dict:
    """Compute hit-rate for a single (player, stat, line) over the last
    `window` games.

    Returns:
        {
          "player_id": ..., "sport": ..., "stat": ..., "line": ...,
          "window": 10, "games_used": N, "hits": K,
          "hit_rate": float (0..1), "avg": float, "last": float | None,
          "values": [float, ...],   # most-recent first
        }
    """
    if db is None or not player_id:
        return {"error": "no_db_or_player"}
    sport_l = (sport or "").lower()
    cursor = db.player_game_logs.find(
        {"player_id": player_id, "sport": sport_l},
        {"_id": 0, stat: 1, "date": 1, "game_id": 1},
    ).sort("date", -1).limit(max(1, int(window)))
    rows = [doc async for doc in cursor]
    values: list[float] = []
    for r in rows:
        v = r.get(stat)
        if v is None:
            continue
        try:
            values.append(float(v))
        except (TypeError, ValueError):
            continue
    n = len(values)
    hits = _hits_over_line(values, line)
    return {
        "player_id": player_id,
        "sport": sport_l,
        "stat": stat,
        "line": float(line),
        "window": int(window),
        "games_used": n,
        "hits": hits,
        "hit_rate": round(hits / n, 4) if n else 0.0,
        "avg": _avg(values),
        "last": values[0] if values else None,
        "values": values,
    }


async def compute_player_props_summary(
    db,
    *,
    player_id: Any,
    sport: str,
    lookback_games: int = 25,
    role: Optional[str] = None,
) -> dict:
    """Compute hit-rate summary across ALL catalog props for one player.

    Returns shape:
        {
          "player_id": ...,
          "sport": ...,
          "as_of": iso,
          "games_logged": N,
          "props": {
            "mlb_hits": {
              "stat": "hits",
              "lines": {
                "0.5": {window: {5: {...}, 10: {...}, 20: {...}, "season": {...}}},
                "1.5": ...
              },
              "consistency": float,   # share of last10 with stat > 0
            },
            ...
          }
        }

    Designed to be called once per player per cron run and stored in
    `props_history` for instant UI consumption.
    """
    if db is None or not player_id:
        return {"error": "no_db_or_player"}
    sport_l = (sport or "").lower()

    # Single fetch — we'll slice into windows in-memory.
    cursor = db.player_game_logs.find(
        {"player_id": player_id, "sport": sport_l},
        {"_id": 0},
    ).sort("date", -1).limit(max(20, int(lookback_games)))
    rows = [doc async for doc in cursor]

    def _vals_for(stat: str, limit: int) -> list[float]:
        out = []
        for r in rows[:limit]:
            v = r.get(stat)
            if v is None:
                continue
            try:
                out.append(float(v))
            except (TypeError, ValueError):
                continue
        return out

    out_props: dict[str, Any] = {}
    catalog = get_catalog(sport_l)
    for spec in catalog:
        # Role-gate skip if player doesn't match (e.g. pitcher-only props
        # for a hitter). Role is OPTIONAL: if caller doesn't pass it, we
        # still include the row but flag mismatch.
        rf = spec.get("role_filter")
        if rf and role and rf != role:
            continue
        stat = spec["stat"]
        lines: dict[str, Any] = {}
        for line in spec.get("default_lines") or []:
            per_window = {}
            for win_name, win_size in (("5", 5), ("10", 10), ("20", 20), ("season", len(rows))):
                if win_size <= 0:
                    continue
                vals = _vals_for(stat, win_size)
                if not vals:
                    per_window[win_name] = {"games_used": 0, "hit_rate": 0.0, "avg": 0.0, "hits": 0}
                    continue
                hits = _hits_over_line(vals, float(line))
                per_window[win_name] = {
                    "games_used": len(vals),
                    "hits": hits,
                    "hit_rate": round(hits / len(vals), 4),
                    "avg": _avg(vals),
                }
            lines[str(line)] = per_window
        # Consistency = share of last 10 games where stat > 0 (i.e. player
        # produced at all — a robust floor signal independent of line).
        last10 = _vals_for(stat, 10)
        consistency = round(sum(1 for v in last10 if v and v > 0) / len(last10), 4) if last10 else 0.0
        out_props[spec["key"]] = {
            "stat": stat,
            "lines": lines,
            "consistency": consistency,
            "last10_avg": _avg(last10),
        }

    return {
        "player_id": player_id,
        "sport": sport_l,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "games_logged": len(rows),
        "props": out_props,
    }


async def recompute_all_props(
    db,
    *,
    sport: Optional[str] = None,
    limit: Optional[int] = None,
) -> dict:
    """Walk every player with logs (optionally scoped to a sport) and
    upsert their props summary into `props_history`. Designed to be run
    nightly from a cron loop.

    `limit` caps how many players we recompute per invocation — useful for
    incremental nightly runs that don't want to hammer the DB on every
    cycle.

    Returns counts + timing.
    """
    if db is None:
        return {"error": "no_db"}

    started = datetime.now(timezone.utc)
    match: dict[str, Any] = {}
    if sport:
        match["sport"] = (sport or "").lower()

    # Find players who have at least one game log in scope.
    pipeline: list[dict] = []
    if match:
        pipeline.append({"$match": match})
    pipeline += [
        {"$group": {"_id": {"player_id": "$player_id", "sport": "$sport"}, "n": {"$sum": 1}}},
        {"$match": {"n": {"$gte": 1}}},
        {"$sort": {"n": -1}},
    ]
    if limit:
        pipeline.append({"$limit": int(limit)})

    processed = 0
    upserts = 0
    errors = 0
    async for row in db.player_game_logs.aggregate(pipeline, allowDiskUse=True):
        pid = row.get("_id", {}).get("player_id")
        sp = row.get("_id", {}).get("sport") or "mlb"
        if not pid:
            continue
        try:
            # Look up role for filtering (mlb pitcher / nhl goalie / nfl qb).
            role = None
            try:
                pdoc = await db.players.find_one({"player_id": pid, "sport": sp}, {"position": 1})
                pos = (pdoc or {}).get("position") or ""
                pos_l = str(pos).lower()
                if sp == "mlb" and pos_l in ("p", "sp", "rp", "pitcher"):
                    role = "pitcher"
                elif sp == "nhl" and pos_l in ("g", "goalie"):
                    role = "goalie"
                elif sp == "nfl" and pos_l == "qb":
                    role = "qb"
                elif sp == "cfb" and pos_l == "qb":
                    role = "qb"
            except Exception:
                pass

            summary = await compute_player_props_summary(
                db, player_id=pid, sport=sp, role=role,
            )
            if "error" in summary:
                errors += 1
                continue
            doc_id = f"{sp}:{pid}"
            await db.props_history.update_one(
                {"_id": doc_id},
                {"$set": {
                    **summary,
                    "_id": doc_id,
                    "updated_at": datetime.now(timezone.utc),
                }},
                upsert=True,
            )
            upserts += 1
        except Exception as e:
            errors += 1
            logger.warning("recompute_all_props %s/%s failed: %s", sp, pid, e)
        processed += 1

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    return {
        "sport_filter": sport,
        "players_processed": processed,
        "upserts": upserts,
        "errors": errors,
        "elapsed_sec": round(elapsed, 2),
    }


async def get_player_props_snapshot(
    db, *, player_id: Any, sport: str,
) -> Optional[dict]:
    """Fast read of the latest stored snapshot for a player. Used by the
    UI / Lock Engine to surface "L10 8/10 hits ≥ 1" without re-running
    the aggregation."""
    if db is None or not player_id:
        return None
    sp = (sport or "").lower()
    doc = await db.props_history.find_one({"_id": f"{sp}:{player_id}"})
    if not doc:
        return None
    doc.pop("_id", None)
    return doc
