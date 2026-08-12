"""Magic Layer 2.0 — Shared Evidence Contract.

Every evidence item across every sport / market MUST conform to
``EvidenceItem``.  This is the single canonical shape so downstream
consumers see a UNIFORM record regardless of which adapter emitted it.

Rules (Session Magic-2.0)
─────────────────────────
* Availability is a first-class enum — NEVER coerced to a boolean.
* Missing data is ``UNAVAILABLE`` — never 0.  A stale-but-present
  value is ``STALE``.  Two authoritative sources disagreeing is
  ``CONTRADICTORY`` — the evidence is not silently averaged.
* Provenance (source, source_class, timestamp, sample_size) is
  MANDATORY when availability is AVAILABLE or PARTIAL.
* ``EvidenceItem.value`` is a raw comparable number when applicable
  (hit-rate, z-score, ratio, count).  A human-readable ``label``
  is optional.  The convergence layer NEVER interprets ``label``
  numerically.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


class Availability(str, enum.Enum):
    AVAILABLE      = "AVAILABLE"
    PARTIAL        = "PARTIAL"
    STALE          = "STALE"
    CONTRADICTORY  = "CONTRADICTORY"
    UNAVAILABLE    = "UNAVAILABLE"


class EvidenceType(str, enum.Enum):
    HISTORICAL_EXACT_THRESHOLD = "HISTORICAL_EXACT_THRESHOLD"
    RECENT_FORM                = "RECENT_FORM"
    ROLE_OPPORTUNITY           = "ROLE_OPPORTUNITY"
    MATCHUP                    = "MATCHUP"
    LINEUP_INJURY              = "LINEUP_INJURY"
    MODEL_PROBABILITY          = "MODEL_PROBABILITY"
    SIMULATOR_PROBABILITY      = "SIMULATOR_PROBABILITY"
    CALIBRATED_PROBABILITY     = "CALIBRATED_PROBABILITY"
    SPORTSBOOK_CONSENSUS       = "SPORTSBOOK_CONSENSUS"
    LINE_MOVEMENT              = "LINE_MOVEMENT"
    SURFACE_CONTEXT            = "SURFACE_CONTEXT"
    OPPONENT_STRENGTH          = "OPPONENT_STRENGTH"
    CLV                        = "CLV"


class MagicTier(str, enum.Enum):
    ALIGNED_STRONG      = "ALIGNED_STRONG"
    ALIGNED             = "ALIGNED"
    NEUTRAL             = "NEUTRAL"
    CONFLICTED          = "CONFLICTED"
    RISK_ELEVATED       = "RISK_ELEVATED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


@dataclass
class EvidenceItem:
    """Single canonical evidence record."""

    evidence_type: EvidenceType
    availability:  Availability

    sport:      str = ""
    league:     Optional[str] = None
    market:     Optional[str] = None
    selection:  Optional[str] = None
    line:       Optional[float] = None

    canonical_player_id: Optional[str] = None
    canonical_team_id:   Optional[str] = None

    value:      Optional[float] = None
    label:      Optional[str]   = None
    direction:  Optional[str]   = None   # "positive"/"negative"/"neutral"
    confidence: Optional[float] = None   # 0..1
    sample_size:  Optional[int]   = None
    time_window:  Optional[str]   = None

    source:       Optional[str] = None
    source_class: Optional[str] = None   # "authoritative"/"mapped"/…
    timestamp:    Optional[str] = None

    provenance:   dict[str, Any] = field(default_factory=dict)
    notes:        Optional[str] = None

    def __post_init__(self) -> None:
        if isinstance(self.evidence_type, str):
            self.evidence_type = EvidenceType(self.evidence_type)
        if isinstance(self.availability, str):
            self.availability = Availability(self.availability)
        if self.timestamp is None:
            self.timestamp = datetime.now(timezone.utc).isoformat(
                ).replace("+00:00", "Z")

    def to_dict(self) -> dict[str, Any]:
        out = {
            "evidence_type": self.evidence_type.value,
            "availability":  self.availability.value,
            "sport":         self.sport,
            "league":        self.league,
            "market":        self.market,
            "selection":     self.selection,
            "line":          self.line,
            "canonical_player_id": self.canonical_player_id,
            "canonical_team_id":   self.canonical_team_id,
            "value":         self.value,
            "label":         self.label,
            "direction":     self.direction,
            "confidence":    self.confidence,
            "sample_size":   self.sample_size,
            "time_window":   self.time_window,
            "source":        self.source,
            "source_class":  self.source_class,
            "timestamp":     self.timestamp,
            "provenance":    dict(self.provenance),
            "notes":         self.notes,
        }
        return out


def availability_from(value: Any, sample_size: Optional[int] = None,
                       min_sample: int = 5) -> Availability:
    """Small helper for adapters — derive Availability from a value +
    sample-size pair.  ``None`` value → UNAVAILABLE; low sample →
    PARTIAL; otherwise AVAILABLE."""
    if value is None:
        return Availability.UNAVAILABLE
    if sample_size is not None and sample_size < min_sample:
        return Availability.PARTIAL
    return Availability.AVAILABLE


@dataclass
class MagicOutput:
    """Compact, transparent per-candidate Magic bundle."""

    pick_id:     str
    sport:       str
    market:      Optional[str] = None
    selection:   Optional[str] = None
    line:        Optional[float] = None
    canonical_player_id: Optional[str] = None
    canonical_team_id:   Optional[str] = None
    identity_class:      Optional[str] = None

    magic_score: Optional[float] = None
    magic_tier:  MagicTier = MagicTier.INSUFFICIENT_EVIDENCE
    magic_score_available: bool = False

    evidence:            list[EvidenceItem] = field(default_factory=list)
    strongest_positive:  Optional[str] = None
    strongest_negative:  Optional[str] = None
    model_market_state:  Optional[str] = None
    risk_flags:          list[str] = field(default_factory=list)

    generated_at:        str = ""

    def __post_init__(self) -> None:
        if not self.generated_at:
            self.generated_at = datetime.now(timezone.utc).isoformat(
                ).replace("+00:00", "Z")

    def add(self, ev: EvidenceItem) -> None:
        self.evidence.append(ev)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pick_id":     self.pick_id,
            "sport":       self.sport,
            "market":      self.market,
            "selection":   self.selection,
            "line":        self.line,
            "canonical_player_id": self.canonical_player_id,
            "canonical_team_id":   self.canonical_team_id,
            "identity_class":      self.identity_class,
            "magic_score":         self.magic_score,
            "magic_tier":          self.magic_tier.value,
            "magic_score_available": self.magic_score_available,
            "model_market_state":  self.model_market_state,
            "risk_flags":          list(self.risk_flags),
            "strongest_positive":  self.strongest_positive,
            "strongest_negative":  self.strongest_negative,
            "evidence":            [e.to_dict() for e in self.evidence],
            "generated_at":        self.generated_at,
        }


__all__ = [
    "Availability", "EvidenceType", "MagicTier",
    "EvidenceItem", "MagicOutput", "availability_from",
]
