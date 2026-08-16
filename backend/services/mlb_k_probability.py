"""MLB Strikeout Poisson Probability Engine.

USER MANDATE (2026-07-27): "Went 6/11 on K's. Want to go 8/11 or 11/11.
Better research behind them. Main line only."

This module computes P(K's ≥ line) using a Poisson distribution around
expected K's = pitcher_K/9 × expected_IP × opponent_K%_adjustment ×
park_K_factor × umpire_K_factor.

Then compares P(model) to P(book_implied). We surface picks ONLY when:
  - |P(model) - P(book_implied)| >= edge_threshold_pp
  - P(model_side) >= min_win_prob
  - Cross-conflict guard: same pitcher can't have both Over AND Under
    surface on same slate (only the stronger side wins).

Returns per-pitcher-line evaluation. Feeds into sports_engine's K-prop
emission gate so we drop weak K picks BEFORE they hit the board.
"""
from __future__ import annotations

import math
import logging
from typing import Any, Optional

logger = logging.getLogger("lockscore.mlb_k_math")

# Edge / prob thresholds — tune here to hit user's target win rate
EDGE_THRESHOLD_PP = 5.0        # Model must beat book implied by >= 5pp
MIN_MODEL_WIN_PROB = 0.60      # Model side must project >= 60% win
MAX_STRIKEOUT_ODDS = -220      # No K pick priced worse than -220 (chalk trap)

# League-average K/9 (2025 season) — baseline for pitchers with no data
LEAGUE_AVG_K_PER_9 = 8.5
LEAGUE_AVG_IP_PER_START = 5.4


def _poisson_pmf(k: int, lam: float) -> float:
    """Poisson probability mass function P(X = k) given rate λ."""
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    try:
        return (lam ** k) * math.exp(-lam) / math.factorial(k)
    except (OverflowError, ValueError):
        return 0.0


def _poisson_cdf(k: int, lam: float) -> float:
    """P(X ≤ k) — used for Under-side probability."""
    if lam <= 0:
        return 1.0
    return sum(_poisson_pmf(i, lam) for i in range(k + 1))


def _implied_from_american(odds: int) -> float:
    """Convert American odds to implied probability."""
    if odds is None:
        return 0.5
    try:
        odds = int(odds)
    except (TypeError, ValueError):
        return 0.5
    if odds < 0:
        return (-odds) / ((-odds) + 100.0)
    return 100.0 / (odds + 100.0)


def compute_expected_k(ctx: dict, pitcher_name: str) -> Optional[dict]:
    """Compute expected K's (λ) using all real data.

    Returns:
        {
            "expected_k": float,
            "components": dict of each multiplier applied,
            "data_quality": int (# of real signals used),
        }
    None if pitcher not found in ctx.
    """
    # Find the pitcher
    sp_data = None
    for side_key in ("starting_pitcher_home", "starting_pitcher_away"):
        sp = ctx.get(side_key) or {}
        if sp.get("name", "").strip().lower() == pitcher_name.strip().lower():
            sp_data = sp
            break

    if not sp_data:
        return None

    components: dict[str, float] = {}
    signals = 0

    # ── Base K rate: prefer L5 avg → season K/9 → league avg ──
    k_per_9 = None
    l5_avg_k = sp_data.get("l5_avg_k")
    l5_avg_ip = sp_data.get("l5_avg_ip") or sp_data.get("ip_per_start")
    ip_per_start = sp_data.get("ip_per_start")

    if isinstance(l5_avg_k, (int, float)) and isinstance(l5_avg_ip, (int, float)) and l5_avg_ip > 0:
        # Recent K/9 from last 5 starts
        k_per_9 = (float(l5_avg_k) / float(l5_avg_ip)) * 9.0
        components["source_l5"] = round(k_per_9, 2)
        signals += 1
    else:
        # Season K% × PA/inning estimate → K/9 ~ K% × ~4.3 PA/inning × 9
        k_pct = sp_data.get("k_pct")
        if isinstance(k_pct, (int, float)):
            k_per_9 = float(k_pct) * 4.3 * 9.0
            components["source_season_k_pct"] = round(k_per_9, 2)
            signals += 1
        else:
            k_per_9 = LEAGUE_AVG_K_PER_9
            components["source_league_avg"] = k_per_9

    # ── Expected IP for THIS start ──
    exp_ip = ip_per_start if isinstance(ip_per_start, (int, float)) else LEAGUE_AVG_IP_PER_START
    exp_ip = float(exp_ip)
    if isinstance(l5_avg_ip, (int, float)):
        # Blend season and L5 to smooth out one-off short outings
        exp_ip = 0.5 * float(exp_ip) + 0.5 * float(l5_avg_ip)
    components["expected_ip"] = round(exp_ip, 2)

    # ── Baseline K expectation ──
    lam = (k_per_9 / 9.0) * exp_ip
    components["baseline_k"] = round(lam, 2)

    # ── Opponent K% multiplier ──
    opp_k = sp_data.get("opp_k_pct")
    if isinstance(opp_k, (int, float)):
        # League avg team K% ≈ 22%. Multiplier = opp_k / 0.22
        opp_mult = float(opp_k) / 0.22
        # Cap to reasonable range (0.75 - 1.30)
        opp_mult = max(0.75, min(1.30, opp_mult))
        lam *= opp_mult
        components["opp_k_multiplier"] = round(opp_mult, 3)
        signals += 1

    # ── PITCHER-vs-TEAM (PvT) multiplier ──────────────────────────
    # 2026-07-27 BUG FIX (Wheeler bug): we picked Wheeler Under 6.5 K's
    # even though he goes for 8-9 K's every time vs the Marlins. The
    # opponent K% multiplier only knows the AVERAGE team K% — not how
    # THIS pitcher performs against THIS specific team.
    # PvT is pre-fetched into sp_data["pvt"] by the pipeline. See
    # services/mlb_pvt.py for the data model.
    pvt = sp_data.get("pvt")
    if isinstance(pvt, dict) and pvt.get("significance") in ("medium", "high"):
        try:
            from services.mlb_pvt import compute_pvt_k_multiplier
            # league_avg_k_per_gs = the pitcher's own baseline K per game
            # (approximated as k_per_9 × exp_ip / 9). Prevents double-
            # counting: PvT multiplier reflects DEVIATION from this
            # pitcher's own average, not from league.
            baseline_k_per_gs = (float(k_per_9) / 9.0) * float(exp_ip)
            pvt_mult = compute_pvt_k_multiplier(pvt, league_avg_k_per_gs=baseline_k_per_gs)
            lam *= pvt_mult
            components["pvt_multiplier"] = round(pvt_mult, 3)
            components["pvt_career_k_per_gs"] = pvt.get("k_per_gs_vs_team")
            components["pvt_recent_k"] = pvt.get("recent_k_vs_team")
            signals += 1
        except Exception as _pvtx:
            logger.debug("PvT multiplier failed for %s: %s", pitcher_name, _pvtx)

    # ── Park K factor ──
    try:
        from services.signal_engine.mlb_deep import _PARK_FACTORS
        home_team = ctx.get("home_team")
        if home_team:
            pf = _PARK_FACTORS.get(home_team) or {}
            runs_pf = pf.get("runs")
            if isinstance(runs_pf, (int, float)):
                # Inverted: high-run parks suppress K's a touch
                park_k_mult = 1.0 + (100.0 - float(runs_pf)) / 500.0
                park_k_mult = max(0.94, min(1.06, park_k_mult))
                lam *= park_k_mult
                components["park_k_multiplier"] = round(park_k_mult, 3)
                signals += 1
    except Exception:
        pass

    # ── Umpire K-zone bias ──
    ump = ctx.get("plate_umpire") or {}
    delta = ump.get("delta_pct")
    if isinstance(delta, (int, float)):
        # Each +1pp K-zone delta = +2% K's for the pitcher
        ump_mult = 1.0 + float(delta) * 0.02
        ump_mult = max(0.93, min(1.07, ump_mult))
        lam *= ump_mult
        components["ump_k_multiplier"] = round(ump_mult, 3)
        signals += 1

    # ── Statcast xwOBA-against bump (elite whiff pitchers) ──
    sc = sp_data.get("statcast") or {}
    xw = sc.get("xwoba_against") or sc.get("xwoba")
    if isinstance(xw, (int, float)):
        # xwOBA < 0.290 = elite whiff pitcher → +5% K's
        # xwOBA > 0.340 = hittable → -5% K's
        # Linear between 0.260 (best) and 0.360 (worst)
        xw_mult = 1.0 + (0.315 - float(xw)) * 1.0  # +1 → 0.315 = neutral
        xw_mult = max(0.92, min(1.10, xw_mult))
        lam *= xw_mult
        components["xwoba_multiplier"] = round(xw_mult, 3)
        signals += 1

    return {
        "expected_k": round(lam, 2),
        "components": components,
        "data_quality": signals,
        # PHASE 2 (2026-06) — Universal Simulator Provenance Tagging.
        # Poisson K probability is CAUSAL_INDEPENDENT from the book:
        # λ is computed from pitcher K/9 × expected IP × opponent K% ×
        # park × umpire × Statcast — NOT back-solved from sportsbook
        # implied probability.  When only the league-average K/9
        # fallback is used (zero real pitcher signals), the simulator
        # degrades to PRIOR_ONLY and decision_valid=False.
        "provenance":     _classify_provenance(components, signals),
        "input_quality":  _classify_input_quality(signals),
        "decision_valid": signals >= 2,
    }


# ─────────────────────────────────────────────────────────────────────
# PHASE 2 (2026-06) — Universal simulator classification helpers.
# Applied to every MLB-K evaluation so downstream Magic/Bet-Quality
# can distinguish real independent evidence from league-average priors.
# ─────────────────────────────────────────────────────────────────────
def _classify_provenance(components: dict, signals: int) -> str:
    """Return one of:
        CAUSAL_INDEPENDENT  — pitcher-specific evidence drove λ
        EMPIRICAL_INDEPENDENT — recent L5 form + opponent + park
        MODEL_CONDITIONED   — NEVER for MLB-K (kept for parity)
        PRIOR_ONLY          — only league-avg K/9 available
        INVALID             — no pitcher found upstream (compute_expected_k
                              already returns None in this case; kept for
                              defensive external callers)
    """
    if signals <= 0:
        return "PRIOR_ONLY"
    # If the base K/9 came from league-average (no pitcher-specific
    # source) and only 1 auxiliary signal exists, treat as PRIOR_ONLY.
    if "source_league_avg" in components and signals < 2:
        return "PRIOR_ONLY"
    # L5-form-driven base → EMPIRICAL_INDEPENDENT (recent games are the
    # empirical evidence).  Season K% base → CAUSAL_INDEPENDENT (season
    # rate is a stable causal input).
    if "source_l5" in components:
        return "EMPIRICAL_INDEPENDENT"
    if "source_season_k_pct" in components:
        return "CAUSAL_INDEPENDENT"
    return "PRIOR_ONLY"


def _classify_input_quality(signals: int) -> str:
    """Return one of FULL / STRONG / PARTIAL / PRIOR_ONLY / INVALID.

    Delegates to the universal contract in
    :mod:`services.simulator_provenance` so every sport shares the
    same signal-count → quality mapping.
    """
    from services.simulator_provenance import classify_input_quality
    return classify_input_quality(signals)


def evaluate_k_pick(
    ctx: dict,
    pitcher_name: str,
    line: float,
    side: str,
    book_odds: Optional[int] = None,
) -> Optional[dict]:
    """Return {emit: bool, model_prob, edge_pp, expected_k, reason}.

    side: "over" or "under"
    line: e.g. 5.5 (main-line K prop)
    """
    exp = compute_expected_k(ctx, pitcher_name)
    if not exp:
        return {"emit": False, "reason": "no_pitcher_data"}

    if exp["data_quality"] < 2:
        return {"emit": False, "reason": "insufficient_signals", "expected_k": exp["expected_k"]}

    lam = exp["expected_k"]
    side_low = str(side).lower()

    # Line is X.5 — the threshold for a bet is different for Over vs Under.
    # Over line means K's must be > line, i.e. >= ceil(line)
    # Under line means K's must be < line, i.e. <= floor(line)
    k_threshold_over = math.ceil(line)   # Over 5.5 → K >= 6
    k_threshold_under = math.floor(line) # Under 5.5 → K <= 5

    if side_low == "over":
        # P(K >= k_threshold_over) = 1 - CDF(k_threshold_over - 1)
        model_prob = 1.0 - _poisson_cdf(k_threshold_over - 1, lam)
    else:  # under
        model_prob = _poisson_cdf(k_threshold_under, lam)

    # Book implied
    book_implied = _implied_from_american(book_odds) if book_odds is not None else 0.5
    edge_pp = (model_prob - book_implied) * 100.0

    # ── Emission gates ──
    reason = None

    # Odds cap: no chalk traps worse than MAX_STRIKEOUT_ODDS
    if book_odds is not None and int(book_odds) <= MAX_STRIKEOUT_ODDS:
        return {
            "emit": False, "reason": "odds_too_chalky",
            "model_prob": round(model_prob, 3), "expected_k": lam,
            "book_odds": int(book_odds),
        }

    # Model must beat book implied by threshold
    if edge_pp < EDGE_THRESHOLD_PP:
        return {
            "emit": False, "reason": "insufficient_edge",
            "model_prob": round(model_prob, 3), "book_implied": round(book_implied, 3),
            "edge_pp": round(edge_pp, 2), "expected_k": lam,
        }

    # Model win prob floor
    if model_prob < MIN_MODEL_WIN_PROB:
        return {
            "emit": False, "reason": "model_win_prob_low",
            "model_prob": round(model_prob, 3), "expected_k": lam,
            "edge_pp": round(edge_pp, 2),
        }

    # Special: Under fade on high-K arms — reject Under X.5 if expected K
    # is >= line (means model actually expects Over)
    if side_low == "under" and lam >= line:
        return {
            "emit": False, "reason": "under_but_expected_over",
            "expected_k": lam, "line": line,
        }
    # And vice-versa for Over
    if side_low == "over" and lam <= line - 0.5:
        return {
            "emit": False, "reason": "over_but_expected_under",
            "expected_k": lam, "line": line,
        }

    return {
        "emit": True,
        "model_prob": round(model_prob, 3),
        "book_implied": round(book_implied, 3),
        "edge_pp": round(edge_pp, 2),
        "expected_k": lam,
        "components": exp["components"],
        "signals_used": exp["data_quality"],
        # PHASE 2 (2026-06) — expose simulator provenance downstream.
        "provenance":     exp.get("provenance"),
        "input_quality":  exp.get("input_quality"),
        "decision_valid": exp.get("decision_valid", True),
    }


__all__ = [
    "compute_expected_k",
    "evaluate_k_pick",
    "MAX_STRIKEOUT_ODDS",
    "EDGE_THRESHOLD_PP",
    "MIN_MODEL_WIN_PROB",
]
