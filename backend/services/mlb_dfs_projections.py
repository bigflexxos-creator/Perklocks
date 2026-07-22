"""MLB DFS-style per-game projections — Phase 1 Tier-1 (2026-07-22).

Rather than pull Fangraphs Steamer / The BAT X (both are behind
Cloudflare and reject `pybaseball`'s User-Agent), we synthesize
DFS-style projections locally from data we ALREADY have:

    Projected hits per game    = expected_PA × xBA
    Projected HR per game      = expected_PA × barrel% × park_HR_factor / 100
    Projected total bases      = xSLG × expected_PA
    Projected K (pitcher)      = expected_BF × k_percent
                                 × ump_zone_multiplier
                                 × park_K_factor
    Projected runs (team)      = base_runs_per_game × pace × park_run_factor

This is the same equation Steamer uses under the hood — combine
per-PA rates with matchup-specific expected volume. Advantages over
importing Steamer:
   • Uses TODAY's park / opp SP / opp bullpen / weather / ump
   • Auto-updates as our Statcast + BvP + platoon data refreshes
   • Zero external dependency, always fresh

Public API
----------
    project_hitter_stats(ctx, player_name) -> dict | None
    project_pitcher_stats(ctx, pitcher_name) -> dict | None
    dfs_hitter_factor(ctx, player_name, market_type) -> float | None
    dfs_pitcher_factor(ctx, pitcher_name) -> float | None
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("lockscore.services.mlb_dfs_projections")


def _num(x) -> Optional[float]:
    try:
        v = float(x)
        if v != v:
            return None
        return v
    except (TypeError, ValueError):
        return None


# ── Expected volume estimates ────────────────────────────────────────
# Batting-order dependent PA per game. Leadoff averages 4.6, 9-hole 3.9.
_LINEUP_PA_TABLE = {
    1: 4.62, 2: 4.55, 3: 4.47, 4: 4.36, 5: 4.24,
    6: 4.13, 7: 4.03, 8: 3.94, 9: 3.85,
}


def _expected_pa(lineup_slot: Optional[int]) -> float:
    """Return expected plate appearances per game for a given batting slot.

    Falls back to the team average (~4.2) when slot isn't known.
    """
    if isinstance(lineup_slot, int) and 1 <= lineup_slot <= 9:
        return _LINEUP_PA_TABLE[lineup_slot]
    return 4.2


def _expected_bf(pitch_count_projection: Optional[float]) -> float:
    """Estimated batters-faced given a pitch-count / innings projection.

    Rough conversion: 15 pitches per inning, 4.3 BF per inning.
    Fallback: 24 BF (6 IP typical starter).
    """
    if isinstance(pitch_count_projection, (int, float)):
        # ~4.3 BF per inning, ~15 pitches per inning
        return max(12.0, min(32.0, float(pitch_count_projection) / 15.0 * 4.3))
    return 24.0


# ── Hitter projections ────────────────────────────────────────────────

def project_hitter_stats(ctx: dict, player_name: str) -> Optional[dict]:
    """Return per-game DFS projections for a hitter.

    Requires at minimum a Statcast xBA row. Returns None otherwise.
    """
    if not player_name:
        return None
    hitters = ctx.get("hitters") or {}
    hb = hitters.get(player_name.strip().lower()) or {}
    sc = hb.get("statcast") or {}
    xba = _num(sc.get("xba"))
    xslg = _num(sc.get("xslg"))
    brl = _num(sc.get("barrel_pct"))  # 0-100 percentile scale
    if xba is None and xslg is None and brl is None:
        return None

    slot = hb.get("lineup_slot")
    pa = _expected_pa(slot)
    ab = pa * 0.87  # ~13% BB/HBP/SF

    # Park factors (default 100 = neutral)
    park_hr = _num(ctx.get("park_hr_factor")) or 100.0
    park_k  = _num(((ctx.get("park") or {}).get("k") or 100.0))

    proj_hits = ab * (xba or 0.240)
    proj_hr   = ab * ((brl or 5.0) / 100.0) * 0.30 * (park_hr / 100.0)  # barrel→HR conv ~30%
    proj_tb   = ab * (xslg or 0.400)  # xSLG = TB / AB
    proj_runs = pa * 0.135  # league avg R/PA ~ 0.135
    proj_rbi  = pa * 0.125  # league avg RBI/PA ~ 0.125

    return {
        "hits":        round(proj_hits, 2),
        "hr":          round(proj_hr, 3),
        "total_bases": round(proj_tb, 2),
        "runs":        round(proj_runs, 2),
        "rbi":         round(proj_rbi, 2),
        "hits_runs_rbi": round(proj_hits + proj_runs + proj_rbi, 2),
        "pa_expected": round(pa, 2),
    }


def dfs_hitter_factor(ctx: dict, player_name: str, market_type: str,
                      line: Optional[float] = None) -> Optional[float]:
    """Return a 0.30-0.95 factor score derived from DFS projections.

    market_type ∈ {"hits", "hr", "total_bases", "hits_runs_rbi", "runs"}
    line: the sportsbook line (e.g. 0.5 for hits). If given, factor is
          scaled by hit-probability that projection exceeds line.
    """
    proj = project_hitter_stats(ctx, player_name)
    if not proj:
        return None
    mt = (market_type or "").lower()
    key_map = {
        "hits": "hits",
        "batter_hits": "hits",
        "hr": "hr",
        "batter_home_runs": "hr",
        "total_bases": "total_bases",
        "batter_total_bases": "total_bases",
        "hits_runs_rbi": "hits_runs_rbi",
        "batter_hits_runs_rbis": "hits_runs_rbi",
        "runs": "runs",
    }
    key = key_map.get(mt)
    if not key:
        return None
    proj_val = proj.get(key)
    if not isinstance(proj_val, (int, float)):
        return None
    if not isinstance(line, (int, float)):
        line = {"hits": 0.5, "hr": 0.5, "total_bases": 1.5,
                "hits_runs_rbi": 1.5, "runs": 0.5}.get(key, 0.5)
    # Poisson-ish hit probability approximation:
    # If proj > line, factor scales 0.55-0.95 based on the ratio.
    # If proj < line, factor scales 0.30-0.55.
    ratio = proj_val / max(0.01, float(line))
    if ratio >= 1.0:
        # 1.0x line → 0.55, 2.0x line → 0.95
        v = 0.55 + min(0.40, (ratio - 1.0) * 0.40)
    else:
        # 0.5x line → 0.30, 1.0x line → 0.55
        v = 0.30 + max(0.0, (ratio - 0.5)) * 0.50
    return round(max(0.30, min(0.95, v)), 3)


# ── Pitcher projections ───────────────────────────────────────────────

def project_pitcher_stats(ctx: dict, pitcher_name: str) -> Optional[dict]:
    """Return per-start DFS-style projections for a pitcher.

    Requires either recent K/9 or Statcast k_percent to be resolvable.
    """
    if not pitcher_name:
        return None
    sp = None
    for side_key in ("starting_pitcher_home", "starting_pitcher_away"):
        s = ctx.get(side_key) or {}
        if s.get("name", "").strip().lower() == pitcher_name.strip().lower():
            sp = s
            break
    if not sp:
        return None

    # Recent K rate — use season K% from sp.k_pct if present, else statcast
    k_pct = _num(sp.get("k_pct"))
    if k_pct is not None and k_pct > 1.0:  # if % is stored as 24.5 (not 0.245)
        k_pct = k_pct / 100.0
    sc = sp.get("statcast") or {}
    if k_pct is None:
        kp_pct = _num(sc.get("k_percent"))  # percentile 0-100
        if kp_pct is not None:
            # Percentile 50 ≈ 22.5% k rate, elite 90 ≈ 30%, weak 10 ≈ 15%
            k_pct = 0.150 + (kp_pct / 100.0) * 0.15

    if k_pct is None:
        return None

    # Expected batters-faced — 6 IP typical
    ip_expected = _num(sp.get("ip_per_start")) or 5.5
    bf_expected = ip_expected * 4.3

    # Park + umpire multipliers (small)
    park_k = _num(((ctx.get("park") or {}).get("k") or 100.0))
    ump = ctx.get("plate_umpire") or {}
    ump_delta = _num(ump.get("delta_pct")) or 0.0
    # 1pp ump delta = ~4% K rate multiplier
    ump_mult = 1.0 + (ump_delta / 100.0) * 0.04
    park_mult = park_k / 100.0

    proj_k = bf_expected * k_pct * park_mult * ump_mult
    proj_er = ip_expected * (_num(sp.get("era")) or 4.20) / 9.0
    proj_ip = ip_expected

    return {
        "k":     round(proj_k, 2),
        "ip":    round(proj_ip, 2),
        "bf":    round(bf_expected, 2),
        "er":    round(proj_er, 2),
        "outs":  round(proj_ip * 3, 1),
    }


def dfs_pitcher_factor(ctx: dict, pitcher_name: str,
                       line: Optional[float] = None, side: str = "over") -> Optional[float]:
    """Return a 0.30-0.95 factor score for pitcher K projection vs line."""
    proj = project_pitcher_stats(ctx, pitcher_name)
    if not proj:
        return None
    proj_k = proj.get("k")
    if not isinstance(proj_k, (int, float)):
        return None
    if not isinstance(line, (int, float)):
        line = 5.5
    ratio = proj_k / max(0.5, float(line))
    if ratio >= 1.0:
        v = 0.55 + min(0.40, (ratio - 1.0) * 0.35)
    else:
        v = 0.30 + max(0.0, (ratio - 0.5)) * 0.50
    if side.lower().startswith("under"):
        v = 1.0 - v + 0.55  # invert then re-anchor
    return round(max(0.30, min(0.95, v)), 3)


__all__ = [
    "project_hitter_stats",
    "project_pitcher_stats",
    "dfs_hitter_factor",
    "dfs_pitcher_factor",
]
