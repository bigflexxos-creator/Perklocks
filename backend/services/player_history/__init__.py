"""Player History — Magic 2.0 Foundation (Phase 5.3 Stage 1).

Public entry point: ``services.player_history.get_player_history``.

Architecture:
    threshold_engine.py   — sport-agnostic Over/Under/Push/milestone math
    models.py             — PlayerHistoryEvidence, DataQuality, etc.
    service.py            — top-level dispatcher (routes by sport → adapter)
    mlb.py                — MLB adapter (Stage 1 - this session)
    nfl.py / nba.py / …   — deferred to Stage 2

DO NOT calculate Magic Score.  DO NOT touch Lock Score.
This is EVIDENCE for Magic 2.0 to consume later.
"""
from .service import get_player_history
from .models import (
    PlayerHistoryEvidence, ThresholdResult, DataQuality,
    HistoryDirection, MilestoneSemantics,
)
from .threshold_engine import (
    evaluate_threshold, evaluate_milestone, PUSH_TOLERANCE,
)

__all__ = [
    "get_player_history",
    "PlayerHistoryEvidence", "ThresholdResult", "DataQuality",
    "HistoryDirection", "MilestoneSemantics",
    "evaluate_threshold", "evaluate_milestone", "PUSH_TOLERANCE",
]
