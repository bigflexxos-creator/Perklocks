"""MLB Feature Engine — REAL data replacement for `_factors_random()`.

USER MANDATE (2026-07-21): "Never substitute randomness for missing data."

This module provides the ONE canonical source of truth for MLB
factor values consumed by `compute_lock_score()` and any other
place that used to call `player_rng.uniform()` or `_factors_random()`.

DESIGN:
    Every factor function returns `Optional[float]` in [0.0, 1.0]:
      • float  → real value derived from actual data
      • None   → data unavailable; caller MUST decide whether the
                 pick has enough real coverage to emit at all.

    NO RANDOM FALLBACK. NO PLACEHOLDER VALUES. When we can't compute
    a factor, the answer is None. Callers gate pick emission on:

        real_factors = {k: v for k, v in factors.items() if v is not None}
        if len(real_factors) < MIN_REAL_FACTORS:
            return None    # pick not emitted — insufficient data

USAGE:
    ctx = await build_mlb_game_context(game)  # already populated upstream
    factors = build_mlb_pitcher_k_factors(ctx, player="Framber Valdez",
                                            side="over", line=4.5)
    # factors == {
    #   "Pitcher K/9 (recent)":       0.86,    # from statsapi season k_pct
    #   "Opp K% vs same hand":        0.72,    # from mlb_team_k_intel
    #   "Pitch Count / Workload":     0.68,    # from statsapi ip_per_start
    #   "Park Strikeout Factor":      0.62,    # from _PARK_FACTORS
    #   "Recent Strikeout Form (L5)": None,    # data unavailable
    # }
    # 4/5 factors present → emit pick with confidence signal.
    # 0-1/5 factors present → do not emit.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("lockscore.mlb_feature_engine")


# ── Minimum factor coverage required to emit a pick ──────────────────
# If fewer than N real factors fire, the pick is dropped. We choose
# thresholds so a K prop needs at least 3/5, hitter prop 3/5, ML 4/7.
MIN_FACTORS_K_PROP     = 3
MIN_FACTORS_HITTER_PROP = 3
MIN_FACTORS_ML          = 4
MIN_FACTORS_TOTAL       = 4


# ═══════════════════════════════════════════════════════════════════════
# NUMERIC HELPERS
# ═══════════════════════════════════════════════════════════════════════
def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _scale(value: float, low: float, high: float,
           out_low: float = 0.40, out_high: float = 0.95) -> float:
    """Linearly map `value` in [low..high] onto [out_low..out_high],
    clamped at the edges. Used to convert raw stats (K%, ERA, xBA, ...)
    into 0.4-0.95 factor scores."""
    if high == low:
        return (out_low + out_high) / 2.0
    frac = (value - low) / (high - low)
    return _clamp(out_low + frac * (out_high - out_low), out_low, out_high)


# ═══════════════════════════════════════════════════════════════════════
# PITCHER K-PROP FACTORS (5 factors, ≥3 required to emit)
# ═══════════════════════════════════════════════════════════════════════
def factor_pitcher_recent_k(ctx: dict, pitcher_name: str) -> Optional[float]:
    """Season K% for the starter, mapped to a 0.4-0.95 factor score.

    Source: statsapi.mlb.com people?stats=season, populated in
    build_mlb_game_context step 5 (starting_pitcher_{home,away}.k_pct).

    Range: MLB starter K% roughly spans 15%-32%.
      • 32% (elite: Cole/Skenes) → 0.95
      • 22% (league avg)          → 0.62
      • 15% (soft-tosser)         → 0.40
    """
    for side_key in ("starting_pitcher_home", "starting_pitcher_away"):
        sp = ctx.get(side_key) or {}
        if sp.get("name", "").strip().lower() == pitcher_name.strip().lower():
            k_pct = sp.get("k_pct")
            if isinstance(k_pct, (int, float)):
                return round(_scale(float(k_pct), 0.15, 0.32), 3)
    return None


def factor_opp_team_k_vs_hand(ctx: dict, pitcher_name: str,
                              side: str = "over") -> Optional[float]:
    """Opposing team's season K% vs the starter's throwing hand.

    Source: mlb_team_k_intel (statsapi statSplits with sitCodes=vl,vr).
    Populated by build_mlb_game_context step 6.

    Range: MLB team K% vs a given hand spans ~17%-28%.
      • 28% (whiff-happy)     → 0.95 for OVER K
      • 22% (league avg)      → 0.62
      • 17% (contact lineup)  → 0.40 for OVER K

    For UNDER K props, the score is mirrored (weak-K team = high score).
    """
    for side_key in ("starting_pitcher_home", "starting_pitcher_away"):
        sp = ctx.get(side_key) or {}
        if sp.get("name", "").strip().lower() == pitcher_name.strip().lower():
            opp_k = sp.get("opp_k_pct")
            if isinstance(opp_k, (int, float)):
                score = _scale(float(opp_k), 0.17, 0.28)
                if str(side).lower() == "under":
                    # Mirror around 0.675 midpoint — weak K teams help
                    # the Under, whiff-happy teams hurt it.
                    score = 1.35 - score
                    score = _clamp(score, 0.40, 0.95)
                return round(score, 3)
    return None


def factor_pitch_count_workload(ctx: dict, pitcher_name: str,
                                line: Optional[float] = None) -> Optional[float]:
    """Innings-per-start proxy for pitch count / workload capacity.

    Source: statsapi season pitching stats (IP / gamesStarted).
    Populated by build_mlb_game_context step 5.

    Interpretation for K OVER: deeper starters = more Ks per outing.
      • 6.5+ IP/start (elite ace) → 0.92
      • 5.5 IP/start (avg)         → 0.60
      • 4.5 IP/start (short leash) → 0.35
    """
    for side_key in ("starting_pitcher_home", "starting_pitcher_away"):
        sp = ctx.get(side_key) or {}
        if sp.get("name", "").strip().lower() == pitcher_name.strip().lower():
            ip_per_start = sp.get("ip_per_start")
            if isinstance(ip_per_start, (int, float)):
                return round(_scale(float(ip_per_start), 4.0, 7.0), 3)
    return None


def factor_park_k(ctx: dict) -> Optional[float]:
    """Park K factor — inverse of park HR factor.

    Pitcher-friendly parks (San Diego, San Francisco, Seattle, Miami)
    boost K rates because pitchers work confidently over the plate;
    hitter havens (Coors, Cincinnati) suppress Ks slightly (hitters
    protect the plate).

    Source: _PARK_FACTORS lookup in signal_engine.mlb_deep.
    """
    try:
        from services.signal_engine.mlb_deep import _PARK_FACTORS
    except ImportError:
        return None
    home_team = ctx.get("home_team")
    if not home_team:
        return None
    pf = _PARK_FACTORS.get(home_team) or {}
    runs_pf = pf.get("runs")
    if not isinstance(runs_pf, (int, float)):
        return None
    # 100 = neutral, higher = more offense (bad for Ks).
    # Invert: park_k_factor = 200 - runs_pf, then scale.
    inverse = 200.0 - float(runs_pf)
    return round(_scale(inverse, 82.0, 110.0), 3)


def factor_recent_k_form(ctx: dict, pitcher_name: str) -> Optional[float]:
    """L5-start average K count factor.

    Requires L5 game-log fetch (not yet in build_mlb_game_context).
    Returns None until we wire that fetch. Users see this as "missing
    data" in the pick rationale — NEVER a random placeholder.
    """
    for side_key in ("starting_pitcher_home", "starting_pitcher_away"):
        sp = ctx.get(side_key) or {}
        if sp.get("name", "").strip().lower() == pitcher_name.strip().lower():
            l5_avg_k = sp.get("l5_avg_k")
            if isinstance(l5_avg_k, (int, float)):
                # L5 avg Ks typically 3-10. Elite = 8+, avg = 5, soft = <4.
                return round(_scale(float(l5_avg_k), 3.0, 9.0), 3)
    return None


def build_mlb_pitcher_k_factors(ctx: dict, player: str,
                                side: str, line: Optional[float] = None) -> tuple[dict, list[str]]:
    """Build all K-prop factors from REAL data.

    Returns:
        (factors_dict, sources_list)

    factors_dict keys → Optional[float] (None if unavailable).
    Callers MUST filter out Nones and check MIN_FACTORS_K_PROP before emit.
    """
    factors: dict[str, Optional[float]] = {
        "Pitcher K/9 (recent)":       factor_pitcher_recent_k(ctx, player),
        "Opp K% vs same hand":        factor_opp_team_k_vs_hand(ctx, player, side),
        "Pitch Count / Workload":     factor_pitch_count_workload(ctx, player, line),
        "Park Strikeout Factor":      factor_park_k(ctx),
        "Recent Strikeout Form (L5)": factor_recent_k_form(ctx, player),
    }
    sources = []
    if factors["Pitcher K/9 (recent)"] is not None:
        sources.append("statsapi_pitcher_season_k")
    if factors["Opp K% vs same hand"] is not None:
        sources.append("statsapi_team_k_split")
    if factors["Pitch Count / Workload"] is not None:
        sources.append("statsapi_pitcher_ip_per_start")
    if factors["Park Strikeout Factor"] is not None:
        sources.append("park_factors_table")
    if factors["Recent Strikeout Form (L5)"] is not None:
        sources.append("statsapi_pitcher_l5")
    return factors, sources


# ═══════════════════════════════════════════════════════════════════════
# BATTER HIT / HR / TB PROP FACTORS
# ═══════════════════════════════════════════════════════════════════════
def factor_batter_recent_form(ctx: dict, batter_name: str) -> Optional[float]:
    """L10 hit-rate factor from batter recent form.

    Source: mlb_hitter_intel (or bvp cache with recent PA/H).
    Population: needs batter enrichment upstream — see
    build_mlb_game_context roadmap. Returns None if not attached.
    """
    hitters = ctx.get("hitters") or {}
    hb = hitters.get(batter_name.strip().lower()) or {}
    l10 = hb.get("l10_hit_rate")
    if isinstance(l10, (int, float)):
        # L10 hit rate typically 0.15-0.40. 0.15 = slump, 0.40 = red hot.
        return round(_scale(float(l10), 0.15, 0.40), 3)
    return None


def factor_batter_matchup_vs_defense(ctx: dict, batter_name: str) -> Optional[float]:
    """Opposing pitcher xERA / stuff+ signal — inverted for hitter side.

    Weak pitcher (bad Stuff+, high xERA) = higher factor for hitter Overs.
    Populated via starting_pitcher_{home,away}.stuff_plus / era.
    """
    # Identify which pitcher this batter faces (opposite side's SP).
    # Simplest: use the current game's away pitcher for home batters, vice versa.
    # We need home/away batter → pitcher mapping. For now grab whichever SP
    # is present with a valid stuff+ metric.
    sp_stuff = []
    for side_key in ("starting_pitcher_home", "starting_pitcher_away"):
        sp = ctx.get(side_key) or {}
        s = sp.get("stuff_plus")
        if isinstance(s, (int, float)):
            sp_stuff.append(float(s))
    if not sp_stuff:
        return None
    # Take the average and INVERT (better pitcher = harder matchup = lower score).
    avg = sum(sp_stuff) / len(sp_stuff)
    # Stuff+ ~100 = league avg. 110+ = elite. 90 = weak.
    # For hitters facing them: 90 stuff+ → 0.92 (easy), 110 → 0.40 (hard).
    inverted = 200.0 - avg
    return round(_scale(inverted, 90.0, 110.0), 3)


def factor_batter_home_away(ctx: dict, batter_name: str, is_home: bool) -> Optional[float]:
    """Batter home/away split OPS factor.

    Source: statsapi splits (needs enrichment; None until wired).
    """
    hitters = ctx.get("hitters") or {}
    hb = hitters.get(batter_name.strip().lower()) or {}
    ops_key = "home_ops" if is_home else "away_ops"
    ops = hb.get(ops_key)
    if isinstance(ops, (int, float)):
        # OPS 0.600 = poor, 0.850 = above avg, 1.000 = elite.
        return round(_scale(float(ops), 0.600, 1.000), 3)
    return None


def factor_batter_platoon(ctx: dict, batter_name: str,
                          opp_pitcher_name: Optional[str] = None) -> Optional[float]:
    """Batter OPS vs opp pitcher's throwing hand.

    Source: statsapi splits (batter vs L / vs R). Populated by
    mlb_hitter_intel.fetch_batter_splits — must be attached to ctx.
    """
    hitters = ctx.get("hitters") or {}
    hb = hitters.get(batter_name.strip().lower()) or {}
    # Determine opposing pitcher hand:
    opp_hand = hb.get("opp_pitcher_hand")
    if opp_hand not in ("L", "R"):
        return None
    ops_vs = hb.get(f"vs_{opp_hand.lower()}hp_ops") or hb.get(f"vs_{opp_hand.lower()}_ops")
    if not isinstance(ops_vs, (int, float)):
        return None
    return round(_scale(float(ops_vs), 0.600, 1.000), 3)


def factor_batter_bvp(ctx: dict, batter_name: str) -> Optional[float]:
    """Career OPS vs the starting pitcher (BvP), sample-size gated.

    Source: mlb_bvp cache. Requires ≥8 PA lifetime for reliability.
    """
    hitters = ctx.get("hitters") or {}
    hb = hitters.get(batter_name.strip().lower()) or {}
    bvp = hb.get("bvp") or {}
    pa = bvp.get("pa") or 0
    ops = bvp.get("ops")
    if not (isinstance(ops, (int, float)) and pa >= 8):
        return None
    # BvP OPS: <0.500 = crushed, 0.720 = neutral, >1.000 = owns pitcher.
    return round(_scale(float(ops), 0.500, 1.100), 3)


def build_mlb_hitter_factors(ctx: dict, player: str, is_home: bool = True,
                             opp_pitcher_name: Optional[str] = None) -> tuple[dict, list[str]]:
    """Build hitter-prop factors from REAL data (or None)."""
    factors: dict[str, Optional[float]] = {
        "Recent L10 Hit Rate":     factor_batter_recent_form(ctx, player),
        "Matchup vs Defense":      factor_batter_matchup_vs_defense(ctx, player),
        "Home/Away Splits":        factor_batter_home_away(ctx, player, is_home),
        "Platoon Advantage":       factor_batter_platoon(ctx, player, opp_pitcher_name),
        "BvP (career vs pitcher)": factor_batter_bvp(ctx, player),
    }
    sources = []
    for k, v in factors.items():
        if v is not None:
            sources.append(k)
    return factors, sources


# ═══════════════════════════════════════════════════════════════════════
# MLB MONEYLINE / TOTAL FACTORS
# ═══════════════════════════════════════════════════════════════════════
def factor_starting_pitcher_edge(ctx: dict, pick_team: str) -> Optional[float]:
    """Delta between starting pitchers' Stuff+ (higher = advantage).

    Both starters must be resolved with Stuff+ for this to fire.
    """
    sph = ctx.get("starting_pitcher_home") or {}
    spa = ctx.get("starting_pitcher_away") or {}
    home_stuff = sph.get("stuff_plus")
    away_stuff = spa.get("stuff_plus")
    if not (isinstance(home_stuff, (int, float)) and isinstance(away_stuff, (int, float))):
        return None
    home_team = ctx.get("home_team", "")
    away_team = ctx.get("away_team", "")
    is_home = pick_team.strip().lower() == home_team.strip().lower()
    is_away = pick_team.strip().lower() == away_team.strip().lower()
    if not (is_home or is_away):
        return None
    # Delta: my pitcher's stuff+ minus opponent's.
    my_stuff = float(home_stuff) if is_home else float(away_stuff)
    opp_stuff = float(away_stuff) if is_home else float(home_stuff)
    delta = my_stuff - opp_stuff
    # Delta typically -20 to +20. +20 = huge edge, -20 = huge disadvantage.
    return round(_scale(delta, -20.0, 20.0), 3)


def factor_park_run_total(ctx: dict) -> Optional[float]:
    """Park runs factor for total picks. Higher = more offense = supports Over."""
    try:
        from services.signal_engine.mlb_deep import _PARK_FACTORS
    except ImportError:
        return None
    home_team = ctx.get("home_team")
    if not home_team:
        return None
    pf = _PARK_FACTORS.get(home_team) or {}
    runs_pf = pf.get("runs")
    if not isinstance(runs_pf, (int, float)):
        return None
    return round(_scale(float(runs_pf), 82.0, 118.0), 3)


def factor_weather_impact(ctx: dict, side_bias: str = "over") -> Optional[float]:
    """Weather signal for MLB totals — real temp / wind data via enricher.

    side_bias: "over" or "under" — mirrors the score for Under picks.
    """
    w = ctx.get("weather") or {}
    if not w or w.get("is_dome"):
        return None
    temp = w.get("temp_f")
    wind_mph = w.get("wind_mph") or 0
    wind_deg = w.get("wind_deg") or 0
    conditions = (w.get("conditions") or "").lower()
    lift = 0.0
    has_data = False
    if isinstance(temp, (int, float)):
        has_data = True
        if temp >= 85:   lift += 0.10
        elif temp >= 75: lift += 0.05
        elif temp <= 55: lift -= 0.06
    if wind_mph >= 8:
        has_data = True
        if 45 <= wind_deg <= 135:   lift += 0.08   # blowing out
        elif 180 <= wind_deg <= 270: lift -= 0.08  # blowing in
    if "rain" in conditions or "thunder" in conditions:
        has_data = True
        lift -= 0.06
    if not has_data:
        return None
    # Center at 0.60, ±0.30 range.
    score = 0.60 + lift
    if str(side_bias).lower() == "under":
        score = 1.20 - score
    return round(_clamp(score, 0.30, 0.95), 3)


def factor_team_bullpen(ctx: dict, pick_team: str) -> Optional[float]:
    """Bullpen ERA factor. Requires ctx['bullpens'] enrichment (not wired yet).
    Returns None until upstream fetch is added."""
    bullpens = ctx.get("bullpens") or {}
    pen = bullpens.get(pick_team.strip().lower())
    if not pen or "era" not in pen:
        return None
    era = pen["era"]
    if not isinstance(era, (int, float)):
        return None
    # Bullpen ERA range: 2.80 (elite) to 5.50 (bad). Lower = better = higher factor.
    inv = 8.00 - float(era)
    return round(_scale(inv, 2.5, 5.2), 3)


def factor_team_offense_recent(ctx: dict, pick_team: str) -> Optional[float]:
    """Runs-per-game over last 15 games. Requires ctx['team_runs'] enrichment."""
    tr = ctx.get("team_runs") or {}
    v = tr.get(pick_team.strip().lower())
    if not isinstance(v, (int, float)):
        return None
    # Runs/game range: 3.0 (bad) to 6.0 (elite). League avg ~4.5.
    return round(_scale(float(v), 3.0, 6.0), 3)


def factor_bvp_team_summary(ctx: dict, pick_team: str) -> Optional[float]:
    """Team's aggregate OPS vs today's opposing SP (from mlb_bvp)."""
    bvp_team = (ctx.get("bvp_team_vs_sp") or {}).get(pick_team.strip().lower())
    if not isinstance(bvp_team, (int, float)):
        return None
    return round(_scale(float(bvp_team), 0.550, 0.950), 3)


def build_mlb_ml_factors(ctx: dict, pick_team: str) -> tuple[dict, list[str]]:
    """Moneyline factors: 7 slots, ≥4 required to emit (see MIN_FACTORS_ML)."""
    factors: dict[str, Optional[float]] = {
        "Starting Pitcher Edge":  factor_starting_pitcher_edge(ctx, pick_team),
        "Team Offense (L15)":     factor_team_offense_recent(ctx, pick_team),
        "Team BvP vs Opp SP":     factor_bvp_team_summary(ctx, pick_team),
        "Bullpen ERA":            factor_team_bullpen(ctx, pick_team),
        "Park Effect":            factor_park_run_total(ctx),
        "Weather":                factor_weather_impact(ctx, "over"),
        "L/R Split Advantage":    None,   # roadmap: team OPS vs opp SP hand
    }
    sources = [k for k, v in factors.items() if v is not None]
    return factors, sources


def build_mlb_total_factors(ctx: dict, side: str) -> tuple[dict, list[str]]:
    """Game total factors: 6 slots, ≥4 required to emit."""
    factors: dict[str, Optional[float]] = {
        "Park Run Total":       factor_park_run_total(ctx),
        "Weather":              factor_weather_impact(ctx, side),
        "Combined Bullpen":     None,  # avg of both team bullpens
        "Combined Team Offense": None,
        "Starter Quality":      factor_starting_pitcher_edge(ctx, ctx.get("home_team", "")),
        "Umpire Strike Zone":   None,  # roadmap: mlb_umpire integration
    }
    sources = [k for k, v in factors.items() if v is not None]
    return factors, sources


# ═══════════════════════════════════════════════════════════════════════
# COVERAGE CHECK
# ═══════════════════════════════════════════════════════════════════════
def has_enough_real_data(factors: dict, market_type: str) -> bool:
    """Return True if enough real factors fired to emit a pick.

    market_type: 'k_prop' | 'hitter_prop' | 'ml' | 'total'.
    """
    real = sum(1 for v in factors.values() if v is not None)
    threshold = {
        "k_prop":       MIN_FACTORS_K_PROP,
        "hitter_prop":  MIN_FACTORS_HITTER_PROP,
        "ml":           MIN_FACTORS_ML,
        "total":        MIN_FACTORS_TOTAL,
    }.get(market_type, 3)
    return real >= threshold


__all__ = [
    "build_mlb_pitcher_k_factors",
    "build_mlb_hitter_factors",
    "build_mlb_ml_factors",
    "build_mlb_total_factors",
    "has_enough_real_data",
    "MIN_FACTORS_K_PROP",
    "MIN_FACTORS_HITTER_PROP",
    "MIN_FACTORS_ML",
    "MIN_FACTORS_TOTAL",
]
