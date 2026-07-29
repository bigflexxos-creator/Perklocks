"""Parlay Intelligence Engine (Phase 5, 2026-06-30).

Upgrades the existing Parlay tab with:
  1. Smart Leg Ranking (fused prob + model agreement + matchup grade +
     sample confidence + simulator confidence + historical perf +
     consistency).
  2. Correlation Engine (positive / negative / same-game / usage).
  3. Parlay Optimizer modes (Safe / Balanced / Aggressive).
  4. Parlay Backtester (historical parlay performance from
     `parlay_history`).
  5. Parlay Learning Loop (post-settlement failure attribution).

Existing UI is NOT changed. Sportsbook odds are NEVER used as prediction
inputs. Legacy simulator math is untouched.
"""
from __future__ import annotations

from .leg_ranker import (
    LegRanking,
    rank_leg,
    rank_legs,
    grade_from_score,
    risk_level_from_score,
)
from .correlation_engine import (
    CorrelationReport,
    analyze_correlations,
    pairwise_correlation,
    combine_with_guard,
)
from .parlay_modes import (
    MODE_PROFILES,
    ModeProfile,
    profile_for,
    resolve_mode,
)
from .parlay_backtester import (
    backtest_parlays,
    summarize_backtest,
)
from .learning_loop import (
    record_completed_parlay,
    get_leg_reliability,
    infer_failure_reason,
)

__all__ = [
    "LegRanking",
    "rank_leg",
    "rank_legs",
    "grade_from_score",
    "risk_level_from_score",
    "CorrelationReport",
    "analyze_correlations",
    "pairwise_correlation",
    "combine_with_guard",
    "MODE_PROFILES",
    "ModeProfile",
    "profile_for",
    "resolve_mode",
    "backtest_parlays",
    "summarize_backtest",
    "record_completed_parlay",
    "get_leg_reliability",
    "infer_failure_reason",
]
