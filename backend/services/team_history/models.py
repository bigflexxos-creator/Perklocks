"""Team History — data contracts (Phase 5.3 Stage 3).

Every numeric field is Optional and defaults to ``None``.  Missing
data NEVER becomes zero (§17).  Legitimate zero (0-0 draw / 0-run
shutout) is preserved as ``0`` — the distinction is essential.

Historical team identity is stored separately from
current-event/current-market identity (§12) — a team appearing in
history NEVER proves current participation.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional


class TeamHistoryStatus(str, Enum):
    OK               = "OK"
    NOT_APPLICABLE   = "NOT_APPLICABLE"     # e.g. Tennis / UFC
    UNAVAILABLE      = "UNAVAILABLE"
    IDENTITY_UNRESOLVED = "TEAM_IDENTITY_UNRESOLVED"
    OPPONENT_IDENTITY_UNRESOLVED = "OPPONENT_IDENTITY_UNRESOLVED"


class TeamHistoryQuality(str, Enum):
    HIGH         = "HIGH"
    MEDIUM       = "MEDIUM"
    LOW          = "LOW"
    INSUFFICIENT = "INSUFFICIENT"
    UNAVAILABLE  = "UNAVAILABLE"


@dataclass
class TeamHistoryWindow:
    """Aggregation over a window of team games."""
    label:              str
    sample_size:        int                = 0
    wins:               int                = 0
    losses:             int                = 0
    draws:              int                = 0     # Soccer / CFB
    ot_losses:          int                = 0     # NHL
    scored_avg:         Optional[float]    = None
    conceded_avg:       Optional[float]    = None
    scored_q25:         Optional[float]    = None
    scored_median:      Optional[float]    = None
    scored_q75:         Optional[float]    = None
    scored_variance:    Optional[float]    = None
    conceded_q25:       Optional[float]    = None
    conceded_median:    Optional[float]    = None
    conceded_q75:       Optional[float]    = None
    conceded_variance:  Optional[float]    = None
    total_avg:          Optional[float]    = None    # scored + conceded
    scored_values:      list[float]        = field(default_factory=list)
    conceded_values:    list[float]        = field(default_factory=list)
    date_range:         Optional[tuple[str, str]] = None
    seasons:            list[int]          = field(default_factory=list)
    events_requested:   Optional[int]      = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class H2HResult:
    """Head-to-head aggregate — team_side is always the perspective
    of the queried team (§8, §9)."""
    canonical_team_id:      Optional[str] = None
    canonical_opponent_id:  Optional[str] = None
    sample_size:            int           = 0
    wins:                   int           = 0
    losses:                 int           = 0
    draws:                  int           = 0
    ot_losses:              int           = 0
    events:                 list[dict]    = field(default_factory=list)
    scored_avg:             Optional[float] = None
    conceded_avg:           Optional[float] = None
    scored_median:          Optional[float] = None
    conceded_median:        Optional[float] = None
    scored_variance:        Optional[float] = None
    conceded_variance:      Optional[float] = None
    seasons:                list[int]     = field(default_factory=list)
    competitions:           list[str]     = field(default_factory=list)
    home_events:            int           = 0
    away_events:            int           = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TeamHistoryEvidence:
    """Universal team-history evidence contract."""
    # Identity block ────────────────────────────────────────────
    canonical_team_id:      Optional[str] = None
    canonical_opponent_id:  Optional[str] = None
    team_name:              Optional[str] = None
    sport:                  Optional[str] = None
    league:                 Optional[str] = None
    identity_confidence:    Optional[str] = None    # HIGH / MEDIUM / LOW / UNKNOWN

    # Request block ─────────────────────────────────────────────
    metric:                 Optional[str] = None
    home_away:              Optional[str] = None
    competition:            Optional[str] = None
    as_of:                  Optional[str] = None    # ISO cutoff

    # Windows ───────────────────────────────────────────────────
    last_5:                 Optional[dict] = None
    last_10:                Optional[dict] = None
    last_20:                Optional[dict] = None
    season:                 Optional[dict] = None
    previous_season:        Optional[dict] = None
    multi_season:           Optional[dict] = None
    home:                   Optional[dict] = None
    away:                   Optional[dict] = None
    h2h:                    Optional[dict] = None

    # Provenance / quality ──────────────────────────────────────
    source:                 Optional[str] = None
    source_timestamp:       Optional[str] = None
    events_requested:       Optional[int] = None
    events_available:       Optional[int] = None
    events_used:            Optional[int] = None
    missing_events:         Optional[int] = None
    data_quality:           Optional[str] = None
    status:                 str = TeamHistoryStatus.OK.value

    # Sport-specific extras ────────────────────────────────────
    extras:                 dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


__all__ = [
    "TeamHistoryEvidence",
    "TeamHistoryWindow",
    "TeamHistoryStatus",
    "TeamHistoryQuality",
    "H2HResult",
]
