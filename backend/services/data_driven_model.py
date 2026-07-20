"""Data-Driven Model Probability — Phase 3 (2026-07-19).

Replaces the pure random `model_win_prob` tilt that used to drive pick
generation with a **feature-based** estimate that combines:

  - Book implied (anchor, always the base)
  - Weather (temp / wind / precip)                       — MLB
  - Park HR factor (base + by-handedness)                — MLB
  - Starting pitcher Stuff+ / xERA / K rate               — MLB
  - Batter Statcast xwOBA / barrel% + platoon splits      — MLB
  - Team run-per-game / batting order slot                — MLB
  - Rolling xG (10-match) + set-piece takers              — Soccer
  - Sackmann first-serve/first-set stats + surface Elo    — Tennis

DESIGN:
  - All lifts are BOUNDED (±3-5 percentage points each).
  - When enrichment data is missing, the lift is 0 (no penalty for
    absent data; the pick just anchors on book implied).
  - The function returns model_win_prob AND a `contributions` dict so
    the signal engine can surface "data-driven" evidence lines that
    mirror the reasoning that generated the pick.

SHAPE:

    result = data_driven_model_prob(pick_ctx, sport, market, side, implied)
    # result = {
    #     'mp':            0.548,                # capped 0.15-0.90
    #     'anchor':        implied,
    #     'contributions': {'weather': +0.024, 'park_hr': +0.012, ...},
    #     'confidence':    0.8,                  # 0-1 how much data we had
    #     'used_data':     ['weather','park_hr'],
    # }

This moves the pick pipeline from "generate then enrich" to "enrich
then generate" for MLB Totals + MLB Hitter Props — the highest-
leverage markets. Other sports still use the legacy random tilt
until their data-driven modules land in Phase 3 follow-ups.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("lockscore.data_driven_model")


# ── Per-lift caps ───────────────────────────────────────────────────────────────
# Each individual feature contributes at most these many percentage
# points to the model's win probability. Total combined lift capped
# to ±10 pp in the final blend so the model can't invent 20%+ edges
# just because 6 features aligned.
CAP_WEATHER    = 0.045   # ±4.5 pp — e.g. 20mph wind at Wrigley
CAP_PARK       = 0.035   # ±3.5 pp
CAP_PITCHER    = 0.050   # ±5.0 pp — elite Stuff+ pitcher vs weak lineup
CAP_BATTER     = 0.045   # ±4.5 pp
CAP_PLATOON    = 0.020   # ±2.0 pp
CAP_LINEUP     = 0.030   # ±3.0 pp
CAP_XG         = 0.045   # ±4.5 pp
CAP_SETPIECE   = 0.035   # ±3.5 pp
CAP_TENNIS_ELO = 0.055   # ±5.5 pp
CAP_TENNIS_SVC = 0.040   # ±4.0 pp
CAP_TOTAL      = 0.100   # ±10 pp combined — fundamental modelling limit


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


# ══ MLB TOTALS ═══════════════════════════════════════════════════════════════════
def mlb_total_prob(
    side: str,           # "Over" | "Under"
    line: float,         # e.g. 8.5
    implied: float,      # book no-vig implied, 0..1
    ctx: dict,           # game context (may have weather, park, pitchers)
) -> dict[str, Any]:
    """Data-driven win prob for an MLB Game Total side.

    Reads game context that's ATTACHED TO THE GAME OBJECT (not the
    pick, because the pick doesn't exist yet at generation time):
      - ctx['weather'] = {'temp_f','wind_mph','wind_deg','conditions','is_dome'}
      - ctx['park_hr_factor'] = int (100 = neutral)
      - ctx['starting_pitcher_home'] / _away = {'stuff_plus','era','k_pct'}
      - ctx['team_runs_avg_home'] / _away = float (last-15 games)
    """
    contribs: dict[str, float] = {}
    used: list[str] = []

    over_bias = 0.0  # positive = favors Over, negative = favors Under

    # ── Weather ────────────────────────────────────────────────────────────────────
    weather = ctx.get("weather") or {}
    if weather and not weather.get("is_dome"):
        w_lift = 0.0
        temp = weather.get("temp_f")
        wind_mph = weather.get("wind_mph") or 0
        wind_deg = weather.get("wind_deg") or 0
        conditions = (weather.get("conditions") or "").lower()
        # Warm air / high humidity carries the ball — favors Over
        if isinstance(temp, (int, float)):
            if temp >= 85:    w_lift += 0.024
            elif temp >= 75:  w_lift += 0.012
            elif temp >= 65:  w_lift += 0.004
            elif temp <= 55:  w_lift -= 0.016
        # Wind blowing out (roughly 45-135° from home plate) helps HRs.
        # Wind blowing in (180-270°) suppresses HRs — favors Under.
        if wind_mph >= 8:
            if 45 <= wind_deg <= 135:
                w_lift += min(0.030, 0.0015 * wind_mph)
            elif 180 <= wind_deg <= 270:
                w_lift -= min(0.030, 0.0015 * wind_mph)
        # Rain / storms suppress totals — pitchers get extra edge as
        # hitters lose reaction time and umpires shrink the strike zone.
        if "rain" in conditions or "thunder" in conditions:
            w_lift -= 0.018
        w_lift = _clamp(w_lift, -CAP_WEATHER, CAP_WEATHER)
        if abs(w_lift) >= 0.002:
            contribs["weather"] = round(w_lift, 4)
            over_bias += w_lift
            used.append("weather")

    # ── Park HR factor ───────────────────────────────────────────────────────────────
    park_hr = ctx.get("park_hr_factor")
    if isinstance(park_hr, (int, float)):
        park_dev = (float(park_hr) - 100.0) / 100.0   # e.g. Coors 120 → +0.20
        park_lift = _clamp(park_dev * 0.20, -CAP_PARK, CAP_PARK)
        if abs(park_lift) >= 0.002:
            contribs["park_hr"] = park_lift
            over_bias += park_lift
            used.append("park_hr")

    # ── Starting pitchers (avg Stuff+) ───────────────────────────────────────────────
    sp_h = ctx.get("starting_pitcher_home") or {}
    sp_a = ctx.get("starting_pitcher_away") or {}
    stuff_h = sp_h.get("stuff_plus")
    stuff_a = sp_a.get("stuff_plus")
    if isinstance(stuff_h, (int, float)) or isinstance(stuff_a, (int, float)):
        # Avg stuff+ across both starters. Elite pitchers (110+) suppress
        # totals; below-avg (90-) inflate them.
        vals = [v for v in (stuff_h, stuff_a) if isinstance(v, (int, float))]
        avg = sum(vals) / len(vals)
        # Deviation from league average 100 → percentage points.
        # A pitcher pair averaging 110 vs 100 = +10 stuff pts → -3 pp on Over.
        stuff_lift = _clamp(-(avg - 100.0) * 0.003, -CAP_PITCHER, CAP_PITCHER)
        if abs(stuff_lift) >= 0.005:
            contribs["pitching"] = stuff_lift
            over_bias += stuff_lift
            used.append("pitching")

    # ── Team offense (runs per game) vs the line ──────────────────────────────────
    tr_h = ctx.get("team_runs_avg_home")
    tr_a = ctx.get("team_runs_avg_away")
    if isinstance(tr_h, (int, float)) and isinstance(tr_a, (int, float)):
        proj = float(tr_h) + float(tr_a)
        # If projection is well above line → Over. Below line → Under.
        gap = proj - float(line)
        lineup_lift = _clamp(gap * 0.015, -CAP_LINEUP, CAP_LINEUP)
        if abs(lineup_lift) >= 0.005:
            contribs["team_scoring"] = lineup_lift
            over_bias += lineup_lift
            used.append("team_scoring")

    # Combine — flip sign if the pick is on the Under.
    if side.lower() == "under":
        over_bias = -over_bias
        contribs = {k: -v for k, v in contribs.items()}

    total_lift = _clamp(over_bias, -CAP_TOTAL, CAP_TOTAL)
    mp = _clamp(implied + total_lift, 0.15, 0.90)

    # Confidence = fraction of features that fired (max 4 here).
    confidence = min(1.0, len(used) / 4.0)

    return {
        "mp":            mp,
        "anchor":        implied,
        "contributions": contribs,
        "confidence":    confidence,
        "used_data":     used,
        "total_lift":    total_lift,
    }


# ══ MLB HITTER PROP (HR / Hits / Total Bases) ══════════════════════════════════
def mlb_hitter_prob(
    market: str,         # e.g. "Aaron Judge Over 0.5 Home Runs"
    side: str,           # usually "Over"
    line: float,
    implied: float,
    ctx: dict,           # batter + opposing pitcher context
) -> dict[str, Any]:
    """Data-driven prob for an MLB hitter Over/Under prop.

    ctx fields (any subset):
      - ctx['batter_stats'] = {'xwoba','barrel_pct','iso','xba','hr_per_pa'}
      - ctx['batter_hand']  = 'L'|'R'
      - ctx['pitcher_stats'] = {'era','hr_allowed_9','stuff_plus','xwoba_allowed'}
      - ctx['pitcher_hand']  = 'L'|'R'
      - ctx['park_hr_hand_factor']  = int (per-hand HR factor)
      - ctx['weather']  = {...}   (same shape as totals)
      - ctx['bvp_history'] = {'ops': float, 'pa': int}
    """
    contribs: dict[str, float] = {}
    used: list[str] = []
    lift = 0.0

    market_l = (market or "").lower()
    is_hr = "home run" in market_l
    is_hits = "hits" in market_l and "home run" not in market_l
    is_tb = "total bases" in market_l

    # ── Batter quality ─────────────────────────────────────────────────────────────
    bs = ctx.get("batter_stats") or {}
    xwoba = bs.get("xwoba")
    barrel = bs.get("barrel_pct")
    if isinstance(xwoba, (int, float)):
        # League avg xwOBA ~ 0.320. +0.030 = elite. -0.030 = poor.
        b_lift = _clamp((xwoba - 0.320) * 0.4, -CAP_BATTER, CAP_BATTER)
        if abs(b_lift) >= 0.005:
            contribs["batter_xwoba"] = b_lift
            lift += b_lift
            used.append("batter_xwoba")
    if is_hr and isinstance(barrel, (int, float)):
        # Barrel% — direct predictor of HRs. League avg ~ 8%.
        bar_lift = _clamp((barrel - 8.0) * 0.005, -CAP_BATTER, CAP_BATTER)
        if abs(bar_lift) >= 0.005:
            contribs["barrel_pct"] = bar_lift
            lift += bar_lift
            used.append("barrel_pct")

    # ── Opposing pitcher ───────────────────────────────────────────────────────────
    ps = ctx.get("pitcher_stats") or {}
    stuff = ps.get("stuff_plus")
    hr9 = ps.get("hr_allowed_9")
    if isinstance(stuff, (int, float)):
        # Elite pitcher suppresses batter production.
        p_lift = _clamp(-(stuff - 100.0) * 0.002, -CAP_PITCHER, CAP_PITCHER)
        if abs(p_lift) >= 0.005:
            contribs["opposing_pitcher"] = p_lift
            lift += p_lift
            used.append("opposing_pitcher")
    if is_hr and isinstance(hr9, (int, float)):
        # HR/9 — league avg ~1.2. Higher = more HRs allowed.
        hr_lift = _clamp((hr9 - 1.2) * 0.02, -CAP_PITCHER, CAP_PITCHER)
        if abs(hr_lift) >= 0.005:
            contribs["pitcher_hr_allowed"] = hr_lift
            lift += hr_lift
            used.append("pitcher_hr_allowed")

    # ── Park HR by hand (HR / TB props only) ────────────────────────────────────
    if is_hr or is_tb:
        park_hand = ctx.get("park_hr_hand_factor")
        if isinstance(park_hand, (int, float)):
            park_lift = _clamp((park_hand - 100.0) * 0.001, -CAP_PARK, CAP_PARK)
            if abs(park_lift) >= 0.005:
                contribs["park_hr_hand"] = park_lift
                lift += park_lift
                used.append("park_hr_hand")

    # ── Platoon (batter hand vs pitcher hand) ────────────────────────────────────
    bh = ctx.get("batter_hand")
    ph = ctx.get("pitcher_hand")
    if bh in ("L", "R") and ph in ("L", "R"):
        # L vs R (and R vs L) = +2pp advantage historically.
        # Same-side = -2pp.
        plat_lift = 0.015 if bh != ph else -0.010
        contribs["platoon"] = plat_lift
        lift += plat_lift
        used.append("platoon")

    # ── BvP history ───────────────────────────────────────────────────────────────────
    bvp = ctx.get("bvp_history") or {}
    ops = bvp.get("ops")
    pa = bvp.get("pa") or 0
    if isinstance(ops, (int, float)) and pa >= 8:
        # Sample-size gated: only trust BvP when there's ≥8 PAs.
        # League avg OPS ~0.720. Big-sample crushers (>1.000) get +1.5pp.
        bvp_lift = _clamp((ops - 0.720) * 0.03, -0.020, 0.020)
        if abs(bvp_lift) >= 0.003:
            contribs["bvp"] = bvp_lift
            lift += bvp_lift
            used.append("bvp")

    # Weather (HR props specifically care about wind + temp).
    if is_hr:
        weather = ctx.get("weather") or {}
        if weather and not weather.get("is_dome"):
            wl = 0.0
            temp = weather.get("temp_f")
            wind_mph = weather.get("wind_mph") or 0
            wind_deg = weather.get("wind_deg") or 0
            if isinstance(temp, (int, float)) and temp >= 82:
                wl += 0.010
            if wind_mph >= 10 and 45 <= wind_deg <= 135:
                wl += 0.015
            if wind_mph >= 10 and 180 <= wind_deg <= 270:
                wl -= 0.015
            wl = _clamp(wl, -CAP_WEATHER, CAP_WEATHER)
            if abs(wl) >= 0.005:
                contribs["weather"] = wl
                lift += wl
                used.append("weather")

    # Flip sign for Under (rare for hitter props but supported).
    if side.lower() == "under":
        lift = -lift
        contribs = {k: -v for k, v in contribs.items()}

    total_lift = _clamp(lift, -CAP_TOTAL, CAP_TOTAL)
    mp = _clamp(implied + total_lift, 0.15, 0.90)
    confidence = min(1.0, len(used) / 5.0)

    return {
        "mp":            mp,
        "anchor":        implied,
        "contributions": contribs,
        "confidence":    confidence,
        "used_data":     used,
        "total_lift":    total_lift,
    }
