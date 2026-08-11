"""Soccer Feature Engine — real data replacement for `_factors_random`.

USER MANDATE (2026-07-21): "Replace Soccer with xG, shots, form,
injuries, rest. Never substitute randomness for missing data."

Real-data sources (mostly attached upstream by build_soccer_game_context
and pick-generation enrichment):
  • xG rolling avg (home/away)  → soccer_team_xg / form_proxy
  • Recent form (L5 goals ±)    → sportdb_client lookup_team_form
  • H2H trend / manager style   → context enrichment
  • Injuries / suspensions      → team_injuries cache (when populated)
  • Rest / days-since-last-match→ schedule delta from commence times
"""
from __future__ import annotations
import logging
from typing import Any, Optional

logger = logging.getLogger("lockscore.soccer_feature_engine")

MIN_FACTORS_SOCCER_ML = 3
MIN_FACTORS_SOCCER_TOTAL = 3


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _scale(value: float, low: float, high: float,
           out_low: float = 0.40, out_high: float = 0.95) -> float:
    if high == low:
        return (out_low + out_high) / 2.0
    frac = (value - low) / (high - low)
    return _clamp(out_low + frac * (out_high - out_low), out_low, out_high)


# ═══════════════════════════════════════════════════════════════════════
# Factor functions — read from game._ctx.
# `pick_team` is the side of the pick (home or away team name).
# ═══════════════════════════════════════════════════════════════════════
def _is_home(ctx: dict, pick_team: str) -> Optional[bool]:
    h = (ctx.get("home_team") or "").strip().lower()
    a = (ctx.get("away_team") or "").strip().lower()
    p = (pick_team or "").strip().lower()
    if p == h: return True
    if p == a: return False
    return None


def factor_xg_diff(ctx: dict, pick_team: str) -> Optional[float]:
    """Rolling xG differential (xG scored − xGA) for the picked team."""
    is_home = _is_home(ctx, pick_team)
    if is_home is None:
        return None
    key = "home_xg_rolling" if is_home else "away_xg_rolling"
    xg = ctx.get(key) or {}
    diff = xg.get("xg_diff")
    if not isinstance(diff, (int, float)):
        return None
    # xG diff typically ±2.0. +1.5 = elite dominance, -1.5 = getting outshot.
    return round(_scale(float(diff), -2.0, 2.0), 3)


def factor_form_ppg(ctx: dict, pick_team: str) -> Optional[float]:
    """Points-per-game over last 5-10 matches from soccer_form cache."""
    is_home = _is_home(ctx, pick_team)
    if is_home is None:
        return None
    key = "home_form" if is_home else "away_form"
    form = ctx.get(key) or {}
    ppg = form.get("ppg_avg") or form.get("ppg")
    if not isinstance(ppg, (int, float)):
        return None
    # PPG range 0-3. 2.5+ = title contender, 1.5 = mid, 0.5 = relegation form.
    return round(_scale(float(ppg), 0.5, 2.5), 3)


def factor_goals_scored(ctx: dict, pick_team: str) -> Optional[float]:
    """L5-10 goals scored per game — attacking form signal."""
    is_home = _is_home(ctx, pick_team)
    if is_home is None:
        return None
    key = "home_form" if is_home else "away_form"
    form = ctx.get(key) or {}
    gf = form.get("gf_avg")
    if not isinstance(gf, (int, float)):
        return None
    # 0.5 (dry) - 2.5 (prolific).
    return round(_scale(float(gf), 0.5, 2.5), 3)


def factor_goals_conceded(ctx: dict, pick_team: str) -> Optional[float]:
    """L5-10 goals conceded — defensive form (inverted for pick side)."""
    is_home = _is_home(ctx, pick_team)
    if is_home is None:
        return None
    key = "home_form" if is_home else "away_form"
    form = ctx.get(key) or {}
    ga = form.get("ga_avg")
    if not isinstance(ga, (int, float)):
        return None
    # Lower GA = better defense = higher factor for pick side.
    # 0.5 GA/game (elite) → 0.95, 2.0 GA/game (leaky) → 0.40.
    inverted = 3.0 - float(ga)
    return round(_scale(inverted, 1.0, 2.5), 3)


def factor_h2h_recent(ctx: dict, pick_team: str) -> Optional[float]:
    """Recent H2H trend if attached to ctx (team_h2h_recent)."""
    h2h = ctx.get("team_h2h_recent") or {}
    pk = (pick_team or "").strip().lower()
    row = h2h.get(pk)
    if not row:
        return None
    wins = row.get("wins", 0)
    total = row.get("total", 0)
    if not total:
        return None
    share = wins / total
    return round(_scale(share, 0.2, 0.8), 3)


def factor_injuries(ctx: dict, pick_team: str) -> Optional[float]:
    """Injury / suspension impact — needs upstream team_injuries fetch.
    Higher score = fewer key absences (favors the pick)."""
    inj = ctx.get("team_injuries") or {}
    pk = (pick_team or "").strip().lower()
    row = inj.get(pk)
    if not row:
        return None
    # row expected: {"key_absences": int}
    n = row.get("key_absences", 0)
    if not isinstance(n, (int, float)):
        return None
    # 0 key absences = 0.85, 3+ = 0.35.
    inverted = 4.0 - float(n)
    return round(_scale(inverted, 1.0, 4.0), 3)


def factor_rest_days(ctx: dict, pick_team: str) -> Optional[float]:
    """Days since last match — 4-7 = optimal, 2-3 = fatigued, 10+ = rusty."""
    rest = ctx.get("team_rest_days") or {}
    pk = (pick_team or "").strip().lower()
    d = rest.get(pk)
    if not isinstance(d, (int, float)):
        return None
    # Bell curve: peak around 5 days.
    if 4 <= d <= 7: return 0.85
    if d == 3 or d == 8: return 0.70
    if d == 2 or 9 <= d <= 11: return 0.55
    if d <= 1: return 0.35
    return 0.50


# ═══════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════
def build_soccer_ml_factors(ctx: dict, pick_team: str) -> tuple[dict, list[str]]:
    """Soccer moneyline factors: 7 slots, ≥3 required."""
    factors: dict[str, Optional[float]] = {
        "xG Differential":       factor_xg_diff(ctx, pick_team),
        "Form PPG":              factor_form_ppg(ctx, pick_team),
        "Goals Scored (avg)":    factor_goals_scored(ctx, pick_team),
        "Goals Conceded (avg)":  factor_goals_conceded(ctx, pick_team),
        "H2H Recent":            factor_h2h_recent(ctx, pick_team),
        "Injuries":              factor_injuries(ctx, pick_team),
        "Rest Days":             factor_rest_days(ctx, pick_team),
    }
    sources = [k for k, v in factors.items() if v is not None]
    return factors, sources


def build_soccer_total_factors(ctx: dict, side: str) -> tuple[dict, list[str]]:
    """Soccer total factors — combined xG + combined goals + combined
    goals conceded.

    Block 2D Closure §4 (2026-08) — ``Combined Goals Conceded`` is
    now wired from the SAME ``factor_goals_conceded`` helper used by
    the ML path (was hardcoded None).  H2H BTTS trend / Manager
    Styles / Injuries remain None until upstream data lands —
    MISSING DATA stays MISSING, never invented.

    Falls to ``PARTIAL`` classification when only 2 of 6 factors fire
    (below MIN_FACTORS_SOCCER_TOTAL=3) — caller drops the pick.
    """
    home_team = ctx.get("home_team") or ""
    away_team = ctx.get("away_team") or ""
    hx = factor_xg_diff(ctx, home_team) or 0.60
    ax = factor_xg_diff(ctx, away_team) or 0.60
    both_have_xg = (ctx.get("home_xg_rolling") is not None
                    and ctx.get("away_xg_rolling") is not None)
    combined_xg = round((hx + ax) / 2.0, 3) if both_have_xg else None

    hf = factor_goals_scored(ctx, home_team)
    af = factor_goals_scored(ctx, away_team)
    combined_goals = round((hf + af) / 2.0, 3) if (hf is not None and af is not None) else None

    # Combined goals conceded — SAME helper as the ML path.
    hc = factor_goals_conceded(ctx, home_team)
    ac = factor_goals_conceded(ctx, away_team)
    combined_conceded = round((hc + ac) / 2.0, 3) if (hc is not None and ac is not None) else None

    factors: dict[str, Optional[float]] = {
        "Combined xG":              combined_xg,
        "Combined Goals Scored":    combined_goals,
        "Combined Goals Conceded":  combined_conceded,
        "H2H BTTS trend":           None,   # roadmap (data ingest pending)
        "Manager Styles":           None,   # roadmap
        "Injuries (both teams)":    None,   # roadmap
    }
    sources = [k for k, v in factors.items() if v is not None]
    return factors, sources


def has_enough_soccer_data(factors: dict, market_type: str = "ml") -> bool:
    real = sum(1 for v in factors.values() if v is not None)
    return real >= (MIN_FACTORS_SOCCER_ML if market_type == "ml"
                    else MIN_FACTORS_SOCCER_TOTAL)


__all__ = [
    "build_soccer_ml_factors",
    "build_soccer_total_factors",
    "has_enough_soccer_data",
    "MIN_FACTORS_SOCCER_ML",
    "MIN_FACTORS_SOCCER_TOTAL",
]
