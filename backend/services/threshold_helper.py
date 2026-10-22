"""Shared integer-count ``Over`` semantics helper.

NHL / NBA / NFL / MLB discrete-count markets (Goals, Shots, Assists,
Points, Receptions, Hits, …) require a single canonical rule for how
``Over N.5`` resolves against an integer actual.  Prior to centralisation
this rule was duplicated inline across 5+ sites in nhl_v2/models.py
and others, risking drift.

Rule
----
``Over N.5``  → actual >= floor(N.5) + 1   == ``N + 1``
``Under N.5`` → actual <= floor(N.5)      == ``N``

Examples::

    integer_count_over_target(0.5) == 1    # "Over 0.5 Goals"   → >= 1
    integer_count_over_target(1.5) == 2    # "Over 1.5 Goals"   → >= 2
    integer_count_over_target(3.5) == 4    # "Over 3.5 Shots"   → >= 4

Why this helper exists:
* ONE source of truth consumed by historical hit-rate, model
  distribution probability, simulation, and exact-market evidence.
* Decimal lines are rare in integer-count markets but defensively
  rounded up to the next integer (`.0` → `.5` convention).
"""
from __future__ import annotations

import math


def integer_count_over_target(line: float) -> int:
    """Return the minimum integer actual that satisfies ``Over <line>``.

    Line of 0.5 → 1, 1.5 → 2, 2.5 → 3, 3.5 → 4 …

    If a .0 line slips through, round up (conservative: Over 3.0 is
    traditionally a push on 3, so an actual >= 4 fully wins).
    """
    try:
        f = float(line)
    except (TypeError, ValueError):
        return 0
    return int(math.floor(f) + 1)


def integer_count_under_target(line: float) -> int:
    """Return the maximum integer actual that satisfies ``Under <line>``."""
    try:
        f = float(line)
    except (TypeError, ValueError):
        return 0
    return int(math.floor(f))


def integer_count_hit_rate(values: list, line: float, side: str) -> float:
    """Empirical hit rate for an integer-count Over/Under at ``line``.

    Returns the fraction of ``values`` for which the pick side wins.
    Non-numeric values are ignored.
    """
    target = integer_count_over_target(line)
    nums = [v for v in values if isinstance(v, (int, float))]
    if not nums:
        return 0.0
    is_over = (side or "").strip().lower() == "over"
    if is_over:
        hits = sum(1 for v in nums if v >= target)
    else:
        under_max = integer_count_under_target(line)
        hits = sum(1 for v in nums if v <= under_max)
    return hits / len(nums)


__all__ = [
    "integer_count_over_target",
    "integer_count_under_target",
    "integer_count_hit_rate",
]
