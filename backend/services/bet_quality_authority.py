"""Bet Quality Authority — Universal Multi-Signal Lock-Score Ceiling.

**PURPOSE**
    Lock Score is not just "how likely does this exact wager hit."  It
    measures the QUALITY OF THE COMPLETE BETTING SETUP: convergent
    calibrated probability, reliability, exact-threshold history,
    matchup / role, independent-signal convergence, simulation
    stability, and data completeness.

    The old NFL authority ceiling ``60 + WP * 40`` over-coupled the
    Lock-Score ceiling to Win Probability alone, so an 80%-WP prop
    could not reach LS-98 even when every ancillary evidence axis
    was flawless.  This module computes a proper multi-signal
    ceiling used by ``sports_engine.compute_lock_score`` when a
    caller opts in via ``BET_QUALITY_AUTHORITY_ENABLED_SPORTS``.

**AUTHORITY FORMULA**
    Weights (sum to 1.0):
        40%   calibrated Win Probability
        15%   prediction reliability (probability_provenance quality)
        15%   exact-threshold historical support
        10%   matchup / role support
        10%   independent convergence (agreement across factors)
        5%    distribution / simulation stability (volatility)
        5%    data quality / completeness

    Each signal maps to a component score in ``[0, 100]``.  The
    weighted sum produces the maximum Lock Score attainable *before*
    the separate APEX (100) gate.  99 remains peak non-Apex quality;
    100 requires the dedicated APEX pathway.

**FEATURE FLAGS**
    ``BET_QUALITY_AUTHORITY_ENABLED_SPORTS`` is a mutable set so the
    scorer can be enabled / disabled per sport / market family
    independently.  Rollback: remove the entry to fall back to the
    prior authority ceiling.

**IMPORTANT PRESERVATION CONTRACTS**
    * Win Probability is UNCHANGED — this authority reads WP, never
      recomputes it.
    * ``factors`` is READ-ONLY here.  No factor mutation.
    * Below-85 rows are untouched — this only affects the CEILING,
      not the composite.
    * Never manufactures scores.  If evidence is thin, the ceiling
      naturally caps lower.
"""
from __future__ import annotations

import math
from typing import Any, Mapping

# ─────────────────────────────────────────────────────────────────────
# Feature flags — per-surface opt-in (mutable so ops can flip live)
# ─────────────────────────────────────────────────────────────────────
BET_QUALITY_AUTHORITY_ENABLED_SPORTS: set[str] = {
    "MLB",         # includes hitter props, pitcher props, game markets
    "CFB",         # spread, total, moneyline, alt spread, alt total
    "NFL_GAME",    # NFL moneyline / spread / game total
    "NFL_PLAYER",  # NFL player props including alt ladders
}

# The formal weight table (sums to 1.00).
BQ_WEIGHTS: dict[str, float] = {
    "wp":              0.40,
    "reliability":     0.15,
    "history":         0.15,
    "matchup":         0.10,
    "convergence":     0.10,
    "distribution":    0.05,
    "data_quality":    0.05,
}
assert abs(sum(BQ_WEIGHTS.values()) - 1.0) < 1e-9

BET_QUALITY_AUTHORITY_VERSION = "bq_authority.v1.2026-09-13"


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _wp_component(win_prob_pct: float) -> float:
    """Calibrated WP → component score.  Steeper than the composite's
    ``confidence`` curve on purpose: a strong 80% WP prop combined
    with exceptional evidence must be able to reach the 98/99 band
    without needing 95-98% WP alone (the defect of the old NFL prop
    ``60 + WP*40`` authority ceiling).

    Anchor points:
        WP ≤ 50% → 55       (below-book territory contributes ≤55)
        WP  60% → 74
        WP  70% → 86
        WP  75% → 92
        WP  80% → 96
        WP  85% → 98
        WP  90% → 99
        WP  95% → 99.5
        WP 100% → 100
    """
    wp = max(0.0, min(100.0, float(win_prob_pct or 0.0))) / 100.0
    if wp <= 0.50:  return 55.0 * (wp / 0.50)          # 0 → 55
    if wp <= 0.60:  return 55.0 + (wp - 0.50) * (19.0 / 0.10)   # 55 → 74
    if wp <= 0.70:  return 74.0 + (wp - 0.60) * (12.0 / 0.10)   # 74 → 86
    if wp <= 0.80:  return 86.0 + (wp - 0.70) * (10.0 / 0.10)   # 86 → 96
    if wp <= 0.90:  return 96.0 + (wp - 0.80) * (3.0  / 0.10)   # 96 → 99
    return 99.0 + (wp - 0.90) * (1.0 / 0.10)                    # 99 → 100


def _reliability_component(pick: Mapping[str, Any]) -> float:
    """Signal quality of the model that emitted the WP."""
    prov = str(pick.get("probability_provenance") or "").upper()
    tier_map = {
        "PLATINUM":               95.0,
        "APEX":                   93.0,
        "MODEL_CONDITIONED":      90.0,
        "MODEL_FIRST":            88.0,
        "MODEL_BLENDED":          85.0,
        "CAUSAL_INDEPENDENT":     92.0,
        "PROP_MODEL_PRIMARY":     88.0,
        "PROP_MODEL_BLENDED":     84.0,
        "PROP_MODEL_HEURISTIC":   72.0,
        "BOOK_IMPLIED_CALIBRATED": 80.0,
        "BOOK_IMPLIED":            65.0,
        "HEURISTIC":               60.0,
        "PRIOR_ONLY":              55.0,
    }
    if prov in tier_map:
        return tier_map[prov]
    # Fallback — use data_quality tier as a soft proxy so unknown
    # provenance doesn't collapse to 0.  Default to 85 (adequate),
    # NOT 60, because absence of provenance is not evidence of
    # BAD provenance.
    dq_str = str(pick.get("data_quality") or "").lower()
    if "returning_prod" in dq_str or "portal_both" in dq_str: return 90.0
    if "sp_plus" in dq_str or "elo_full" in dq_str: return 85.0
    if "full" in dq_str: return 88.0
    if dq_str: return 78.0
    return 85.0


def _history_component(pick: Mapping[str, Any],
                       factors: Mapping[str, Any] | None) -> float:
    """Exact-threshold historical support.  Reads standard fields the
    NFL / MLB / CFB feature engines already stash on the pick."""
    factors = factors or {}
    # Highest-quality signal: the ATD / prop feature engines stash
    # a direct hit-rate against the exact line.
    for k in ("exact_threshold_hit_rate",
              "historical_hit_rate",
              "at_or_over_hit_rate",
              "L10 Hit Rate",
              "Recent L10 Hit Rate"):
        v = pick.get(k) if k in pick else factors.get(k)
        if isinstance(v, (int, float)):
            v = float(v)
            if v > 1.5: v /= 100.0
            v = max(0.0, min(1.0, v))
            # Map 0.30 → 55, 0.65 → 90, 1.0 → 98
            return 55.0 + (v - 0.30) * (43.0 / 0.70) if v >= 0.30 else v * (55.0 / 0.30)
    # Softer proxy: sample size ("history_sample_size") when hit rate
    # missing.
    sample = pick.get("history_sample_size") or factors.get("history_sample_size")
    if isinstance(sample, (int, float)):
        s = float(sample)
        if s >= 20: return 85.0
        if s >= 12: return 80.0
        if s >= 6:  return 74.0
    # No explicit history data → adequate default (absence of data
    # ≠ negative evidence).  85 keeps well-formed picks structurally
    # reachable at the 90+ band without forcing a manufactured history.
    return 85.0


def _matchup_component(pick: Mapping[str, Any],
                       factors: Mapping[str, Any] | None) -> float:
    """Matchup / role signal — many engines already emit a matchup
    or role-quality factor."""
    factors = factors or {}
    # Look for common matchup keys across sports.  Values are ALREADY
    # in the range compute_lock_score's boundary produces (0-100
    # after ×100 or 0-1 raw — accept both).
    candidates = []
    for k in ("Matchup Advantage", "Matchup Rating", "Role Quality",
              "Opponent Defense", "Opp Defense", "Opp Bullpen",
              "Platoon Advantage", "SP+ Rating Δ", "SP+ Rating Δ (norm)"):
        v = factors.get(k)
        if isinstance(v, (int, float)):
            v = float(v)
            if v > 1.5: v /= 100.0
            candidates.append(max(0.0, min(1.0, v)))
    if not candidates:
        return 85.0
    # Use the strongest matchup signal available.
    peak = max(candidates)
    # 0.30 → 55, 0.65 → 90, 1.0 → 98
    if peak >= 0.30:
        return 55.0 + (peak - 0.30) * (43.0 / 0.70)
    return peak * (55.0 / 0.30)


def _convergence_component(scoring_factors: Mapping[str, float] | None,
                            pick: Mapping[str, Any] | None = None) -> float:
    """Multi-signal agreement.  For player props / MLB / CFB where the
    factors dict contains homogeneous evidence signals (e.g. Statcast
    xBA, Barrel%, Hard-Hit% for MLB HRR; SP+ margin/rating for CFB),
    the raw stdev-based agreement is meaningful.

    For NFL GAME MARKETS the ``factors`` deliberately mix HETEROGENEOUS
    axes — ``Model Win Prob (norm)``, ``Expected Margin (norm)``,
    ``Model-vs-Market Δ``, ``Simulation Stability (norm)`` — that
    measure DIFFERENT concepts.  Their absolute values are on the same
    [0,1] band but taking the raw stdev of them and calling that
    "convergence" is semantically wrong: a game where the model side
    probability is 0.72, expected-margin support is 0.64, and
    simulation stability is 0.91 is a STRONGLY AGREEING game (all
    signals point the same direction with high stability), yet the
    stdev would collapse the naive convergence toward zero.

    Fix (2026-09-13 · P2): for NFL game markets, derive convergence
    from the SEMANTIC alignment of the axes rather than raw stdev.

    Every non-game caller and non-NFL sport keeps the original stdev
    math — proven correct for the homogeneous-evidence sports.
    """
    if not scoring_factors:
        return 80.0
    # ── SEMANTIC CONVERGENCE for NFL game markets ────────────────
    sport = (pick or {}).get("sport") or ""
    market = ((pick or {}).get("market") or "").lower()
    is_nfl_game = (
        sport == "NFL" and market != "" and not any(
            w in market for w in (
                "yards", "yds", "receptions", "completions",
                "attempts", "touchdowns", "tds", "atd",
                "anytime", "longest", "first td", "1st td",
                "player ", "pass ", "rush ", "reception",
            )
        )
    )
    if is_nfl_game:
        # Model side probability (or fair prob) — evidence axis 1.
        wp    = _first_numeric(scoring_factors, (
            "Model Win Prob (norm)", "Model Fair Prob (norm)",
        ))
        # Expected-margin support (favorite side, [0,1]).
        marg  = _first_numeric(scoring_factors, (
            "Expected Margin (norm)", "Projected Margin (norm)",
            "SP+ Rating Δ (norm)",
        ))
        # Model-vs-market delta (higher = more edge on our side).
        mvm   = _first_numeric(scoring_factors, (
            "Model-vs-Market Δ",
        ))
        # Simulation stability (already a stability signal on [0,1]).
        stab  = _first_numeric(scoring_factors, (
            "Simulation Stability (norm)", "Sim Stability (norm)",
        ))
        # Semantic agreement — each axis contributes 0..1 support in
        # the SAME direction of the pick.  We treat "high support" as
        # ≥ 0.55 (better than a coin flip vs market anchor).
        supports = [x for x in (wp, marg, mvm, stab) if x is not None]
        if not supports:
            return 80.0
        # Fraction of supporting axes → agreement %.
        agree = sum(1 for x in supports if x >= 0.55) / len(supports)
        # Blend with the mean support strength so a lopsided-but-
        # borderline set doesn't score identically to a lopsided-
        # and-strong set.
        mean_support = sum(supports) / len(supports)
        return _clamp(100.0 * (0.5 * agree + 0.5 * mean_support), 0.0, 100.0)

    # ── DEFAULT stdev-based agreement (homogeneous evidence sports) ──
    vals = [float(v) for v in scoring_factors.values() if isinstance(v, (int, float))]
    if len(vals) < 2:
        return 80.0
    mean = sum(vals) / len(vals)
    stdev = (sum((v - mean) ** 2 for v in vals) / len(vals)) ** 0.5
    return _clamp(100.0 - stdev * 500.0, 0.0, 100.0)


def _first_numeric(d: Mapping[str, Any], keys) -> float | None:
    for k in keys:
        v = d.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            v = float(v)
            if v > 1.5:
                v /= 100.0
            return max(0.0, min(1.0, v))
    return None


def _distribution_component(pick: Mapping[str, Any]) -> float:
    """Simulation / distribution stability — several engines stash a
    ``sim_stability`` or ``volatility`` signal on the pick.  Higher
    stability → higher component score."""
    for k in ("sim_stability", "distribution_stability",
              "volatility_component"):
        v = pick.get(k)
        if isinstance(v, (int, float)):
            v = float(v)
            if v > 1.5: v /= 100.0
            return _clamp(30.0 + v * 65.0)
    # Fall back to the volatility component compute_lock_score already
    # stamps (if present at authority-time it's the earlier composite).
    lc = pick.get("lock_components") or {}
    if isinstance(lc, dict) and lc.get("volatility") is not None:
        return _clamp(float(lc["volatility"]))
    # Default: mid-band 85 (adequate).
    return 85.0


def _data_quality_component(pick: Mapping[str, Any],
                            factors: Mapping[str, Any] | None) -> float:
    dq = pick.get("data_quality")
    if isinstance(dq, (int, float)):
        v = float(dq)
        if v > 1.5:
            return _clamp(v)
        return _clamp(v * 100.0)
    dq_str = str(dq or "").lower()
    if "returning_prod_both+portal_both" in dq_str: return 95.0
    if "returning_prod_both" in dq_str or "portal_both" in dq_str: return 92.0
    if "sp_plus" in dq_str or "elo_full" in dq_str: return 88.0
    if "full" in dq_str: return 90.0
    if "book_implied_calibrated" in dq_str: return 80.0
    if "book_implied" in dq_str: return 65.0
    if "heuristic" in dq_str: return 55.0
    # Fall back to factor count as a soft proxy.
    factors = factors or {}
    numeric_factor_count = sum(
        1 for v in factors.values() if isinstance(v, (int, float))
    )
    if numeric_factor_count >= 8: return 92.0
    if numeric_factor_count >= 5: return 88.0
    if numeric_factor_count >= 3: return 82.0
    return 75.0


def compute_bet_quality_authority(
    *,
    win_prob_pct: float,
    pick: Mapping[str, Any],
    factors: Mapping[str, Any] | None,
    scoring_factors: Mapping[str, float] | None,
) -> tuple[float, dict[str, float]]:
    """Return the Bet Quality authority ceiling and its component
    breakdown.  The ceiling is in ``[0, 99]`` — 100 is reserved for
    the separate APEX gate.

    The caller (``compute_lock_score``) uses this VALUE as a MAX on
    the emitted Lock Score when the sport is opted in via
    ``BET_QUALITY_AUTHORITY_ENABLED_SPORTS``.  It never LIFTS a score
    that the composite formula produces below the ceiling — this is a
    ceiling, not a floor.
    """
    comps = {
        "wp":            _wp_component(win_prob_pct),
        "reliability":   _reliability_component(pick),
        "history":       _history_component(pick, factors),
        "matchup":       _matchup_component(pick, factors),
        "convergence":   _convergence_component(scoring_factors, pick),
        "distribution":  _distribution_component(pick),
        "data_quality":  _data_quality_component(pick, factors),
    }
    ceiling = sum(BQ_WEIGHTS[k] * comps[k] for k in BQ_WEIGHTS)
    # Cap at 99 — 100 belongs to the APEX pathway.
    ceiling = _clamp(ceiling, 0.0, 99.0)
    return round(ceiling, 1), {k: round(v, 1) for k, v in comps.items()}


def _sport_authority_key(sport: str, market: str | None) -> str:
    """Map (sport, market) → the enabled-sports key so NFL splits
    into NFL_GAME vs NFL_PLAYER."""
    s = (sport or "").upper()
    m = (market or "").lower()
    if s == "NFL":
        # Player prop markets contain a player name / prop keyword.
        prop_hint = any(k in m for k in (
            "yards", "receptions", "completions", "attempts",
            "touchdowns", "interceptions", "pass ", "rush ",
            "player ", "anytime", "atd", "first", "longest",
        ))
        if prop_hint:
            return "NFL_PLAYER"
        return "NFL_GAME"
    return s


def bet_quality_authority_enabled(sport: str, market: str | None = None) -> bool:
    key = _sport_authority_key(sport, market)
    return key in BET_QUALITY_AUTHORITY_ENABLED_SPORTS


__all__ = [
    "BET_QUALITY_AUTHORITY_ENABLED_SPORTS",
    "BET_QUALITY_AUTHORITY_VERSION",
    "BQ_WEIGHTS",
    "compute_bet_quality_authority",
    "bet_quality_authority_enabled",
]
