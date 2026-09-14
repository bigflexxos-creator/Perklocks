"""
UNIVERSAL EVIDENCE AUTHORITY CONTRACT (2026-06 · P0–P26)
========================================================

Single semantic contract used by ALL mature in-scope sports to convert
their own sport-specific evidence into a shared, portable, progressive
Lock-Score ceiling that can legitimately reach:

    85–89  = Qualified          (moderate coverage)
    90–92  = Strong             (good coverage + multiple agreeing signals)
    93–95  = Elite              (strong reliability + history + convergence)
    96–98  = Rare exceptional   (high coverage + stable dist + no contradictions)
    99     = PEAK_NON_APEX      (near-complete coverage + all axes agree)
    100    = Apex               (handled by the separate Apex gate — never here)

SPORTS IN SCOPE:  MLB · NFL · CFB · SOCCER · TENNIS
OUT OF SCOPE:     NBA · NHL · UFC/MMA  (not touched by this pass)

────────────────────────────────────────────────────────────────
KEY DESIGN CONTRACTS
────────────────────────────────────────────────────────────────
1.  MISSING evidence is MISSING — it is NOT converted into an
    automatic 85-quality signal.  Every component either yields a
    real score in [0, 100] OR reports EvidenceValue.MISSING.
    Coverage is (available / required) — not "assume-adequate".

2.  Win Probability is NOT Lock Score.  WP is one of nine axes.
    Peak (99) does NOT require 99% WP.

3.  Existing sport-specific prediction models are FROZEN.  This
    module reads what those models already produce and reshapes
    it into the universal contract; it never rewrites WP, does
    not touch alt ladders, provider intake, canonical identity,
    thresholds, odds, or sportsbook lines.

4.  Apex 100 is preserved.  This module clamps to 99.  Apex is
    evaluated separately (services/magic/apex_gate.py).

5.  No fake 99s.  Legacy 98/99 manufacturers (edge≥8%, static
    magic bumps, name bonuses) contribute EVIDENCE — they never
    independently determine 98/99.

Author: main agent, 2026-06 Universal Evidence Authority pass.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional

# ─────────────────────────────────────────────────────────────────────
# 1. FEATURE FLAG — safe rollback (env-flippable, mutable set)
# ─────────────────────────────────────────────────────────────────────
UEA_ENABLED_SPORTS: set[str] = {
    "MLB",
    "NFL",     # both NFL_GAME and NFL_PLAYER — handoff decided by adapter
    "CFB",
    "SOCCER",
    "TENNIS",
    # NBA / NHL / UFC intentionally excluded.
}

UEA_VERSION = "uea.v1.2026-06"

# ─────────────────────────────────────────────────────────────────────
# 2. MISSING SEMANTICS — a first-class value, not a default
# ─────────────────────────────────────────────────────────────────────
class EvidenceStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    MISSING   = "MISSING"          # legitimately absent
    UNAVAILABLE = "UNAVAILABLE"    # provider/api couldn't produce
    NOT_APPLICABLE = "NOT_APPLICABLE"  # market family doesn't use this axis


@dataclass
class EvidenceValue:
    """A single evidence axis result.

    ``score`` is the raw axis quality in [0, 100] when
    ``status == AVAILABLE``.  For MISSING / UNAVAILABLE /
    NOT_APPLICABLE the ``score`` is None — the coverage calculation
    treats it as "not present" rather than "assume adequate".
    """
    status: EvidenceStatus = EvidenceStatus.MISSING
    score:  Optional[float] = None    # 0..100 when AVAILABLE
    detail: Optional[str]   = None    # short human-readable reason

    @property
    def available(self) -> bool:
        return self.status == EvidenceStatus.AVAILABLE and self.score is not None

    @classmethod
    def available_score(cls, score: float, detail: str = "") -> "EvidenceValue":
        return cls(EvidenceStatus.AVAILABLE, float(max(0.0, min(100.0, score))), detail)

    @classmethod
    def missing(cls, reason: str = "not_present") -> "EvidenceValue":
        return cls(EvidenceStatus.MISSING, None, reason)

    @classmethod
    def unavailable(cls, reason: str = "provider_unavailable") -> "EvidenceValue":
        return cls(EvidenceStatus.UNAVAILABLE, None, reason)

    @classmethod
    def not_applicable(cls, reason: str = "n/a for market") -> "EvidenceValue":
        return cls(EvidenceStatus.NOT_APPLICABLE, None, reason)


# ─────────────────────────────────────────────────────────────────────
# 3. THE CONTRACT — 9 axes, sport-agnostic semantics
# ─────────────────────────────────────────────────────────────────────
REQUIRED_AXES = (
    "model_probability",
    "prediction_reliability",
    "history_threshold_support",
    "matchup_role_support",
    "independent_convergence",
    "simulation_distribution_support",
    "data_quality",
    # evidence_coverage and contradictions are DERIVED, not axes.
)


@dataclass
class EvidenceAuthorityContract:
    model_probability:          EvidenceValue = field(default_factory=EvidenceValue.missing)
    prediction_reliability:     EvidenceValue = field(default_factory=EvidenceValue.missing)
    history_threshold_support:  EvidenceValue = field(default_factory=EvidenceValue.missing)
    matchup_role_support:       EvidenceValue = field(default_factory=EvidenceValue.missing)
    independent_convergence:    EvidenceValue = field(default_factory=EvidenceValue.missing)
    simulation_distribution_support: EvidenceValue = field(default_factory=EvidenceValue.missing)
    data_quality:               EvidenceValue = field(default_factory=EvidenceValue.missing)
    # These two are DERIVED at compute-time:
    evidence_coverage:  float = 0.0          # 0..1
    contradictions:     list[str] = field(default_factory=list)
    # Metadata:
    sport:              str = ""
    market_family:      str = ""
    adapter_version:    str = ""

    # -- helpers -----------------------------------------------------
    def axes(self) -> dict[str, EvidenceValue]:
        return {
            "model_probability":          self.model_probability,
            "prediction_reliability":     self.prediction_reliability,
            "history_threshold_support":  self.history_threshold_support,
            "matchup_role_support":       self.matchup_role_support,
            "independent_convergence":    self.independent_convergence,
            "simulation_distribution_support": self.simulation_distribution_support,
            "data_quality":               self.data_quality,
        }

    def available_axes(self) -> dict[str, EvidenceValue]:
        return {k: v for k, v in self.axes().items() if v.available}

    def missing_axes(self) -> list[str]:
        return [k for k, v in self.axes().items() if not v.available]

    def compute_coverage(self, required: Optional[list[str]] = None) -> float:
        """Coverage = available axes / applicable axes.  ``required``
        lets an adapter narrow the denominator for market families
        that legitimately don't use every axis (e.g. tennis without
        a distribution simulator counts NOT_APPLICABLE as neutral)."""
        axes = self.axes()
        req = required or [k for k, v in axes.items()
                            if v.status != EvidenceStatus.NOT_APPLICABLE]
        if not req:
            self.evidence_coverage = 0.0
            return 0.0
        avail = sum(1 for k in req if axes[k].available)
        self.evidence_coverage = round(avail / len(req), 4)
        return self.evidence_coverage


# ─────────────────────────────────────────────────────────────────────
# 4. AUTHORITY SCORER — progressive tier reachability with gates
# ─────────────────────────────────────────────────────────────────────
def _clamp(x: float, lo: float = 0.0, hi: float = 99.0) -> float:
    return max(lo, min(hi, x))


# Weights sum to 1.0.  Same shape as legacy BQ so the two authorities
# integrate cleanly; the KEY difference is that missing evidence here
# EXCLUDES the axis from the weighted mean AND is tracked in coverage,
# rather than folding a synthetic 85 back into the numerator.
UEA_WEIGHTS: dict[str, float] = {
    "model_probability":              0.30,
    "prediction_reliability":         0.15,
    "history_threshold_support":      0.13,
    "matchup_role_support":           0.11,
    "independent_convergence":        0.13,
    "simulation_distribution_support":0.10,
    "data_quality":                   0.08,
}
assert abs(sum(UEA_WEIGHTS.values()) - 1.0) < 1e-9


def _tier_cap_for_coverage(coverage: float,
                           strong_axes: int,
                           has_convergence: bool,
                           has_distribution: bool,
                           has_history_or_matchup: bool,
                           contradictions: int) -> float:
    """Progressive tier cap based on evidence coverage AND the
    presence of the specific axes each tier demands.

    P3 CONTRACT:
        85–89: moderate coverage allowed  (≥ 0.55 coverage)
        90–92: good coverage + ≥2 strong  (≥ 0.65 + strong_axes ≥ 2)
        93–95: strong reliability + history/context + convergence
                                             (≥ 0.72 + convergence)
        96–98: high coverage + strong convergence + stable dist
                                             (≥ 0.82 + all four hooks)
        99   : near-complete PEAK           (≥ 0.90 + zero contradictions)
    """
    # Contradictions collapse the cap hard.
    if contradictions >= 3:
        return 84.9      # below board floor — evidence too broken
    if contradictions >= 2:
        return 89.9      # cannot reach Strong

    if coverage >= 0.90 and strong_axes >= 5 and has_convergence \
            and has_distribution and has_history_or_matchup \
            and contradictions == 0:
        return 99.0
    if coverage >= 0.82 and strong_axes >= 4 and has_convergence \
            and has_distribution and has_history_or_matchup:
        return 98.0
    if coverage >= 0.72 and strong_axes >= 3 and has_convergence \
            and has_history_or_matchup:
        return 95.0
    if coverage >= 0.65 and strong_axes >= 2:
        return 92.0
    if coverage >= 0.55:
        return 89.0
    if coverage >= 0.40:
        return 87.0
    return 84.9              # below board — insufficient evidence


def compute_authority_score(
    contract: EvidenceAuthorityContract,
) -> dict[str, Any]:
    """Compute the UEA Lock-Score ceiling for a single pick.

    Returns a dict containing:
        ``ceiling``       — max Lock Score attainable (0–99)
        ``weighted_score``— raw weighted mean of available axes (0–100)
        ``coverage``      — 0..1
        ``strong_axes``   — count of axes ≥ 85
        ``contradictions``— list of contradiction strings
        ``components``    — {axis: {status, score}} audit trail
        ``tier_gate``     — which tier gate was applied
        ``peak_non_apex`` — bool (True iff 99 legitimately earned)

    A caller (``compute_lock_score``) can then use ``ceiling`` as:
        * LIFT for a valid strong composite that under-scored, OR
        * CEILING for a thin-evidence composite that over-scored.
    """
    axes = contract.axes()
    coverage = contract.compute_coverage()

    # Weighted mean over AVAILABLE axes only.
    num, den = 0.0, 0.0
    strong = 0
    comps_out: dict[str, dict[str, Any]] = {}
    for name, ev in axes.items():
        w = UEA_WEIGHTS.get(name, 0.0)
        if ev.available:
            num += w * ev.score
            den += w
            if ev.score >= 85.0:
                strong += 1
            comps_out[name] = {"status": ev.status.value,
                                "score":  round(ev.score, 2),
                                "detail": ev.detail or ""}
        else:
            comps_out[name] = {"status": ev.status.value,
                                "score":  None,
                                "detail": ev.detail or ""}

    weighted = round((num / den) if den > 0 else 0.0, 2)

    has_convergence = axes["independent_convergence"].available and \
        (axes["independent_convergence"].score or 0.0) >= 70.0
    has_distribution = axes["simulation_distribution_support"].available and \
        (axes["simulation_distribution_support"].score or 0.0) >= 70.0
    has_history_or_matchup = (
        (axes["history_threshold_support"].available
            and (axes["history_threshold_support"].score or 0.0) >= 70.0)
        or (axes["matchup_role_support"].available
            and (axes["matchup_role_support"].score or 0.0) >= 70.0)
    )
    tier_cap = _tier_cap_for_coverage(
        coverage,
        strong_axes=strong,
        has_convergence=has_convergence,
        has_distribution=has_distribution,
        has_history_or_matchup=has_history_or_matchup,
        contradictions=len(contract.contradictions),
    )

    ceiling = _clamp(min(weighted, tier_cap), 0.0, 99.0)
    peak = (tier_cap >= 99.0 and weighted >= 96.0
             and coverage >= 0.90 and len(contract.contradictions) == 0)

    return {
        "ceiling":         round(ceiling, 1),
        "weighted_score":  weighted,
        "coverage":        coverage,
        "strong_axes":     strong,
        "contradictions":  list(contract.contradictions),
        "components":      comps_out,
        "tier_gate":       tier_cap,
        "peak_non_apex":   bool(peak),
        "version":         UEA_VERSION,
        "sport":           contract.sport,
        "market_family":   contract.market_family,
    }


# ─────────────────────────────────────────────────────────────────────
# 5. UNIVERSAL PEAK_NON_APEX(99) CONTRACT
# ─────────────────────────────────────────────────────────────────────
def peak_non_apex_eligible(authority: Mapping[str, Any]) -> tuple[bool, str]:
    """Universal 99 eligibility — same rule for MLB / NFL / CFB /
    Soccer / Tennis.  Returns ``(True, "")`` when eligible, or
    ``(False, reason)`` otherwise.
    """
    if not authority:
        return False, "no_authority_block"
    cov = float(authority.get("coverage") or 0.0)
    strong = int(authority.get("strong_axes") or 0)
    contras = authority.get("contradictions") or []
    if cov < 0.90:
        return False, f"coverage_below_90:{cov:.2f}"
    if strong < 5:
        return False, f"strong_axes_below_5:{strong}"
    if contras:
        return False, f"contradictions:{len(contras)}"
    comps = authority.get("components") or {}
    axes = ("model_probability", "prediction_reliability",
            "history_threshold_support", "matchup_role_support",
            "independent_convergence", "simulation_distribution_support",
            "data_quality")
    for a in axes:
        c = comps.get(a) or {}
        if c.get("status") != "AVAILABLE":
            # matchup and history are alternatives — allow one to be
            # NOT_APPLICABLE if the other is strong.
            if a in ("history_threshold_support", "matchup_role_support"):
                other = ("matchup_role_support"
                          if a == "history_threshold_support"
                          else "history_threshold_support")
                other_c = comps.get(other) or {}
                if other_c.get("status") == "AVAILABLE" \
                        and (other_c.get("score") or 0) >= 85:
                    continue
            if a == "simulation_distribution_support":
                # Some tennis markets legitimately lack a simulator —
                # accept when convergence + history are both strong.
                if c.get("status") == "NOT_APPLICABLE":
                    conv = (comps.get("independent_convergence") or {})
                    hist = (comps.get("history_threshold_support") or {})
                    if (conv.get("score") or 0) >= 85 and \
                            (hist.get("score") or 0) >= 85:
                        continue
            return False, f"axis_missing:{a}"
    return True, ""


def enabled(sport: str) -> bool:
    return (sport or "").upper() in UEA_ENABLED_SPORTS


__all__ = [
    "UEA_ENABLED_SPORTS", "UEA_VERSION", "UEA_WEIGHTS",
    "EvidenceStatus", "EvidenceValue",
    "EvidenceAuthorityContract", "REQUIRED_AXES",
    "compute_authority_score",
    "peak_non_apex_eligible",
    "enabled",
]
