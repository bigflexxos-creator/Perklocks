"""Parlay Mode Profiles (Phase 5, 2026-06-30).

Three modes tune the pool filters + leg targets:

  • Safe        — max survival, min variance. 2-3 legs.
  • Balanced    — best mix of lock/edge/consistency. 3-5 legs.
  • Aggressive  — swing for the fences without becoming a lottery. 5-8 legs.

Modes are additive on top of the existing high_risk / advanced / today
paths in `routes/parlay_routes.py`. They tune the ranking floor but do
NOT change any Monte Carlo simulator math.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict


@dataclass
class ModeProfile:
    name: str
    min_lock: float
    min_edge_pct: float
    min_win_probability: float
    min_parlay_score: float           # from LegRanking
    min_legs: int
    max_legs: int
    max_positive_correlations: int    # from CorrelationReport.positive_pairs
    max_negative_correlations: int    # from CorrelationReport.negative_pairs
    max_same_sport_ratio: float       # 0..1 cap, 1.0 = no cap
    label: str
    description: str

    def to_dict(self) -> dict:
        return asdict(self)


MODE_PROFILES = {
    "safe": ModeProfile(
        name="safe",
        min_lock=92.0,
        min_edge_pct=4.0,
        min_win_probability=68.0,
        min_parlay_score=70.0,
        min_legs=2,
        max_legs=3,
        max_positive_correlations=0,
        max_negative_correlations=0,
        max_same_sport_ratio=0.66,     # 2 of 3 legs same sport OK
        label="SAFE",
        description="Tight filters, minimum variance parlay.",
    ),
    "balanced": ModeProfile(
        name="balanced",
        min_lock=88.0,
        min_edge_pct=3.0,
        min_win_probability=60.0,
        min_parlay_score=58.0,
        min_legs=3,
        max_legs=5,
        max_positive_correlations=1,
        max_negative_correlations=0,
        max_same_sport_ratio=0.5,
        label="BALANCED",
        description="Best mix of confidence and payout.",
    ),
    "aggressive": ModeProfile(
        name="aggressive",
        min_lock=78.0,
        min_edge_pct=1.0,
        min_win_probability=45.0,
        min_parlay_score=42.0,
        min_legs=5,
        max_legs=8,
        max_positive_correlations=2,
        max_negative_correlations=1,
        max_same_sport_ratio=0.4,
        label="AGGRESSIVE",
        description="Bigger upside, higher variance.",
    ),
}


def resolve_mode(mode: str | None) -> str:
    """Map a raw mode string to one of {safe, balanced, aggressive}."""
    m = (mode or "").strip().lower()
    if m in MODE_PROFILES:
        return m
    if m in ("standard", "default", ""):
        return "balanced"
    if m in ("high_risk", "highrisk", "high-risk", "lottery"):
        return "aggressive"
    if m in ("today", "today_window", "1-5h"):
        return "safe"
    if m in ("advanced", "safer"):
        return "safe"
    if m in ("ev",):
        return "balanced"
    return "balanced"


def profile_for(mode: str | None) -> ModeProfile:
    return MODE_PROFILES[resolve_mode(mode)]


def leg_passes_profile(pick: dict, ranking, profile: ModeProfile) -> tuple[bool, str]:
    """Check hard eligibility of a candidate leg under a mode profile.

    `ranking` is a LegRanking (or dict) already computed for this pick."""
    lock = float(pick.get("lock_score") or pick.get("lock_score_v2") or 0)
    edge = float(pick.get("edge_percent") or 0)
    win_p = float(pick.get("win_probability") or 0)
    if lock < profile.min_lock:
        return False, f"lock {lock:.0f} < {profile.min_lock:.0f}"
    if edge < profile.min_edge_pct:
        return False, f"edge {edge:+.1f}% < {profile.min_edge_pct:+.1f}%"
    if win_p and win_p < profile.min_win_probability:
        return False, (f"win_p {win_p:.0f}% < "
                       f"{profile.min_win_probability:.0f}%")
    # Ranking floor
    score = getattr(ranking, "parlay_score", None)
    if score is None and isinstance(ranking, dict):
        score = ranking.get("parlay_score")
    if score is not None and float(score) < profile.min_parlay_score:
        return False, (f"parlay_score {score:.0f} < "
                       f"{profile.min_parlay_score:.0f}")
    return True, ""
