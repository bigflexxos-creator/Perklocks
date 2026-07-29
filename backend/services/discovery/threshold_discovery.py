"""Threshold Discovery Engine (2026-07-28).

Walks the full statistical ladder for a (sport, player, stat) and
returns hit-rate + grade at every level.

    result = await analyse_thresholds(
        db,
        sport="NFL", player="Joe Burrow",
        stat="passing_yards",
        thresholds=[149.5, 174.5, 199.5, 224.5, 249.5, 274.5, 299.5],
    )

    → {
        "player": "Joe Burrow", "stat": "passing_yards",
        "sport": "NFL",
        "games_used": 38,
        "average_output": 258.4,
        "median_output":  260.0,
        "stdev":          62.1,
        "consistency_score": 0.68,
        "thresholds": [
          {threshold, games, hits, hit_rate, lb95, ub95,
            grade, confidence_label, is_strong},
          ...
        ],
        "strongest": {threshold, grade, hit_rate},   # highest lb95
        "safest":    {threshold, grade, hit_rate},   # highest hit_rate ≥ 0.75
        "notes":     [...],
      }

Zero writes. Never raises. Sport-agnostic — pulls from
`nfl_player_weekly` for NFL, `tennis_matches_history` for Tennis,
`player_game_logs` for MLB.

**No sportsbook odds. No betting lines.** The threshold ladder is
user-supplied (or sport defaults) and interpreted purely against
historical performance.
"""
from __future__ import annotations

import logging
import statistics
from typing import Optional

from .confidence_system import (
    wilson_lower_bound, wilson_upper_bound, confidence_grade,
    confidence_label, consistency_score,
)

logger = logging.getLogger("lockscore.services.discovery.threshold_discovery")


# Sport-default threshold ladders — realistic prop lines.
_DEFAULT_LADDERS: dict[tuple[str, str], list[float]] = {
    ("NFL", "passing_yards"):   [174.5, 199.5, 224.5, 249.5, 274.5, 299.5, 324.5],
    ("NFL", "rushing_yards"):   [39.5, 49.5, 59.5, 69.5, 79.5, 89.5, 99.5, 124.5],
    ("NFL", "receiving_yards"): [39.5, 49.5, 59.5, 69.5, 79.5, 89.5, 99.5, 124.5],
    ("NFL", "receptions"):      [2.5, 3.5, 4.5, 5.5, 6.5, 7.5, 8.5],
    ("MLB", "hits"):            [0.5, 1.5, 2.5, 3.5],
    ("MLB", "home_runs"):       [0.5, 1.5],
    ("MLB", "total_bases"):     [0.5, 1.5, 2.5, 3.5],
    ("MLB", "pitcher_strikeouts"): [3.5, 4.5, 5.5, 6.5, 7.5, 8.5, 9.5, 10.5],
    ("Tennis", "aces"):         [1.5, 3.5, 5.5, 7.5, 9.5, 11.5, 14.5],
    ("Tennis", "double_faults"):[0.5, 1.5, 2.5, 3.5, 4.5, 5.5],
}


async def _fetch_stat_values(db, sport: str, player: str,
                              stat: str) -> list[float]:
    """Return a list of the player's raw stat values across history."""
    s = (sport or "").upper()
    values: list[float] = []
    if s == "NFL":
        q = {"$or": [{"player_display_name": player},
                      {"player_name": player}]}
        cursor = db.nfl_player_weekly.find(q, {stat: 1, "_id": 0})
    elif s == "MLB":
        # Lookup player_id from mlb_bvp / pitcher-h2h helpers.
        try:
            from mlb_bvp import lookup_player_id
            pid = await lookup_player_id(player)
        except Exception:
            pid = None
        if not pid:
            try:
                from mlb_pitcher_h2h import _resolve_pitcher_id
                pid = await _resolve_pitcher_id(player)
            except Exception:
                pid = None
        if not pid:
            return []
        cursor = db.player_game_logs.find(
            {"sport": "mlb", "player_id": pid},
            {stat: 1, "_id": 0},
        )
    elif s == "TENNIS":
        # Grab both winner-side and loser-side rows for this player.
        q = {"$or": [{"winner_name": player}, {"loser_name": player}]}
        cursor = db.tennis_matches_history.find(q, {"_id": 0})
        rows = [r async for r in cursor]
        # Pick winner side or loser side based on the stat.
        # Aces = w_ace / l_ace, DF = w_df / l_df, total_games = total_games_match
        col_map = {"aces": ("w_ace", "l_ace"),
                    "double_faults": ("w_df", "l_df"),
                    "total_games":   ("total_games_match", "total_games_match")}
        w_col, l_col = col_map.get(stat, (stat, stat))
        for r in rows:
            v = r.get(w_col) if r.get("winner_name") == player else r.get(l_col)
            try:
                if v is not None:
                    values.append(float(v))
            except (TypeError, ValueError):
                continue
        return values
    else:
        return []
    async for r in cursor:
        v = r.get(stat)
        try:
            if v is not None:
                values.append(float(v))
        except (TypeError, ValueError):
            continue
    return values


async def analyse_thresholds(
    db,
    *,
    sport: str,
    player: str,
    stat: str,
    thresholds: Optional[list[float]] = None,
    values: Optional[list[float]] = None,
) -> dict:
    """Compute hit rate + grade at every threshold on the ladder.

    Callers may pass pre-fetched `values` to avoid a DB round-trip
    (used by the pattern-discovery engine when it already has the data).
    """
    sport_u = (sport or "").upper()
    if not thresholds:
        thresholds = _DEFAULT_LADDERS.get(
            (sport_u.title() if sport_u == "TENNIS" else sport_u, stat),
            [0.5, 1.5, 2.5, 5.5, 10.5],
        )
    if values is None:
        values = await _fetch_stat_values(db, sport, player, stat)

    result = {
        "player":            player,
        "stat":              stat,
        "sport":             sport,
        "games_used":        len(values),
        "average_output":    round(sum(values) / len(values), 3) if values else 0.0,
        "median_output":     round(statistics.median(values), 3) if values else 0.0,
        "stdev":             round(statistics.stdev(values), 3) if len(values) >= 2 else 0.0,
        "consistency_score": round(consistency_score(values), 4),
        "thresholds":        [],
        "strongest":         None,
        "safest":            None,
        "notes":             [],
    }
    if not values:
        result["notes"].append("no historical rows found")
        return result

    strongest = None
    strongest_score = -1.0
    safest = None
    safest_hit_rate = -1.0
    for t in thresholds:
        n = len(values)
        hits = sum(1 for v in values if v > t)
        hit_rate = hits / n
        lb = wilson_lower_bound(hits, n)
        ub = wilson_upper_bound(hits, n)
        grade = confidence_grade(hits, n, expected_p=0.5)
        row = {
            "threshold":         float(t),
            "games":             n,
            "hits":              hits,
            "hit_rate":          round(hit_rate, 4),
            "lb95":              round(lb, 4),
            "ub95":              round(ub, 4),
            "grade":             grade,
            "confidence_label":  confidence_label(n),
            "is_strong":         grade in {"A+", "A", "B"},
        }
        result["thresholds"].append(row)
        # Strongest = highest Wilson lower bound (safest bet).
        if lb > strongest_score:
            strongest_score = lb
            strongest = row
        # Safest = highest raw hit rate that still qualifies as strong.
        if row["is_strong"] and hit_rate > safest_hit_rate:
            safest_hit_rate = hit_rate
            safest = row

    result["strongest"] = strongest
    result["safest"] = safest
    return result


__all__ = ["analyse_thresholds"]
