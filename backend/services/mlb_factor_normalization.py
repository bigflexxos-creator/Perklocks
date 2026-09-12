"""MLB → v4 scorer factor normalization boundary.

**PURPOSE**
    Universal Lock Score v4 (2026-06-14) computes ``market_alignment`` as::

        100 - stdev(factors.values()) * 500

    which mathematically REQUIRES every numeric factor to be on a
    normalised ``[0, 1]`` scale.  Feeding it 0-100 percentages causes
    ``stdev`` to explode by 100× and ``market_alignment`` collapses to
    0 — costing MLB picks ~20 legitimate Lock Score points and blocking
    them from ever reaching the 85+ publication band.

    All MLB feature builders in ``services.mlb_feature_engine`` return
    factors clamped to the ``[0.30, 0.95]`` band via the internal
    ``_scale`` helper.  This module is the SINGLE authoritative boundary
    between MLB emission sites and ``compute_lock_score`` — it converts
    ANY stray 0-100 values back to ``[0, 1]``, preserves already-
    normalised values verbatim, and quarantines impossible / mis-scaled
    inputs so a wiring defect anywhere upstream cannot silently corrupt
    scoring math.

**CONTRACT**
    ``normalize_mlb_factors_for_scoring(factors)`` accepts a mixed
    scorer-facing factor dict and returns a new dict where every
    numeric value is guaranteed on ``[0, 1]``.  Missing / non-numeric
    keys are preserved as-is (strings, ``None`` values) so downstream
    ``compute_lock_score`` filters them out naturally.  The function is
    idempotent (running it twice is safe).

**RULES**
    1. Percentage / rate factors (``xBA``, ``Barrel%``, ``Hard-Hit %``,
       ``Recent L10 Hit Rate``, ``Home/Away Splits``, ``Platoon``,
       ``Regression Signal``, ``DFS Projection``, ``Umpire Zone``,
       ``Stuff+``, ``xwOBA``, ``K/9``, ``HR Intel Composite``, ``BvP``)
       already in ``[0, 1]`` → kept as-is.
    2. Same factor keys with values > 1.5 → divided by 100 (they came
       from an upstream site that forgot to scale down).
    3. Raw counts / odds / signed deltas / categorical strings /
       provenance keys (``__data_quality``, ``__model_uncertainty_reason``)
       → NEVER divided.  A raw integer strike-out count like ``6.5``
       is a betting *line*, not a rate; it never enters scoring math.
       Such keys are dropped from the scoring view so ``market_alignment``
       is computed only from proper rate factors.
    4. Impossible values (NaN, Inf, negative rate) → quarantined
       (dropped from scoring dict).
    5. ``None`` → kept out of the scoring dict (missing evidence stays
       missing; no synthetic neutral 0.5 injected).
"""

from __future__ import annotations

import math
from typing import Any, Mapping

__all__ = [
    "normalize_mlb_factors_for_scoring",
    "MLB_RATE_FACTOR_KEYS",
    "MLB_NON_RATE_FACTOR_KEYS",
]

# ── Whitelist of MLB factor key SEMANTIC FAMILIES that ARE rate/pct ──
# Match is case-insensitive substring on the factor key name.  These
# families originate from ``services.mlb_feature_engine`` builders and
# from ``services.mlb_hr_intel`` composites; every one is designed to
# clamp its output to ``[0.30, 0.95]`` via ``_scale`` so any value
# outside ``[0, 1.5]`` we see here IS a scale defect that must be
# corrected before it reaches the market_alignment stdev math.
MLB_RATE_FACTOR_KEYS: tuple[str, ...] = (
    "recent l10 hit rate",
    "home/away splits",
    "platoon advantage",
    "bvp",                              # Career OPS vs pitcher (scaled)
    "expected ba",                      # xBA Statcast rate
    "barrel%",                          # Barrel percentage
    "barrel% (quality of contact)",
    "hard-hit %",                       # Hard-hit percentage
    "hard-hit % (statcast)",
    "regression signal",                # xBA − BA delta scaled
    "regression signal (xba-ba)",
    "dfs projection",                   # DFS projection normalised
    "dfs projection vs line",
    "umpire zone",                      # Umpire K-zone bias factor
    "umpire zone (hitter bias)",
    "umpire zone (pitcher bias)",
    "stuff+",                           # Pitcher Stuff+ scaled
    "xwoba",                            # xwOBA against scaled
    "k/9",                              # Pitcher K/9 scaled
    "k per 9",
    "hr intel composite",               # HR Intel composite scaled
    "hr/9",                             # Pitcher HR/9 scaled
    "iso",                              # ISO scaled
    "hr per pa",                        # HR per PA scaled
    "team k rate",                      # Team K rate scaled
    "park factor",                      # Park HR factor scaled
    "weather",                          # Weather multiplier scaled
    "temp/roof",                        # Temperature effect scaled
    "recent form",                      # Recent form scaled
    "batter power",                     # Batter power scaled
    "opp bullpen",                      # Bullpen matchup scaled
    "opp defense",                      # Defense allow rate scaled
    "opp k%",                           # Opposition K rate scaled
    "book implied",                     # Book implied (already 0..1)
    "model probability",                # Model prob (already 0..1)
    "market alignment",                 # Market alignment factor
)

# Keys that are semantically NOT rates and must never be divided
# by 100 even if numeric.  These are raw counts, betting lines,
# signed deltas, or provenance annotations.
MLB_NON_RATE_FACTOR_KEYS: tuple[str, ...] = (
    "line",                             # e.g. "K Line 6.5"
    "book odds",
    "american odds",
    "delta",
    "signed",
    "count",
    "outs",
    "innings pitched",
    "pitch count",
    "at bats",
    "plate appearances",
    "batters faced",
)


def _key_is_rate(key: str) -> bool:
    """Return True when the factor key semantically represents a rate/pct."""
    if not isinstance(key, str):
        return False
    lk = key.lower()
    # Fail-safe: explicit non-rate keys win.
    for nrk in MLB_NON_RATE_FACTOR_KEYS:
        if nrk in lk:
            return False
    for rk in MLB_RATE_FACTOR_KEYS:
        if rk in lk:
            return True
    # A generic MLB factor key we do NOT know about — treat it as a
    # rate ONLY if the caller stamped a "%" or "rate" marker in the
    # name itself.  This keeps the boundary conservative: unknown
    # numeric factors bypass normalisation and reach the scorer as-is
    # so they never get incorrectly divided by 100.
    return "%" in lk or " rate" in lk or lk.endswith("rate")


def _is_finite_number(v: Any) -> bool:
    if not isinstance(v, (int, float)):
        return False
    if isinstance(v, bool):
        # bool is a subclass of int — reject it explicitly.
        return False
    try:
        return math.isfinite(float(v))
    except (TypeError, ValueError):
        return False


def normalize_mlb_factors_for_scoring(
    factors: Mapping[str, Any] | None,
) -> dict[str, float]:
    """Return an MLB scorer-facing factor dict guaranteed on ``[0, 1]``.

    Parameters
    ----------
    factors : Mapping[str, Any] | None
        Raw MLB factor dict as emitted by ``services.mlb_feature_engine``
        (or any downstream mutator).  May be ``None`` / empty.

    Returns
    -------
    dict[str, float]
        A **new** dict containing only the numeric rate factors
        normalised to ``[0, 1]``.  Non-numeric values, ``None`` values,
        provenance keys (``__``-prefixed) and non-rate keys are
        removed — they never enter the ``market_alignment`` stdev math.

    Notes
    -----
    Callers should preserve the ORIGINAL ``factors`` dict for
    display / rationale generation (human-readable "Barrel% = 71.8%")
    and use the returned dict only for feeding ``compute_lock_score``.
    """
    if not factors:
        return {}
    out: dict[str, float] = {}
    for k, v in factors.items():
        # Provenance / sentinel keys are always ignored by the scorer.
        if isinstance(k, str) and k.startswith("__"):
            continue
        # Missing evidence stays missing.
        if v is None:
            continue
        # Non-numeric (strings such as "3.2 pts") are display-only.
        if not _is_finite_number(v):
            continue
        fv = float(v)
        # Quarantine impossible negatives (a rate can never be < 0).
        # Same for wildly-out-of-range values (>200) — those are
        # obvious scale defects, not just percentage inputs.
        if fv < 0.0 or fv > 200.0:
            continue
        # Only apply the 0-100 → 0-1 conversion for keys we RECOGNISE
        # as rate/percentage families.  Unknown keys with values in
        # ``[0, 1.5]`` pass through untouched (already-normalised
        # third-party factors); unknown keys with values > 1.5 are
        # dropped defensively so an accidental odds/count leak can't
        # inflate stdev.
        if _key_is_rate(k):
            if fv > 1.5:
                fv = fv / 100.0
            # Clamp to [0, 1] AFTER the conversion so extreme upstream
            # bugs (e.g. Barrel% = 132) collapse to the top of the
            # rate band rather than tainting stdev.
            fv = max(0.0, min(1.0, fv))
            out[k] = round(fv, 4)
        else:
            # Not a known rate family and value ≤ 1.5 → treat as
            # already-normalised numeric and forward it.
            if 0.0 <= fv <= 1.5:
                out[k] = round(min(1.0, fv), 4)
            # else: drop (not a rate, out-of-[0,1.5] → not scorer-safe)
    return out


def has_out_of_scale_factors(
    factors: Mapping[str, Any] | None,
    threshold: float = 1.5,
) -> bool:
    """Diagnostic helper — True when ANY numeric MLB factor is > threshold.

    Used by ``compute_lock_score``'s defensive normalisation path to
    decide whether the caller mistakenly emitted 0-100 factors.  A
    single out-of-scale factor is enough to collapse alignment, so we
    normalise the whole dict when any value trips the check.
    """
    if not factors:
        return False
    for k, v in factors.items():
        if isinstance(k, str) and k.startswith("__"):
            continue
        if not _is_finite_number(v):
            continue
        if float(v) > threshold:
            return True
    return False
