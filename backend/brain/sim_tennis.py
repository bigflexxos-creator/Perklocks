"""Tennis Prop Simulator — per-point Markov chain anchored to model match WP.

Phase B. Tennis picks carry one-sided rating factors (Hold % / Break %) but no
explicit matchup-aware opponent serve quality, so a free-floating Markov sim
can wildly disagree with the model. We instead CALIBRATE:
  • Use model_wp as the target match-win probability
  • Find a serve-point-quality gap (∆p) such that a best-of-3 Markov chain
    produces P(pick wins match) ≈ model_wp
  • Re-simulate to produce a CI and derive secondary outputs (total games,
    set totals, expected score)

This makes the sim a consistency check + uncertainty quantifier rather than
fighting the matchup-aware model.

Markets routed:
  • Moneyline / Match Winner
  • Total Games Over/Under
"""
from __future__ import annotations
import math
import random
import re
from typing import Optional

RUNS = 20_000
                # sim is heavier so runtime ≈ 5x but still finishes in the pipeline window
SETS_BO3 = 3

LEAGUE_AVG_SERVE_PT_PCT = 0.63


def _wilson_ci(p: float, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt((p * (1 - p) / n) + (z * z / (4 * n * n))) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def _extract_threshold(market: str) -> float:
    m = re.search(r"(?:over|under)\s+(\d+(?:\.\d+)?)", (market or "").lower())
    return float(m.group(1)) if m else 0.5


def _is_under(market: str) -> bool:
    return "under " in (market or "").lower()


def _classify_tennis_market(market: str) -> str:
    m = (market or "").lower()
    if "moneyline" in m or "match winner" in m:
        return "moneyline"
    # Game spread: "Hugo Gaston -3.5 Games (Alt)" or "Player -2.5 Spread"
    # MUST be checked BEFORE the totals classifier since "games" appears in both.
    if re.search(r"[-+]\d+(?:\.\d+)?\s*(?:games?|spread)", m):
        return "game_spread"
    if "total games" in m or ("over " in m and "game" in m) or ("under " in m and "game" in m):
        return "totals"
    return "unknown"


_SPREAD_RE = re.compile(r"([-+]\d+(?:\.\d+)?)\s*(?:games?|spread)", re.I)


def _extract_spread(market: str) -> float:
    """Extract the spread value (negative = favorite, positive = underdog)."""
    m = _SPREAD_RE.search(market or "")
    if not m:
        return 0.0
    try:
        return float(m.group(1))
    except ValueError:
        return 0.0


def _extract_spread_player(market: str) -> str:
    """Extract the player name preceding the spread number."""
    m = re.match(r"^(.+?)\s+[-+]\d", (market or ""))
    return m.group(1).strip() if m else ""


def _simulate_game(server_pt_win: float) -> int:
    s, r = 0, 0
    while True:
        if random.random() < server_pt_win:
            s += 1
        else:
            r += 1
        if s >= 4 and s - r >= 2:
            return 1
        if r >= 4 and r - s >= 2:
            return 0


def _simulate_tiebreak(p_serve: float, o_serve: float) -> int:
    """Return 1 if pick wins tiebreak. Server rotates P, OO, PP, OO, ..."""
    pp, op = 0, 0
    pt = 0
    server_is_pick = True
    while True:
        prob = p_serve if server_is_pick else o_serve
        if random.random() < prob:
            if server_is_pick:
                pp += 1
            else:
                op += 1
        else:
            if server_is_pick:
                op += 1
            else:
                pp += 1
        pt += 1
        if pp >= 7 and pp - op >= 2:
            return 1
        if op >= 7 and op - pp >= 2:
            return 0
        # Rotation: 1, 2, 2, 2, 2, ...
        if pt == 1 or pt % 2 == 1:
            server_is_pick = not server_is_pick


def _simulate_set(p_serve: float, o_serve: float, pick_serves_first: bool) -> tuple[int, int, int]:
    """Returns (pick_games, opp_games, who_won {1,0}). Also tells which player
    serves first in the NEXT set (the one who didn't serve the last game)."""
    pg, og = 0, 0
    pick_serves = pick_serves_first
    while True:
        if pick_serves:
            if _simulate_game(p_serve):
                pg += 1
            else:
                og += 1
        else:
            if _simulate_game(o_serve):
                og += 1
            else:
                pg += 1
        pick_serves = not pick_serves
        if pg >= 6 and pg - og >= 2:
            return (pg, og, 1)
        if og >= 6 and og - pg >= 2:
            return (pg, og, 0)
        if pg == 6 and og == 6:
            won = _simulate_tiebreak(p_serve, o_serve)
            if won:
                return (7, 6, 1)
            return (6, 7, 0)


def _simulate_match(p_serve: float, o_serve: float, bo: int = SETS_BO3) -> tuple[int, int, int]:
    """Backwards-compatible wrapper that returns (total_games, p_sets, o_sets)."""
    total_games, p_sets, o_sets, _, _ = _simulate_match_full(p_serve, o_serve, bo)
    return total_games, p_sets, o_sets


def _simulate_match_full(p_serve: float, o_serve: float, bo: int = SETS_BO3) -> tuple[int, int, int, int, int]:
    """Full simulation. Returns (total_games, p_sets, o_sets, p_games, o_games).
    p_games / o_games are the cumulative game counts per player (needed for
    game-spread markets like "Hugo Gaston -3.5 Games")."""
    pick_sets = opp_sets = 0
    total_games = 0
    p_games_total = 0
    o_games_total = 0
    pick_serves_first = True
    sets_to_win = (bo // 2) + 1
    while pick_sets < sets_to_win and opp_sets < sets_to_win:
        pg, og, pw = _simulate_set(p_serve, o_serve, pick_serves_first)
        total_games += pg + og
        p_games_total += pg
        o_games_total += og
        if pw == 1:
            pick_sets += 1
        else:
            opp_sets += 1
        pick_serves_first = not pick_serves_first
    return total_games, pick_sets, opp_sets, p_games_total, o_games_total


def _calibrate_serve_gap_for_spread(spread_line: float, target_cover_pct: float) -> tuple[float, float]:
    """Bisect on serve gap so that P(margin > -spread_line) ≈ target_cover_pct.

    For spread picks, the model's WP represents probability of COVERING THE
    SPREAD (not winning the match). Calibrating against match_win_prob would
    massively overstate the favorite's strength. This routine instead finds
    the serve gap such that the SPREAD COVER rate matches the model.
    """
    if target_cover_pct >= 0.99:
        return 0.78, 0.48
    if target_cover_pct <= 0.01:
        return 0.48, 0.78
    lo, hi = -0.25, 0.25
    for _ in range(14):
        mid = (lo + hi) / 2
        p_serve = LEAGUE_AVG_SERVE_PT_PCT + mid
        o_serve = LEAGUE_AVG_SERVE_PT_PCT - mid
        covers = 0
        for _ in range(400):
            _, ps, os_, pg, og = _simulate_match_full(p_serve, o_serve)
            margin = pg - og
            if margin > -spread_line:
                covers += 1
        cover_pct = covers / 400.0
        if abs(cover_pct - target_cover_pct) < 0.02:
            return p_serve, o_serve
        # If covering happens too rarely, pick is stronger than we assumed
        if cover_pct < target_cover_pct:
            lo = mid
        else:
            hi = mid
    final_gap = (lo + hi) / 2
    return LEAGUE_AVG_SERVE_PT_PCT + final_gap, LEAGUE_AVG_SERVE_PT_PCT - final_gap


def _calibrate_serve_gap(target_match_wp: float) -> tuple[float, float]:
    """Bisect on serve quality gap. Both players serve around 63% but we
    adjust until P(pick wins match) ≈ target. Returns (p_serve, o_serve).

    We hold p_serve + o_serve = 2 × LEAGUE_AVG_SERVE_PT_PCT constant so total
    points per game is realistic; only the gap changes."""
    if target_match_wp >= 0.99:
        return 0.78, 0.48
    if target_match_wp <= 0.01:
        return 0.48, 0.78
    lo, hi = -0.25, 0.25   # gap in serve %
    for _ in range(14):
        mid = (lo + hi) / 2
        p_serve = LEAGUE_AVG_SERVE_PT_PCT + mid
        o_serve = LEAGUE_AVG_SERVE_PT_PCT - mid
        # Quick estimate using 400 trials (will be re-simulated for final CI)
        wins = 0
        for _ in range(400):
            _, ps, os_ = _simulate_match(p_serve, o_serve)
            if ps > os_:
                wins += 1
        wp = wins / 400.0
        if abs(wp - target_match_wp) < 0.02:
            return p_serve, o_serve
        if wp < target_match_wp:
            lo = mid
        else:
            hi = mid
    final_gap = (lo + hi) / 2
    return LEAGUE_AVG_SERVE_PT_PCT + final_gap, LEAGUE_AVG_SERVE_PT_PCT - final_gap


def _signal(disagreement: float) -> str:
    if disagreement > 5:
        return "stronger"
    if disagreement < -5:
        return "weaker"
    return "neutral"


def simulate_tennis_pick(pick: dict, tennis_ctx: dict | None = None) -> Optional[dict]:
    """PHASE 2 (2026-06) UPGRADE — when ``tennis_ctx`` carries real
    surface Elo / hold_pct / form / H2H signals compatible with
    :func:`services.tennis_math_engine.score_tennis_matchup`, this sim
    derives serve percentages from an EMPIRICAL_INDEPENDENT baseline
    (surface Elo + hold/break) instead of back-solving from model_wp.

    Fallback (no ctx) preserves the existing model-conditioned
    calibration for backwards compatibility.
    """
    if (pick.get("sport") or "") != "Tennis":
        return None
    market = pick.get("market") or ""
    cat = _classify_tennis_market(market)
    if cat == "unknown":
        return None

    model_wp = float(pick.get("win_probability") or 0) / 100.0
    if model_wp <= 0 or model_wp >= 1:
        model_wp = max(0.05, min(0.95, model_wp))

    # For game-spread markets we need to know who the spread is on relative
    # to the pick's main player so we can interpret the games margin correctly.
    spread_line = _extract_spread(market) if cat == "game_spread" else 0.0

    # PHASE 2 (2026-06) — INDEPENDENT SERVE-GAP path.  When
    # ``tennis_ctx`` carries surface Elo + hold_pct signals, derive
    # p_serve / o_serve from those inputs (independent of the model's
    # win probability).  Otherwise fall back to WP-calibration.
    independent_signals = 0
    serve_derivation = "model_calibrated"
    if tennis_ctx and cat != "game_spread":
        try:
            # Elo baseline gives the win_prob directly; convert to a
            # serve-gap by inverting our _calibrate helper against the
            # ELO-derived WP so the sim samples from the INDEPENDENT
            # probability.
            from services.tennis_math_engine import score_tennis_matchup
            home = tennis_ctx.get("home") or pick.get("home_team") or ""
            away = tennis_ctx.get("away") or pick.get("away_team") or ""
            surface = tennis_ctx.get("surface") or "hard"
            sig = score_tennis_matchup(home, away, surface, 0.5, tennis_ctx)
            elo_wp = sig.get("home_win_prob")
            used = sig.get("used_signals") or []
            has_elo = sig.get("has_elo_baseline", False)
            if elo_wp is not None and (has_elo or len(used) >= 2):
                # elo_wp is home's WP; determine whether pick side is home.
                pick_team = (pick.get("selection") or "").strip().lower()
                is_pick_home = bool(home) and (
                    pick_team == home.strip().lower() or
                    pick_team in home.strip().lower()
                )
                pick_wp = float(elo_wp) if is_pick_home else 1.0 - float(elo_wp)
                pick_wp = max(0.05, min(0.95, pick_wp))
                p_serve, o_serve = _calibrate_serve_gap(pick_wp)
                independent_signals = len(used)
                serve_derivation = "elo_hold_break"
        except Exception:
            pass

    if serve_derivation == "model_calibrated":
        if cat == "game_spread":
            p_serve, o_serve = _calibrate_serve_gap_for_spread(spread_line, model_wp)
        else:
            p_serve, o_serve = _calibrate_serve_gap(model_wp)

    threshold = _extract_threshold(market)
    is_under = _is_under(market)

    # PERKLOCKS MAIN 36 · P0-9 — Tennis BO3/BO5 authority.
    # Previously Brain silently used SETS_BO3 for every match, so an
    # ATP men's Grand Slam (BO5) was simulated as BO3 → wrong total
    # games distribution & wrong match-winner p.  Thread the resolved
    # format from the authoritative helper.
    try:
        from services.tennis_match_format import resolve_tennis_match_format
        _bo = int(resolve_tennis_match_format(
            sport_key=pick.get("sport_key") or pick.get("league") or "",
            league=pick.get("league") or pick.get("event") or "",
            event_payload=pick,
            tournament_name=pick.get("tournament") or pick.get("event") or "",
        ) or SETS_BO3)
        if _bo not in (SETS_BO3, 5):
            _bo = SETS_BO3
    except Exception:
        _bo = SETS_BO3

    wins = 0
    total_games_dist: list[int] = []
    pick_match_wins = 0
    games_margin_dist: list[int] = []   # pick_games - opp_games (for spread analytics)
    for _ in range(RUNS):
        # Simulate match returning total games AND game count per side
        total_games, p_sets, o_sets, p_games, o_games = _simulate_match_full(
            p_serve, o_serve, bo=_bo,
        )
        total_games_dist.append(total_games)
        games_margin_dist.append(p_games - o_games)
        pick_won = p_sets > o_sets
        if pick_won:
            pick_match_wins += 1
        if cat == "moneyline":
            if pick_won:
                wins += 1
        elif cat == "totals":
            if (total_games < threshold) if is_under else (total_games > threshold):
                wins += 1
        elif cat == "game_spread":
            # Pick covers if (pick_games - opp_games) > -spread_line.
            # Examples:
            #   spread -3.5 → pick must win by 4+ → margin > 3.5
            #   spread +3.5 → pick can lose by ≤3 → margin > -3.5
            margin = p_games - o_games
            if margin > -spread_line:
                wins += 1

    n = RUNS
    p_win = wins / n
    ci_lo, ci_hi = _wilson_ci(p_win, n)
    sim_wp_pct = round(p_win * 100, 1)
    disagreement = round(sim_wp_pct - model_wp * 100, 2)
    avg_games = sum(total_games_dist) / max(1, len(total_games_dist))

    # Alt-line sensitivity for totals + game-spread markets
    alt_lines: dict = {}
    if cat == "totals":
        for delta in (-4.5, -2.5, -0.5, 0.5, 2.5, 4.5):
            alt = round(threshold + delta, 1)
            if alt <= 0:
                continue
            over_hits = sum(1 for g in total_games_dist if g > alt)
            alt_lines[str(alt)] = round(over_hits / n * 100, 1)
    elif cat == "game_spread":
        # Show sensitivity at adjacent spread lines (±1, ±2 games)
        for delta in (-2.0, -1.0, 0.0, 1.0, 2.0):
            alt = round(spread_line + delta, 1)
            covers = sum(1 for margin in games_margin_dist if margin > -alt)
            sign = "+" if alt > 0 else ""
            alt_lines[f"{sign}{alt}"] = round(covers / n * 100, 1)

    payload = {
        "sim_win_probability": sim_wp_pct,
        "sim_ci_lower": round(ci_lo * 100, 1),
        "sim_ci_upper": round(ci_hi * 100, 1),
        "sim_runs": n,
        "sim_pick_serve_pct": round(p_serve * 100, 1),
        "sim_opp_serve_pct": round(o_serve * 100, 1),
        "sim_avg_total_games": round(avg_games, 1),
        "sim_pick_match_win_pct": round(pick_match_wins / n * 100, 1),
        "sim_market_category": cat,
        "sim_threshold": threshold if cat == "totals" else None,
        "sim_is_under": is_under if cat == "totals" else None,
        "sim_alt_lines": alt_lines if alt_lines else None,
        "sim_disagreement_with_model": disagreement,
        "sim_signal": _signal(disagreement),
        "sim_match_format": _bo,   # PERKLOCKS MAIN 36 · P0-9
    }
    if cat == "game_spread":
        payload["sim_spread_line"] = spread_line
        payload["sim_avg_games_margin"] = round(sum(games_margin_dist) / max(1, n), 2)

    # PHASE 2 (2026-06) — Universal Simulator Provenance Envelope.
    # * ``serve_derivation == "elo_hold_break"`` — serve gap came from
    #   real surface Elo + hold/break signals (INDEPENDENT of model_wp).
    #   Provenance → EMPIRICAL_INDEPENDENT with quality by signal count.
    # * ``serve_derivation == "model_calibrated"`` — legacy path,
    #   back-solves from model_wp → MODEL_CONDITIONED (kept for
    #   callers without a Tennis ctx).
    try:
        from services.simulator_provenance import (
            stamp_sim_output, classify_input_quality,
        )
        if serve_derivation == "elo_hold_break" and independent_signals >= 2:
            provenance = "EMPIRICAL_INDEPENDENT"
            quality = classify_input_quality(independent_signals)
        else:
            provenance = "MODEL_CONDITIONED"
            quality = "PARTIAL"
        stamp_sim_output(
            payload, provenance=provenance, input_quality=quality,
            sim_prob=(sim_wp_pct / 100.0), model_prob=float(model_wp),
        )
        payload["sim_serve_derivation"] = serve_derivation
    except Exception:
        pass
    return payload
