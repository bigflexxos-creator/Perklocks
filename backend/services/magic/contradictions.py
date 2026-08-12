"""Magic Layer 2.0 — Contradiction / Risk Engine.

Detects when strong positive evidence is offset by meaningful risk.
Never averages contradictions away — the risk flag is EXPOSED so
downstream consumers can weight positive and negative signals
separately.

Emitted flags
─────────────
* HISTORICAL_STRONG_BUT_ROLE_REDUCED
* HISTORICAL_STRONG_BUT_NOT_STARTER
* MODEL_STRONG_BUT_ADVERSE_LINE_MOVEMENT
* FORM_STRONG_BUT_WEAK_UNDERLYING
* FINISHING_UNSUPPORTED_BY_SHOT_QUALITY
* INJURY_UNCERTAINTY
* SMALL_SAMPLE
* STALE_EVIDENCE
* CONFLICTING_AUTHORITATIVE_SOURCES
* MATCHUP_DIVERGES_FROM_HISTORICAL_CONTEXT
* IDENTITY_PROVISIONAL
* NO_REAL_MARKET
"""
from __future__ import annotations

import enum
from typing import Any, Optional

from services.magic.contract import EvidenceItem, Availability, EvidenceType


class RiskFlag(str, enum.Enum):
    HISTORICAL_STRONG_BUT_ROLE_REDUCED      = "HISTORICAL_STRONG_BUT_ROLE_REDUCED"
    HISTORICAL_STRONG_BUT_NOT_STARTER       = "HISTORICAL_STRONG_BUT_NOT_STARTER"
    MODEL_STRONG_BUT_ADVERSE_LINE_MOVEMENT  = "MODEL_STRONG_BUT_ADVERSE_LINE_MOVEMENT"
    FORM_STRONG_BUT_WEAK_UNDERLYING         = "FORM_STRONG_BUT_WEAK_UNDERLYING"
    FINISHING_UNSUPPORTED_BY_SHOT_QUALITY   = "FINISHING_UNSUPPORTED_BY_SHOT_QUALITY"
    INJURY_UNCERTAINTY                      = "INJURY_UNCERTAINTY"
    SMALL_SAMPLE                            = "SMALL_SAMPLE"
    STALE_EVIDENCE                          = "STALE_EVIDENCE"
    CONFLICTING_AUTHORITATIVE_SOURCES       = "CONFLICTING_AUTHORITATIVE_SOURCES"
    MATCHUP_DIVERGES_FROM_HISTORICAL_CONTEXT = "MATCHUP_DIVERGES_FROM_HISTORICAL_CONTEXT"
    IDENTITY_PROVISIONAL                    = "IDENTITY_PROVISIONAL"
    NO_REAL_MARKET                          = "NO_REAL_MARKET"


def _positive_hist_evidence(items: list[EvidenceItem]) -> Optional[EvidenceItem]:
    for it in items:
        if (it.evidence_type == EvidenceType.HISTORICAL_EXACT_THRESHOLD
                and it.availability in (Availability.AVAILABLE,
                                          Availability.PARTIAL)
                and it.value is not None
                and it.value >= 0.6):
            return it
    return None


def _by_type(items: list[EvidenceItem],
              t: EvidenceType) -> list[EvidenceItem]:
    return [it for it in items if it.evidence_type == t]


def detect_contradictions(
    *,
    evidence:            list[EvidenceItem],
    identity_class:      Optional[str] = None,
    no_real_book_line:   bool = False,
    line_movement_pts:   Optional[float] = None,
    starter_status:      Optional[str] = None,    # "STARTER"/"BENCH"/"RESERVE"/None
    minutes_projected:   Optional[float] = None,
    injury_probability:  Optional[float] = None,
    goals_over_xg_ratio: Optional[float] = None,
    model_probability:   Optional[float] = None,
    model_market_state:  Optional[str] = None,
    role_recently_reduced: bool = False,
    conflicting_sources: bool = False,
    matchup_divergence:  Optional[float] = None,
) -> list[str]:
    """Return a list of active risk flags (each is a ``RiskFlag`` value)."""
    flags: list[str] = []
    hist_positive = _positive_hist_evidence(evidence)

    if identity_class not in ("AUTHORITATIVE", "MAPPED"):
        flags.append(RiskFlag.IDENTITY_PROVISIONAL.value)

    if no_real_book_line:
        flags.append(RiskFlag.NO_REAL_MARKET.value)

    # Historical strong but role reduced / not starter.
    if hist_positive is not None:
        if role_recently_reduced:
            flags.append(RiskFlag.HISTORICAL_STRONG_BUT_ROLE_REDUCED.value)
        if starter_status and starter_status.upper() not in (
                "STARTER", "STARTING", "STARTING_PROJECTED"):
            flags.append(RiskFlag.HISTORICAL_STRONG_BUT_NOT_STARTER.value)

    # Adverse line movement against a strong model.
    if (model_probability is not None and model_probability >= 0.55
            and line_movement_pts is not None
            and line_movement_pts <= -2.0):
        flags.append(
            RiskFlag.MODEL_STRONG_BUT_ADVERSE_LINE_MOVEMENT.value)

    # Overperformance vs xG (finishing unsupported by shot quality).
    if (goals_over_xg_ratio is not None
            and float(goals_over_xg_ratio) >= 1.30):
        flags.append(
            RiskFlag.FINISHING_UNSUPPORTED_BY_SHOT_QUALITY.value)

    # Injury uncertainty (probability > 0.15).
    if injury_probability is not None and injury_probability > 0.15:
        flags.append(RiskFlag.INJURY_UNCERTAINTY.value)

    # Small sample.
    for it in evidence:
        if (it.evidence_type == EvidenceType.HISTORICAL_EXACT_THRESHOLD
                and it.sample_size is not None
                and it.sample_size < 5
                and it.availability != Availability.UNAVAILABLE):
            flags.append(RiskFlag.SMALL_SAMPLE.value)
            break

    # Stale evidence signal (evidence tagged STALE by an adapter).
    if any(it.availability == Availability.STALE for it in evidence):
        flags.append(RiskFlag.STALE_EVIDENCE.value)

    # Conflicting authoritative sources.
    if conflicting_sources or any(
            it.availability == Availability.CONTRADICTORY
            for it in evidence):
        flags.append(RiskFlag.CONFLICTING_AUTHORITATIVE_SOURCES.value)

    # Matchup divergence (adapter emits divergence >= 1.0 z-units).
    if matchup_divergence is not None and abs(matchup_divergence) >= 1.0:
        flags.append(
            RiskFlag.MATCHUP_DIVERGES_FROM_HISTORICAL_CONTEXT.value)

    # Return unique flags in order.
    seen = set()
    out: list[str] = []
    for f in flags:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


__all__ = ["RiskFlag", "detect_contradictions"]
