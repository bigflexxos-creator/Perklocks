"""Block 2B.1B — NFL candidate rejection funnel diagnostic.

Every NFL candidate that fails to make the board must be classifiable
by exact rejection stage per spec §28:

    NO_REAL_MARKET
    INVALID_SPORTSBOOK_LINE
    CANONICAL_EVENT_FAILURE
    CANONICAL_PLAYER_FAILURE
    STALE_ROSTER_OR_TEAM
    UNSUPPORTED_MARKET
    MISSING_EVIDENCE
    SEASON_TYPE_UNKNOWN
    SIMULATOR_FAILED
    LIFECYCLE_INVALID
    DUPLICATE
    CONTRADICTION_RISK_RULE
    LOCK_SCORE_BELOW_BOARD_THRESHOLD
    CONSUMER_SPECIFIC_INELIGIBILITY

This module is a thin adapter over ``services.pipeline_diagnostic``
so NFL-specific rejection reasons are named consistently across the
runtime.
"""
from __future__ import annotations

import enum
from typing import Optional


class NFLRejectionStage(str, enum.Enum):
    NO_REAL_MARKET                        = "NO_REAL_MARKET"
    INVALID_SPORTSBOOK_LINE               = "INVALID_SPORTSBOOK_LINE"
    CANONICAL_EVENT_FAILURE               = "CANONICAL_EVENT_FAILURE"
    CANONICAL_PLAYER_FAILURE              = "CANONICAL_PLAYER_FAILURE"
    STALE_ROSTER_OR_TEAM                  = "STALE_ROSTER_OR_TEAM"
    UNSUPPORTED_MARKET                    = "UNSUPPORTED_MARKET"
    MISSING_EVIDENCE                      = "MISSING_EVIDENCE"
    SEASON_TYPE_UNKNOWN                   = "SEASON_TYPE_UNKNOWN"
    SIMULATOR_FAILED                      = "SIMULATOR_FAILED"
    LIFECYCLE_INVALID                     = "LIFECYCLE_INVALID"
    DUPLICATE                             = "DUPLICATE"
    CONTRADICTION_RISK_RULE               = "CONTRADICTION_RISK_RULE"
    LOCK_SCORE_BELOW_BOARD_THRESHOLD      = "LOCK_SCORE_BELOW_BOARD_THRESHOLD"
    CONSUMER_SPECIFIC_INELIGIBILITY       = "CONSUMER_SPECIFIC_INELIGIBILITY"


def record_nfl_rejection(
    *,
    stage: NFLRejectionStage,
    market: Optional[str] = None,
    player: Optional[str] = None,
    detail: Optional[str] = None,
) -> None:
    """Log an NFL candidate rejection via the shared pipeline
    diagnostic sink.  Safe to call from within the sync
    ``_props_picks_from_event`` branch.
    """
    try:
        from services.pipeline_diagnostic import log_reason
    except Exception:
        return
    reason = stage.value if isinstance(stage, NFLRejectionStage) else str(stage)
    if detail:
        reason = f"{reason}::{detail}"
    try:
        log_reason(sport="NFL", market=market, player=player,
                    reason=reason)
    except Exception:
        pass


def classify_from_sim_output(sim_output: dict) -> Optional[NFLRejectionStage]:
    """Return the appropriate rejection stage for a Platinum
    simulator ``ran=False`` output (per §16 failure contract), or
    None when the simulator ran successfully."""
    if not isinstance(sim_output, dict):
        return NFLRejectionStage.SIMULATOR_FAILED
    if sim_output.get("ran"):
        return None
    r = str(sim_output.get("reason") or "").upper()
    if r == "SEASON_TYPE_UNKNOWN":
        return NFLRejectionStage.SEASON_TYPE_UNKNOWN
    if r == "UNSUPPORTED_MARKET" or r == "UNSUPPORTED_PLAYER_MARKET":
        return NFLRejectionStage.UNSUPPORTED_MARKET
    if r in ("MISSING_EXPECTED_MARGIN", "MISSING_TOTAL_LINE",
              "MISSING_OPPORTUNITY", "MISSING_LINE",
              "MISSING_TOTAL_LINE_ON_PICK"):
        return NFLRejectionStage.MISSING_EVIDENCE
    if r in ("WRONG_SPORT", ):
        return NFLRejectionStage.CANONICAL_EVENT_FAILURE
    return NFLRejectionStage.SIMULATOR_FAILED


__all__ = [
    "NFLRejectionStage",
    "record_nfl_rejection",
    "classify_from_sim_output",
]
