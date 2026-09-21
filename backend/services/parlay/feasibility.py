"""Parlay Feasibility Engine — compute the REAL funnel before optimizing
=========================================================================

P0 Root Closure — Parlay 3.0 (2026-06-21).

Before this module, HIGH_RISK NFL / MLB would silently return zero
tickets because the optimizer would burn budget trying to build 10-leg
parlays that were geometrically impossible with the current candidate
pool (e.g. only 5 unique NFL events on the slate).  The user saw
nothing.  No diagnostic.  No "6 of 10 target legs".

``compute_funnel`` measures the ACTUAL candidate universe under the
requested policy + filters BEFORE we spend CPU on ticket search.  It
returns a machine-readable ``FeasibilityReport`` with:

  * canonical_pool_count       — pool AFTER canonical/publication gate
  * mode_eligible_count        — after lock/edge/prob gates
  * real_line_eligible_count   — non-null book_odds
  * unique_events              — count of distinct canonical_event_id
  * unique_market_families     — count of distinct families
  * dependency_safe_max_legs   — max legs achievable with no dependent-sport
                                  same-event violations (soft upper bound)
  * requested_target           — the caller's target
  * min_useful                 — policy floor for partial tickets
  * status                     — READY | PARTIAL_ONLY | INSUFFICIENT
  * reason_codes               — machine-readable structured empty reasons

The route consults the report BEFORE build_top_parlays and adapts:
  * READY:           use requested_target
  * PARTIAL_ONLY:    lower target to dependency_safe_max_legs, build,
                     surface actual_legs vs requested_target in DTO
  * INSUFFICIENT:    return structured empty state, do not run optimizer
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field
from collections import Counter
from typing import Any, Dict, List, Optional, Set

from services.parlay.mode_policy import ModePolicy


# Reason codes returned to the API for truthful empty-state UX.
REASON_INSUFFICIENT_CANONICAL_LEGS       = "INSUFFICIENT_CANONICAL_LEGS"
REASON_INSUFFICIENT_UNIQUE_EVENTS        = "INSUFFICIENT_UNIQUE_EVENTS"
REASON_MODE_THRESHOLD_STARVATION         = "MODE_THRESHOLD_STARVATION"
REASON_DEPENDENCY_CONFLICT               = "DEPENDENCY_CONFLICT"
REASON_MARKET_CONCENTRATION_LIMIT        = "MARKET_CONCENTRATION_LIMIT"
REASON_NO_REAL_ODDS                      = "NO_REAL_ODDS"
REASON_NO_SUPPORTED_COMBINATION          = "NO_SUPPORTED_COMBINATION"


# Status classifications
STATUS_READY         = "READY"
STATUS_PARTIAL_ONLY  = "PARTIAL_ONLY"
STATUS_INSUFFICIENT  = "INSUFFICIENT"


@dataclass(frozen=True)
class FeasibilityReport:
    mode_key: str
    requested_target: int
    min_useful: int
    max_feasible_legs: int
    dependency_safe_max_legs: int

    canonical_pool_count: int
    real_line_eligible_count: int
    mode_eligible_count: int
    edge_eligible_count: int
    probability_eligible_count: int

    unique_events: int
    unique_market_families: int
    market_family_counts: Dict[str, int]
    sport_counts: Dict[str, int]

    status: str
    reason_codes: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _market_family(market: str) -> str:
    m = (market or "").lower()
    if "anytime goal scorer" in m: return "goal_scorer"
    if "first goal scorer" in m:   return "first_goal"
    if "to score or assist" in m:  return "score_or_assist"
    if "win or draw" in m or "double chance" in m: return "win_or_draw"
    if "moneyline" in m:           return "moneyline"
    if "spread" in m or "run line" in m: return "spread"
    if "over" in m and ("hits" in m or "total bases" in m): return "batter_over"
    if "over" in m or "under" in m: return "total_over_under"
    if "wins by" in m:             return "mma_method"
    return "other"


def _has_real_odds(p: dict) -> bool:
    o = p.get("book_odds") if p.get("book_odds") is not None else p.get("published_odds")
    try:
        return o is not None and int(o) != 0
    except (TypeError, ValueError):
        return False


def _lock_score(p: dict) -> float:
    """Prefer published_lock_score, fall back to lock_score."""
    try:
        v = p.get("published_lock_score")
        if v is not None:
            return float(v)
    except (TypeError, ValueError):
        pass
    try:
        return float(p.get("lock_score") or 0)
    except (TypeError, ValueError):
        return 0.0


def _edge_pct(p: dict) -> float:
    try:
        return float(p.get("edge_percent") or 0)
    except (TypeError, ValueError):
        return 0.0


def _win_prob(p: dict) -> float:
    try:
        return float(p.get("win_probability") or 0)
    except (TypeError, ValueError):
        return 0.0


def _event_key(p: dict) -> str:
    return (p.get("canonical_event_id") or p.get("event_id") or p.get("event") or "").strip()


def compute_funnel(pool: List[dict], *, policy: ModePolicy,
                   requested_target: int) -> FeasibilityReport:
    """Compute the feasibility funnel for the requested policy + target.

    ``pool`` is the already canonicalized candidate pool the route
    passes to the optimizer — the caller is responsible for the
    canonical-publication gate + real-line filter first.  This
    function reports counts at each subsequent stage.
    """
    canonical_pool_count = len(pool or [])
    reasons: List[str] = []

    # Stage 1 — real book odds present (mandatory for any real parlay)
    real_line = [p for p in pool if _has_real_odds(p)]

    # Stage 2 — mode admission (lock floor + optional edge gate)
    mode_eligible = []
    edge_eligible_count = 0
    probability_eligible_count = 0
    for p in real_line:
        ls = _lock_score(p)
        if ls < policy.lock_floor:
            continue
        wp = _win_prob(p)
        if wp <= 0:
            continue
        probability_eligible_count += 1
        if policy.min_edge_pct is not None and _edge_pct(p) < policy.min_edge_pct:
            continue
        edge_eligible_count += 1
        mode_eligible.append(p)

    # Stage 3 — unique events + dependency-safe upper bound
    unique_events_set: Set[str] = set()
    for p in mode_eligible:
        k = _event_key(p)
        if k:
            unique_events_set.add(k)

    # Dependency-safe max legs = number of unique events (in dependent
    # sports we cap same-event to 1 leg; in independent sports we could
    # go higher, but conservatively use unique events as the ceiling).
    dep_safe_max = len(unique_events_set)

    # Market-family concentration (used for diagnostic)
    fam_counts: Counter = Counter(_market_family(p.get("market") or "") for p in mode_eligible)
    sport_counts: Counter = Counter((p.get("sport") or "") for p in mode_eligible)
    unique_families = len([f for f in fam_counts if f != "other"])

    # Family cap effect on max feasible legs:
    #   Sum of min(count_per_family, policy.max_same_market_family) for
    #   each family (plus ‘other’ counted as its own bucket)
    family_ceiling = sum(min(c, policy.max_same_market_family) for c in fam_counts.values())

    # Same-sport ceiling: with a soft cap ratio r, the largest sport
    # bucket that ratio allows for target T legs is floor(T * r).  Turn
    # that inside out: max_target = floor(largest_bucket / r).  But since
    # the user picks T, we compute for the requested target and record
    # if the ratio would be violated.
    max_feasible = min(dep_safe_max, family_ceiling, len(mode_eligible))

    # Classify status
    status = STATUS_READY

    if canonical_pool_count == 0:
        status = STATUS_INSUFFICIENT
        reasons.append(REASON_INSUFFICIENT_CANONICAL_LEGS)
    elif not real_line:
        status = STATUS_INSUFFICIENT
        reasons.append(REASON_NO_REAL_ODDS)
    elif not mode_eligible:
        status = STATUS_INSUFFICIENT
        reasons.append(REASON_MODE_THRESHOLD_STARVATION)
    elif max_feasible < policy.min_useful_legs:
        status = STATUS_INSUFFICIENT
        # Choose most explanatory reason
        if len(unique_events_set) < policy.min_useful_legs:
            reasons.append(REASON_INSUFFICIENT_UNIQUE_EVENTS)
        elif family_ceiling < policy.min_useful_legs:
            reasons.append(REASON_MARKET_CONCENTRATION_LIMIT)
        else:
            reasons.append(REASON_NO_SUPPORTED_COMBINATION)
    elif max_feasible < requested_target:
        status = STATUS_PARTIAL_ONLY
        # Non-fatal reasons — surface WHICH gate is squeezing the ticket
        if len(unique_events_set) < requested_target:
            reasons.append(REASON_INSUFFICIENT_UNIQUE_EVENTS)
        if family_ceiling < requested_target:
            reasons.append(REASON_MARKET_CONCENTRATION_LIMIT)

    return FeasibilityReport(
        mode_key=policy.key,
        requested_target=requested_target,
        min_useful=policy.min_useful_legs,
        max_feasible_legs=max_feasible,
        dependency_safe_max_legs=dep_safe_max,
        canonical_pool_count=canonical_pool_count,
        real_line_eligible_count=len(real_line),
        mode_eligible_count=len(mode_eligible),
        edge_eligible_count=edge_eligible_count,
        probability_eligible_count=probability_eligible_count,
        unique_events=len(unique_events_set),
        unique_market_families=unique_families,
        market_family_counts=dict(fam_counts),
        sport_counts=dict(sport_counts),
        status=status,
        reason_codes=reasons,
    )


__all__ = [
    "FeasibilityReport", "compute_funnel",
    "STATUS_READY", "STATUS_PARTIAL_ONLY", "STATUS_INSUFFICIENT",
    "REASON_INSUFFICIENT_CANONICAL_LEGS", "REASON_INSUFFICIENT_UNIQUE_EVENTS",
    "REASON_MODE_THRESHOLD_STARVATION", "REASON_DEPENDENCY_CONFLICT",
    "REASON_MARKET_CONCENTRATION_LIMIT", "REASON_NO_REAL_ODDS",
    "REASON_NO_SUPPORTED_COMBINATION",
]
