"""Shared dataclasses for the Player Prop Intelligence System."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Archetype(str, Enum):
    """5-way soccer player archetype classification.

    Standard football analytics thresholds calibrated to top-5 leagues
    over 2019-2026 (Understat / FBref).
    """
    GOAL_SCORER      = "goal_scorer"       # G/90 ≥ 0.35, A/90 < 0.25
    CREATOR          = "creator"           # A/90 ≥ 0.25, G/90 < 0.20
    DUAL_THREAT      = "dual_threat"       # G/90 ≥ 0.25 AND A/90 ≥ 0.20
    PLAYMAKER        = "playmaker"         # A/90 ≥ 0.15 AND KP/90 ≥ 2.0
    LOW_INVOLVEMENT  = "low_involvement"   # G/90 < 0.15 AND A/90 < 0.15
    UNKNOWN          = "unknown"           # insufficient data

    def display(self) -> str:
        return {
            "goal_scorer":     "Goal Scorer",
            "creator":         "Creator",
            "dual_threat":     "Dual Threat",
            "playmaker":       "Playmaker",
            "low_involvement": "Low Involvement",
            "unknown":         "Unknown",
        }[self.value]


@dataclass
class PlayerStats:
    """Unified season-to-date statistics for a soccer player.

    Populated by `stats_aggregator.get_player_stats()` which merges
    across ESPN MLS, Understat form (Big-5), Wikipedia leaderboards.
    """
    player_name: str
    name_norm: str
    team: str = ""
    league: str = ""
    season: int = 0

    # Volume
    games: int = 0
    minutes: int = 0

    # Attack output
    goals: int = 0
    assists: int = 0

    # Per-90 (preferred — fair across low-minute subs)
    goals_per_90: float = 0.0
    assists_per_90: float = 0.0
    shots_per_90: float = 0.0
    key_passes_per_90: float = 0.0   # a.k.a. chances created / 90
    npxg_per_90: float = 0.0         # non-penalty xG / 90

    # Form (0-100, 50 = neutral)
    form_score: float = 50.0
    form_label: str = "NEUTRAL"

    # Optional position hint from source ("F", "M", "D", "GK" or mixed like "F M")
    position: str = ""

    # Data provenance for rationale display
    source: str = ""             # "espn_mls" | "understat" | "wiki" | "merged"
    data_ok: bool = True         # False → skip in models

    # Derived
    def games_effective(self) -> int:
        """Effective sample size for per-match rate fallback."""
        return max(self.games, self.minutes // 60 if self.minutes else 0, 1)

    def gpm(self) -> float:
        """Goals per match (fallback when per-90 missing)."""
        g = self.games_effective()
        return self.goals / g if g else 0.0

    def apm(self) -> float:
        """Assists per match."""
        g = self.games_effective()
        return self.assists / g if g else 0.0

    def is_attacker(self) -> bool:
        """Position-based heuristic — forwards + wingers + attacking mids."""
        pos = (self.position or "").upper()
        if not pos:
            # No position → infer from output.
            return (self.goals_per_90 >= 0.15
                    or self.assists_per_90 >= 0.15
                    or self.gpm() >= 0.15)
        # Common tags: F, S, W, AM, CF, LW, RW, ATT
        atk = ("F", "S", "W", "AM", "CF", "LW", "RW", "ATT")
        return any(tag in pos for tag in atk)


@dataclass
class MatchupSplit:
    """Aggregated per-opponent history (from mls_player_matchup_history
    or wired H2H sources).
    """
    opponent: str = ""
    matches: int = 0
    goals: int = 0
    assists: int = 0
    scored_matches: int = 0     # matches with ≥1 goal
    assist_matches: int = 0     # matches with ≥1 assist
    shots: int = 0

    def gpm(self) -> float:
        return self.goals / self.matches if self.matches else 0.0

    def apm(self) -> float:
        return self.assists / self.matches if self.matches else 0.0

    def score_rate(self) -> float:
        return self.scored_matches / self.matches if self.matches else 0.0

    def assist_rate(self) -> float:
        return self.assist_matches / self.matches if self.matches else 0.0

    def gi_rate(self) -> float:
        """Combined 'goal or assist' rate (upper bound on independent OR)."""
        if not self.matches:
            return 0.0
        # If we don't have combined-match count, approximate by union.
        p_g = self.score_rate()
        p_a = self.assist_rate()
        return min(1.0, p_g + p_a - p_g * p_a)


@dataclass
class PickRecommendation:
    """Output of a market model."""
    market: str                        # e.g. "anytime_goal_scorer"
    player_name: str
    probability: float                 # 0-1, per-match "anytime" prob
    confidence: str                    # "HIGH" | "MEDIUM" | "LOW"
    archetype: Archetype
    data_ok: bool = True
    evidence: list[str] = field(default_factory=list)
    concerns: list[str] = field(default_factory=list)
    debug: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "market": self.market,
            "player": self.player_name,
            "probability": round(self.probability, 4),
            "confidence": self.confidence,
            "archetype": self.archetype.value,
            "archetype_display": self.archetype.display(),
            "data_ok": self.data_ok,
            "evidence": self.evidence,
            "concerns": self.concerns,
            "debug": self.debug,
        }
