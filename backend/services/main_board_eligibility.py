"""Central authoritative eligibility contract for the main Locks board.

Phase-1 (2026-08-08) contract:
    FINAL LOCK SCORE > 85 = eligible
    FINAL LOCK SCORE <= 85 = NOT eligible

An exact score of 85 must NOT appear on the main Locks board.

We use an epsilon-adjusted `$gte` floor (85.01) so the constant plugs
cleanly into existing Mongo queries and Python comparisons without a
`$gt` refactor.  Because Lock Score is quantised to one decimal place
by `sports_engine._grade` / `_confidence`, 85.01 is the smallest
representable value strictly greater than 85 on the board.

DO NOT change the underlying Lock Score formula from this module —
this module only enforces board eligibility, not scoring.
"""
from __future__ import annotations


# The "strict >85" contract expressed as a >= threshold.  A pick at
# exactly 85.0 falls BELOW this; a pick at 85.01 or higher CLEARS.
MAIN_BOARD_LOCK_FLOOR_INCLUSIVE: float = 85.01


def is_main_board_eligible(pick: dict) -> bool:
    """Return True iff `pick` clears the main Locks board contract.

    Uses the maximum of `lock_score` and `lock_score_v2` to match the
    `$or` semantics of the main /picks/today query (either alias
    clearing 85.01 is sufficient).
    """
    try:
        ls = float(pick.get("lock_score") or 0)
        ls_v2 = float(pick.get("lock_score_v2") or 0)
    except (TypeError, ValueError):
        return False
    return max(ls, ls_v2) >= MAIN_BOARD_LOCK_FLOOR_INCLUSIVE


def main_board_lock_score_query() -> dict:
    """Return the Mongo `$or` fragment that filters picks by the
    strict->85 rule.  Callers should merge this into their query."""
    return {
        "$or": [
            {"lock_score":    {"$gte": MAIN_BOARD_LOCK_FLOOR_INCLUSIVE}},
            {"lock_score_v2": {"$gte": MAIN_BOARD_LOCK_FLOOR_INCLUSIVE}},
        ]
    }


__all__ = [
    "MAIN_BOARD_LOCK_FLOOR_INCLUSIVE",
    "is_main_board_eligible",
    "main_board_lock_score_query",
]
