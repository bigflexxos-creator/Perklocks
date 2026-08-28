"""Universal Trend Contract — Strategy Lab 10X §4.

One reusable schema so MLB / NFL / NBA trend radars never diverge.
SHADOW_SIGNAL provenance is enforced — trends are RESEARCH_ONLY. They
NEVER modify Lock math or publication.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


class TrendType(str, Enum):
    # MLB (multi-hit / power / discipline)
    HOT_CONFIRMED = "HOT_CONFIRMED"
    BREAKOUT = "BREAKOUT"
    POSITIVE_REGRESSION = "POSITIVE_REGRESSION"
    OVERPERFORMING = "OVERPERFORMING"
    ROLE_DECLINE = "ROLE_DECLINE"
    NEUTRAL = "NEUTRAL"
    # NFL (opportunity)
    ROLE_BREAKOUT = "ROLE_BREAKOUT"
    TARGET_SURGE = "TARGET_SURGE"
    ROUTE_SURGE = "ROUTE_SURGE"
    RUSH_VOLUME_SURGE = "RUSH_VOLUME_SURGE"
    RED_ZONE_SURGE = "RED_ZONE_SURGE"
    TD_OPPORTUNITY = "TD_OPPORTUNITY"
    # NBA (usage/pace)
    SCORING_SURGE = "SCORING_SURGE"
    USAGE_BREAKOUT = "USAGE_BREAKOUT"
    MINUTES_INCREASE = "MINUTES_INCREASE"
    PLAYMAKING_SURGE = "PLAYMAKING_SURGE"
    REBOUND_OPPORTUNITY = "REBOUND_OPPORTUNITY"
    THREE_POINT_VOLUME_SURGE = "THREE_POINT_VOLUME_SURGE"


class TrendDirection(str, Enum):
    OVER = "OVER"
    UNDER = "UNDER"
    NEUTRAL = "NEUTRAL"


class TrendStrength(str, Enum):
    STRONG = "STRONG"
    MODERATE = "MODERATE"
    WEAK = "WEAK"


class TrendDataQuality(str, Enum):
    FULL = "FULL"
    STRONG = "STRONG"
    PARTIAL = "PARTIAL"
    PRIOR_ONLY = "PRIOR_ONLY"


@dataclass
class TrendSignal:
    """The canonical trend row. RESEARCH_ONLY.

    A TrendSignal is a *hypothesis* about a subject/market — it never
    changes any Lock math. Consumers may show it on the Lab workstation
    and label it as SHADOW-flavored context.
    """
    sport: str                           # MLB | NFL | NBA
    event_id: str | None
    player_id: str | None
    subject: str                         # display name
    trend_type: TrendType
    market_relevance: list[str] = field(default_factory=list)  # e.g. ["Over 0.5 Hits"]
    direction: TrendDirection = TrendDirection.NEUTRAL
    strength: TrendStrength = TrendStrength.MODERATE
    confidence: float = 0.5              # 0..1
    supporting_features: list[str] = field(default_factory=list)
    contradicting_features: list[str] = field(default_factory=list)
    sample_size: int = 0
    data_quality: TrendDataQuality = TrendDataQuality.PARTIAL
    observed_at: str = ""
    provenance: str = "SHADOW_SIGNAL"    # never FACTUAL — RESEARCH_ONLY
    extra: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["trend_type"] = self.trend_type.value
        d["direction"] = self.direction.value
        d["strength"] = self.strength.value
        d["data_quality"] = self.data_quality.value
        return d
