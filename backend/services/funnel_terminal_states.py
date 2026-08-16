"""Funnel Terminal-State Taxonomy — PHASE 5 FIX 2 (2026-06).

Single source of truth for the terminal states a candidate can reach.
Consumer analytics + Phase 5-style rejection-funnel reports MUST use
these strings so PROVIDER availability is never confused with model /
edge / display rejection.

Two orthogonal axes:

  Provider axis
  -------------
  PROVIDER_UNAVAILABLE
      The sportsbook provider offered ZERO usable real market rows
      for the event / window.  We literally had no line to price.
      This is the ONLY correct label when provider coverage is
      missing — do NOT use it for "we saw the line but rejected it".

  Perklocks evaluation axis
  --------------------------
  IDENTITY_UNRESOLVED         canonical player / team could not be resolved
  HISTORY_UNAVAILABLE         no evidence rows available for the entity
  INPUT_QUALITY_INSUFFICIENT  fewer than the required real signals present
  MODEL_UNAVAILABLE           the sport does not have an authoritative
                              model wired for this market (e.g. NBA game)
  BELOW_SCORE_THRESHOLD       provider row exists, model priced it, but
                              Lock Score < 85 board floor
  NO_POSITIVE_EDGE            devigged model probability <= book implied
  CANONICAL_REJECTED          rejected pre-publication by canonical
                              eligibility (no_bet / off_board / model_only /
                              missing book_odds)
  DISPLAY_REJECTED            board utility layer hid the pick from the
                              main board (EXTREME_JUICE / DISPLAY_LADDER_
                              SUPERSEDED / DISPLAY_CAPPED / DISPLAY_DEDUPED)
  VISIBLE                     survived to /picks/today.

Rule
----
PROVIDER_UNAVAILABLE ⇔ provider_row_count_for_query == 0.
Any candidate that reached evaluation (provider row was present) may
only receive one of the Perklocks-evaluation labels.  Never
PROVIDER_UNAVAILABLE.
"""
from __future__ import annotations


# Provider-axis
PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"

# Perklocks evaluation axis
IDENTITY_UNRESOLVED         = "IDENTITY_UNRESOLVED"
HISTORY_UNAVAILABLE         = "HISTORY_UNAVAILABLE"
INPUT_QUALITY_INSUFFICIENT  = "INPUT_QUALITY_INSUFFICIENT"
MODEL_UNAVAILABLE           = "MODEL_UNAVAILABLE"
BELOW_SCORE_THRESHOLD       = "BELOW_SCORE_THRESHOLD"
NO_POSITIVE_EDGE            = "NO_POSITIVE_EDGE"
CANONICAL_REJECTED          = "CANONICAL_REJECTED"
DISPLAY_REJECTED            = "DISPLAY_REJECTED"
VISIBLE                     = "VISIBLE"

VALID_TERMINAL_STATES: frozenset[str] = frozenset({
    PROVIDER_UNAVAILABLE,
    IDENTITY_UNRESOLVED,
    HISTORY_UNAVAILABLE,
    INPUT_QUALITY_INSUFFICIENT,
    MODEL_UNAVAILABLE,
    BELOW_SCORE_THRESHOLD,
    NO_POSITIVE_EDGE,
    CANONICAL_REJECTED,
    DISPLAY_REJECTED,
    VISIBLE,
})

# Labels that require the provider row to have been PRESENT.
# Using PROVIDER_UNAVAILABLE with any of these upstream is a bug.
_REQUIRES_PROVIDER_PRESENT: frozenset[str] = frozenset({
    IDENTITY_UNRESOLVED,
    HISTORY_UNAVAILABLE,
    INPUT_QUALITY_INSUFFICIENT,
    MODEL_UNAVAILABLE,
    BELOW_SCORE_THRESHOLD,
    NO_POSITIVE_EDGE,
    CANONICAL_REJECTED,
    DISPLAY_REJECTED,
    VISIBLE,
})


def classify_terminal_state(
    *,
    provider_row_present: bool,
    identity_resolved: bool = True,
    has_history: bool = True,
    input_quality_ok: bool = True,
    model_available: bool = True,
    lock_score: float | None = None,
    lock_floor: float = 85.0,
    positive_edge: bool = True,
    canonical_eligible: bool = True,
    display_visible: bool = True,
) -> str:
    """Return the correct terminal-state label for a candidate.

    Contract: if ``provider_row_present is False`` the ONLY valid
    return is PROVIDER_UNAVAILABLE.  Otherwise we walk the evaluation
    axis in canonical order.  This prevents the caller from accidentally
    tagging a below-threshold candidate as PROVIDER_UNAVAILABLE.
    """
    if not provider_row_present:
        return PROVIDER_UNAVAILABLE
    if not identity_resolved:
        return IDENTITY_UNRESOLVED
    if not has_history:
        return HISTORY_UNAVAILABLE
    if not input_quality_ok:
        return INPUT_QUALITY_INSUFFICIENT
    if not model_available:
        return MODEL_UNAVAILABLE
    if not positive_edge:
        return NO_POSITIVE_EDGE
    if lock_score is not None and lock_score < lock_floor:
        return BELOW_SCORE_THRESHOLD
    if not canonical_eligible:
        return CANONICAL_REJECTED
    if not display_visible:
        return DISPLAY_REJECTED
    return VISIBLE


__all__ = [
    "PROVIDER_UNAVAILABLE",
    "IDENTITY_UNRESOLVED",
    "HISTORY_UNAVAILABLE",
    "INPUT_QUALITY_INSUFFICIENT",
    "MODEL_UNAVAILABLE",
    "BELOW_SCORE_THRESHOLD",
    "NO_POSITIVE_EDGE",
    "CANONICAL_REJECTED",
    "DISPLAY_REJECTED",
    "VISIBLE",
    "VALID_TERMINAL_STATES",
    "classify_terminal_state",
]
