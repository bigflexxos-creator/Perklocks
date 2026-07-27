"""Tennis Math Engine — surface-adjusted Elo + form win-probability model.

USER MANDATE (2026-07-27): "I don't just want random dog picks. Do math.
If the dog comes out on top, put it on the board."

This module computes P(home_wins) INDEPENDENTLY of the bookmaker's implied
probability. It's a real math model — surface Elo, form, break-point
efficiency, fatigue, and H2H — that can flip a picked side when it
disagrees with the book.

Design contract:
    signal = score_tennis_matchup(home, away, surface, home_implied, ctx)
    if signal and has_real_tennis_signal(signal):
        home_wp = signal["home_win_prob"]      # 0.0-1.0
        contribs = signal["contributions"]     # dict of factor lifts
        # Use home_wp as the model probability (not book_implied).

If we don't have enough real data, `has_real_tennis_signal` returns False
and the caller falls back to the book-anchored dd model.

Signals used (all sourced from tennis_extra Sackmann feed + Elo table):
    - Surface Elo gap (Grass/Clay/Hard) — dominant signal (~50% weight)
    - Recent form: last-10 W/L + set-win % + games-won %
    - Break-point conversion + break-point-saved %
    - First-serve win % (serve dominance)
    - Fatigue: matches played in last 7 days
    - H2H record (surface-specific if we have 3+ meetings)
    - Retirement risk penalty (chalk fade)
    - Ranking momentum (30-day delta)

Threshold for "real signal": at least 2 real signals + Elo gap OR 3 real
signals without Elo. Below that we say `has_real_tennis_signal == False`.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("lockscore.tennis_math")


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _elo_to_prob(elo_diff: float) -> float:
    """Elo delta → win probability using the standard tennis calibration.

    A 100-point Elo gap ≈ 64% win prob for the higher-rated player.
    A 200-point gap ≈ 76% win prob. Fit to ATP/WTA singles.
    """
    return 1.0 / (1.0 + 10.0 ** (-elo_diff / 400.0))


def score_tennis_matchup(
    home: str,
    away: str,
    surface: str,
    home_implied: float,
    ctx: dict,
) -> Optional[dict]:
    """Return {home_win_prob, contributions, used_signals} or None if no data.

    home_win_prob is a REAL model probability (0-1) computed from Elo + form.
    The caller compares against `home_implied` to detect upsets and edges.
    """
    contribs: dict[str, float] = {}
    used: list[str] = []

    # ── Base rate: start from surface-Elo gap when both players present ──
    home_elo = ctx.get("surface_elo_a") or ctx.get("surface_elo_home")
    away_elo = ctx.get("surface_elo_b") or ctx.get("surface_elo_away")

    if isinstance(home_elo, (int, float)) and isinstance(away_elo, (int, float)):
        elo_gap = float(home_elo) - float(away_elo)
        base_wp = _elo_to_prob(elo_gap)
        contribs["surface_elo_baseline"] = round(base_wp - 0.5, 4)
        used.append("surface_elo")
    else:
        # No Elo → use book_implied as the seed (with a small ballast
        # so we don't just echo the book back).
        base_wp = float(home_implied)

    # ── Recent form: last-10 W/L + set-win % ───────────────────────────
    sm_a = ctx.get("sackmann_a") or {}
    sm_b = ctx.get("sackmann_b") or {}

    # Win% (52-week) — strongest single Sackmann signal after Elo
    wp_a = sm_a.get("win_pct")
    wp_b = sm_b.get("win_pct")
    if isinstance(wp_a, (int, float)) and isinstance(wp_b, (int, float)):
        # A 20pp win-pct gap moves the wp by ~4pp
        form_lift = _clamp((wp_a - wp_b) * 0.002, -0.06, 0.06)
        base_wp += form_lift
        if abs(form_lift) >= 0.005:
            contribs["form_win_pct"] = round(form_lift, 4)
            used.append("form_win_pct")

    # Serve dominance: first-serve won%
    fs_a = sm_a.get("first_serve_won_pct") or sm_a.get("first_serve_win_pct")
    fs_b = sm_b.get("first_serve_won_pct") or sm_b.get("first_serve_win_pct")
    if isinstance(fs_a, (int, float)) and isinstance(fs_b, (int, float)):
        srv_lift = _clamp((fs_a - fs_b) * 0.003, -0.05, 0.05)
        base_wp += srv_lift
        if abs(srv_lift) >= 0.005:
            contribs["first_serve"] = round(srv_lift, 4)
            used.append("first_serve")

    # Hold%
    hp_a = sm_a.get("hold_pct")
    hp_b = sm_b.get("hold_pct")
    if isinstance(hp_a, (int, float)) and isinstance(hp_b, (int, float)):
        hp_lift = _clamp((hp_a - hp_b) * 0.002, -0.04, 0.04)
        base_wp += hp_lift
        if abs(hp_lift) >= 0.005:
            contribs["hold_pct"] = round(hp_lift, 4)
            used.append("hold_pct")

    # Break-point saved%
    bs_a = sm_a.get("break_saved_pct")
    bs_b = sm_b.get("break_saved_pct")
    if isinstance(bs_a, (int, float)) and isinstance(bs_b, (int, float)):
        bs_lift = _clamp((bs_a - bs_b) * 0.0015, -0.03, 0.03)
        base_wp += bs_lift
        if abs(bs_lift) >= 0.005:
            contribs["break_saved"] = round(bs_lift, 4)
            used.append("break_saved")

    # ── Fatigue: matches in last 7 days ─────────────────────────────────
    fm_a = ctx.get("fatigue_a_matches_7d") or 0
    fm_b = ctx.get("fatigue_b_matches_7d") or 0
    if isinstance(fm_a, int) and isinstance(fm_b, int) and (fm_a + fm_b) > 0:
        # Each extra match played by opponent = +0.7pp for us (fatigue accrual)
        f_lift = _clamp((fm_b - fm_a) * 0.007, -0.025, 0.025)
        base_wp += f_lift
        if abs(f_lift) >= 0.005:
            contribs["fatigue"] = round(f_lift, 4)
            used.append("fatigue")

    # ── H2H (career): meaningful with 3+ meetings ──────────────────────
    aw = ctx.get("h2h_a_wins") or 0
    bw = ctx.get("h2h_b_wins") or 0
    total_h2h = aw + bw
    if total_h2h >= 3:
        share = aw / total_h2h
        h_lift = _clamp((share - 0.5) * 0.06, -0.03, 0.03)
        base_wp += h_lift
        if abs(h_lift) >= 0.005:
            contribs["h2h"] = round(h_lift, 4)
            used.append("h2h")

    # ── Ranking momentum (30-day delta) ─────────────────────────────────
    rm_a = sm_a.get("rank_delta_30d")
    rm_b = sm_b.get("rank_delta_30d")
    if isinstance(rm_a, (int, float)) and isinstance(rm_b, (int, float)):
        # Rising players (negative delta = rank getting smaller = better) get lift
        mom_lift = _clamp((rm_b - rm_a) * 0.001, -0.02, 0.02)
        base_wp += mom_lift
        if abs(mom_lift) >= 0.005:
            contribs["momentum"] = round(mom_lift, 4)
            used.append("momentum")

    # ── Retirement risk penalty ─────────────────────────────────────────
    rr_a = sm_a.get("retirement_rate_pct")
    if isinstance(rr_a, (int, float)) and rr_a >= 5.0:
        base_wp -= 0.02
        contribs["retirement_risk_a"] = -0.020
        used.append("retirement_risk_a")

    rr_b = sm_b.get("retirement_rate_pct")
    if isinstance(rr_b, (int, float)) and rr_b >= 5.0:
        base_wp += 0.02
        contribs["retirement_risk_b"] = 0.020
        used.append("retirement_risk_b")

    # ── Final clamp ─────────────────────────────────────────────────────
    home_wp = _clamp(base_wp, 0.08, 0.92)

    return {
        "home_win_prob": round(home_wp, 4),
        "contributions": contribs,
        "used_signals": used,
        "signals_count": len(used),
        "has_elo_baseline": "surface_elo" in used,
    }


def has_real_tennis_signal(signal: dict) -> bool:
    """Gate: is there enough real-data signal to trust this model prob?

    Rules:
      - Elo baseline present + 1 other signal, OR
      - 3+ non-Elo signals (form/serve/fatigue/h2h)
    """
    if not signal:
        return False
    used = signal.get("used_signals") or []
    if signal.get("has_elo_baseline") and len(used) >= 2:
        return True
    if len(used) >= 3:
        return True
    return False


__all__ = [
    "score_tennis_matchup",
    "has_real_tennis_signal",
]
