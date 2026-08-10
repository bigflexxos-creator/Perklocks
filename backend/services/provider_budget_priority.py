"""ProviderBudget priority tiers — Phase 3 (2026-08-11).

Priority-aware wrapper over :mod:`services.provider_budget`.  Introduces
the five-tier priority ladder from the Phase 3 spec:

    P1 — today's Locks lines (highest — never budget-shed on its own)
    P2 — today's player props
    P3 — alt lines for strong candidates
    P4 — upcoming / preload
    P5 — background / research (lowest — shed first)

The wrapper does NOT replace ``ProviderBudget`` — it *composes* with it.
Callers still perform ``reserve → commit/release`` against the shared
budget state; this module simply refuses the reservation up-front when
the caller's priority is at/below the currently-blocked tier.

The block-threshold is derived from the live budget headroom:
  * headroom ≥ 25%       → no priority-shedding (accept P1..P5)
  * 10% ≤ headroom < 25% → shed P5
  * 5%  ≤ headroom < 10% → shed P5..P4
  * 2%  ≤ headroom < 5%  → shed P5..P3
  * headroom < 2%        → accept only P1 (emergency mode)

Concrete tests live in ``tests/test_p3_infra_hardening.py``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# Public priority constants
P1_LOCKS_TODAY       = 1
P2_PLAYER_PROPS      = 2
P3_ALT_STRONG        = 3
P4_UPCOMING_PRELOAD  = 4
P5_BACKGROUND        = 5

VALID_PRIORITIES = {P1_LOCKS_TODAY, P2_PLAYER_PROPS, P3_ALT_STRONG,
                     P4_UPCOMING_PRELOAD, P5_BACKGROUND}


@dataclass(frozen=True)
class PriorityDecision:
    allowed: bool
    priority: int
    threshold: int   # the current cutoff — priorities > threshold are blocked
    headroom_pct: float
    reason: str


def _threshold_for_headroom(headroom_pct: float) -> int:
    """Return the lowest priority *number* still allowed.

    Higher priority numbers = lower actual priority, so a threshold
    of ``5`` means everything is allowed, ``1`` means only P1.
    """
    if headroom_pct is None:
        return 5
    if headroom_pct >= 25:
        return 5
    if headroom_pct >= 10:
        return 4
    if headroom_pct >= 5:
        return 3
    if headroom_pct >= 2:
        return 2
    return 1


def decide(
    priority: int,
    daily_used: int,
    daily_limit: int,
) -> PriorityDecision:
    """Pure function — decides whether a request at ``priority`` is
    allowed given the current budget headroom.

    ``daily_used`` and ``daily_limit`` are integers.  ``daily_limit ==
    0`` is treated as "budget disabled" → always allowed.
    """
    if priority not in VALID_PRIORITIES:
        raise ValueError(f"priority must be in {VALID_PRIORITIES}, got {priority}")
    if daily_limit is None or daily_limit <= 0:
        return PriorityDecision(True, priority, 5, 100.0,
                                 "budget_disabled")
    remaining = max(0, daily_limit - max(0, daily_used))
    headroom_pct = 100.0 * remaining / float(daily_limit)
    threshold = _threshold_for_headroom(headroom_pct)
    allowed = priority <= threshold
    reason = ("allowed" if allowed
              else f"blocked_priority_{priority}_below_threshold_{threshold}")
    return PriorityDecision(allowed, priority, threshold, headroom_pct, reason)


__all__ = [
    "P1_LOCKS_TODAY", "P2_PLAYER_PROPS", "P3_ALT_STRONG",
    "P4_UPCOMING_PRELOAD", "P5_BACKGROUND",
    "VALID_PRIORITIES", "PriorityDecision",
    "decide",
]
