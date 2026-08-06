"""Phase 4D — NBA Prop Feature Engine.

Uses the existing NBA gamelog ingest (``services.nba_gamelog_ingest``)
which persists per-player game logs under
``db.player_game_logs``  with ``sport="nba"``.  Rows carry:

  player_id, player_name, season, date, opp, is_home, minutes,
  points, rebounds, assists, steals, blocks, threes_made, threes_att,
  fgm, fga, ftm, fta, plus_minus, usage, pace, rest_days, ...

The engine returns a factors dict (0-1 scaled) + a list of source
tags so `_props_picks_from_event` can drop through to
`has_enough_real_data_nba`.

**Design invariants** (matches NFL feature engine style):
  • Deterministic.  No RNG.  Cache-friendly.
  • Min 3 real factors required (`has_enough_real_data_nba`).
  • Missing data → None (never a fake value); caller drops the pick.
  • Every factor is 0-1 scaled where 0.5 = neutral.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("lockscore.nba_feature_engine")

MIN_FACTORS_NBA_PROP = 3


# ═══════════════════════════════════════════════════════════════════
# Factor helpers
# ═══════════════════════════════════════════════════════════════════
def _scale(value: float, low: float, high: float,
            *, invert: bool = False) -> float:
    if value is None:
        return 0.5
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.5
    if high == low:
        return 0.5
    x = (v - low) / (high - low)
    x = max(0.0, min(1.0, x))
    return 1.0 - x if invert else x


def has_enough_real_data_nba(factors: dict) -> bool:
    """Gate — must have at least ``MIN_FACTORS_NBA_PROP`` non-None factors."""
    return sum(1 for v in factors.values() if v is not None) >= MIN_FACTORS_NBA_PROP


# ═══════════════════════════════════════════════════════════════════
# Per-market stat mapping
# ═══════════════════════════════════════════════════════════════════
# Maps Odds API market_key → gamelog stat column + display name.
_MARKET_STAT_MAP = {
    "player_points":                     ("points",      "PTS"),
    "player_rebounds":                   ("rebounds",    "REB"),
    "player_assists":                    ("assists",     "AST"),
    "player_threes":                     ("threes_made", "3PM"),
    "player_steals":                     ("steals",      "STL"),
    "player_blocks":                     ("blocks",      "BLK"),
    "player_points_rebounds_assists":    ("pra",         "PRA"),
    "player_points_rebounds":            ("pr",          "PTS+REB"),
    "player_points_assists":             ("pa",          "PTS+AST"),
    "player_rebounds_assists":           ("ra",          "REB+AST"),
    # Alternate lines share the same stat column.
    "player_points_alternate":           ("points",      "PTS"),
    "player_rebounds_alternate":         ("rebounds",    "REB"),
    "player_assists_alternate":          ("assists",     "AST"),
    "player_threes_alternate":           ("threes_made", "3PM"),
    "player_points_rebounds_assists_alternate": ("pra", "PRA"),
}


def _stat_value(row: dict, stat_key: str) -> Optional[float]:
    """Compute the stat value for a row.  Handles composite stats
    (PRA / PR / PA / RA) by summing components."""
    if stat_key == "pra":
        p = row.get("points"); r = row.get("rebounds"); a = row.get("assists")
        if p is None or r is None or a is None:
            return None
        return float(p) + float(r) + float(a)
    if stat_key == "pr":
        p = row.get("points"); r = row.get("rebounds")
        if p is None or r is None:
            return None
        return float(p) + float(r)
    if stat_key == "pa":
        p = row.get("points"); a = row.get("assists")
        if p is None or a is None:
            return None
        return float(p) + float(a)
    if stat_key == "ra":
        r = row.get("rebounds"); a = row.get("assists")
        if r is None or a is None:
            return None
        return float(r) + float(a)
    v = row.get(stat_key)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ═══════════════════════════════════════════════════════════════════
# DB accessors
# ═══════════════════════════════════════════════════════════════════
async def _player_recent_logs(db, player_name: str, *,
                                 limit: int = 15) -> list[dict]:
    """Recent gamelog rows for a player (most recent first)."""
    if not player_name:
        return []
    q = {"sport": "nba", "player_name": {"$regex": f"^{player_name}$",
                                            "$options": "i"}}
    cursor = db.player_game_logs.find(q, {"_id": 0}).sort("date", -1).limit(limit)
    return await cursor.to_list(limit)


# ═══════════════════════════════════════════════════════════════════
# Factor builders
# ═══════════════════════════════════════════════════════════════════
def _factor_rolling_avg_vs_line(rows: list[dict], stat: str,
                                  line: float, side: str) -> Optional[float]:
    """L10 average vs the line — over side scores high when avg > line."""
    if not rows:
        return None
    vals = [_stat_value(r, stat) for r in rows[:10]]
    vals = [v for v in vals if v is not None]
    if len(vals) < 3:
        return None
    avg = sum(vals) / len(vals)
    # Sensitivity band: ±30 % around the line.
    band = max(0.5, line * 0.30)
    x = (avg - line) / band                # positive means over, negative under
    x = max(-1.5, min(1.5, x))
    x01 = (x + 1.5) / 3.0                  # to 0..1
    return x01 if side.lower() == "over" else 1.0 - x01


def _factor_hit_rate(rows: list[dict], stat: str,
                       line: float, side: str) -> Optional[float]:
    """Hit rate on the exact line over the last 10 games."""
    if not rows:
        return None
    hits = 0
    total = 0
    for r in rows[:10]:
        v = _stat_value(r, stat)
        if v is None:
            continue
        total += 1
        if side.lower() == "over":
            if v > line: hits += 1
        else:
            if v < line: hits += 1
    if total < 3:
        return None
    return hits / total


def _factor_minutes_stability(rows: list[dict]) -> Optional[float]:
    """High score when minutes are consistent (low variance)."""
    if not rows:
        return None
    mins = [r.get("minutes") for r in rows[:10]]
    mins = [float(m) for m in mins if isinstance(m, (int, float))]
    if len(mins) < 3:
        return None
    avg = sum(mins) / len(mins)
    if avg < 15:
        return 0.15         # bench player — low confidence
    var = sum((m - avg) ** 2 for m in mins) / len(mins)
    sd = var ** 0.5
    # High minutes + low sd → high stability.
    stability = _scale(avg, 20, 40) * (1.0 - min(1.0, sd / 12.0))
    return max(0.0, min(1.0, stability))


def _factor_usage(rows: list[dict]) -> Optional[float]:
    """Average usage rate over recent games (0-1 scaled)."""
    if not rows:
        return None
    usage = [r.get("usage") for r in rows[:10]]
    usage = [float(u) for u in usage if isinstance(u, (int, float)) and u > 0]
    if len(usage) < 2:
        return None
    avg = sum(usage) / len(usage)
    # Usage 15 % = low; 25 % = average; 35 % = star.
    return _scale(avg, 15, 35)


def _factor_pace(rows: list[dict]) -> Optional[float]:
    """Higher pace → more possessions → more counting stats."""
    if not rows:
        return None
    pace = [r.get("pace") for r in rows[:10]]
    pace = [float(p) for p in pace if isinstance(p, (int, float)) and p > 0]
    if len(pace) < 2:
        return None
    return _scale(sum(pace) / len(pace), 95, 105)


def _factor_rest(rows: list[dict]) -> Optional[float]:
    """Rest days ≥ 1 → 0.55 (positive).  Back-to-back → 0.30."""
    if not rows:
        return None
    latest = rows[0]
    rd = latest.get("rest_days")
    if rd is None:
        return None
    try:
        r = int(rd)
    except (TypeError, ValueError):
        return None
    if r == 0:  return 0.30        # back-to-back → fatigue
    if r == 1:  return 0.55        # normal rest
    if r == 2:  return 0.60        # good rest
    if r >= 3:  return 0.65        # extended rest
    return 0.5


def _factor_l3_trend(rows: list[dict], stat: str,
                       side: str) -> Optional[float]:
    """L3 average vs L10 average — trending up = good for Over."""
    if len(rows) < 6:
        return None
    l3 = [_stat_value(r, stat) for r in rows[:3]]
    l10 = [_stat_value(r, stat) for r in rows[:10]]
    l3 = [v for v in l3 if v is not None]
    l10 = [v for v in l10 if v is not None]
    if len(l3) < 3 or len(l10) < 6:
        return None
    trend = (sum(l3) / len(l3)) / max(1.0, sum(l10) / len(l10))
    x = _scale(trend, 0.75, 1.25)
    return x if side.lower() == "over" else 1.0 - x


# ═══════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════
async def build_nba_prop_factors(
    db: Any,
    *,
    player: str,
    market_key: str,
    side: str,
    line: Optional[float],
) -> tuple[dict, list[str]]:
    """Build the NBA prop factor dict + source-tag list.

    Returns ({factor_name: 0-1 value or None}, [source_tag, ...]).
    Emission gate: :func:`has_enough_real_data_nba(factors)`.
    """
    stat_info = _MARKET_STAT_MAP.get(market_key)
    if not stat_info:
        return {}, []
    stat_col, disp = stat_info

    rows = await _player_recent_logs(db, player)
    if not rows:
        return {}, []

    factors: dict[str, Optional[float]] = {}
    sources: list[str] = []

    if line is not None:
        f_avg = _factor_rolling_avg_vs_line(rows, stat_col, line, side)
        if f_avg is not None:
            factors[f"{disp} L10 vs Line"] = f_avg
            sources.append("player_game_logs.l10_avg")

        f_hit = _factor_hit_rate(rows, stat_col, line, side)
        if f_hit is not None:
            factors[f"{disp} L10 Hit Rate"] = f_hit
            sources.append("player_game_logs.hit_rate")

    f_min = _factor_minutes_stability(rows)
    if f_min is not None:
        factors["Minutes Stability"] = f_min
        sources.append("player_game_logs.minutes")

    f_usage = _factor_usage(rows)
    if f_usage is not None:
        factors["Usage Rate"] = f_usage
        sources.append("player_game_logs.usage")

    f_pace = _factor_pace(rows)
    if f_pace is not None:
        factors["Team Pace"] = f_pace
        sources.append("player_game_logs.pace")

    f_rest = _factor_rest(rows)
    if f_rest is not None:
        factors["Rest Days"] = f_rest
        sources.append("player_game_logs.rest")

    f_trend = _factor_l3_trend(rows, stat_col, side)
    if f_trend is not None:
        factors["L3 Trend"] = f_trend
        sources.append("player_game_logs.l3_trend")

    return factors, sources


__all__ = [
    "MIN_FACTORS_NBA_PROP",
    "has_enough_real_data_nba",
    "build_nba_prop_factors",
    "precompute_nba_prop_factors",
]


# ═══════════════════════════════════════════════════════════════════
# Precompute — mirrors the NFL pattern.  Call once per slate before
# emission; populates ctx["nba_precomputed"][player_lower][market_key]
# ═══════════════════════════════════════════════════════════════════
async def precompute_nba_prop_factors(
    db: Any,
    *,
    players: list[str],
    market_keys: list[str],
    lines_by_player_market: Optional[dict[tuple[str, str], list[tuple[float, str]]]] = None,
) -> dict:
    """Precompute NBA factor dicts for a slate.

    Parameters
    ----------
    players :
        Canonicalised player names to fetch gamelogs for.
    market_keys :
        Odds API market keys (``player_points``, ``player_threes``, ...).
    lines_by_player_market :
        Optional mapping ``(player_lower, market_key)`` → ``[(line, side), ...]``.
        When provided, the factor set includes line-sensitive factors
        (rolling avg vs line + hit rate) for each specified line.

    Returns
    -------
    dict :
        ``{"nba_precomputed": {player_lower: {market_key: {"factors": {...},
        "sources": [...]}}}}`` — ready to be merged into ``ctx``.
    """
    out: dict = {}
    for player in players:
        if not player:
            continue
        pl = player.strip().lower()
        rows = await _player_recent_logs(db, player)
        if not rows:
            continue
        per_player: dict = {}
        for mk in market_keys:
            if mk not in _MARKET_STAT_MAP:
                continue
            stat_col, disp = _MARKET_STAT_MAP[mk]
            # Aggregate factor bundle across every (line, side) pair for
            # this (player, market) so the sync emission path can dip in.
            lines_sides = ((lines_by_player_market or {}).get((pl, mk))
                             or [(None, "Over"), (None, "Under")])
            for (line, side) in lines_sides:
                factors: dict = {}
                sources: list[str] = []
                if line is not None:
                    f_avg = _factor_rolling_avg_vs_line(rows, stat_col, line, side)
                    if f_avg is not None:
                        factors[f"{disp} L10 vs Line"] = f_avg
                        sources.append("player_game_logs.l10_avg")
                    f_hit = _factor_hit_rate(rows, stat_col, line, side)
                    if f_hit is not None:
                        factors[f"{disp} L10 Hit Rate"] = f_hit
                        sources.append("player_game_logs.hit_rate")
                for name, val, tag in (
                    ("Minutes Stability", _factor_minutes_stability(rows),
                     "player_game_logs.minutes"),
                    ("Usage Rate",        _factor_usage(rows),
                     "player_game_logs.usage"),
                    ("Team Pace",         _factor_pace(rows),
                     "player_game_logs.pace"),
                    ("Rest Days",         _factor_rest(rows),
                     "player_game_logs.rest"),
                    ("L3 Trend",          _factor_l3_trend(rows, stat_col, side),
                     "player_game_logs.l3_trend"),
                ):
                    if val is not None:
                        factors[name] = val
                        sources.append(tag)
                if has_enough_real_data_nba(factors):
                    per_player[mk] = {"factors": factors, "sources": sources}
                    break              # first qualifying bundle wins
        if per_player:
            out[pl] = per_player
    return {"nba_precomputed": out}
