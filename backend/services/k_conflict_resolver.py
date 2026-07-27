"""Shared K-family (MLB Strikeouts) Over/Under conflict resolver.

Both the in-memory K conflict resolver in `sports_engine.py` (post-emit,
same pick_date) and the DB-level `_reconcile_player_prop_contradictions`
in `server.py` (post-insert, cross pick_date) need the SAME math to
decide which side of a Zack-Wheeler-style Over/Under contradiction wins.

Prior to 2026-07-28 each callsite had its own inline logic which drifted
over time — the DB reconciler was picking by edge/lock while the
in-memory resolver was already using `k_math_expected_k`. This module
consolidates the decision into a single helper so future tweaks land
in ONE place.

Signals used, in priority order:
  1. `k_math_gate` == "passed" AND `k_math_expected_k` present:
     - if expected_k > line + KMATH_TOLERANCE → OVER wins
     - if expected_k < line - KMATH_TOLERANCE → UNDER wins
     - otherwise "indeterminate" (caller falls back).
  2. Fallback → edge_percent then lock_score.
"""
from __future__ import annotations

from typing import Optional, Any

KMATH_TOLERANCE = 0.30  # 0.3 K's away from the line to declare a winner


def _get_number(val: Any) -> Optional[float]:
    try:
        if val is None:
            return None
        return float(val)
    except (TypeError, ValueError):
        return None


def resolve_k_family_winner(
    over_pick: dict,
    under_pick: dict,
    line: Optional[float],
) -> tuple[Optional[str], str]:
    """Return (winning_side, reason).

    winning_side ∈ {"over", "under", None}
    reason ∈ {"kmath_over", "kmath_under", "edge_lock_over",
              "edge_lock_under", "indeterminate"}

    None winning_side ⇒ caller should apply its own safety policy
    (drop both, or defer to edge/lock — depending on context).
    """
    if not over_pick or not under_pick:
        return (None, "indeterminate")

    line_f = _get_number(line)

    # ── K-math signal first ──
    exp_over = _get_number(over_pick.get("k_math_expected_k"))
    exp_under = _get_number(under_pick.get("k_math_expected_k"))
    # Both sides usually see the SAME model output (built off pitcher +
    # opponent). If one side lacks it, fall back to whichever is present.
    exp_k = exp_over if exp_over is not None else exp_under

    if exp_k is not None and line_f is not None:
        if exp_k > line_f + KMATH_TOLERANCE:
            return ("over", "kmath_over")
        if exp_k < line_f - KMATH_TOLERANCE:
            return ("under", "kmath_under")
        # Straddles the line ± tolerance → indeterminate by k-math.

    # ── Fallback: edge > lock ──
    def _rank(p: dict) -> tuple[float, float]:
        return (
            _get_number(p.get("edge_percent")) or 0.0,
            _get_number(p.get("lock_score")) or 0.0,
        )

    over_r = _rank(over_pick)
    under_r = _rank(under_pick)
    # Require a MEANINGFUL edge/lock gap so we don't decide a tie-break
    # on a 0.1 lock_score difference. Same policy as the sports_engine
    # in-memory resolver (lock_gap ≥ 3).
    edge_gap = abs(over_r[0] - under_r[0])
    lock_gap = abs(over_r[1] - under_r[1])
    if edge_gap >= 1.0 or lock_gap >= 3.0:
        if over_r > under_r:
            return ("over", "edge_lock_over")
        return ("under", "edge_lock_under")

    return (None, "indeterminate")
