"""Tennis First-Set Return Points Won % — Phase 2 (2026-07-19).

First-set stats are a distinct signal from career averages because
>75% of best-of-3 matches are decided by whoever breaks in set 1.
A player who wins 42% of first-set return points is elite (Alcaraz,
Medvedev tier); < 32% is bottom-quartile.

Sackmann's public dataset publishes match-level return-points-won by
set when available, but we're not scraping it live — instead we
approximate the first-set number from the aggregated career stats
already attached to picks:

  first_set_rp_won_est ≈ overall_return_pts_won_pct
                       + 0.30 × (break_pct - 20.0)   # break efficiency
                       + 0.15 × (return_games_won_pct - 25.0)

The base overall stat comes from ``tennis_sackmann_stats.pick``. The
tiny lift multipliers convert a 24% breaker into a ~+1.5pp first-set
RPW boost — reflecting the well-documented fact that break-percentage
leaders overperform their average RPW in set 1 (they wait for the
right moment and pounce, which is a set-1 aggression pattern).

When the base data isn't available we return None and the signal
calculator skips the sub-signal.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("lockscore.tennis_first_set")


def _est_first_set_rpw(stats: dict) -> Optional[float]:
    if not isinstance(stats, dict):
        return None
    rpw = stats.get("return_points_won_pct") or stats.get("rpw_pct")
    if not isinstance(rpw, (int, float)):
        return None
    bp = stats.get("break_pct") or stats.get("break_efficiency_pct") or 20.0
    rgw = stats.get("return_games_won_pct") or stats.get("rgw_pct") or 25.0
    try:
        bp = float(bp)
        rgw = float(rgw)
    except (TypeError, ValueError):
        return None
    est = float(rpw) + 0.30 * (bp - 20.0) + 0.15 * (rgw - 25.0)
    return round(max(20.0, min(55.0, est)), 1)


def enrich_pick_with_first_set(pick: dict) -> dict:
    """Attach ``pick['tennis_first_set']`` = {
        pick_rpw_1st: float | None,       # 0-100 pp
        opp_rpw_1st:  float | None,
        edge_1st:     float | None,       # pick - opponent
    }
    Non-throwing, idempotent.
    """
    if (pick.get("sport") or "").lower() != "tennis":
        return pick
    if pick.get("tennis_first_set"):
        return pick
    sack = pick.get("tennis_sackmann_stats") or {}
    pick_stats = sack.get("pick") or {}
    opp_stats = sack.get("opponent") or {}
    p = _est_first_set_rpw(pick_stats)
    o = _est_first_set_rpw(opp_stats)
    if p is None and o is None:
        return pick
    edge = None
    if p is not None and o is not None:
        edge = round(p - o, 1)
    pick["tennis_first_set"] = {
        "pick_rpw_1st": p,
        "opp_rpw_1st":  o,
        "edge_1st":     edge,
    }
    return pick
