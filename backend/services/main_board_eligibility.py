"""Central authoritative eligibility contract for the main Locks board.

Phase-1 Final Closure (2026-08-11) contract — TRUE `> 85`:

    FINAL LOCK SCORE > 85   ⇒ eligible for the Locks board
    FINAL LOCK SCORE ≤ 85   ⇒ NOT eligible

Boundary examples (verified by ``tests/test_phase1_final_closure.py``):

    84.99   → OFF
    85.00   → OFF
    85.001  → ON
    85.01   → ON
    86.00   → ON

The contract is expressed **directly** — no epsilon approximation.
Mongo queries use ``{"$gt": 85}``; Python comparisons use ``> 85.0``.

This central rule governs **every** Locks-experience view:

    * Main Locks board
    * Market-filtered Locks (Hits, Home Runs, Player Points, etc.)
    * Alt-line Locks (Over 1.5, Under 3.5, etc.)

Filters narrow the qualifying ``>85`` pool.  Filters must **never** lower
the Locks threshold.  Per-view lowerings (75, 55, etc.) that existed
before this closure have been retired at every call site.

Canonical source of truth
─────────────────────────
When a pick carries ``published_lock_score`` (i.e. the canonical
publication service has stamped an authoritative snapshot), that value
is the ONLY score consulted.  Legacy shadow fields
(``lock_score`` / ``lock_score_v2`` / ``lock_score_raw`` / ``lock_score_peak``)
can drift up **or** down between refreshes; for published picks they are
irrelevant to eligibility.

For pre-Phase-1c legacy rows without ``published_lock_score`` we still
fall back to ``max(lock_score, lock_score_v2) > 85`` so nothing goes
dark until the v0 backfill lands.

DO NOT change the underlying Lock Score formula from this module —
this module only enforces board eligibility, not scoring.
"""
from __future__ import annotations

from typing import Optional


# ── The contract, expressed directly ─────────────────────────────────
# Strict exclusive floor: a score of exactly 85.0 is NOT on the board.
# Anything > 85.0 (e.g. 85.001, 85.01, 90, 99) IS on the board.
MAIN_BOARD_LOCK_FLOOR_EXCLUSIVE: float = 85.0

# Backwards-compat alias for any external caller (tests, ops scripts)
# still importing the pre-closure name.  Its numeric value is
# irrelevant now — callers should compare via ``is_main_board_eligible``
# or ``main_board_lock_score_query`` rather than the constant directly.
# Left at the historical 85.01 so downstream `>= 85.01` comparisons
# continue to behave identically to the true ``> 85`` contract.
MAIN_BOARD_LOCK_FLOOR_INCLUSIVE: float = 85.01


def _f(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def is_main_board_eligible(pick: dict) -> bool:
    """Return True iff `pick` clears the ``> 85`` Locks contract.

    Canonical source preference:
      1. ``published_lock_score`` (authoritative snapshot value) when set.
      2. Otherwise ``max(lock_score, lock_score_v2)`` legacy fallback.

    For canonically-published picks we deliberately IGNORE
    ``lock_score`` / ``lock_score_v2`` / raw / peak so a stale legacy
    field cannot override the authoritative published Lock Score.
    """
    if not isinstance(pick, dict):
        return False
    pls = _f(pick.get("published_lock_score"))
    if pls is not None:
        return pls > MAIN_BOARD_LOCK_FLOOR_EXCLUSIVE

    ls = _f(pick.get("lock_score")) or 0.0
    ls_v2 = _f(pick.get("lock_score_v2")) or 0.0
    return max(ls, ls_v2) > MAIN_BOARD_LOCK_FLOOR_EXCLUSIVE


def main_board_lock_score_query(min_lock: Optional[float] = None) -> dict:
    """Return the Mongo predicate that enforces the Locks contract.

    * If ``min_lock`` is None or ≤ 85 → strict base contract ``> 85``.
    * If ``min_lock`` > 85            → user-narrowed floor ``>= min_lock``.

    Callers merge the returned dict into their outer query.  The predicate
    always prefers ``published_lock_score`` (canonical) and only falls
    back to legacy shadow fields for rows that have not been published
    yet (i.e. ``published_lock_score`` does not exist on the doc).
    """
    ml = _f(min_lock)
    if ml is None or ml <= MAIN_BOARD_LOCK_FLOOR_EXCLUSIVE:
        # Base contract — strict >85 via $gt.
        return {
            "$or": [
                {"published_lock_score": {"$gt": MAIN_BOARD_LOCK_FLOOR_EXCLUSIVE}},
                {
                    "$and": [
                        {"published_lock_score": {"$exists": False}},
                        {"$or": [
                            {"lock_score":    {"$gt": MAIN_BOARD_LOCK_FLOOR_EXCLUSIVE}},
                            {"lock_score_v2": {"$gt": MAIN_BOARD_LOCK_FLOOR_EXCLUSIVE}},
                        ]},
                    ]
                },
            ]
        }
    # User narrowing (min_lock > 85) — inclusive $gte on the narrower band.
    return {
        "$or": [
            {"published_lock_score": {"$gte": ml}},
            {
                "$and": [
                    {"published_lock_score": {"$exists": False}},
                    {"$or": [
                        {"lock_score":    {"$gte": ml}},
                        {"lock_score_v2": {"$gte": ml}},
                    ]},
                ]
            },
        ]
    }


__all__ = [
    "MAIN_BOARD_LOCK_FLOOR_EXCLUSIVE",
    "MAIN_BOARD_LOCK_FLOOR_INCLUSIVE",   # deprecated alias, kept for compat
    "is_main_board_eligible",
    "main_board_lock_score_query",
]
