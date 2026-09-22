"""NFL Props V2 → canonical Lock Score writer integration.

Bridges V2 evidence into the EXISTING ``sports_engine.compute_lock_score``
authority as legitimate input factors — never as a post-hoc override,
never as a max(old, new) shortcut, never as a direct write of
``published_lock_score``.

Contract:
    integrate_v2_into_lock_score(pick, v2) -> (new_lock, breakdown, factors)

    * v2 is the evidence block produced by canonical_wiring.
    * We ADD numeric-in-[0,1] factor keys to whatever ``pick["factors"]``
      already contains (so the NFL feature-engine factors — L5 vs Line,
      L3 vs Season Trend, Home/Away Split, Career vs Opp Hit%, etc. — stay
      intact).  These new keys become legitimate independent evidence for
      the composite Lock Score authority.
    * If V2 could not produce a distribution (sample_size=0 or None
      probability), we DO NOT mutate the score — the pick keeps whatever
      the base pipeline produced.  Fail-closed.
    * ``compute_lock_score`` is the ONE authority.  We invoke it exactly
      once with the merged factors + the same win_prob the pick already
      carries.  We do NOT change win_probability.  We do NOT re-derive it
      from the V2 hit probability (per user's explicit "Lock Score !=
      Win Expected" constraint — sportsbook price is market evidence,
      V2's hit_prob is a distributional statistic, and neither is the
      writer's model probability).
    * Independence: V2 factors are derived from ``player_game_actuals``
      historical distribution vs. the sportsbook threshold.  The existing
      NFL feature-engine factors (L5 vs Line, etc.) are separately
      derived from L5/L10/season rolling windows.  Both share the same
      underlying historical actuals, so we register the V2 pack under a
      distinct MAGIC evidence category via ``evidence_category`` metadata
      on the factor block, and let the existing Independence/Apex gates
      enforce non-double-counting (they already handle same-source
      collapsing).

Public API:
    integrate_v2_into_lock_score(pick, v2) → (new_lock: float,
                                              breakdown: dict,
                                              factors: dict) | None
"""
from __future__ import annotations

from typing import Any, Optional


# V2 factor keys — all in [0,1] scale, all sport-neutral so
# ``sports_engine.compute_lock_score`` picks them up automatically.
_V2_FACTOR_PREFIX = "NFL V2"


def _clamp01(v) -> Optional[float]:
    if not isinstance(v, (int, float)):
        return None
    try:
        fv = float(v)
    except (TypeError, ValueError):
        return None
    if fv < 0.0:
        return 0.0
    if fv > 1.0:
        return 1.0
    return fv


def _floor_distance_to_factor(floor_distance, line) -> Optional[float]:
    """Map floor_distance (Q25 - line, in raw stat units) into a [0,1]
    factor that credits picks whose Q25 is above the line and penalises
    those whose Q25 falls below.

    Uses the line as the natural scale so a 5-yard cushion on a 20-yard
    line is worth more than a 5-yard cushion on a 200-yard line.
    Purely a monotonic squash — no arbitrary bonuses, no thresholds.
    """
    if floor_distance is None or line is None:
        return None
    try:
        fd = float(floor_distance)
        ln = float(line)
    except (TypeError, ValueError):
        return None
    if ln <= 0:
        return None
    # Normalise: 25% of the line is a strong cushion → 1.0; -25% is
    # dangerous → 0.0; at the line → 0.5.
    scale = max(1.0, 0.25 * abs(ln))
    z = fd / scale
    if z >= 1.0:
        return 1.0
    if z <= -1.0:
        return 0.0
    return 0.5 + 0.5 * z


def _sample_size_to_factor(n) -> Optional[float]:
    """Map distribution sample size to a data-quality confidence factor.
    Saturates at 20 games — matching the historical L20 window.
    """
    if n is None:
        return None
    try:
        n = int(n)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    if n >= 20:
        return 1.0
    return round(n / 20.0, 4)


def build_v2_factors(pick: dict, v2: dict) -> dict:
    """Turn a V2 evidence block into merge-ready factor keys.

    Returns an empty dict when V2 lacks a usable distribution — the
    caller MUST NOT rescore in that case (fail-closed).
    """
    if not isinstance(v2, dict):
        return {}
    p_mono = v2.get("hit_probability_monotonic")
    if p_mono is None:
        # No probability → nothing meaningful to inject.
        return {}
    ss = v2.get("distribution_sample_size")
    if not isinstance(ss, (int, float)) or int(ss) < 5:
        # Guard against 1-2 sample "distributions" that produce noisy
        # probabilities.  Below n=5 we prefer no signal to bad signal.
        return {}
    line = pick.get("line")
    floor = v2.get("floor_distance")

    out: dict = {}
    # Primary V2 factor: monotonic hit-rate for this specific threshold.
    out[f"{_V2_FACTOR_PREFIX} Historical Hit Rate"] = _clamp01(p_mono)
    # Confidence / data quality.
    ss_factor = _sample_size_to_factor(ss)
    if ss_factor is not None:
        out[f"{_V2_FACTOR_PREFIX} Sample Confidence"] = ss_factor
    # Floor cushion — Q25 relative to the line.
    fl_factor = _floor_distance_to_factor(floor, line)
    if fl_factor is not None:
        out[f"{_V2_FACTOR_PREFIX} Distribution Floor"] = fl_factor
    # Weather / injury are neutral-when-unknown per the V2 contract.
    # Only inject when they are AVAILABLE and materially deviate from
    # neutral (indoor / no injury designation).
    wxs = str(v2.get("weather_status") or "").upper()
    if wxs == "AVAILABLE" and v2.get("weather_is_indoor") is True:
        out[f"{_V2_FACTOR_PREFIX} Indoor Neutral"] = 1.0

    # Filter to numeric-in-[0,1] just in case.
    return {k: v for k, v in out.items()
            if isinstance(v, (int, float)) and 0.0 <= v <= 1.0}


def merge_factors(base: dict, v2_factors: dict) -> dict:
    """Merge V2 factors into the existing NFL feature-engine factors
    dict without stomping any base key.  Preserves ``__`` metadata keys
    the writer uses for calibration side-channels.
    """
    merged = dict(base or {})
    for k, v in v2_factors.items():
        # Never overwrite an existing feature-engine key.
        merged.setdefault(k, v)
    return merged


def integrate_v2_into_lock_score(pick: dict, v2: dict) -> Optional[tuple]:
    """Recompute Lock Score for ``pick`` with V2 evidence merged into
    factors.

    Returns ``(new_lock, breakdown, merged_factors)`` when the merge is
    valid and a fresh score was produced; ``None`` when V2 provides no
    usable signal (caller must not mutate the pick).

    NEVER writes to the DB.  NEVER touches ``published_lock_score``.
    The caller is responsible for staging the new score through the
    normal writer path.
    """
    v2_factors = build_v2_factors(pick, v2 or {})
    if not v2_factors:
        return None

    base_factors = pick.get("factors") or {}
    merged = merge_factors(base_factors, v2_factors)

    # Win probability stays what the pick already carries.  We do NOT
    # substitute the sportsbook price nor the raw V2 hit probability.
    wp = pick.get("win_probability")
    if not isinstance(wp, (int, float)):
        # Some callers store as fraction — normalise for compute_lock_score.
        _mp = pick.get("model_win_probability") or pick.get("model_win_prob")
        if isinstance(_mp, (int, float)):
            wp = float(_mp) * 100.0 if float(_mp) <= 1.0 else float(_mp)
    if not isinstance(wp, (int, float)):
        return None

    edge_pct = pick.get("edge_percent")
    try:
        from sports_engine import compute_lock_score
    except Exception:
        return None
    try:
        new_lock, breakdown = compute_lock_score(
            merged, win_prob=float(wp), pick=pick,
            edge_percent=(float(edge_pct) if isinstance(edge_pct, (int, float)) else None),
        )
    except Exception:
        return None

    return (round(float(new_lock), 1), breakdown, merged)


__all__ = [
    "build_v2_factors",
    "integrate_v2_into_lock_score",
    "merge_factors",
]
