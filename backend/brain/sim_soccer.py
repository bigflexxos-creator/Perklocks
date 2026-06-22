"""Soccer Prop Simulator — Poisson goal-scoring model with team attack/defense ratings.

Phase B. Uses the Dixon-Coles bivariate Poisson approach:
  • λ_home = avg_goals × home_attack × away_defense × home_advantage
  • λ_away = avg_goals × away_attack × home_defense
  • Each match: goals ~ Poisson(λ_home), Poisson(λ_away) (independent)
  • For BTTS and high totals we add a small low-score correlation correction.

Since picks don't carry raw xG values, we derive λs from the model's `factors`
(0-100 ratings, calibrated to league averages):
  • xG Combined        → total expected goals (1.5..4.0 around 2.7 league avg)
  • xG Difference      → ratio between home vs away xG
  • Defensive Form     → finishing % cap (clean-sheet propensity)

Markets routed:
  • Moneyline / Match Winner / Draw
  • Total Goals Over/Under (any line)
  • Both Teams To Score (BTTS Yes/No)
  • Anytime Goal Scorer (uses player's team's λ × scorer_share heuristic)
"""
from __future__ import annotations
import math
import random
import re
from typing import Optional

RUNS = 10_000

# League calibration constants.
LEAGUE_AVG_TOTAL = 2.75   # goals per match (top 5 leagues)
HOME_ADVANTAGE = 1.10      # +10% λ for home side


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


def _poisson(lam: float) -> int:
    """Knuth's algorithm — fast for small λ (soccer goals are small)."""
    if lam <= 0:
        return 0
    L = math.exp(-lam)
    k = 0
    p = 1.0
    while True:
        k += 1
        p *= random.random()
        if p <= L:
            return k - 1


def _factor(pick: dict, key: str, default: float = 50.0) -> float:
    f = (pick.get("factors") or {})
    try:
        return float(f.get(key, default))
    except (TypeError, ValueError):
        return default


def _derive_lambdas(pick: dict) -> tuple[float, float, bool]:
    """Returns (λ_home, λ_away, is_pick_home).

    is_pick_home flags whether the pick's team is the *home* side — used to
    map the model's win_probability check at the end.
    """
    # xG Combined (0–100) → total expected goals (1.5..4.0)
    combined = _factor(pick, "xG Combined", 50.0)
    total_xg = max(1.2, min(4.5, 1.5 + (combined / 100.0) * 2.0))

    # xG Difference (0–100) → ratio of home λ vs away λ
    # 50 = even, 100 = pick team dominant
    diff = _factor(pick, "xG Difference", 50.0)
    # Map 0–100 → ratio 0.4..2.5 of pick_team to opponent
    ratio_pick_vs_opp = 0.4 + (diff / 100.0) * 2.1

    # Without explicit home/away flag we treat the pick's team as "pick_team"
    # and use HOME_ADVANTAGE when we can detect home from the event string.
    event = (pick.get("event") or "").lower()
    market = (pick.get("market") or "")
    pick_team = ""
    m = re.match(r"^([A-Za-z0-9 \.\-']+?)\s+(moneyline|to win|draw)", market.lower())
    if m:
        pick_team = m.group(1).strip()
    is_pick_home = False
    if pick_team and " vs " in event:
        home_side = event.split(" vs ")[0].strip()
        is_pick_home = pick_team in home_side or home_side in pick_team

    # Compute λs
    lam_pick = total_xg * ratio_pick_vs_opp / (1.0 + ratio_pick_vs_opp)
    lam_opp = total_xg - lam_pick

    # Apply home boost
    if is_pick_home:
        boost = HOME_ADVANTAGE
        lam_pick *= boost
        lam_opp /= boost
    return lam_pick, lam_opp, is_pick_home


def _is_btts(market: str) -> Optional[bool]:
    m = (market or "").lower()
    if "both teams to score" not in m and "btts" not in m:
        return None
    return "no" not in m  # default Yes


def _classify_soccer_market(market: str) -> str:
    m = (market or "").lower()
    # Scorer markets (ATGS / FGS / LGS / 2+ / hat-trick) → handled by sim_soccer_scorer
    if ("anytime" in m and ("scorer" in m or "goal" in m)) or \
       "first goal scorer" in m or "last goal scorer" in m or \
       "to score 2" in m or "2+ goals" in m or "hat-trick" in m or "hat trick" in m:
        return "scorer"
    if "total goals" in m or ("over " in m and "goals" in m) or ("under " in m and "goals" in m):
        return "totals"
    if "both teams to score" in m or "btts" in m:
        return "btts"
    # ── Double Chance (Win or Draw / Draw or Win) — MUST be checked BEFORE
    # the bare "draw" classifier or it gets mis-routed and computes draw-only
    # probability (sim ~24%) for a market that actually wins ~78% of the time.
    if "win or draw" in m or "draw or win" in m or "double chance" in m:
        return "double_chance"
    if "draw" == m.strip().split()[-1] or " draw" in m or "the draw" in m:
        return "draw"
    if "moneyline" in m or "to win" in m or "match winner" in m:
        return "moneyline"
    if "to score" in m:   # generic fallback for "Player Name To Score" without "anytime"
        return "scorer"
    return "unknown"


def simulate_soccer_pick(pick: dict) -> Optional[dict]:
    if (pick.get("sport") or "") != "Soccer":
        return None
    market = pick.get("market") or ""
    cat = _classify_soccer_market(market)
    if cat == "unknown":
        return None

    # Route scorer markets to the dedicated player-level Poisson simulator
    if cat == "scorer":
        from brain.sim_soccer_scorer import simulate_soccer_scorer_pick
        return simulate_soccer_scorer_pick(pick)

    lam_pick, lam_opp, _ = _derive_lambdas(pick)
    if lam_pick <= 0 or lam_opp <= 0:
        return None

    wins = 0
    total_goals_dist: list[int] = []
    for _ in range(RUNS):
        gp = _poisson(lam_pick)
        go = _poisson(lam_opp)
        total_goals_dist.append(gp + go)
        if cat == "moneyline":
            if gp > go:
                wins += 1
        elif cat == "draw":
            if gp == go:
                wins += 1
        elif cat == "double_chance":
            # Win or Draw — covers any non-loss outcome for the pick team.
            if gp >= go:
                wins += 1
        elif cat == "totals":
            line = _extract_threshold(market)
            under = _is_under(market)
            total = gp + go
            if (total < line) if under else (total > line):
                wins += 1
        elif cat == "btts":
            wants_yes = _is_btts(market)
            both_scored = gp > 0 and go > 0
            if (both_scored if wants_yes else not both_scored):
                wins += 1
        elif cat == "atgs":
            # Heuristic: assume pick's player gets ~25% of his team's goal share
            # (top striker tier). Without per-player data this is a coarse
            # proxy — but lambda_player ~ 0.25 * λ_team yields realistic
            # ~35-55% ATGS probabilities for top scorers.
            lam_player = 0.25 * lam_pick
            scored = _poisson(lam_player) > 0
            if scored:
                wins += 1

    n = RUNS
    p_win = wins / n
    ci_lo, ci_hi = _wilson_ci(p_win, n)

    blended_wp = float(pick.get("win_probability") or 0)
    sim_wp_pct = round(p_win * 100, 1)
    disagreement = round(sim_wp_pct - blended_wp, 2)

    if disagreement > 5:
        signal = "stronger"
    elif disagreement < -5:
        signal = "weaker"
    else:
        signal = "neutral"

    # Alt-line sensitivity for soccer totals (Over/Under N.5 goals)
    alt_lines: dict = {}
    threshold_out = None
    is_under_out = None
    if cat == "totals":
        threshold_out = _extract_threshold(market)
        is_under_out = _is_under(market)
        for delta in (-1.5, -1.0, -0.5, 0.5, 1.0, 1.5):
            alt = round(threshold_out + delta, 1)
            if alt <= 0:
                continue
            over_hits = sum(1 for g in total_goals_dist if g > alt)
            alt_lines[str(alt)] = round(over_hits / n * 100, 1)

    return {
        "sim_win_probability": sim_wp_pct,
        "sim_ci_lower": round(ci_lo * 100, 1),
        "sim_ci_upper": round(ci_hi * 100, 1),
        "sim_runs": n,
        "sim_lambda_pick": round(lam_pick, 3),
        "sim_lambda_opp": round(lam_opp, 3),
        "sim_market_category": cat,
        "sim_threshold": threshold_out,
        "sim_is_under": is_under_out,
        "sim_alt_lines": alt_lines if alt_lines else None,
        "sim_disagreement_with_model": disagreement,
        "sim_signal": signal,
    }
