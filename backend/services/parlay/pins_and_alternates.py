"""Pin & Alternate helpers — Parlay 3.0 Universal Closure
========================================================

Universal-closure additions:

* ``validate_pin(pick, policy, feasibility)`` — validates a pinned
  leg against canonical eligibility, real book odds, and mode
  admission thresholds.  Returns ``(status, reason)`` where status is
  either ``PIN_ACCEPTED`` or ``PIN_CONFLICT`` with a machine-readable
  reason code.

* ``rank_alternates(current_legs, candidates, policy, bucket_map)`` —
  ranks replacement candidates for the CURRENT ticket by mode intent,
  canonical eligibility, dependency safety, event/market
  diversification, and joint survival impact — NOT just standalone
  Lock score.
"""
from __future__ import annotations

from typing import Optional, List, Tuple

from services.parlay.dependency import (
    classify_against_ticket, SAME_EVENT_UNSUPPORTED, UNKNOWN_DEPENDENCY,
    INDEPENDENT_ENOUGH,
)


# Pin status codes
PIN_ACCEPTED = "PIN_ACCEPTED"
PIN_CONFLICT = "PIN_CONFLICT"

# Pin conflict reasons
PIN_REASON_NOT_CANONICAL         = "NOT_CANONICAL"
PIN_REASON_NO_REAL_ODDS          = "NO_REAL_ODDS"
PIN_REASON_BELOW_LOCK_FLOOR      = "BELOW_LOCK_FLOOR"
PIN_REASON_BELOW_EDGE_FLOOR      = "BELOW_EDGE_FLOOR"
PIN_REASON_STATUS_INVALID        = "STATUS_INVALID"
PIN_REASON_DEPENDENCY_CONFLICT   = "DEPENDENCY_CONFLICT"


def _lock_score(p: dict) -> float:
    v = p.get("published_lock_score")
    try:
        if v is not None:
            return float(v)
    except (TypeError, ValueError):
        pass
    try:
        return float(p.get("lock_score") or 0)
    except (TypeError, ValueError):
        return 0.0


def _edge_pct(p: dict) -> float:
    try:
        return float(p.get("edge_percent") or 0)
    except (TypeError, ValueError):
        return 0.0


def _has_real_odds(p: dict) -> bool:
    o = p.get("book_odds") if p.get("book_odds") is not None else p.get("published_odds")
    try:
        return o is not None and int(o) != 0
    except (TypeError, ValueError):
        return False


def _is_canonical(p: dict) -> bool:
    """Best-effort canonical eligibility — delegates to
    ``services.main_board_eligibility.is_canonical_eligible`` when
    available and falls back to a conservative check otherwise."""
    try:
        from services.main_board_eligibility import is_canonical_eligible
        return bool(is_canonical_eligible(p))
    except Exception:
        return not (p.get("off_board") or p.get("no_real_book_line")
                    or p.get("model_only") or p.get("no_bet"))


def validate_pin(pick: dict, policy, existing_legs: Optional[list] = None
                 ) -> Tuple[str, Optional[str], str]:
    """Validate that ``pick`` can be pinned into a ticket under ``policy``.

    Returns ``(status, reason_code, human_message)``.

      * ``status``  ∈ {PIN_ACCEPTED, PIN_CONFLICT}
      * ``reason_code`` — machine-readable code for the frontend, or
        None when accepted.
      * ``human_message`` — short user-facing sentence.
    """
    existing_legs = existing_legs or []

    if not isinstance(pick, dict) or not pick.get("id"):
        return (PIN_CONFLICT, PIN_REASON_NOT_CANONICAL,
                "This wager is missing a canonical identifier.")

    if not _is_canonical(pick):
        return (PIN_CONFLICT, PIN_REASON_NOT_CANONICAL,
                "This wager is no longer canonically eligible for parlays.")

    status = (pick.get("status") or "pending").lower()
    if status not in ("pending", "open", "live", ""):
        return (PIN_CONFLICT, PIN_REASON_STATUS_INVALID,
                f"This wager is {status} — pinning is not allowed.")

    if not _has_real_odds(pick):
        return (PIN_CONFLICT, PIN_REASON_NO_REAL_ODDS,
                "No real sportsbook price is available for this wager.")

    if _lock_score(pick) < getattr(policy, "lock_floor", 0):
        return (PIN_CONFLICT, PIN_REASON_BELOW_LOCK_FLOOR,
                f"Lock score is below the {policy.display_name} floor "
                f"({policy.lock_floor:.0f}).")

    min_edge = getattr(policy, "min_edge_pct", None)
    if min_edge is not None and _edge_pct(pick) < min_edge:
        return (PIN_CONFLICT, PIN_REASON_BELOW_EDGE_FLOOR,
                f"Edge is below the {policy.display_name} floor "
                f"(+{min_edge:.0f}%).")

    if existing_legs:
        dep = classify_against_ticket(pick, existing_legs)
        if dep in (SAME_EVENT_UNSUPPORTED, UNKNOWN_DEPENDENCY):
            return (PIN_CONFLICT, PIN_REASON_DEPENDENCY_CONFLICT,
                    "This wager depends on another leg already in the ticket "
                    "(same event or same player) and cannot combine safely.")

    return (PIN_ACCEPTED, None, "Pinned.")


# ═══════════════════════════════════════════════════════════════════════
# ALTERNATE RANKING
# ═══════════════════════════════════════════════════════════════════════

def _american_to_decimal(a: int) -> float:
    if a > 0:
        return 1 + a / 100.0
    if a < 0:
        return 1 + 100.0 / abs(a)
    return 1.0


def _leg_win_prob(L: dict) -> float:
    try:
        return max(0.01, min(0.99, float(L.get("win_probability") or 0) / 100.0))
    except (TypeError, ValueError):
        return 0.01


def _current_survival(legs: list) -> float:
    p = 1.0
    for L in legs:
        p *= _leg_win_prob(L)
    return p


def rank_alternates(current_legs: list, candidates: list, *, policy,
                    max_return: int = 5) -> list[dict]:
    """Return ranked replacement legs for the CURRENT ticket.

    Ranking key (lower = better):
      1. mode-eligibility distance (lock/edge below policy floor → last)
      2. dependency safety (SAME_EVENT_UNSUPPORTED → excluded)
      3. joint-survival delta if we swapped this leg in
      4. event diversification (fewer duplicate events → better)
      5. market-family diversification (fewer duplicate families → better)
      6. published Lock Score (higher is better) — final tiebreak

    Returns up to ``max_return`` alternates, each with `_rank_reason`
    diagnostic fields attached.
    """
    if not candidates:
        return []
    used_events = {L.get("canonical_event_id") or L.get("event_id")
                   or L.get("event") for L in current_legs}
    used_families = set()
    try:
        from parlay_optimizer import _market_family
        used_families = {_market_family(L.get("market") or "") for L in current_legs}
    except Exception:
        pass
    used_ids = {L.get("id") for L in current_legs}
    base_survival = _current_survival(current_legs) if current_legs else 1.0

    scored: list[tuple] = []
    for c in candidates:
        if c.get("id") in used_ids:
            continue
        # Fail-closed dependency
        dep = classify_against_ticket(c, current_legs)
        if dep in (SAME_EVENT_UNSUPPORTED, UNKNOWN_DEPENDENCY):
            continue
        # Mode admission (soft — under-floor legs sink to the bottom)
        lock = _lock_score(c)
        edge = _edge_pct(c)
        below_lock = 1 if lock < getattr(policy, "lock_floor", 0) else 0
        min_edge = getattr(policy, "min_edge_pct", None)
        below_edge = 1 if (min_edge is not None and edge < min_edge) else 0

        # Event / family diversification
        ev = c.get("canonical_event_id") or c.get("event_id") or c.get("event")
        event_dup = 1 if ev in used_events else 0
        try:
            from parlay_optimizer import _market_family
            fam = _market_family(c.get("market") or "")
            fam_dup = 1 if fam in used_families else 0
        except Exception:
            fam_dup = 0

        # Joint survival delta (higher win_prob leg → smaller drop)
        wp = _leg_win_prob(c)
        # If we ADD this leg, joint = base * wp.  Ranking prefers legs
        # that PRESERVE more of the base survival.
        delta_survival = base_survival - (base_survival * wp)

        # Sort key: dependency-safe → mode-eligible → less duplication
        # → smaller survival drop → higher lock score
        key = (below_lock + below_edge, event_dup, fam_dup,
               delta_survival, -lock)
        scored.append((key, c, {
            "dependency": dep,
            "below_lock_floor": bool(below_lock),
            "below_edge_floor": bool(below_edge),
            "event_duplicate": bool(event_dup),
            "family_duplicate": bool(fam_dup),
            "delta_survival": round(delta_survival, 4),
            "published_lock_score": lock,
        }))

    scored.sort(key=lambda x: x[0])
    out = []
    for _, c, diag in scored[:max_return]:
        c2 = dict(c)
        c2["_alternate_rank_diagnostic"] = diag
        out.append(c2)
    return out


__all__ = [
    "PIN_ACCEPTED", "PIN_CONFLICT",
    "PIN_REASON_NOT_CANONICAL", "PIN_REASON_NO_REAL_ODDS",
    "PIN_REASON_BELOW_LOCK_FLOOR", "PIN_REASON_BELOW_EDGE_FLOOR",
    "PIN_REASON_STATUS_INVALID", "PIN_REASON_DEPENDENCY_CONFLICT",
    "validate_pin", "rank_alternates",
]
