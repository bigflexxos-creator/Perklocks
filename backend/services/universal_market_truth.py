"""Universal Market Truth Contract — Phase C Root Closure (2026-06).

Enforces §§11-18 of the Root-Closure spec:

  §11 · UNIVERSAL GAME TOTAL O/U CONSERVATION
      For every MODEL_AVAILABLE game total: P(Over)+P(Under)+P(Push)=1
      ± tolerance from ONE shared distribution.

  §12 · UNIVERSAL PLAYER PROP O/U CONSERVATION
      Same as §11 for paired threshold player props.

  §13 · UNDER DIRECTIONAL EVIDENCE
      Evidence carries direction (OVER / UNDER / NEUTRAL).  Over-
      positive evidence cannot count positively for Under and vice
      versa.

  §14 · ALT-LINE MONOTONICITY
      For ONE frozen distribution:
          P(Over lower)  ≥  P(Over higher)
          P(Under lower) ≤  P(Under higher)
      (subject to legitimate discrete/push behaviour)

  §15 · UNIVERSAL PROBABILITY PROVENANCE WIRING
      Every published probability declares its true provenance;
      BOOK_IMPLIED_SEED cannot masquerade as independent evidence.

This module SHIPS the contract enforcement helpers.  Producers OPT-IN
via ``check_ou_conservation()`` / ``assert_alt_monotonicity()`` /
``stamp_provenance()``.  Callers that omit calls remain unaffected
by design — this is a policy library, not a runtime middleware, so
existing GREEN paths cannot regress.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from services.probability_units import (
    CAUSAL_INDEPENDENT, EMPIRICAL_INDEPENDENT,
    MODEL_CONDITIONED, MARKET_CONDITIONED,
    BOOK_ANCHORED, BOOK_IMPLIED_SEED,
    INDEPENDENT_PROVENANCE, MARKET_CONDITIONED_PROVENANCE,
    is_finite_number, to_fraction,
)


# ─────────────────────────────────────────────────────────────────────
# §13 — Directional evidence semantics
# ─────────────────────────────────────────────────────────────────────
DIRECTION_OVER    = "OVER"
DIRECTION_UNDER   = "UNDER"
DIRECTION_NEUTRAL = "NEUTRAL"

ALL_DIRECTIONS = frozenset({DIRECTION_OVER, DIRECTION_UNDER, DIRECTION_NEUTRAL})


@dataclass
class DirectionalFactor:
    """One evidence axis with explicit direction / strength / quality."""
    name: str
    direction: str = DIRECTION_NEUTRAL          # OVER / UNDER / NEUTRAL
    strength: float = 0.0                       # [0.0, 1.0] evidence weight
    quality: float = 0.0                        # [0.0, 1.0] source reliability
    provenance: Optional[str] = None            # from probability_units taxonomy
    value: Optional[float] = None               # raw factor value (audit only)


# ─────────────────────────────────────────────────────────────────────
# §11-12 — O/U conservation
# ─────────────────────────────────────────────────────────────────────
@dataclass
class OUConservationResult:
    valid: bool
    total: float
    tolerance: float
    error: Optional[str] = None
    distribution_id: Optional[str] = None
    distribution_version: Optional[str] = None
    p_over: Optional[float] = None
    p_under: Optional[float] = None
    p_push: Optional[float] = None
    is_pushable: bool = False


def check_ou_conservation(
    p_over,
    p_under,
    p_push=None,
    *,
    tolerance: float = 0.01,
    distribution_id: Optional[str] = None,
    distribution_version: Optional[str] = None,
) -> OUConservationResult:
    """Verify P(Over)+P(Under)+P(Push) = 1 ± tolerance for pushable
    markets, or P(Over)+P(Under) = 1 ± tolerance for non-pushable.

    ``p_push=None`` → non-pushable market.
    Values may be either fractions (0..1) or percentages (0..100); we
    coerce with ``services.probability_units.to_fraction``.
    """
    po = to_fraction(p_over)
    pu = to_fraction(p_under)
    pp = to_fraction(p_push) if p_push is not None else None
    if po is None or pu is None:
        return OUConservationResult(
            valid=False, total=math.nan, tolerance=tolerance,
            error="non_finite_probability",
            distribution_id=distribution_id,
            distribution_version=distribution_version,
        )
    if pp is None:
        total = po + pu
        pushable = False
    else:
        total = po + pu + pp
        pushable = True
    valid = abs(total - 1.0) <= tolerance
    return OUConservationResult(
        valid=valid, total=round(total, 6), tolerance=tolerance,
        error=None if valid else f"conservation_violation:total={total:.4f}",
        distribution_id=distribution_id,
        distribution_version=distribution_version,
        p_over=po, p_under=pu, p_push=pp, is_pushable=pushable,
    )


# ─────────────────────────────────────────────────────────────────────
# §14 — Alt-line monotonicity
# ─────────────────────────────────────────────────────────────────────
@dataclass
class AltLineRung:
    line: float
    p_over: float
    p_under: Optional[float] = None
    p_push: Optional[float] = None
    distribution_id: Optional[str] = None
    distribution_version: Optional[str] = None


@dataclass
class MonotonicityViolation:
    lower_line: float
    higher_line: float
    lower_p_over: float
    higher_p_over: float
    detail: str


def assert_alt_monotonicity(
    rungs: Iterable[AltLineRung],
    *,
    tolerance: float = 0.005,
) -> list[MonotonicityViolation]:
    """Validate a frozen distribution's alt ladder.  Returns the list
    of violations (empty list = PASS).

    Contract:
      P(Over line_low)  >=  P(Over line_high)  ± tolerance
      P(Under line_low) <=  P(Under line_high) ± tolerance

    Distributions must share the same ``distribution_id`` +
    ``distribution_version`` — a mixed-version ladder is a semantic
    error and returns a synthetic violation.
    """
    rungs_list = sorted(rungs, key=lambda r: r.line)
    if len(rungs_list) < 2:
        return []
    violations: list[MonotonicityViolation] = []
    # Distribution identity check.
    dids = {(r.distribution_id, r.distribution_version) for r in rungs_list}
    if len(dids) > 1:
        r0 = rungs_list[0]
        r1 = rungs_list[-1]
        violations.append(MonotonicityViolation(
            lower_line=r0.line, higher_line=r1.line,
            lower_p_over=r0.p_over, higher_p_over=r1.p_over,
            detail=f"mixed_distribution_ids:{dids}",
        ))
        return violations
    for i in range(len(rungs_list) - 1):
        low, high = rungs_list[i], rungs_list[i + 1]
        # P(Over) must be monotonically NON-INCREASING as line increases.
        if low.p_over + tolerance < high.p_over:
            violations.append(MonotonicityViolation(
                lower_line=low.line, higher_line=high.line,
                lower_p_over=low.p_over, higher_p_over=high.p_over,
                detail="p_over_increasing_with_higher_line",
            ))
        # P(Under) must be monotonically NON-DECREASING as line increases.
        if low.p_under is not None and high.p_under is not None:
            if low.p_under - tolerance > high.p_under:
                violations.append(MonotonicityViolation(
                    lower_line=low.line, higher_line=high.line,
                    lower_p_over=low.p_over, higher_p_over=high.p_over,
                    detail="p_under_decreasing_with_higher_line",
                ))
    return violations


# ─────────────────────────────────────────────────────────────────────
# §13 — Directional aggregation
# ─────────────────────────────────────────────────────────────────────
@dataclass
class DirectionalAggregation:
    selection_direction: str            # OVER / UNDER
    aligned_strength: float
    contradictory_strength: float
    neutral_strength: float
    aligned_count: int
    contradictory_count: int
    neutral_count: int
    contradictions: list[str] = field(default_factory=list)


def aggregate_directional_evidence(
    selection_direction: str,
    factors: Iterable[DirectionalFactor],
) -> DirectionalAggregation:
    """Aggregate evidence WITH RESPECT TO SELECTION DIRECTION.

    An Over-positive factor DOES NOT count as evidence for an Under
    selection — it becomes a contradiction (§13-15).  Symmetric for
    Under-positive factors against Over selections.

    ``aligned_strength`` counts only factors whose direction matches
    the selection.  ``contradictory_strength`` counts opposite-
    direction factors and populates ``contradictions``.  ``neutral``
    factors contribute to neither side.
    """
    sd = (selection_direction or "").upper()
    if sd not in (DIRECTION_OVER, DIRECTION_UNDER):
        return DirectionalAggregation(
            selection_direction=sd or DIRECTION_NEUTRAL,
            aligned_strength=0.0, contradictory_strength=0.0,
            neutral_strength=0.0, aligned_count=0,
            contradictory_count=0, neutral_count=0,
        )
    opposite = DIRECTION_UNDER if sd == DIRECTION_OVER else DIRECTION_OVER
    aligned_s = 0.0
    contra_s = 0.0
    neutral_s = 0.0
    aligned_n = contra_n = neutral_n = 0
    contras: list[str] = []
    for f in factors:
        d = (f.direction or "").upper()
        # Multiply by quality so unreliable evidence weighs less.
        s = max(0.0, min(1.0, float(f.strength or 0.0))) * max(
            0.0, min(1.0, float(f.quality or 0.0))
        )
        if d == sd:
            aligned_s += s
            aligned_n += 1
        elif d == opposite:
            contra_s += s
            contra_n += 1
            contras.append(f.name)
        else:
            neutral_s += s
            neutral_n += 1
    return DirectionalAggregation(
        selection_direction=sd,
        aligned_strength=round(aligned_s, 4),
        contradictory_strength=round(contra_s, 4),
        neutral_strength=round(neutral_s, 4),
        aligned_count=aligned_n,
        contradictory_count=contra_n,
        neutral_count=neutral_n,
        contradictions=contras,
    )


# ─────────────────────────────────────────────────────────────────────
# §15 — Provenance stamping helpers
# ─────────────────────────────────────────────────────────────────────
def stamp_provenance(
    pick: dict,
    *,
    probability_provenance: Optional[str] = None,
    distribution_id: Optional[str] = None,
    distribution_version: Optional[str] = None,
    evidence_sources: Optional[list[str]] = None,
) -> None:
    """Stamp truthful provenance on a pick in-place.  Idempotent —
    does not overwrite fields already stamped by an authoritative
    upstream writer.  Callers pass ONLY fields they can prove.
    """
    if probability_provenance and "probability_provenance" not in pick:
        pick["probability_provenance"] = probability_provenance
    if distribution_id and "distribution_id" not in pick:
        pick["distribution_id"] = distribution_id
    if distribution_version and "distribution_version" not in pick:
        pick["distribution_version"] = distribution_version
    if evidence_sources:
        existing = pick.setdefault("evidence_sources", [])
        if isinstance(existing, list):
            for s in evidence_sources:
                if s not in existing:
                    existing.append(s)


def is_market_conditioned(provenance: Optional[str]) -> bool:
    """True iff provenance implies the probability is market-anchored
    (§19).  Consumers use this to reject double-counting book-implied
    signals as independent evidence."""
    if not provenance:
        return False
    return provenance.upper() in MARKET_CONDITIONED_PROVENANCE


def is_independent(provenance: Optional[str]) -> bool:
    if not provenance:
        return False
    return provenance.upper() in INDEPENDENT_PROVENANCE


__all__ = [
    # §11-12
    "OUConservationResult", "check_ou_conservation",
    # §13
    "DIRECTION_OVER", "DIRECTION_UNDER", "DIRECTION_NEUTRAL",
    "ALL_DIRECTIONS", "DirectionalFactor",
    "DirectionalAggregation", "aggregate_directional_evidence",
    # §14
    "AltLineRung", "MonotonicityViolation",
    "assert_alt_monotonicity",
    # §15
    "stamp_provenance", "is_market_conditioned", "is_independent",
]
