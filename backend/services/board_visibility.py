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
    • **canonical Lock Score** < 85    (Phase 2A.5C — canonical value,
                                        NOT legacy V1; below `/picks/today`
                                        feed threshold)
    • grade in {'Pass', None}          (Phase 2A.5C — matches the
                                        picks_routes ``grade != Pass``
                                        contract; Playable 85-89 counts
                                        as visible)

RESULT:
    Each pick's document is set with:
        `off_board: bool`           — True when hidden from board
        `off_board_reasons: list`   — audit trail of *why* it was hidden

Downstream settlers filter with `off_board: {"$ne": True}` in their
`db.picks.find(...)` queries so status remains `pending` forever on
off-board picks. Analytics / ROI reports get a clean signal for what
users could actually have played.

Phase 2A.5C DELTA (2026-08) — canonical Lock Score fix
─────────────────────────────────────────────────────
Prior to this delta, `compute_off_board` read the legacy V1
``lock_score`` field.  Under the Phase 1D canonicalization contract,
the authoritative value is:

    published_lock_score  (Phase-1 canonical snapshot)
      → max(lock_score, lock_score_v2)  (pre-canonical fallback)

Reading the stale V1 field caused every Soccer scorer/assist pick
whose V1 landed low (e.g. 55.0 from the legacy engine) but whose
V2/published Lock Score was 85-98 to be silently marked
``off_board=True`` — and dropped from `/api/picks/today?sport=Soccer`.
The visible-grade set also excluded ``Playable``, contradicting the
picks_routes contract (``grade != "Pass"``).  Both are repaired here.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("lockscore.board_visibility")

# Phase 2A.5C DELTA — align with picks_routes ``grade != "Pass"`` contract.
# "Playable" (85 <= LS < 90) and "APEX Lock" (LS == 100) were incorrectly
# excluded, hiding legitimate Elite Soccer scorer picks whose canonical
# Lock Score put them at 85-89 or 100.
_VISIBLE_GRADES = frozenset({
    "APEX Lock", "Elite Lock", "Strong Lock", "Lock", "Playable",
})
_MIN_LOCK_SCORE = 85.0


def _canonical_lock_score(pick: dict[str, Any]) -> float:
    """Return the authoritative Lock Score used by main_board_eligibility.

    Preference order (matches `services.main_board_eligibility`):
        1. ``published_lock_score`` when set (Phase-1 canonical snapshot).
        2. ``max(lock_score, lock_score_v2)`` legacy fallback.
    """
    for key in ("published_lock_score",):
        v = pick.get(key)
        if isinstance(v, (int, float)):
            return float(v)
    ls = pick.get("lock_score") or 0.0
    ls_v2 = pick.get("lock_score_v2") or 0.0
    try:
        return max(float(ls), float(ls_v2))
    except (TypeError, ValueError):
        return 0.0


def _canonical_grade(score: float) -> str:
    """Same band map as ``sports_engine._grade`` — kept in sync."""
    if score >= 100:
        return "APEX Lock"
    if score >= 98:
        return "Elite Lock"
    if score >= 95:
        return "Strong Lock"
    if score >= 90:
        return "Lock"
    if score >= 85:
        return "Playable"
    return "Pass"


def compute_off_board(pick: dict[str, Any]) -> tuple[bool, list[str]]:
    """Return (off_board, reasons) for a single pick.

    Phase 2A.5C — Lock Score check now uses the canonical value.  A stale
    grade field carrying "Pass" from a pre-V2 engine build is IGNORED
    when the canonical Lock Score puts the pick at ``>= 85``.
    """
    reasons: list[str] = []
    if pick.get("no_bet") is True:
        reasons.append("no_bet")
    if pick.get("validation_block") is True:
        reasons.append("validation_block")
    if pick.get("is_model_only") is True:
        reasons.append("model_only")

    # ── Phase 2A.5D MLS PROOF (2026-08) — research-only barrier ────
    # A pick whose odds were synthesized from the model (no real
    # sportsbook line) violates the Phase 2A.5 research-only contract
    # and must NOT appear on the main board.  Detect via any of:
    #   * `odds_source == 'model_derived'`
    #   * `odds_status == 'no_book_line'`
    #   * `no_real_book_line == True`
    _odds_source = str(pick.get("odds_source") or "").lower()
    _odds_status = str(pick.get("odds_status") or "").lower()
    if (_odds_source in ("model_derived", "model_only",
                          "synthetic", "fair_value")
            or _odds_status == "no_book_line"
            or pick.get("no_real_book_line") is True):
        reasons.append("model_only_no_real_book")

    # ── Chalk Trap: HIDE from board (2026-07-21 update) ───────────────
    if pick.get("chalk_trap") is True:
        reasons.append("chalk_trap")
        return (True, reasons)
    if pick.get("chalk_verified") is True:
        return (bool(reasons), reasons)

    # ── Longshot Trap: HIDE from board (2026-07-21 update) ────────────
    if pick.get("longshot_trap") is True:
        reasons.append("longshot_trap")
        return (True, reasons)
    if pick.get("longshot_verified") is True:
        return (bool(reasons), reasons)

    # ── Phase 2A.5C: canonical Lock Score check ───────────────────────
    canonical_ls = _canonical_lock_score(pick)
    if canonical_ls < _MIN_LOCK_SCORE:
        reasons.append(f"lock<{int(_MIN_LOCK_SCORE)}")

    # ── Grade check — derived from the canonical Lock Score, not the
    # stored (possibly stale) grade field.  A pick whose canonical LS
    # is >= 85 is at least Playable and therefore visible.
    canonical_grade = _canonical_grade(canonical_ls)
    if canonical_grade not in _VISIBLE_GRADES:
        reasons.append(f"grade={canonical_grade!r}")

    return (bool(reasons), reasons)


def tag_board_visibility(picks: list[dict[str, Any]]) -> dict[str, int]:
    """Tag each pick in-place with `off_board` + `off_board_reasons`.

    Returns a summary dict `{"total":N, "off_board":K, "on_board":M,
    "reasons": {reason: count}}` for logging.
    """
    stats = {"total": len(picks), "off_board": 0, "on_board": 0}
    reason_counts: dict[str, int] = {}
    for p in picks:
        # ── Phase 2A.5C — refresh stale grade from canonical Lock Score
        # BEFORE the visibility check.
        _c_ls = _canonical_lock_score(p)
        _c_grade = _canonical_grade(_c_ls)
        if p.get("grade") != _c_grade:
            p["grade"] = _c_grade
        # ── Phase 2A.5D FINAL — respect upstream selection decisions ──
        # `apply_soccer_selection` may have already set `off_board=True`
        # with reasons like RELATED_MARKET_DOMINATED / SCORER_TEAM_RANK.
        # Preserve those instead of overriding with the compute_off_board
        # result (which would clear them because the canonical Lock
        # Score alone is ≥ 85).
        _upstream_reasons = list(p.get("off_board_reasons") or [])
        _upstream_off = p.get("off_board") is True
        off, reasons = compute_off_board(p)
        # Union upstream reasons with local compute_off_board reasons.
        if _upstream_off:
            off = True
            for r in _upstream_reasons:
                if r not in reasons:
                    reasons.append(r)
        p["off_board"] = off
        if off:
            p["off_board_reasons"] = reasons
            stats["off_board"] += 1
            for r in reasons:
                key = r if not r.startswith("lock<") else "low_lock_score"
                key = key if not key.startswith("grade=") else "hidden_grade"
                reason_counts[key] = reason_counts.get(key, 0) + 1
        else:
            p.pop("off_board_reasons", None)
            stats["on_board"] += 1
    stats["reasons"] = reason_counts  # type: ignore
    return stats


# ── Standard settlement query filter ────────────────────────────────
# Each settlement module should merge this into its base find() query
# so status transitions never fire on picks that were hidden from users.
ONBOARD_ONLY_FILTER: dict[str, Any] = {"off_board": {"$ne": True}}
