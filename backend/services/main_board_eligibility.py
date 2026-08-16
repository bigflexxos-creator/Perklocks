"""Central authoritative eligibility contract for the main Locks board.

Perklocks Main Locks Board rule (2026-08 Strictness Fix, INCLUSIVE):

    FINAL LOCK SCORE >= 85   ⇒ eligible for the Locks board
    FINAL LOCK SCORE  < 85   ⇒ NOT eligible

    85–100 INCLUSIVE are score-eligible.  An 85.00 must NOT be rejected
    merely because it is 85.

Boundary examples (verified by ``tests/test_main_board_strictness_85_inclusive.py``):

    84.99   → OFF
    85.00   → ON
    85.01   → ON
    86.00   → ON
    99.00   → ON
    100.00  → ON

The contract is expressed **directly** — no epsilon approximation.
Mongo queries use ``{"$gte": 85}``; Python comparisons use ``>= 85.0``.

This central rule governs **every** Locks-experience view:

    * Main Locks board
    * Market-filtered Locks (Hits, Home Runs, Player Points, etc.)
    * Alt-line Locks (Over 1.5, Under 3.5, etc.)

Filters narrow the qualifying ``>=85`` pool.  Filters must **never** lower
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
fall back to ``max(lock_score, lock_score_v2) >= 85`` so nothing goes
dark until the v0 backfill lands.

DO NOT change the underlying Lock Score formula from this module —
this module only enforces board eligibility, not scoring.
"""
from __future__ import annotations

from typing import Optional


# ── The contract, expressed directly ─────────────────────────────────
# INCLUSIVE floor: a score of exactly 85.0 IS on the board.
# 85.00, 85.001, 90, 99, 100 all IN.  84.99 OUT.
MAIN_BOARD_LOCK_FLOOR: float = 85.0

# Backwards-compat aliases.  Both now point at the SAME inclusive
# floor value (85.0) — external callers should compare via
# ``is_main_board_eligible`` or ``main_board_lock_score_query`` rather
# than the constant directly.  The historical "EXCLUSIVE" name is
# retained for existing imports; its semantic behavior has been
# corrected — it now represents the inclusive minimum (85.0).
MAIN_BOARD_LOCK_FLOOR_EXCLUSIVE: float = MAIN_BOARD_LOCK_FLOOR
MAIN_BOARD_LOCK_FLOOR_INCLUSIVE: float = MAIN_BOARD_LOCK_FLOOR


def _f(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ── Real-line integrity (Emergent Support durable fix, 2026-06) ──────
# A pick may ONLY appear on the main Locks board when it is backed by
# a REAL sportsbook line.  Model-only picks (no book_odds, no implied
# probability, or explicitly ``no_real_book_line=True``) are routed
# to Extended Coverage — they are NOT eligible for main-board display
# regardless of Lock Score.  A missing market line is never permission
# to fabricate market evidence.
def _has_real_market_line(pick: dict) -> bool:
    """Return True iff the pick has a real sportsbook line attached."""
    if not isinstance(pick, dict):
        return False
    # Explicit tag from ingestion path — highest authority.
    if pick.get("no_real_book_line") is True:
        return False
    if pick.get("model_only") is True:
        return False
    # Field checks — the ingestion path MUST provide both a numeric
    # book_odds AND a numeric implied_probability for the pick to
    # count as book-backed.  ``None``/missing/non-numeric → not real.
    bo = pick.get("book_odds")
    ip = pick.get("implied_probability")
    if bo is None or ip is None:
        return False
    try:
        int(bo); float(ip)
    except (TypeError, ValueError):
        return False
    return True


def is_main_board_eligible(pick: dict) -> bool:
    """Return True iff `pick` clears the ``>= 85`` Locks contract
    **and** carries a real sportsbook line.

    Canonical source preference:
      1. ``published_lock_score`` (authoritative snapshot value) when set.
      2. Otherwise ``max(lock_score, lock_score_v2)`` legacy fallback.

    For canonically-published picks we deliberately IGNORE
    ``lock_score`` / ``lock_score_v2`` / raw / peak so a stale legacy
    field cannot override the authoritative published Lock Score.

    Real-line integrity (Emergent Support 2026-06 durable fix):
    model-only picks (``no_real_book_line=True``, ``model_only=True``,
    or ``book_odds`` / ``implied_probability`` missing) are NEVER
    eligible for the main Locks board even when their Lock Score
    would otherwise qualify.  They remain available in Extended
    Coverage via `is_extra`.
    """
    if not isinstance(pick, dict):
        return False

    # ── Real-line integrity gate ─────────────────────────────────
    if not _has_real_market_line(pick):
        return False
    # Defensive: `hide_from_main_board` set by any earlier stage.
    if pick.get("hide_from_main_board") is True:
        return False

    pls = _f(pick.get("published_lock_score"))
    if pls is not None:
        return pls >= MAIN_BOARD_LOCK_FLOOR

    ls = _f(pick.get("lock_score")) or 0.0
    ls_v2 = _f(pick.get("lock_score_v2")) or 0.0
    return max(ls, ls_v2) >= MAIN_BOARD_LOCK_FLOOR


def is_canonical_eligible(pick: dict) -> bool:
    """Return True iff `pick` is canonically eligible for any
    downstream product (Locks / Rollover / Parlay).

    PHASE 1D (2026-06) — Shared Product Source contract.
    Canonical eligibility is BROADER than main-board eligibility:

      * Requires a REAL sportsbook line (identical rule).
      * Requires the pick is NOT tagged ``no_bet`` or ``off_board``.
      * Does NOT enforce ``hide_from_main_board`` — an
        EXTREME_JUICE / DISPLAY_LADDER_SUPERSEDED pick may still
        be a Parlay leg where mathematically appropriate
        (Phase 8 directive).
      * Does NOT enforce the >= 85 Locks floor — Parlay 2.0 uses
        its own leg-quality gate; Rollover 2.0 uses calibrated
        survival probability.
      * PHASE 9L (2026-07) — Player→event identity mismatch is
        fail-closed here too. Identity integrity outranks scoring:
        a mismatched candidate cannot enter Parlay/Rollover
        selection regardless of Lock Score / Magic / Apex / edge.

    Callers requiring the STRICT main-board contract should use
    :func:`is_main_board_eligible` instead.
    """
    if not isinstance(pick, dict):
        return False
    if pick.get("no_bet") is True:
        return False
    if pick.get("off_board") is True:
        return False
    if not _has_real_market_line(pick):
        return False
    # Phase 9L + Phase 10A — identity gate defense-in-depth.
    # Both proven MISMATCH and UNRESOLVABLE player identity fail closed.
    try:
        from services.player_event_identity_gate import (
            evaluate_identity, IdentityVerdict,
        )
        _v = evaluate_identity(pick)
        if _v in (IdentityVerdict.PLAYER_EVENT_IDENTITY_MISMATCH,
                  IdentityVerdict.PLAYER_TEAM_UNRESOLVED):
            return False
    except Exception:
        # Never let the gate crash canonical eligibility.
        pass
    return True


def _real_line_mongo_predicate() -> dict:
    """Mongo AND-clause enforcing real-line integrity.

    Excludes picks that:
      * carry ``no_real_book_line=True`` or ``model_only=True``,
      * have ``hide_from_main_board=True``,
      * have ``book_odds`` or ``implied_probability`` missing/null.
    """
    return {
        "$and": [
            {"no_real_book_line": {"$ne": True}},
            {"model_only":        {"$ne": True}},
            {"hide_from_main_board": {"$ne": True}},
            {"book_odds":         {"$nin": [None]}},
            {"book_odds":         {"$exists": True}},
            {"implied_probability": {"$nin": [None]}},
            {"implied_probability": {"$exists": True}},
        ]
    }


def main_board_lock_score_query(min_lock: Optional[float] = None) -> dict:
    """Return the Mongo predicate that enforces the Locks contract.

    Two dimensions are enforced together:

      A) Real-line integrity (Support 2026-06):
         ``no_real_book_line != True`` AND ``model_only != True`` AND
         ``book_odds`` present AND ``implied_probability`` present.

      B) Lock Score gate (INCLUSIVE >= 85):
         * ``min_lock`` None or <= 85 → base contract ``>= 85``.
         * ``min_lock`` > 85           → user-narrowed floor ``>= min_lock``.

    Callers merge the returned dict into their outer query.  The predicate
    always prefers ``published_lock_score`` (canonical) and only falls
    back to legacy shadow fields for rows that have not been published
    yet (i.e. ``published_lock_score`` does not exist on the doc).
    """
    ml = _f(min_lock)
    real_line_predicate = _real_line_mongo_predicate()
    if ml is None or ml <= MAIN_BOARD_LOCK_FLOOR:
        # Base contract — inclusive >=85 via $gte.
        lock_predicate = {
            "$or": [
                {"published_lock_score": {"$gte": MAIN_BOARD_LOCK_FLOOR}},
                {
                    "$and": [
                        {"published_lock_score": {"$exists": False}},
                        {"$or": [
                            {"lock_score":    {"$gte": MAIN_BOARD_LOCK_FLOOR}},
                            {"lock_score_v2": {"$gte": MAIN_BOARD_LOCK_FLOOR}},
                        ]},
                    ]
                },
            ]
        }
    else:
        # User narrowing (min_lock > 85) — inclusive $gte on the narrower band.
        lock_predicate = {
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
    return {"$and": [real_line_predicate, lock_predicate]}


__all__ = [
    "MAIN_BOARD_LOCK_FLOOR",
    "MAIN_BOARD_LOCK_FLOOR_EXCLUSIVE",   # backwards-compat alias (== INCLUSIVE)
    "MAIN_BOARD_LOCK_FLOOR_INCLUSIVE",   # backwards-compat alias
    "is_main_board_eligible",
    "is_canonical_eligible",
    "main_board_lock_score_query",
    "_has_real_market_line",
]
