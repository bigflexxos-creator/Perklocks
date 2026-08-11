"""Runtime Parity Diagnostic — Block 2E §19-§20.

Aggregates the canonical publication pool → Locks visible → Rollover
eligible → Parlay eligible counts and exposes every drop with an
explicit exclusion reason.

Every canonical pick that is NOT in the ``locks_visible`` /
``parlay_eligible`` / ``rollover_eligible`` outputs MUST have an
explicit exclusion reason attached — no silent drops.
"""
from __future__ import annotations

from typing import Any


# ── Canonical exclusion reason taxonomy (Block 2E §2, §4, §7) ─────
EXCLUSION_REASONS = {
    "NO_BET",
    "OFF_BOARD",
    "UNDER_LOCK",
    "BELOW_FLOOR",
    "NO_REAL_BOOK_LINE",
    "STALE_LINE",
    "EVENT_STARTED",
    "MARKET_FILTERED",
    "SPORT_FILTERED",
    "IDENTITY_INVALID",
    "PUBLICATION_REJECTED",
    "SETTLEMENT_STARTED",
    "DUPLICATE_PUBLICATION",
    "CAPABILITY_DORMANT",   # e.g., First-TD PARTIAL_DORMANT
    "MODE_RESTRICTION",
    "CORRELATION_CONFLICT",
    "OTHER_EXPLICIT_REASON",
}


def _classify_exclusion(pick: dict, floor: int) -> "str | None":
    """Return the FIRST reason a pick would be excluded by a
    canonical consumer, or None when the pick is eligible.

    Order matches the query-level filter order used by the real
    routes (parlay_routes.py + picks_routes.py):
      1. no_bet
      2. is_under_lock
      3. off_board (implicit — MLS/soccer-prop direct-inject failures)
      4. capability_state PARTIAL_DORMANT (First-TD)
      5. no_real_book_line
      6. lock_score < floor
    """
    if pick.get("no_bet") is True:
        return "NO_BET"
    if pick.get("is_under_lock") is True:
        return "UNDER_LOCK"
    if pick.get("off_board") is True:
        # Distinguish dormant capability from generic off_board.
        if pick.get("capability_state") == "PARTIAL_DORMANT":
            return "CAPABILITY_DORMANT"
        return "OFF_BOARD"
    if pick.get("no_real_book_line") is True or pick.get("book_odds") in (None, ""):
        return "NO_REAL_BOOK_LINE"
    try:
        lock = float(pick.get("lock_score") or 0)
    except (TypeError, ValueError):
        lock = 0.0
    if lock < floor:
        return "BELOW_FLOOR"
    return None


def compute_parity_summary(
    canonical_picks: list[dict],
    *,
    parlay_floor: int = 85,
    rollover_floor: int = 89,
    locks_floor: int = 85,
) -> dict:
    """Compute the Block 2E parity summary.

    Returns:
        {
          "total":              int,
          "locks_visible":      int,
          "rollover_eligible":  int,
          "parlay_eligible":    int,
          "exclusion_reasons":  {reason: count, ...},
          "unexplained_delta":  int,  # MUST always be 0
        }
    """
    total = len(canonical_picks)
    locks_visible = 0
    rollover_eligible = 0
    parlay_eligible = 0
    reasons: dict[str, int] = {}

    for p in canonical_picks:
        # Locks visibility.
        lock_reason = _classify_exclusion(p, floor=locks_floor)
        if lock_reason is None:
            locks_visible += 1

        # Parlay eligibility.
        parlay_reason = _classify_exclusion(p, floor=parlay_floor)
        if parlay_reason is None:
            parlay_eligible += 1
        else:
            reasons[parlay_reason] = reasons.get(parlay_reason, 0) + 1

        # Rollover eligibility (stricter floor).
        rollover_reason = _classify_exclusion(p, floor=rollover_floor)
        if rollover_reason is None:
            rollover_eligible += 1

    unexplained = total - parlay_eligible - sum(reasons.values())
    return {
        "total":              total,
        "locks_visible":      locks_visible,
        "rollover_eligible":  rollover_eligible,
        "parlay_eligible":    parlay_eligible,
        "exclusion_reasons":  reasons,
        "unexplained_delta":  unexplained,
    }


def compute_by_sport(canonical_picks: list[dict], *,
                     parlay_floor: int = 85,
                     rollover_floor: int = 89,
                     locks_floor: int = 85) -> dict:
    """Per-sport breakdown of the parity summary."""
    by_sport: dict[str, list[dict]] = {}
    for p in canonical_picks:
        sport = p.get("sport") or "Unknown"
        by_sport.setdefault(sport, []).append(p)
    out: dict[str, dict] = {}
    for sport, picks in by_sport.items():
        out[sport] = compute_parity_summary(
            picks,
            parlay_floor=parlay_floor,
            rollover_floor=rollover_floor,
            locks_floor=locks_floor,
        )
    return out


def build_pick_diagnostic_row(pick: dict, *,
                                parlay_floor: int = 85,
                                rollover_floor: int = 89) -> dict:
    """Per-pick diagnostic row for the runtime matrix (Block 2E §20)."""
    parlay_reason = _classify_exclusion(pick, floor=parlay_floor)
    rollover_reason = _classify_exclusion(pick, floor=rollover_floor)
    return {
        "sport":               pick.get("sport"),
        "market":              pick.get("market"),
        "pick_id":             pick.get("id") or pick.get("external_id"),
        "canonical":           pick.get("publication_gate") != "canonical_barrier_rejected",
        "lock_score":          pick.get("lock_score"),
        "real_odds":           pick.get("book_odds") not in (None, "")
                                and pick.get("no_real_book_line") is not True,
        "locks_visible":       parlay_reason is None,   # same visibility floor 85
        "rollover_evaluated":  rollover_reason is None,
        "rollover_reason":     rollover_reason or "ROLLOVER_ELIGIBLE",
        "parlay_evaluated":    parlay_reason is None,
        "parlay_reason":       parlay_reason or "PARLAY_ELIGIBLE",
        "settlement_supported": True,   # every canonical market is settlement-supported
    }


__all__ = [
    "EXCLUSION_REASONS",
    "compute_parity_summary",
    "compute_by_sport",
    "build_pick_diagnostic_row",
]
