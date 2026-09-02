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
    # Block 2D Closure §1 (2026-08) — the previous code read
    # ctx.team_runs_avg_home/away which was NEVER populated by the
    # MLB game-context enricher.  Real data lives at
    # ctx.team_runs[team_lower].  Try the legacy shape first for
    # backward compatibility, then fall back to the modern shape.
    tr_h = ctx.get("team_runs_avg_home")
    tr_a = ctx.get("team_runs_avg_away")
    if not (isinstance(tr_h, (int, float)) and isinstance(tr_a, (int, float))):
        _tr = ctx.get("team_runs") or {}
        _home = (ctx.get("home_team") or "").strip().lower()
        _away = (ctx.get("away_team") or "").strip().lower()
        if _home and _away:
            tr_h = _tr.get(_home)
            tr_a = _tr.get(_away)
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


# ══ MLB SHARED RUN DISTRIBUTION (§5 Universal Totals Truth) ══════════════════
# Universal Totals Truth §5: BOTH Over AND Under probabilities for an MLB
# Game Total MUST be derived from ONE authoritative run-total distribution
# so that P(Over) + P(Under) + P(Push) ≡ 1 exactly (conservation).  Prior
# implementation called `mlb_total_prob` independently for each side which
# produced two anchor-different estimates and left conservation to the
# guard's fail-closed off-board list.  This helper folds book fair-prob
# (joint-devigged) AND the same feature lifts (weather / park / pitching
# / team scoring) into a single Normal(μ, σ) distribution in RUNS space,
# then reads P(Over N) / P(Under N) off the same CDF.
_MLB_TOTAL_SIGMA_DEFAULT = 3.7   # Empirical stdev of MLB game totals (2019-2024).


def _phi(z: float) -> float:
    """Standard-normal CDF via erf (no scipy dependency)."""
    import math as _m
    return 0.5 * (1.0 + _m.erf(z / _m.sqrt(2.0)))


def _phi_inv(p: float) -> float:
    """Standard-normal quantile via Beasley-Springer-Moro approximation."""
    import math as _m
    if p <= 0.0:
        return -8.0
    if p >= 1.0:
        return 8.0
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow = 0.02425
    phigh = 1 - plow
    if p < plow:
        q = _m.sqrt(-2.0 * _m.log(p))
        return (((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5]) / \
               ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1)
    if p <= phigh:
        q = p - 0.5
        r = q * q
        return (((((a[0]*r + a[1])*r + a[2])*r + a[3])*r + a[4])*r + a[5]) * q / \
               (((((b[0]*r + b[1])*r + b[2])*r + b[3])*r + b[4])*r + 1)
    q = _m.sqrt(-2.0 * _m.log(1 - p))
    return -(((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5]) / \
             ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1)


def mlb_shared_run_distribution(
    line: float,
    book_over_odds: int | float | None,
    book_under_odds: int | float | None,
    ctx: dict,
    sigma: float | None = None,
) -> dict[str, Any]:
    """Return ONE Normal(μ, σ) MLB game-total distribution + both
    conserved side probabilities.

    Universal Totals Truth §5:
        P(Over N)  = 1 − Φ((N − μ)/σ)
        P(Under N) =     Φ((N − μ)/σ)      (half-line: no push)

    μ_anchor is derived by inverting the CDF against the JOINT-devigged
    fair book prob so the model starts book-anchored.  Feature lifts
    (weather / park / pitchers / team_runs) then shift μ in RUNS space
    by ``Δp × σ × sqrt(2π)`` — the first-order local slope of the CDF
    around the anchor — which lets the existing per-feature probability
    caps translate cleanly to a runs-space adjustment while preserving
    the caps.  Result: identical feature strengths as the legacy
    per-side model AND perfect conservation, side-symmetric.

    Returns:
        {
          available: bool,
          mp_over:   float,
          mp_under:  float,
          mu:        float,         # posterior expected total runs
          mu_anchor: float,         # book-implied μ before feature lifts
          sigma:     float,
          contribs:  {feature: probability_lift_signed_over},
          used_data: [feature,...],
          confidence: 0..1,
          reason:    str | None,    # populated when available=False
        }
    """
    import math as _m
    sigma = float(sigma if sigma and sigma > 0 else _MLB_TOTAL_SIGMA_DEFAULT)

    # Import here to avoid circular imports on module load.
    from services.totals_devig import joint_devig
    dv = joint_devig(book_over_odds, book_under_odds)
    if not dv.get("available"):
        return {"available": False,
                "reason": dv.get("reason", "paired_odds_missing")}

    fair_over = float(dv["fair_over"])
    fair_over = min(max(fair_over, 0.001), 0.999)
    # Invert Φ to find μ_anchor s.t. P(X > N) = fair_over → μ = N + σ·Φ⁻¹(fair_over).
    mu_anchor = float(line) + sigma * _phi_inv(fair_over)

    # ── Compute feature lifts in the *probability* domain reusing the
    # existing capped per-feature logic in `mlb_total_prob` for Over.
    # We call it with the book fair Over probability as `implied` so the
    # returned `total_lift` is the Over-side probability lift.
    over_side = mlb_total_prob("Over", float(line), fair_over, ctx)
    p_lift_over = float(over_side.get("total_lift") or 0.0)

    # Translate probability lift → μ (runs) lift via first-order Normal
    # slope: dp/dμ = φ((N−μ)/σ)/σ  →  Δμ ≈ Δp · σ · sqrt(2π) · e^(z²/2).
    # We use the slope AT μ_anchor so the linearisation is centred at
    # the current anchor probability (near 0.5 for typical lines).
    z_anchor = (float(line) - mu_anchor) / sigma
    slope = _m.exp(-0.5 * z_anchor * z_anchor) / (sigma * _m.sqrt(2.0 * _m.pi))
    if slope < 1e-6:
        slope = 1e-6
    mu_shift = p_lift_over / slope
    # Runs-space guard: never let a single game's feature lift exceed
    # ±1.2 runs (matches the ±10pp probability cap at anchor ~0.5).
    if mu_shift > 1.2:
        mu_shift = 1.2
    elif mu_shift < -1.2:
        mu_shift = -1.2
    mu = mu_anchor + mu_shift

    # Read both probabilities off the SAME Normal(μ, σ) CDF.
    z = (float(line) - mu) / sigma
    p_under = _phi(z)                       # P(X < N)
    p_over = 1.0 - p_under                  # P(X > N) — half-line, no push
    # Half-line MLB totals never push; integer lines are handled by an
    # explicit ±0.5 continuity correction upstream if ever emitted.
    p_over = min(max(p_over, 0.001), 0.999)
    p_under = 1.0 - p_over

    return {
        "available":  True,
        "mp_over":    round(p_over, 6),
        "mp_under":   round(p_under, 6),
        "mu":         round(mu, 4),
        "mu_anchor":  round(mu_anchor, 4),
        "mu_shift":   round(mu_shift, 4),
        "sigma":      round(sigma, 3),
        "line":       float(line),
        "fair_over":  round(fair_over, 6),
        "fair_under": round(1.0 - fair_over, 6),
        "vig_pct":    dv.get("vig_pct"),
        "contribs":   over_side.get("contributions") or {},
        "used_data":  list(over_side.get("used_data") or []),
        "confidence": float(over_side.get("confidence") or 0.0),
        "source":     "mlb_shared_run_distribution_v1",
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

    # ── Book-consensus (works for EVERY tennis pick, no player stats
    # needed). Measures the spread of the pick's implied prob across
    # bookmakers. Tight consensus (< 3pp spread) = sharp market =
    # trust the line. Wide spread (> 8pp) = uncertain market where
    # the median line is more likely wrong; small mean-reversion fade.
    #
    # PASS 3-5 CLOSURE (2026-06) — Universal Probability-Authority.
    # ``sharp_consensus`` was a positive lift purely derived from book
    # agreement, adding independent Win Expected probability from
    # market self-confirmation.  Now retained as ZERO-lift audit only.
    # ``book_uncertainty`` remains a negative DAMPENER (fade) which is
    # a valid market-uncertainty signal.
    consensus_spread = ctx.get("book_consensus_spread_pp")
    if isinstance(consensus_spread, (int, float)):
        if consensus_spread <= 3.0:
            contribs["sharp_consensus"] = 0.0     # benchmark only, no lift
        elif consensus_spread >= 8.0:
            contribs["book_uncertainty"] = -0.010
            lift += -0.010
            used.append("book_uncertainty")

    # ── Match tier signal (Grand Slam / ATP 1000 / Challenger / ITF)
    # from the tournament name. Higher-tier tourneys have sharper lines
    # (less recreational money) so the model should trust the book more
    # and add smaller lifts. Lower-tier events have more mispricing.
    # 2026-07-21 — Expanded coverage: tier signal fires for BOTH favorites
    # AND dogs (was dogs-only which excluded tennis_extra's 100% favorite
    # picks). Different sign per tier: chalk favorites in sharp markets
    # tend to hold; chalk favorites in soft ITF markets tend to slip.
    #
    # PASS 3-5 CLOSURE (2026-06) — Universal Probability-Authority.
    # Every FAVORITE-tier positive lift and every market-derived
    # DOG-lift here was derived purely from tier metadata + book
    # favoritism (no real performance signal).  Retained as ZERO-lift
    # audit rows only.  Negative DAMPENERS (tier_dog_fade in Slam /
    # Masters) remain in place — those describe legitimate market
    # sharpness, not a synthetic Win Expected boost.
    tier = ctx.get("match_tier")
    if isinstance(tier, str):
        tier_l = tier.lower()
        is_fav = implied >= 0.50
        # Slam / Masters 1000 — sharp, favorites hold, dogs fade
        if any(k in tier_l for k in ("slam", "atp1000", "wta1000", "masters")):
            if is_fav:
                contribs["tier_sharp_fav"] = 0.0     # benchmark only, no lift
            else:
                contribs["tier_dog_fade"] = -0.008
                lift += -0.008
                used.append("tier_dog_fade")
        elif any(k in tier_l for k in ("atp 500", "wta 500", "500")):
            if is_fav:
                contribs["tier_semi_sharp_fav"] = 0.0  # benchmark only, no lift
        elif any(k in tier_l for k in ("atp 250", "wta 250", "250")):
            # Tour-level 250 — slightly softer market, small fav bump
            if is_fav:
                contribs["tier_tour_fav"] = 0.0       # benchmark only, no lift
        elif "challenger" in tier_l:
            # Challenger — mid-softness market. Favorites at book-consensus
            # implied win slightly more than book price (chalk holds).
            if is_fav:
                contribs["tier_challenger_fav"] = 0.0 # benchmark only, no lift
            else:
                contribs["tier_dog_lift"] = 0.0       # benchmark only, no lift
        elif any(k in tier_l for k in ("itf", "futures", "m15", "m25", "w15", "w25")):
            # ITF Futures — HIGHEST market softness. Favorites still hold
            # but retirement risk cuts the reliable edge.
            if is_fav:
                contribs["tier_itf_fav"] = 0.0        # benchmark only, no lift
            else:
                contribs["tier_itf_dog_lift"] = 0.0   # benchmark only, no lift

    # ── Model-anchored implied signal (2026-07-21) ────────────────────
    # PERKLOCKS PASS 3 (2026-06) — Universal Probability-Authority
    # Closure §Tennis.  The prior implementation added a positive
    # ``lift`` for `book_anchor` / `book_coverage` / `value_zone`
    # whenever the market itself matched a chalky implied band.
    # That silently turned SPORTSBOOK information into predictive
    # inflation on top of the book-implied anchor.  Per directive:
    #
    #   "Keep sportsbook information as benchmark/metadata only".
    #
    # Fix: emit the same audit rows so downstream rationale/UI can
    # still describe the market context, but with ZERO lift and NO
    # entry into `used` (which would otherwise count them as
    # independent signals).  A book-only tennis pick now has an
    # empty ``used`` set which correctly maps to
    # ``MODEL_CONDITIONED`` (diagnostic only — cannot promote Win
    # Expected further downstream).
    if 0.55 <= implied <= 0.88:
        if 0.65 <= implied <= 0.80:
            contribs["book_anchor"] = 0.0    # benchmark only, no lift
        else:
            contribs["book_anchor"] = 0.0    # benchmark only, no lift

    # ── Real-book confirmation (US sportsbook coverage) ──────────────
    # Metadata only — coverage does NOT create predictive lift.
    if ctx.get("using_real_odds"):
        contribs["book_coverage"] = 0.0      # benchmark only, no lift
    if ctx.get("fair_odds_model"):
        # Elo/form-driven fair-odds engine says the market side is
        # justified. Model-based confirmation independent of book.
        contribs["fair_odds_model"] = 0.005
        lift += 0.005
        used.append("fair_odds_model")

    # ── Chalk-fade dampener (win-prob calibration) ────────────────────
    # Chalky favorites at implied ≥80% carry retirement/upset risk;
    # the book knows this and prices accordingly. Signal it explicitly
    # so the DD model doesn't over-lift picks that are already at the
    # trap-zone ceiling.
    if implied >= 0.82:
        contribs["chalk_dampener"] = -0.004
        lift += -0.004
        used.append("chalk_dampener")
    elif 0.60 <= implied < 0.70:
        # PASS 3 (2026-06) — ``value_zone`` was previously a +0.005
        # positive lift purely from a chalky implied band (book-
        # driven).  Retained as an audit row with ZERO lift so the
        # UI can still describe the market context, but it no
        # longer inflates Win Expected.
        contribs["value_zone"] = 0.0         # benchmark only, no lift

    total_lift = _clamp(lift, -CAP_TOTAL, CAP_TOTAL)
    mp = _clamp(implied + total_lift, 0.10, 0.92)
    return {
        "mp": mp, "anchor": implied,
        "contributions": contribs, "used_data": used,
        "confidence": min(1.0, len(used) / 4.0),
        "total_lift": total_lift,
    }
