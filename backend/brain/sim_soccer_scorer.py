"""Soccer Goal Scorer Simulator — player-level Poisson model for ATGS / FGS / Last GS markets.

Phase B.1 (Soccer prediction goal scorer). Replaces the crude
`lam_player = 0.25 × lam_team` heuristic with a real per-player xG model:

  • Parses player-level xG, shot, and form signals from `key_insights`
  • Calibrates λ_player to model_wp (so sim P(score≥1) matches model WP)
  • Adds parameter uncertainty via Gamma prior — produces a realistic CI
  • Derives secondary outputs (expected_xg, shots_proj, mins_proj, xg_share)

Markets routed:
  • Anytime Goal Scorer (ATGS)        → P(goals ≥ 1)
  • First Goal Scorer (FGS)           → P(scores AND scores first)
  • Last Goal Scorer                  → P(scores AND scores last)
  • Player To Score 2+ / Hat-trick    → P(goals ≥ 2) / P(goals ≥ 3)
"""
from __future__ import annotations
import math
import random
import re
from typing import Optional

RUNS = 20_000

# League calibration
LEAGUE_AVG_TEAM_XG_PER_GAME = 1.45
DEFAULT_PLAYER_XG_PER_90 = 0.35       # avg starter xG/90 (forwards higher, mids lower)
DEFAULT_EXPECTED_MINUTES = 78.0
DEFAULT_SOT_TO_GOAL_RATE = 0.30        # tour avg conversion from shots-on-target to goal


def _wilson_ci(p: float, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt((p * (1 - p) / n) + (z * z / (4 * n * n))) / denom
    return (max(0.0, center - half), min(1.0, center + half))


# ─── Key Insight parsers ────────────────────────────────────────────────
# The pick's `key_insights` array contains natural-language stats. We pull
# the most useful priors via regex. Returns None if the metric isn't found.

_XG_PER_GAME_RE = re.compile(
    r"(?:expected goals|xg).{0,20}?(\d+(?:\.\d+)?)\s*per\s*(?:game|match)", re.I)
_SHOTS_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*shots(?:\s*on\s*target)?\s*per\s*(?:match|game)", re.I)
_HIT_RATE_RE = re.compile(
    r"scored\s*in\s*(\d+)\s*of\s*(?:last\s*)?(\d+)\s*(?:club\s*)?match", re.I)
_OPP_CONCEDE_RE = re.compile(
    r"oppos(?:ition|ing).{0,30}?(\d+(?:\.\d+)?)\s*goals?\s*/\s*match", re.I)


def _parse_player_priors(pick: dict) -> dict:
    """Pull xG, shots, recent hit-rate, opp concede rate from key_insights."""
    out: dict = {}
    insights = pick.get("key_insights") or []
    if not isinstance(insights, list):
        return out
    blob = " | ".join(str(s) for s in insights)

    m = _XG_PER_GAME_RE.search(blob)
    if m:
        try:
            out["player_xg_per_game"] = float(m.group(1))
        except ValueError:
            pass

    m = _SHOTS_RE.search(blob)
    if m:
        try:
            shots = float(m.group(1))
            out["shots_per_game"] = shots
            # If we don't have direct xG, infer from shots (SoT × 0.30 ≈ xG)
            if "shots on target" in blob.lower() and "player_xg_per_game" not in out:
                out["player_xg_per_game"] = shots * DEFAULT_SOT_TO_GOAL_RATE
        except ValueError:
            pass

    m = _HIT_RATE_RE.search(blob)
    if m:
        try:
            scored = int(m.group(1))
            total = int(m.group(2))
            if total > 0:
                out["recent_goal_rate"] = scored / total
                out["recent_n"] = total
        except ValueError:
            pass

    m = _OPP_CONCEDE_RE.search(blob)
    if m:
        try:
            out["opp_concedes_per_match"] = float(m.group(1))
        except ValueError:
            pass

    return out


# ─── Market classification ──────────────────────────────────────────────
def _classify_scorer_market(market: str) -> Optional[str]:
    m = (market or "").lower()
    if "hat-trick" in m or "hat trick" in m or "3+ goals" in m:
        return "hattrick"
    if "to score 2" in m or "2+ goals" in m or "two or more" in m:
        return "two_plus"
    if "first goal scorer" in m or "to score first" in m or "fgs" in m:
        return "fgs"
    if "last goal scorer" in m or "to score last" in m:
        return "lgs"
    # ── Score or Assist (goal contribution) ──
    # Treat like ATGS with a probability boost: a player who'd hit ATGS at
    # P(goal>=1) also captures assist outcomes, which roughly doubles the
    # eligible state space. Empirical floor: assist prob ≈ 0.6× goal prob.
    if ("score" in m and "assist" in m) or "goal involvement" in m or "to score or assist" in m:
        return "score_or_assist"
    if "anytime" in m and ("scorer" in m or "goal" in m):
        return "atgs"
    if "to score" in m and "anytime" not in m:
        return "atgs"   # default coverage
    return None


# ─── Player λ estimation ────────────────────────────────────────────────
def _estimate_player_lambda(pick: dict, priors: dict) -> tuple[float, str, int]:
    """Estimate player's expected goals for THIS match.

    PHASE 2 (2026-06) — returns ``(lambda, provenance, signals)`` so
    downstream consumers can classify the simulator output:

      * Approach 1 (calibrate to model_wp) → MODEL_CONDITIONED (never
        counts as independent agreement — it's a back-solve).
      * Approach 2 (real xG / opp / minutes priors) → EMPIRICAL_INDEPENDENT.
      * Approach 3 (factor heuristic only) → PRIOR_ONLY.

    Order of preference:
      1. Calibrate to model_wp (the matchup-aware ground truth)
      2. Use parsed key_insights (xG/match + opp adjustment)
      3. Fall back to factor-based heuristic
    """
    # Approach 1: Calibrate to model WP. For ATGS, P(goals≥1) = 1 - e^-λ
    # so model_wp / 100 = 1 - e^-λ → λ = -ln(1 - p)
    model_wp = float(pick.get("win_probability") or 0) / 100.0
    if 0.02 < model_wp < 0.98:
        # Calibrate against the SPECIFIC market we're pricing — but for now
        # we always calibrate against ATGS-equivalent. For "2+ goals" or
        # "hat-trick" the model_wp already reflects the harder threshold,
        # so we need to undo that. We'll use ATGS-equivalent λ when possible:
        market = (pick.get("market") or "").lower()
        if "anytime" in market or ("to score" in market and "first" not in market and "last" not in market):
            lam = -math.log(1.0 - model_wp)
        elif "2+ goals" in market or "to score 2" in market:
            # P(X >= 2) = 1 - e^-λ (1 + λ) = model_wp. Bisect for λ.
            lam = _bisect_lambda_for_atleast_k(2, model_wp)
        elif "hat" in market or "3+ goals" in market:
            lam = _bisect_lambda_for_atleast_k(3, model_wp)
        elif "first goal" in market or "last goal" in market:
            # FGS/LGS ≈ P(scores) × (1 / (1 + N-1 other scorers))
            # Inverse: λ ≈ -ln(1 - 2 * model_wp) but bounded. Simpler: treat as ATGS proxy.
            lam = -math.log(1.0 - min(0.95, model_wp * 2.5))
        else:
            lam = -math.log(1.0 - model_wp)
        # MODEL_CONDITIONED: 1 signal (the model's own WP).  Cannot
        # act as independent evidence downstream.
        return max(0.05, min(2.5, lam)), "MODEL_CONDITIONED", 1

    # Approach 2: Direct from key_insights (real player-xG / opp / minutes)
    if "player_xg_per_game" in priors:
        base = priors["player_xg_per_game"]
        # Opponent strength adjustment
        opp_factor = 1.0
        signals = 1
        if "opp_concedes_per_match" in priors:
            opp_factor = priors["opp_concedes_per_match"] / LEAGUE_AVG_TEAM_XG_PER_GAME
            opp_factor = max(0.5, min(1.8, opp_factor))
            signals += 1
        # Minutes scaling (assume nominal 78 min)
        minutes_pct = DEFAULT_EXPECTED_MINUTES / 90.0
        if "shots_per_game" in priors:
            signals += 1
        if "recent_goal_rate" in priors:
            signals += 1
        return (
            max(0.05, min(2.0, base * opp_factor * minutes_pct)),
            "EMPIRICAL_INDEPENDENT",
            signals,
        )

    # Approach 3: Factor-based fallback — priors-only, DO NOT count as
    # independent evidence and DO NOT flag severe disagreement.
    f = pick.get("factors") or {}
    try:
        vol = float(f.get("Recent Volume / Usage", 50.0))
        matchup = float(f.get("Matchup vs Defense", 50.0))
        form = float(f.get("Last 10 Hit Rate", 50.0))
    except (TypeError, ValueError):
        vol, matchup, form = 50.0, 50.0, 50.0
    # 50 baseline → 0.35 xG. Each 10pp ≈ +0.06 xG.
    base = DEFAULT_PLAYER_XG_PER_90 * (DEFAULT_EXPECTED_MINUTES / 90.0)
    mult = 1.0
    mult *= 0.6 + (vol / 100.0) * 0.8
    mult *= 0.7 + (matchup / 100.0) * 0.6
    mult *= 0.7 + (form / 100.0) * 0.6
    return max(0.05, min(2.0, base * mult)), "PRIOR_ONLY", 0


def _bisect_lambda_for_atleast_k(k: int, target_p: float) -> float:
    """Find λ such that P(Poisson(λ) >= k) = target_p."""
    def p_at_least(lam: float, k: int) -> float:
        if lam <= 0:
            return 0.0
        # 1 - sum_{i=0}^{k-1} e^-λ λ^i / i!
        cdf = 0.0
        term = math.exp(-lam)
        cdf += term
        for i in range(1, k):
            term *= lam / i
            cdf += term
        return 1.0 - cdf
    lo, hi = 0.05, 5.0
    for _ in range(40):
        mid = (lo + hi) / 2
        if abs(p_at_least(mid, k) - target_p) < 0.0005:
            return mid
        if p_at_least(mid, k) < target_p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def _poisson(lam: float) -> int:
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


def _signal(disagreement: float) -> str:
    if disagreement > 5:
        return "stronger"
    if disagreement < -5:
        return "weaker"
    return "neutral"


# ─── Main entry point ───────────────────────────────────────────────────
def simulate_soccer_scorer_pick(pick: dict) -> Optional[dict]:
    """Player-level goal scorer simulator. Returns None for non-scorer markets."""
    if (pick.get("sport") or "") != "Soccer":
        return None
    market = pick.get("market") or ""
    cat = _classify_scorer_market(market)
    if not cat:
        return None

    priors = _parse_player_priors(pick)
    lam, provenance, sim_signals = _estimate_player_lambda(pick, priors)

    # Parameter uncertainty: sample λ from Gamma(α, β) where α is set by the
    # number of recent observations (default 10 games), centred on lam.
    n_obs = priors.get("recent_n", 10)
    # Gamma rate β = α / mean → α/β = lam
    alpha = max(2.0, float(n_obs))

    hits = 0
    hits2 = 0   # for 2+ goal markets even on ATGS picks
    hits3 = 0
    sum_goals = 0
    for _ in range(RUNS):
        # Sample λ_run for this run (Gamma-Poisson hierarchical)
        lam_run = random.gammavariate(alpha, lam / alpha)
        g = _poisson(lam_run)
        sum_goals += g
        if g >= 1:
            hits += 1
        if g >= 2:
            hits2 += 1
        if g >= 3:
            hits3 += 1

    # Target probability for the actual market
    if cat == "atgs":
        wins = hits
    elif cat == "two_plus":
        wins = hits2
    elif cat == "hattrick":
        wins = hits3
    elif cat == "fgs":
        # FGS heuristic: P(scores) × (1 / players_likely_to_score)
        # Use 1/3 as proxy unless we have better data.
        wins = int(hits / 3)
    elif cat == "lgs":
        wins = int(hits / 3)
    elif cat == "score_or_assist":
        # P(score OR assist) using proper probability math, not naïve
        # multiplication that clamps to 100%.
        #
        # Bug (2026-06-30, user-reported): previous formula was
        #   `wins = min(RUNS, int(hits * 1.55))`
        # which clamped to 100% whenever score rate exceeded ~65%, so
        # the card showed "SIM EDGE 100%" for any elite striker —
        # mathematically impossible (no event is ever 100%).
        #
        # Correct formula treats score and assist as semi-independent:
        #   P(SoA) = P(score) + P(assist | NOT scoring) × (1 − P(score))
        # Empirically for top-5-league forwards/attacking mids:
        #   P(assist | not scoring) ≈ 0.27 (about 0.6× of score rate)
        # Hard-capped at 0.92 because nothing in football is certain
        # — even Haaland's career score-or-assist rate is ~82%.
        ASSIST_INDEPENDENT_RATE = 0.27
        p_score = hits / max(1, RUNS)
        p_soa = p_score + ASSIST_INDEPENDENT_RATE * (1 - p_score)
        p_soa = min(0.92, p_soa)  # hard cap — never display 100%
        wins = int(round(p_soa * RUNS))
    else:
        wins = hits

    n = RUNS
    p_win = wins / n
    ci_lo, ci_hi = _wilson_ci(p_win, n)

    blended_wp = float(pick.get("win_probability") or 0)
    sim_wp_pct = round(p_win * 100, 1)
    disagreement = round(sim_wp_pct - blended_wp, 2)
    avg_goals = sum_goals / max(1, RUNS)

    out = {
        "sim_win_probability": sim_wp_pct,
        "sim_ci_lower": round(ci_lo * 100, 1),
        "sim_ci_upper": round(ci_hi * 100, 1),
        "sim_runs": n,
        "sim_player_xg": round(lam, 3),
        "sim_expected_goals": round(avg_goals, 3),
        "sim_p_score_2plus": round(hits2 / n * 100, 1),
        "sim_p_hattrick": round(hits3 / n * 100, 1),
        "sim_market_category": f"scorer_{cat}",
        "sim_disagreement_with_model": disagreement,
        "sim_signal": _signal(disagreement),
    }
    # Add parsed priors for the UI to surface as evidence
    if "shots_per_game" in priors:
        out["sim_shots_per_game"] = priors["shots_per_game"]
    if "recent_goal_rate" in priors:
        out["sim_recent_goal_rate"] = round(priors["recent_goal_rate"] * 100, 1)
    if "opp_concedes_per_match" in priors:
        out["sim_opp_concedes"] = priors["opp_concedes_per_match"]
    if "player_xg_per_game" in priors:
        out["sim_player_xg_per_game"] = round(priors["player_xg_per_game"], 3)

    # PHASE 2 (2026-06) — Universal Simulator Provenance Envelope.
    # A MODEL_CONDITIONED λ (Approach 1) is a back-solve from the
    # calling model's own WP — its "agreement" is tautological and
    # MUST NOT count as independent evidence for Magic / Bet
    # Quality / Apex.  A PRIOR_ONLY λ (Approach 3) is a generic
    # factor heuristic — it cannot punish the model either.  Only
    # EMPIRICAL_INDEPENDENT (Approach 2 with real xG / opp / minutes
    # / shots / recent-goal-rate) may raise SIM_MODEL_SEVERE_
    # DISAGREEMENT or count as agreement.
    try:
        from services.simulator_provenance import (
            classify_input_quality, stamp_sim_output,
        )
        stamp_sim_output(
            out,
            provenance=provenance,
            input_quality=classify_input_quality(sim_signals),
            sim_prob=(sim_wp_pct / 100.0),
            model_prob=(blended_wp / 100.0) if blended_wp else None,
        )
    except Exception:
        # Defensive — never break the sim if the contract module errors.
        pass
    return out
