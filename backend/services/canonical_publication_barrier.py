"""Canonical Publication Barrier — Block 2D Closure §5 (2026-08).

Enforces the SAME publication contract on direct-inject writers
(``services.mls_direct_inject`` + ``services.soccer_prop_inject``)
that the canonical ``pick_refresh_orchestrator`` applies to picks
generated inline.

Rules (all must pass for a pick to become user-visible):

  1. Real ``book_odds`` — an integer American price present.  A
     synthetic / model-only / fair-odds computation is NOT a book
     price.
  2. ``lock_score >= 85`` — matches the strict >85 gate that
     canonical picks must clear.  Any lower Lock Score is stored
     but marked ``off_board=True`` + ``no_bet=True`` so it never
     surfaces on user-visible boards.
  3. ``implied_probability`` is derivable from the odds (verified
     via _implied_prob).  Never null when book_odds is set.
  4. Real-line integrity flag ``no_real_book_line != True``.

Behaviour on failure:
    * pick["off_board"]         = True
    * pick["no_bet"]             = True
    * pick["publication_gate"]   = "canonical_barrier_rejected"
    * pick["barrier_failures"]   = list[str] of failed rule ids
    * a NON_CANONICAL_WRITE ReasonCode is still emitted from the
      caller for observability.

The barrier NEVER modifies scoring, ranking, Lock Score, model
probability, book_odds, or edge — it can only demote visibility.
"""
from __future__ import annotations

from typing import Any

# Strict floor identical to the canonical publication gate.
STRICT_LOCK_FLOOR = 85


def _implied_from_american(odds: Any) -> "float | None":
    """Mirror sports_engine._implied_prob without importing the huge
    module.  Returns None on any invalid input."""
    try:
        o = int(odds)
    except (TypeError, ValueError):
        return None
    if o == 0:
        return None
    if o > 0:
        return round(100.0 / (o + 100.0), 4)
    return round((-o) / ((-o) + 100.0), 4)


def apply_canonical_barrier(pick: dict) -> dict:
    """Mutate ``pick`` in-place to enforce the canonical barrier.

    Returns the same dict for call-site chaining convenience.

    Called by ``mls_direct_inject`` + ``soccer_prop_inject`` BEFORE
    ``db.picks.bulk_write``.  Direct-inject picks that FAIL are
    stored with ``off_board=True`` — the shadow storage survives
    (internal telemetry / audit trail), but the user-visible board
    can never surface them.
    """
    failures: list[str] = []

    # Rule 1 — real book_odds present.
    book_odds = pick.get("book_odds")
    try:
        _ = int(book_odds)
        real_odds = True
    except (TypeError, ValueError):
        real_odds = False
    if not real_odds:
        failures.append("no_real_book_odds")
    if pick.get("no_real_book_line") is True:
        failures.append("marked_no_real_book_line")
    # 2026-08-23 CHEAP SURGICAL — synthetic Soccer alt lines must not
    # satisfy the real-line gate either.  Any producer that stamps
    # ``model_line=True`` is signalling "this is a model-derived
    # projection, not a real sportsbook price" — treat identically to
    # ``no_real_book_line``.
    if pick.get("model_line") is True:
        failures.append("marked_model_line")

    # Rule 2 — Lock Score >= 85.
    try:
        lock = float(pick.get("lock_score") or 0)
    except (TypeError, ValueError):
        lock = 0.0
    if lock < STRICT_LOCK_FLOOR:
        failures.append(f"lock_below_strict_floor_{STRICT_LOCK_FLOOR}")

    # Rule 3 — implied_probability derivable from odds (if odds present).
    if real_odds:
        derived = _implied_from_american(book_odds)
        if derived is None:
            failures.append("implied_probability_underivable")
        else:
            # Attach if missing — never overwrite an existing valid value.
            if pick.get("implied_probability") in (None, 0, 0.0):
                pick["implied_probability"] = derived

    # Apply verdict.
    if failures:
        pick["off_board"] = True
        pick["no_bet"] = True
        pick["publication_gate"] = "canonical_barrier_rejected"
        pick["barrier_failures"] = failures
    else:
        pick["publication_gate"] = "canonical_barrier_passed"
        # Leave off_board / no_bet untouched (writer may have set for
        # other reasons).
    return pick


def barrier_summary(picks: list[dict]) -> dict:
    """Aggregate barrier verdicts across a batch — helper for
    observability + regression tests."""
    passed = 0
    rejected = 0
    by_failure: dict[str, int] = {}
    for p in picks:
        gate = p.get("publication_gate")
        if gate == "canonical_barrier_passed":
            passed += 1
        elif gate == "canonical_barrier_rejected":
            rejected += 1
            for f in (p.get("barrier_failures") or []):
                by_failure[f] = by_failure.get(f, 0) + 1
    return {
        "total":         len(picks),
        "passed":        passed,
        "rejected":      rejected,
        "by_failure":    by_failure,
    }


__all__ = [
    "apply_canonical_barrier",
    "barrier_summary",
    "STRICT_LOCK_FLOOR",
]
