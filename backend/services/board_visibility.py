"""Board Visibility Gate — tags picks with `off_board=True` when they
would never have been seen by the user, so downstream settlement,
analytics, learning, and bandit modules can skip them.

USER MANDATE (2026-07-21): "I don't want the app to grade picks that
don't make it to board". Historical ROI analytics were polluted by
~60% of picks that were flagged `no_bet` by the Brain filter, dropped
by the board_validator, or had lock_score below the /picks/today
threshold of 85. Grading these picks (settling them Won/Lost) made
the app look like it was losing money on picks users could never
have bet.

CRITERIA — pick is OFF-BOARD if ANY of:
    • `no_bet == True`                 (Brain filter rejected it)
    • `validation_block == True`       (board_validator dropped it)
    • `is_model_only == True`          (synthetic / model-only, no book price)
    • `lock_score < 85`                (below /picks/today feed threshold)
    • `grade` in {'Pass','Playable','Solid Lean',None}  (not a visible tier)

RESULT:
    Each pick's document is set with:
        `off_board: bool`           — True when hidden from board
        `off_board_reasons: list`   — audit trail of *why* it was hidden

Downstream settlers filter with `off_board: {"$ne": True}` in their
`db.picks.find(...)` queries so status remains `pending` forever on
off-board picks. Analytics / ROI reports get a clean signal for what
users could actually have played.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("lockscore.board_visibility")

_VISIBLE_GRADES = frozenset({"Elite Lock", "Lock", "Strong Lock"})
_MIN_LOCK_SCORE = 85.0


def compute_off_board(pick: dict[str, Any]) -> tuple[bool, list[str]]:
    """Return (off_board, reasons) for a single pick."""
    reasons: list[str] = []
    if pick.get("no_bet") is True:
        reasons.append("no_bet")
    if pick.get("validation_block") is True:
        reasons.append("validation_block")
    if pick.get("is_model_only") is True:
        reasons.append("model_only")

    # ── Chalk Trap exemption (2026-07-21) ─────────────────────────────
    # User mandate: "I still want the 200 picks for options because me
    # or users don't bet every pick just the app grading all picks".
    # Chalk-trapped picks intentionally get lock_score capped to 72 and
    # grade demoted to "Solid Lean" so users see the ⚠️ TRAP warning —
    # but they must STAY visible on the board (that's the whole point
    # of showing the trap, not hiding it). Bypass the lock/grade rules
    # entirely for these picks. Chalk-verified picks (rare +EV chalk
    # that cleared the kill switch) also always stay visible.
    if pick.get("chalk_trap") is True or pick.get("chalk_verified") is True:
        return (bool(reasons), reasons)

    try:
        lock = float(pick.get("lock_score") or 0.0)
    except (TypeError, ValueError):
        lock = 0.0
    if lock < _MIN_LOCK_SCORE:
        reasons.append(f"lock<{int(_MIN_LOCK_SCORE)}")
    grade = pick.get("grade")
    if grade not in _VISIBLE_GRADES:
        reasons.append(f"grade={grade!r}")
    return (bool(reasons), reasons)


def tag_board_visibility(picks: list[dict[str, Any]]) -> dict[str, int]:
    """Tag each pick in-place with `off_board` + `off_board_reasons`.

    Returns a summary dict `{"total":N, "off_board":K, "on_board":M,
    "reasons": {reason: count}}` for logging.
    """
    stats = {"total": len(picks), "off_board": 0, "on_board": 0}
    reason_counts: dict[str, int] = {}
    for p in picks:
        off, reasons = compute_off_board(p)
        p["off_board"] = off
        if off:
            p["off_board_reasons"] = reasons
            stats["off_board"] += 1
            for r in reasons:
                # Normalize lock<85 as one bucket regardless of number
                key = r if not r.startswith("lock<") else "low_lock_score"
                key = key if not key.startswith("grade=") else "hidden_grade"
                reason_counts[key] = reason_counts.get(key, 0) + 1
        else:
            # Explicitly clear any stale reasons on a re-tagged pick.
            p.pop("off_board_reasons", None)
            stats["on_board"] += 1
    stats["reasons"] = reason_counts  # type: ignore
    return stats


# ── Standard settlement query filter ────────────────────────────────
# Each settlement module should merge this into its base find() query
# so status transitions never fire on picks that were hidden from users.
ONBOARD_ONLY_FILTER: dict[str, Any] = {"off_board": {"$ne": True}}
