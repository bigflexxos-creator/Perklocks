"""Canonical Probability Unit Contract — Phase A (2026-06 Root Closure).

Purpose
-------
One authoritative module for probability / edge unit conversion.  All
authority-level unit boundaries MUST route through the helpers here so
we never again ship a `undefined%` implied or a `-1348%` edge.

Semantics
---------
INTERNAL MATHEMATICAL PROBABILITY:  fraction in ``[0.0, 1.0]``
DISPLAY / WIRE PERCENTAGE:           ``[0.0, 100.0]``

The old anti-pattern:
    if x > 1: x /= 100      # magnitude-inference — DO NOT USE

is BANNED at authority boundaries.  Use ``to_fraction()`` /
``to_percent()`` with an explicit ``assume`` parameter when a value's
unit is ambiguous.

Edge is measured in PERCENTAGE POINTS (pp), i.e. the arithmetic
difference between two probabilities expressed as percentages:

    edge_pp = (model_prob_fraction - implied_prob_fraction) * 100

For +3500 odds, implied = 100/(3500+100) = 0.02778 (2.78%).  If
model probability is 0.0143 (1.43%), the correct edge is:

    (0.0143 - 0.02778) * 100  =  -1.35 pp

NOT ``-1348%``.  A magnitude ≥ 100 pp is mathematically impossible and
indicates a unit-mixing bug — ``edge_percentage_points`` clamps to
``EDGE_CLAMP_MAX_PP`` and stamps ``edge_unit_error`` so callers can
detect the bug.
"""
from __future__ import annotations

import math
from typing import Optional, Tuple

# Hard clamp — arithmetic edge in percentage points beyond this is a
# unit-mixing bug and MUST be caught before publication.
EDGE_CLAMP_MAX_PP: float = 100.0

# Provenance labels — canonical taxonomy for section §19.
CAUSAL_INDEPENDENT   = "CAUSAL_INDEPENDENT"
EMPIRICAL_INDEPENDENT = "EMPIRICAL_INDEPENDENT"
MODEL_CONDITIONED    = "MODEL_CONDITIONED"
MARKET_CONDITIONED   = "MARKET_CONDITIONED"
BOOK_ANCHORED        = "BOOK_ANCHORED"
BOOK_IMPLIED_SEED    = "BOOK_IMPLIED_SEED"

INDEPENDENT_PROVENANCE = frozenset({CAUSAL_INDEPENDENT, EMPIRICAL_INDEPENDENT})
MARKET_CONDITIONED_PROVENANCE = frozenset({
    MODEL_CONDITIONED, MARKET_CONDITIONED, BOOK_ANCHORED, BOOK_IMPLIED_SEED,
})


def is_finite_number(x) -> bool:
    """True iff ``x`` coerces to a finite float."""
    try:
        f = float(x)
    except (TypeError, ValueError):
        return False
    return math.isfinite(f)


def to_fraction(x, *, assume: str = "auto") -> Optional[float]:
    """Return ``x`` as a probability fraction in ``[0.0, 1.0]``.

    ``assume``:
        - ``"fraction"`` — treat ``x`` as already in [0, 1]
        - ``"percent"``  — treat ``x`` as a 0-100 percentage
        - ``"auto"``     — legacy magnitude heuristic (do NOT use at
                            authority boundaries; retained for the
                            pre-fixed callers that still emit ambiguous
                            values)

    Returns ``None`` for non-finite / out-of-range inputs.
    """
    if not is_finite_number(x):
        return None
    f = float(x)
    if assume == "fraction":
        if 0.0 <= f <= 1.0001:
            return max(0.0, min(1.0, f))
        return None
    if assume == "percent":
        if 0.0 <= f <= 100.5:
            return max(0.0, min(1.0, f / 100.0))
        return None
    # auto — legacy heuristic
    if -0.001 <= f <= 1.001:
        return max(0.0, min(1.0, f))
    if 0.0 <= f <= 100.5:
        return max(0.0, min(1.0, f / 100.0))
    return None


def to_percent(x, *, assume: str = "auto") -> Optional[float]:
    """Return ``x`` as a percentage in ``[0.0, 100.0]``."""
    frac = to_fraction(x, assume=assume)
    if frac is None:
        return None
    return round(frac * 100.0, 4)


def implied_probability_from_odds(american_odds) -> Optional[float]:
    """Return the raw (VIG-inclusive) implied probability as a fraction
    in ``[0.0, 1.0]`` for the given American odds, or ``None`` if the
    input is not a finite non-zero number.

    Formulas:
        odds >=  100:  100 / (odds + 100)
        odds <= -100:  abs(odds) / (abs(odds) + 100)
        odds in  (-100, 100)\\{0}: still valid — treated symmetrically.
    """
    if not is_finite_number(american_odds):
        return None
    o = float(american_odds)
    if o == 0.0:
        return None
    if o >= 100.0:
        p = 100.0 / (o + 100.0)
    elif o <= -100.0:
        p = abs(o) / (abs(o) + 100.0)
    else:
        # Odds in (-100, 100): still valid book prices exist for
        # +/-95 etc; use the closest valid formula symmetrically.
        if o > 0:
            p = 100.0 / (o + 100.0)
        else:
            p = abs(o) / (abs(o) + 100.0)
    return max(0.0, min(1.0, p))


def edge_percentage_points(
    model_prob, book_impl_or_odds, *,
    impl_is_odds: bool = False,
    assume_model: str = "auto",
) -> Tuple[Optional[float], Optional[str]]:
    """Return ``(edge_pp, error)`` where ``edge_pp`` is the model vs
    implied delta in *percentage points* (i.e. two probability
    fractions differenced then multiplied by 100).

    ``impl_is_odds=True`` treats ``book_impl_or_odds`` as American
    odds (safest — always unambiguous).  ``impl_is_odds=False`` treats
    it as a probability using ``assume_model`` semantics.

    ``error`` is ``None`` on success, otherwise a short code:
        ``"non_finite_model"``, ``"non_finite_book"``, ``"clamped"``.
    """
    mp = to_fraction(model_prob, assume=assume_model)
    if mp is None:
        return None, "non_finite_model"
    if impl_is_odds:
        bp = implied_probability_from_odds(book_impl_or_odds)
    else:
        bp = to_fraction(book_impl_or_odds, assume=assume_model)
    if bp is None:
        return None, "non_finite_book"
    edge = (mp - bp) * 100.0
    if not math.isfinite(edge):
        return None, "non_finite_result"
    if abs(edge) > EDGE_CLAMP_MAX_PP:
        # Never emit a magnitude ≥ 100 pp — that is a unit-mixing bug.
        return max(-EDGE_CLAMP_MAX_PP, min(EDGE_CLAMP_MAX_PP, edge)), "clamped"
    return round(edge, 2), None


def american_from_fraction(prob) -> Optional[int]:
    """Convert a fractional probability to American odds (research use).

    Returns None for prob outside (0, 1) exclusive.
    """
    p = to_fraction(prob, assume="fraction")
    if p is None or p <= 0.0 or p >= 1.0:
        return None
    if p >= 0.5:
        return int(round(-100.0 * p / (1.0 - p)))
    return int(round(100.0 * (1.0 - p) / p))


__all__ = [
    "EDGE_CLAMP_MAX_PP",
    "CAUSAL_INDEPENDENT", "EMPIRICAL_INDEPENDENT",
    "MODEL_CONDITIONED", "MARKET_CONDITIONED",
    "BOOK_ANCHORED", "BOOK_IMPLIED_SEED",
    "INDEPENDENT_PROVENANCE", "MARKET_CONDITIONED_PROVENANCE",
    "is_finite_number",
    "to_fraction", "to_percent",
    "implied_probability_from_odds",
    "edge_percentage_points",
    "american_from_fraction",
]
