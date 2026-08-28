"""Canonical Research Contract — Strategy Lab 10X.

This is the ONE and ONLY schema for research data that flows through the
Lab, into existing production models (FACTUAL only), and back to the UI.

Design invariants (HARD FREEZE — do not modify without user approval):

* `provenance` explicitly tags every observation as FACTUAL or
  SHADOW_SIGNAL. FACTUAL rows are permitted to seed existing production
  model contexts. SHADOW_SIGNAL rows are UI-only and must never influence
  Lock scores, Rollover, Parlay, or any published pick math.

* `quality` reports the reliability of a fact: FULL / STRONG / PARTIAL /
  PRIOR_ONLY / MISSING — matches the universal simulator provenance ladder
  (services/simulator_provenance.py). Consumers respect the ladder — a
  PRIOR_ONLY fact cannot outrank a FULL fact.

* `freshness_sec` tracks data staleness in seconds. Consumers may reject
  facts older than a per-sport threshold when the workstation is used
  for near-real-time pricing.

* `sample_size` is preserved for every observation so downstream
  confidence intervals / Wilson bounds can be computed honestly.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


class ResearchProvenance(str, Enum):
    FACTUAL = "FACTUAL"            # real provider-verified fact
    SHADOW_SIGNAL = "SHADOW_SIGNAL"  # experimental / learned — UI-only


class ResearchQuality(str, Enum):
    FULL = "FULL"
    STRONG = "STRONG"
    PARTIAL = "PARTIAL"
    PRIOR_ONLY = "PRIOR_ONLY"
    MISSING = "MISSING"


class ResearchSection(str, Enum):
    """Which workstation panel this fact belongs to."""
    OPPORTUNITY = "OPPORTUNITY"      # role/usage/carries/targets/minutes
    FORM = "FORM"                    # rolling recent-form windows
    MATCHUP = "MATCHUP"              # opponent defensive posture, H2H
    STATCAST = "STATCAST"            # MLB Statcast/xBA/barrel/hard-hit
    LINEUP = "LINEUP"                # MLB batting order, injury notes
    PITCHER = "PITCHER"              # MLB pitcher form/handedness/K
    PACE = "PACE"                    # NBA pace/tempo
    RED_ZONE = "RED_ZONE"            # NFL red-zone / opportunity share
    DISTRIBUTION = "DISTRIBUTION"    # fair-price / line explorer
    CALIBRATION = "CALIBRATION"      # historical reliability
    PATTERN = "PATTERN"              # discovered patterns (SHADOW)
    OTHER = "OTHER"


@dataclass
class ResearchFact:
    """One canonical research observation.

    `value` is a scalar-or-record — the shape depends on `key`. Consumers
    that thread facts into models look up specific `key` strings; the UI
    renders `label` + `value` + `sample_size` generically.
    """
    key: str
    label: str
    value: Any
    section: ResearchSection = ResearchSection.OTHER
    provenance: ResearchProvenance = ResearchProvenance.FACTUAL
    quality: ResearchQuality = ResearchQuality.FULL
    sample_size: int | None = None
    freshness_sec: int | None = None
    source: str | None = None
    unit: str | None = None
    positive_direction: str | None = None  # "over" | "under" | None
    tags: list[str] = field(default_factory=list)
    extra: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["section"] = self.section.value
        d["provenance"] = self.provenance.value
        d["quality"] = self.quality.value
        return d


@dataclass
class ResearchShadowSignal:
    """A discovered / learned signal. UI-only. Never fed to Lock math."""
    key: str
    label: str
    description: str
    hits: int
    n: int
    hit_rate: float
    wilson_lower: float
    strength: str  # "strong" | "moderate" | "weak"
    tags: list[str] = field(default_factory=list)
    provenance: ResearchProvenance = ResearchProvenance.SHADOW_SIGNAL
    extra: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["provenance"] = self.provenance.value
        return d


@dataclass
class CanonicalResearchSnapshot:
    """Aggregated research snapshot for a single sport / event / subject.

    A snapshot is READ-ONLY. It is built from existing production data
    (feature engines, canonical history, provider caches) — no new
    provider dependencies.
    """
    sport: str
    generated_at: str
    generation_version: str = "research.v1"
    subject: str | None = None          # player_name, team, or event
    event_id: str | None = None
    event_label: str | None = None
    facts: list[ResearchFact] = field(default_factory=list)
    shadow: list[ResearchShadowSignal] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    # Distribution & calibration payloads (arbitrary JSON — the UI renders
    # them; the model never reads them directly).
    distribution: dict[str, Any] | None = None
    calibration: dict[str, Any] | None = None
    matchup_dna: dict[str, Any] | None = None

    def factual_facts(self) -> list[ResearchFact]:
        return [f for f in self.facts if f.provenance == ResearchProvenance.FACTUAL]

    def shadow_facts(self) -> list[ResearchFact]:
        return [f for f in self.facts if f.provenance == ResearchProvenance.SHADOW_SIGNAL]

    def to_ctx(self) -> dict[str, Any]:
        """Return a dict safe to thread into an existing production
        model context. Only FACTUAL facts are included."""
        return {
            f.key: f.value
            for f in self.facts
            if f.provenance == ResearchProvenance.FACTUAL
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "sport": self.sport,
            "generated_at": self.generated_at,
            "generation_version": self.generation_version,
            "subject": self.subject,
            "event_id": self.event_id,
            "event_label": self.event_label,
            "facts": [f.to_dict() for f in self.facts],
            "shadow": [s.to_dict() for s in self.shadow],
            "notes": self.notes,
            "distribution": self.distribution,
            "calibration": self.calibration,
            "matchup_dna": self.matchup_dna,
            "factual_count": sum(1 for f in self.facts
                                 if f.provenance == ResearchProvenance.FACTUAL),
            "shadow_count": (
                sum(1 for f in self.facts
                    if f.provenance == ResearchProvenance.SHADOW_SIGNAL)
                + len(self.shadow)
            ),
        }
