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


# ══ MLB PITCHER PROP (Strikeouts / Outs Recorded) ══════════════════════════
def mlb_pitcher_prop_prob(
    market: str,
    side: str,
    line: float,
    implied: float,
    ctx: dict,
) -> dict[str, Any]:
    """Data-driven prob for MLB pitcher Ks / Outs props.

    ctx (populated by ``build_mlb_game_context`` + per-pitcher lookups):
      - ctx['pitcher_stats'] = {'stuff_plus','k_pct','xERA','xwoba_allowed'}
      - ctx['pitcher_stamina_ip_avg'] = float (career avg IP per start)
      - ctx['opposing_lineup_k_pct'] = float (team K% vs same-hand pitcher)
      - ctx['weather'] = {...}
    """
    contribs: dict[str, float] = {}
    used: list[str] = []
    lift = 0.0
    market_l = (market or "").lower()
    is_ks   = "strikeout" in market_l
    is_outs = "outs recorded" in market_l

    ps = ctx.get("pitcher_stats") or {}
    stuff = ps.get("stuff_plus")
    if isinstance(stuff, (int, float)):
        # Elite Stuff+ (110+) → more Ks + deeper starts.
        s_lift = _clamp((stuff - 100.0) * 0.003, -CAP_PITCHER, CAP_PITCHER)
        if abs(s_lift) >= 0.003:
            contribs["stuff_plus"] = round(s_lift, 4)
            lift += s_lift
            used.append("stuff_plus")

    if is_ks:
        opp_k = ctx.get("opposing_lineup_k_pct")
        if isinstance(opp_k, (int, float)):
            # League avg K% ~ 22%. Whiff-happy lineups (25%+) boost Ks.
            k_lift = _clamp((opp_k - 22.0) * 0.005, -CAP_LINEUP, CAP_LINEUP)
            if abs(k_lift) >= 0.003:
                contribs["opp_lineup_k"] = round(k_lift, 4)
                lift += k_lift
                used.append("opp_lineup_k")

    if is_outs:
        stamina = ctx.get("pitcher_stamina_ip_avg")
        if isinstance(stamina, (int, float)):
            # Line is often 15.5-17.5 outs (5-6 IP). Deep-starter (18+
            # outs avg) lifts Over meaningfully.
            outs_avg = stamina * 3.0
            gap = outs_avg - float(line)
            stam_lift = _clamp(gap * 0.02, -CAP_LINEUP, CAP_LINEUP)
            if abs(stam_lift) >= 0.003:
                contribs["stamina"] = round(stam_lift, 4)
                lift += stam_lift
                used.append("stamina")

    # Weather - pitchers hate hot humid days (fatigue) → shave outs.
    weather = ctx.get("weather") or {}
    if weather and not weather.get("is_dome"):
        temp = weather.get("temp_f")
        if isinstance(temp, (int, float)) and temp >= 90 and is_outs:
            w_lift = -0.010
            contribs["heat_fatigue"] = w_lift
            lift += w_lift
            used.append("heat_fatigue")

    if side.lower() == "under":
        lift = -lift
        contribs = {k: -v for k, v in contribs.items()}
    total_lift = _clamp(lift, -CAP_TOTAL, CAP_TOTAL)
    mp = _clamp(implied + total_lift, 0.15, 0.90)
    return {
        "mp": mp, "anchor": implied,
        "contributions": contribs, "used_data": used,
        "confidence": min(1.0, len(used) / 3.0),
        "total_lift": total_lift,
    }


# ══ SOCCER MONEYLINE ═══════════════════════════════════════════════════════
def soccer_ml_prob(
    side: str,           # home team name or away team name (of the pick)
    home_team: str,
    away_team: str,
    implied: float,
    ctx: dict,
) -> dict[str, Any]:
    """Data-driven prob for Soccer 1X2 moneyline picks.

    ctx (populated by ``build_soccer_game_context``):
      - ctx['home_form'] / ctx['away_form'] = {'gf_avg','ga_avg','n_matches','wins','draws','losses'}
      - ctx['home_xg_rolling'] / ctx['away_xg_rolling'] = {'xg_avg','xga_avg','xg_diff'}
      - ctx['home_manager_style'] / ctx['away_manager_style'] = 'attacking'|'defensive'|'balanced'
      - ctx['pressure'] = 'high'|'normal'
    """
    contribs: dict[str, float] = {}
    used: list[str] = []
    lift = 0.0

    is_home_side = (side.strip().lower() == home_team.strip().lower())
    perspective = "home" if is_home_side else "away"

    # Form-based lift: goal difference over the last N matches.
    my_form  = ctx.get(f"{perspective}_form") or {}
    opp_form = ctx.get(f"{'away' if is_home_side else 'home'}_form") or {}
    if my_form.get("n_matches", 0) >= 5 and opp_form.get("n_matches", 0) >= 5:
        my_gd  = float(my_form.get("gf_avg", 0)) - float(my_form.get("ga_avg", 0))
        opp_gd = float(opp_form.get("gf_avg", 0)) - float(opp_form.get("ga_avg", 0))
        form_gap = my_gd - opp_gd
        form_lift = _clamp(form_gap * 0.03, -0.04, 0.04)
        if abs(form_lift) >= 0.005:
            contribs["form"] = round(form_lift, 4)
            lift += form_lift
            used.append("form")

    # xG rolling window lift
    my_xg  = ctx.get(f"{perspective}_xg_rolling") or {}
    opp_xg = ctx.get(f"{'away' if is_home_side else 'home'}_xg_rolling") or {}
    if isinstance(my_xg.get("xg_diff"), (int, float)) and isinstance(opp_xg.get("xg_diff"), (int, float)):
        xg_gap = float(my_xg["xg_diff"]) - float(opp_xg["xg_diff"])
        xg_lift = _clamp(xg_gap * 0.025, -CAP_XG, CAP_XG)
        if abs(xg_lift) >= 0.005:
            contribs["xg_rolling"] = round(xg_lift, 4)
            lift += xg_lift
            used.append("xg_rolling")

    # Manager style bias (attacking home team vs defensive away = +goals)
    my_style  = ctx.get(f"{perspective}_manager_style") or "balanced"
    opp_style = ctx.get(f"{'away' if is_home_side else 'home'}_manager_style") or "balanced"
    if my_style == "attacking" and opp_style == "defensive":
        mgr_lift = 0.010
    elif my_style == "defensive" and opp_style == "attacking":
        mgr_lift = -0.006
    else:
        mgr_lift = 0.0
    if abs(mgr_lift) >= 0.003:
        contribs["manager"] = mgr_lift
        lift += mgr_lift
        used.append("manager")

    # Home-field advantage (~3-4pp on average in soccer)
    if is_home_side:
        contribs["home_field"] = 0.020
        lift += 0.020
        used.append("home_field")

    # High-pressure fixture on a chalk favorite → fade (upsets more common)
    if ctx.get("pressure") == "high" and implied >= 0.70:
        contribs["pressure_fade"] = -0.015
        lift += -0.015
        used.append("pressure_fade")

    total_lift = _clamp(lift, -CAP_TOTAL, CAP_TOTAL)
    mp = _clamp(implied + total_lift, 0.10, 0.92)
    return {
        "mp": mp, "anchor": implied,
        "contributions": contribs, "used_data": used,
        "confidence": min(1.0, len(used) / 4.0),
        "total_lift": total_lift,
    }


# ══ SOCCER TOTALS ══════════════════════════════════════════════════════════
def soccer_total_prob(
    side: str,           # "Over" | "Under"
    line: float,
    implied: float,
    ctx: dict,
) -> dict[str, Any]:
    """Data-driven prob for Soccer Total Goals Over/Under."""
    contribs: dict[str, float] = {}
    used: list[str] = []
    over_bias = 0.0

    # Projected goals from rolling xG (both teams' scoring + conceding).
    hx = ctx.get("home_xg_rolling") or {}
    ax = ctx.get("away_xg_rolling") or {}
    hf = ctx.get("home_form") or {}
    af = ctx.get("away_form") or {}
    proj = None
    if isinstance(hx.get("xg_avg"), (int, float)) and isinstance(ax.get("xg_avg"), (int, float)):
        proj = float(hx["xg_avg"]) + float(ax["xg_avg"])
        used.append("xg_projection")
    elif hf.get("gf_avg") and af.get("gf_avg"):
        proj = float(hf["gf_avg"]) + float(af["gf_avg"])
        used.append("gf_projection")
    if proj is not None:
        gap = proj - float(line)
        proj_lift = _clamp(gap * 0.035, -0.06, 0.06)
        contribs["projected_goals"] = round(proj_lift, 4)
        over_bias += proj_lift

    # Manager tempo — both attacking = extra goals.
    hs = ctx.get("home_manager_style") or "balanced"
    as_ = ctx.get("away_manager_style") or "balanced"
    if hs == "attacking" and as_ == "attacking":
        contribs["managers"] = 0.015
        over_bias += 0.015
        used.append("managers_attacking")
    elif hs == "defensive" and as_ == "defensive":
        contribs["managers"] = -0.018
        over_bias += -0.018
        used.append("managers_defensive")

    # High-pressure derby → more variance but slightly more goals
    # historically (attacking urgency + red cards + PKs).
    if ctx.get("pressure") == "high":
        contribs["derby_variance"] = 0.008
        over_bias += 0.008
        used.append("derby")

    if side.lower() == "under":
        over_bias = -over_bias
        contribs = {k: -v for k, v in contribs.items()}
    total_lift = _clamp(over_bias, -CAP_TOTAL, CAP_TOTAL)
    mp = _clamp(implied + total_lift, 0.15, 0.90)
    return {
        "mp": mp, "anchor": implied,
        "contributions": contribs, "used_data": used,
        "confidence": min(1.0, len(used) / 3.0),
        "total_lift": total_lift,
    }


# ══ TENNIS MONEYLINE ═══════════════════════════════════════════════════════
def tennis_ml_prob(
    side: str,           # player name of the pick
    player_a: str,
    player_b: str,
    surface: str,
    implied: float,
    ctx: dict,
) -> dict[str, Any]:
    """Data-driven prob for Tennis Moneyline picks.

    ctx (populated by ``build_tennis_match_context``):
      - ctx['sackmann_a'] / ctx['sackmann_b'] = {'first_serve_won_pct',
                                                  'return_pts_won_pct',
                                                  'break_pct'}
      - ctx['surface_elo_a'] / ctx['surface_elo_b'] = float
      - ctx['fatigue_a_matches_7d'] / ctx['fatigue_b_matches_7d'] = int
      - ctx['h2h_a_wins'] / ctx['h2h_b_wins'] = int
    """
    contribs: dict[str, float] = {}
    used: list[str] = []
    lift = 0.0

    is_a = (side.strip().lower() == player_a.strip().lower())
    my_suffix = "a" if is_a else "b"
    opp_suffix = "b" if is_a else "a"

    # Surface Elo (if attached to Sackmann doc; often absent so skip cleanly)
    my_elo  = ctx.get(f"surface_elo_{my_suffix}")
    opp_elo = ctx.get(f"surface_elo_{opp_suffix}")
    if isinstance(my_elo, (int, float)) and isinstance(opp_elo, (int, float)):
        elo_gap = my_elo - opp_elo
        elo_lift = _clamp(elo_gap * 0.00035, -CAP_TENNIS_ELO, CAP_TENNIS_ELO)
        if abs(elo_lift) >= 0.005:
            contribs["surface_elo"] = round(elo_lift, 4)
            lift += elo_lift
            used.append("surface_elo")

    sm_my  = ctx.get(f"sackmann_{my_suffix}") or {}
    sm_opp = ctx.get(f"sackmann_{opp_suffix}") or {}

    # ── Win% (52-week rolling) — strongest single Sackmann signal ─────
    wp_my  = sm_my.get("win_pct")
    wp_opp = sm_opp.get("win_pct")
    if isinstance(wp_my, (int, float)) and isinstance(wp_opp, (int, float)):
        # 20pp win-pct gap ≈ +6pp match win prob
        wp_lift = _clamp((wp_my - wp_opp) * 0.003, -0.040, 0.040)
        if abs(wp_lift) >= 0.003:
            contribs["win_pct"] = round(wp_lift, 4)
            lift += wp_lift
            used.append("win_pct")

    # ── Hold% (serve dominance) ────────────────────────────────────────
    hp_my  = sm_my.get("hold_pct")
    hp_opp = sm_opp.get("hold_pct")
    if isinstance(hp_my, (int, float)) and isinstance(hp_opp, (int, float)):
        hp_lift = _clamp((hp_my - hp_opp) * 0.002, -0.030, 0.030)
        if abs(hp_lift) >= 0.003:
            contribs["hold_pct"] = round(hp_lift, 4)
            lift += hp_lift
            used.append("hold_pct")

    # ── First-serve won% ───────────────────────────────────────────────
    fs_my  = (sm_my.get("first_serve_won_pct") or sm_my.get("first_serve_win_pct"))
    fs_opp = (sm_opp.get("first_serve_won_pct") or sm_opp.get("first_serve_win_pct"))
    if isinstance(fs_my, (int, float)) and isinstance(fs_opp, (int, float)):
        fs_lift = _clamp((fs_my - fs_opp) * 0.002, -CAP_TENNIS_SVC, CAP_TENNIS_SVC)
        if abs(fs_lift) >= 0.003:
            contribs["first_serve"] = round(fs_lift, 4)
            lift += fs_lift
            used.append("first_serve")

    # ── Break-saved% (composure on the ropes) ──────────────────────────
    bs_my  = sm_my.get("break_saved_pct")
    bs_opp = sm_opp.get("break_saved_pct")
    if isinstance(bs_my, (int, float)) and isinstance(bs_opp, (int, float)):
        bs_lift = _clamp((bs_my - bs_opp) * 0.0015, -0.020, 0.020)
        if abs(bs_lift) >= 0.003:
            contribs["break_saved"] = round(bs_lift, 4)
            lift += bs_lift
            used.append("break_saved")

    # ── Retirement risk penalty (chalk fade) ──────────────────────────
    rr_my = sm_my.get("retirement_rate_pct")
    if isinstance(rr_my, (int, float)) and rr_my >= 5.0:
        # >5% retirement rate = injury-prone; shave 1-2pp off chalk favs
        contribs["retirement_risk"] = -0.015
        lift += -0.015
        used.append("retirement_risk")

    # Fatigue: 3+ matches in the last 7 days = -1pp
    fm = ctx.get(f"fatigue_{my_suffix}_matches_7d") or 0
    fo = ctx.get(f"fatigue_{opp_suffix}_matches_7d") or 0
    if isinstance(fm, int) and isinstance(fo, int):
        f_lift = _clamp((fo - fm) * 0.007, -0.020, 0.020)
        if abs(f_lift) >= 0.005:
            contribs["fatigue"] = round(f_lift, 4)
            lift += f_lift
            used.append("fatigue")

    # H2H career
    aw = ctx.get("h2h_a_wins") or 0
    bw = ctx.get("h2h_b_wins") or 0
    total_h2h = aw + bw
    if total_h2h >= 3:
        share = (aw if is_a else bw) / total_h2h
        h_lift = _clamp((share - 0.5) * 0.04, -0.020, 0.020)
        if abs(h_lift) >= 0.005:
            contribs["h2h"] = round(h_lift, 4)
            lift += h_lift
            used.append("h2h")

    total_lift = _clamp(lift, -CAP_TOTAL, CAP_TOTAL)
    mp = _clamp(implied + total_lift, 0.10, 0.92)
    return {
        "mp": mp, "anchor": implied,
        "contributions": contribs, "used_data": used,
        "confidence": min(1.0, len(used) / 4.0),
        "total_lift": total_lift,
    }
