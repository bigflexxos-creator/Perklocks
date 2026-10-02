"""NHL 2.0 package — Preview-only challenger scaffold.

V1 (brain_sim_nhl) remains production champion.  V2 is published as
CHALLENGER with full provenance stamps; promotion gated by walk-
forward evaluation (see services/nhl_v2/evaluate.py).
"""
from __future__ import annotations

from services.nhl_v2.features import (
    FEATURE_CONTRACT_VERSION, MODEL_FAMILY,
    MatchupContext, PlayerContext,
    build_matchup_context, build_player_context,
)
from services.nhl_v2.models import (
    MODEL_VERSION, CALIBRATOR_VERSION,
    predict_ml, predict_puck_line, predict_total,
    predict_sog, predict_goals, predict_assists, predict_points,
)

__all__ = [
    "FEATURE_CONTRACT_VERSION", "MODEL_FAMILY",
    "MODEL_VERSION", "CALIBRATOR_VERSION",
    "MatchupContext", "PlayerContext",
    "build_matchup_context", "build_player_context",
    "predict_ml", "predict_puck_line", "predict_total",
    "predict_sog", "predict_goals", "predict_assists", "predict_points",
]
