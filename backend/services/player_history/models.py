"""Player History — data contracts.

Every field that would ordinarily be a number MUST support ``None``
to preserve the "MISSING DATA != 0" invariant.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional, Any


class HistoryDirection(str, Enum):
    OVER = "over"
    UNDER = "under"
    MILESTONE = "milestone"   # e.g., 1+ Hit, 25+ yards, ATD


class MilestoneSemantics(str, Enum):
    """Distinguish product markets that mean ``>=`` from Over/Under
    (which mean strict ``>`` / ``<``)."""
    GTE = "gte"     # 25+ yards, 1+ hit, ATD
    GT  = "gt"      # Over 25.5


class DataQuality(str, Enum):
    HIGH         = "HIGH"
    MEDIUM       = "MEDIUM"
    LOW          = "LOW"
    INSUFFICIENT = "INSUFFICIENT"
    UNAVAILABLE  = "UNAVAILABLE"


@dataclass
class ThresholdResult:
    """Result of evaluating a set of actuals against ONE threshold.

    ``pushes`` are EXCLUDED from ``decisions`` (never counted as win
    or loss).  ``hit_rate = wins / decisions`` when decisions > 0.
    """
    wins:            int                = 0
    losses:          int                = 0
    pushes:          int                = 0
    decisions:       int                = 0
    sample_size:     int                = 0     # games with valid actual
    hit_rate:        Optional[float]    = None
    average_actual:  Optional[float]    = None
    actual_values:   list[float]        = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


@dataclass
class HistoryWindow:
    """Aggregate over a window of games (L5, L10, L20, season, home,
    away, vs_opponent, etc.).  Wraps a ThresholdResult."""
    label:    str
    result:   Optional[ThresholdResult] = None
    games_used: int   = 0
    games_requested: int = 0

    def to_dict(self) -> dict:
        return {
            "label":            self.label,
            "result":           self.result.to_dict() if self.result else None,
            "games_used":       self.games_used,
            "games_requested":  self.games_requested,
        }


@dataclass
class PlayerHistoryEvidence:
    """Universal history evidence contract (Phase 5.3 §3, §24).

    Every field that could carry uncertainty is ``Optional`` and
    defaults to ``None``.  Consumers MUST NOT treat ``None`` as 0.
    """
    # Identity block ────────────────────────────────────────────
    player_id:          Optional[str]  = None
    canonical_player_id: Optional[str] = None
    player_name:        Optional[str]  = None
    sport:              Optional[str]  = None
    historical_team:    Optional[str]  = None   # per most-recent game
    current_team:       Optional[str]  = None   # from candidate context
    identity_confidence: Optional[str] = None   # HIGH/MEDIUM/LOW/UNKNOWN

    # Request block ─────────────────────────────────────────────
    market:             Optional[str]  = None
    threshold:          Optional[float] = None
    direction:          Optional[str]  = None   # over/under/milestone
    milestone_semantics: Optional[str] = None   # gte / gt when milestone

    # Window results ────────────────────────────────────────────
    last_5:             Optional[dict] = None
    last_10:            Optional[dict] = None
    last_20:            Optional[dict] = None
    season:             Optional[dict] = None
    previous_season:    Optional[dict] = None
    career:             Optional[dict] = None
    home:               Optional[dict] = None
    away:               Optional[dict] = None
    vs_opponent:        Optional[dict] = None
    vs_opponent_recent: Optional[dict] = None
    exact_threshold:    Optional[dict] = None   # season hit-rate at requested threshold

    # Summary numbers ───────────────────────────────────────────
    recent_average:     Optional[float] = None    # L5 avg
    season_average:     Optional[float] = None
    streak:             Optional[str]   = None
    days_since_last_game: Optional[int] = None

    # Provenance / quality ──────────────────────────────────────
    source:             Optional[str]   = None
    source_timestamp:   Optional[str]   = None
    data_quality:       Optional[str]   = None
    games_requested:    Optional[int]   = None
    games_available:    Optional[int]   = None
    games_used:         Optional[int]   = None
    missing_games:      Optional[int]   = None

    # Publication-time freeze (Phase 5.3 §20, §21) ──────────────
    history_as_of:      Optional[str]   = None   # ISO timestamp cutoff

    # Sport-specific extras (never surfaced to Lock Score) ───────
    extras:             dict            = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


__all__ = [
    "HistoryDirection", "MilestoneSemantics", "DataQuality",
    "ThresholdResult", "HistoryWindow", "PlayerHistoryEvidence",
]
