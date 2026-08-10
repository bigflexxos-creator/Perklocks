"""P0 (2026-08-11) — Universal Settlement Contract.

Single, sport-agnostic grading contract every settler MUST call.

Design axioms (from the P0 spec):

  1. MISSING DATA ≠ ZERO.  ``actual = None`` MUST NOT be silently
     graded as a loss.  Missing / not-found / wrong-event / provider-
     error / DNP → ``pending`` / ``unresolved`` — NEVER ``lost``.
  2. ONE authoritative actual per event/participant/market family.
     Alt lines grade off the SAME actual value.
  3. OVER > line, UNDER < line, PUSH == line (when allowed).
  4. Milestone "N+" markets grade `actual >= N` — never confused
     with "Over N.5".
  5. Derived / combo markets (PRA, H+R+RBI) become ``None`` if ANY
     required component is missing.
  6. DNP / void / retired / no_contest have EXPLICIT statuses and
     are NEVER auto-graded as loss.

Public surface:

    grade_over_under(actual, line, side, *, allow_push=True) → dict
    grade_milestone(actual, threshold_min) → dict          # 200+ etc
    grade_derived(components: dict[str, Optional[Number]]) → Optional[Number]
    grade_moneyline(winner, side) → dict
    normalise_actual(raw, *, strict=True) → Optional[Number]
    settlement_envelope(...) → dict     # standard result shape

Every function returns a dict of the settlement envelope shape so
callers can uniformly persist without re-shaping.

Envelope:

    {
      "result":              "won" | "lost" | "push" | "void"
                             | "unresolved" | "pending",
      "actual":              Optional[Number],
      "line":                Optional[Number],
      "side":                Optional[str],
      "settlement_verified": bool,
      "settlement_reason":   str,
      "settlement_status":   str,        # duplicate of result for
                                          # callers who use both keys
    }

NOTE: This module MUST NEVER return ``lost`` when ``actual is None``.
That is a hard contract violated only via test — the caller is
responsible for pushing the envelope back to Mongo.
"""
from __future__ import annotations

import math
from typing import Any, Mapping, Optional, Sequence, Union

Number = Union[int, float]

# ── Result enum ──────────────────────────────────────────────────
RESULT_WON        = "won"
RESULT_LOST       = "lost"
RESULT_PUSH       = "push"
RESULT_VOID       = "void"
RESULT_UNRESOLVED = "unresolved"
RESULT_PENDING    = "pending"

# ── Reason enum (structured) ─────────────────────────────────────
REASON_GRADED_OK              = "graded_ok"
REASON_MISSING_ACTUAL         = "missing_actual"
REASON_MISSING_LINE           = "missing_line"
REASON_MISSING_SIDE           = "missing_side"
REASON_MISSING_COMPONENT      = "missing_required_component"
REASON_PLAYER_NOT_IN_EVENT    = "player_not_in_event"
REASON_EVENT_NOT_FINAL        = "event_not_final"
REASON_PROVIDER_ERROR         = "provider_error"
REASON_DNP                    = "player_did_not_play"
REASON_DID_NOT_START          = "player_did_not_start"
REASON_VOID_MARKET            = "void_market"
REASON_CANCELLED              = "event_cancelled"
REASON_POSTPONED              = "event_postponed"
REASON_SUSPENDED              = "event_suspended"
REASON_RETIRED                = "participant_retired"
REASON_NO_CONTEST             = "no_contest"
REASON_AMBIGUOUS_EVENT        = "ambiguous_event"
REASON_AMBIGUOUS_PARTICIPANT  = "ambiguous_participant"


class SettlementContractViolation(ValueError):
    """Raised when the settlement envelope contract is violated."""


def normalise_actual(raw: Any, *, strict: bool = True) -> Optional[Number]:
    """Convert a raw stat value to a number or ``None``.

    NEVER coerces None/'' to 0.  A NaN float is treated as None.
    A string "0" is a real zero (authoritative source said 0).
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        return int(raw)
    if isinstance(raw, (int, float)):
        if isinstance(raw, float) and math.isnan(raw):
            return None
        return raw
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        # Guard against sentinel strings that mean "missing"
        if s.lower() in ("null", "none", "nan", "n/a", "na", "-", "--"):
            return None
        try:
            v = float(s)
            if math.isnan(v):
                return None
            # Return int when it round-trips exactly (2 not 2.0).
            return int(v) if v.is_integer() else v
        except (TypeError, ValueError):
            if strict:
                return None
            raise
    return None


def settlement_envelope(*, result: str, actual: Optional[Number],
                          line: Optional[Number] = None,
                          side: Optional[str] = None,
                          reason: str = REASON_GRADED_OK,
                          verified: Optional[bool] = None) -> dict:
    """Build the canonical settlement envelope.  Enforces the
    contract that ``lost`` is only allowed with a non-None actual."""
    if result == RESULT_LOST and actual is None:
        raise SettlementContractViolation(
            "cannot settle 'lost' when actual is None — "
            "missing-data must remain 'unresolved'")
    if verified is None:
        verified = (result in (RESULT_WON, RESULT_LOST, RESULT_PUSH,
                                 RESULT_VOID))
    return {
        "result": result,
        "actual": actual,
        "line": line,
        "side": side,
        "settlement_verified": verified,
        "settlement_reason": reason,
        "settlement_status": result,
    }


# ── OVER / UNDER grader ─────────────────────────────────────────
def grade_over_under(
    actual: Optional[Number],
    line: Optional[Number],
    side: Optional[str],
    *,
    allow_push: bool = True,
) -> dict:
    """Grade an Over/Under market.

    Contract:
      * actual is None → ``unresolved`` (missing_actual)
      * line is None   → ``unresolved`` (missing_line)
      * side missing/unknown → ``unresolved`` (missing_side)
      * actual == line → ``push`` if ``allow_push`` else the losing
                            side (bookmaker rule; default push).
      * side "over": actual > line → won; actual < line → lost
      * side "under": actual < line → won; actual > line → lost
    """
    if actual is None:
        return settlement_envelope(
            result=RESULT_UNRESOLVED, actual=None, line=line,
            side=side, reason=REASON_MISSING_ACTUAL)
    if line is None:
        return settlement_envelope(
            result=RESULT_UNRESOLVED, actual=actual, line=None,
            side=side, reason=REASON_MISSING_LINE)
    s = (side or "").strip().lower()
    if s not in ("over", "under"):
        return settlement_envelope(
            result=RESULT_UNRESOLVED, actual=actual, line=line,
            side=side, reason=REASON_MISSING_SIDE)

    a = float(actual)
    l = float(line)
    if math.isclose(a, l, abs_tol=1e-9):
        return settlement_envelope(
            result=RESULT_PUSH if allow_push else (
                RESULT_LOST if s == "over" else RESULT_WON),
            actual=actual, line=line, side=side,
            reason=REASON_GRADED_OK)
    if s == "over":
        return settlement_envelope(
            result=RESULT_WON if a > l else RESULT_LOST,
            actual=actual, line=line, side="over",
            reason=REASON_GRADED_OK)
    # under
    return settlement_envelope(
        result=RESULT_WON if a < l else RESULT_LOST,
        actual=actual, line=line, side="under",
        reason=REASON_GRADED_OK)


# ── Milestone grader ("200+", "1+ hit", "2+ TD") ────────────────
def grade_milestone(
    actual: Optional[Number],
    threshold_min: Optional[Number],
) -> dict:
    """Grade an inclusive-milestone market: actual >= threshold → won.

    Milestone thresholds are ALWAYS integer (200+ = actual ≥ 200) and
    MUST NOT be confused with "Over 200.5" (which is > 200.5).
    """
    if actual is None:
        return settlement_envelope(
            result=RESULT_UNRESOLVED, actual=None, line=threshold_min,
            side="milestone", reason=REASON_MISSING_ACTUAL)
    if threshold_min is None:
        return settlement_envelope(
            result=RESULT_UNRESOLVED, actual=actual, line=None,
            side="milestone", reason=REASON_MISSING_LINE)
    return settlement_envelope(
        result=RESULT_WON if float(actual) >= float(threshold_min)
                          else RESULT_LOST,
        actual=actual, line=threshold_min, side="milestone",
        reason=REASON_GRADED_OK)


# ── Derived / combo grader (PRA, H+R+RBI) ───────────────────────
def grade_derived(components: Mapping[str, Optional[Number]]) -> Optional[Number]:
    """Sum a derived stat safely.  If ANY required component is
    ``None`` → return ``None`` (unresolved).  NEVER treat missing
    components as zero."""
    total: float = 0.0
    for k, v in components.items():
        if v is None:
            return None
        total += float(v)
    if total.is_integer():
        return int(total)
    return total


# ── Moneyline / 1X2 grader ──────────────────────────────────────
def grade_moneyline(
    winner: Optional[str],
    side: Optional[str],
    *,
    is_draw: bool = False,
    draw_side: Optional[str] = None,
) -> dict:
    """Grade a winner-picks-side moneyline.  ``side`` and ``winner``
    are compared case-insensitively after strip.  Draws:

      * If ``is_draw=True`` and ``draw_side`` matches ``side``: won.
      * If ``is_draw=True`` and the market permits draw as a push
        (e.g. Tennis with no draw possible): callers must handle
        that upstream — this function returns ``push`` for equality
        with the draw_side and ``void`` otherwise.
    """
    if not side:
        return settlement_envelope(
            result=RESULT_UNRESOLVED, actual=None, side=None,
            reason=REASON_MISSING_SIDE)
    if winner is None:
        return settlement_envelope(
            result=RESULT_UNRESOLVED, actual=None, side=side,
            reason=REASON_MISSING_ACTUAL)
    ns = (side or "").strip().lower()
    nw = (winner or "").strip().lower()
    if is_draw:
        if draw_side and ns == draw_side.strip().lower():
            return settlement_envelope(
                result=RESULT_WON, actual=winner, side=side,
                reason=REASON_GRADED_OK)
        return settlement_envelope(
            result=RESULT_LOST, actual=winner, side=side,
            reason=REASON_GRADED_OK)
    return settlement_envelope(
        result=RESULT_WON if ns == nw else RESULT_LOST,
        actual=winner, side=side, reason=REASON_GRADED_OK)


# ── Alt-line dispatcher ─────────────────────────────────────────
def grade_alt_lines_from_actual(
    actual: Optional[Number],
    lines: Sequence[tuple[Number, str]],
    *,
    allow_push: bool = True,
) -> list[dict]:
    """Grade many (line, side) pairs against ONE authoritative
    actual.  This is the correct pattern for alt-line markets:

        267 passing yards →
            Over 200.5 → won,  Over 275.5 → lost,
            200+       → won,  275+       → lost

    Callers should pass a list of ``(threshold, side)`` and receive
    a list of envelopes in the same order.  Milestone thresholds
    use ``side="milestone"``.
    """
    out = []
    for line, side in lines:
        if (side or "").lower() == "milestone":
            out.append(grade_milestone(actual, line))
        else:
            out.append(grade_over_under(
                actual, line, side, allow_push=allow_push))
    return out


# ── DNP / void / retired etc. explicit handlers ─────────────────
def envelope_dnp(*, market_grades_dnp_as_loss: bool = False) -> dict:
    """A DNP (Did Not Play) — most books void unless the market
    explicitly grades DNP as a loss (rare, e.g. some SGP legs)."""
    return settlement_envelope(
        result=RESULT_LOST if market_grades_dnp_as_loss else RESULT_VOID,
        actual=None, reason=REASON_DNP,
        verified=True)


def envelope_did_not_start() -> dict:
    return settlement_envelope(
        result=RESULT_VOID, actual=None, reason=REASON_DID_NOT_START,
        verified=True)


def envelope_cancelled() -> dict:
    return settlement_envelope(
        result=RESULT_VOID, actual=None, reason=REASON_CANCELLED,
        verified=True)


def envelope_postponed() -> dict:
    return settlement_envelope(
        result=RESULT_PENDING, actual=None, reason=REASON_POSTPONED,
        verified=False)


def envelope_no_contest() -> dict:
    """UFC no-contest — market voided."""
    return settlement_envelope(
        result=RESULT_VOID, actual=None, reason=REASON_NO_CONTEST,
        verified=True)


def envelope_retired() -> dict:
    """Tennis mid-match retirement — void unless a completed set is
    already reached and the market allows it (upstream decision)."""
    return settlement_envelope(
        result=RESULT_VOID, actual=None, reason=REASON_RETIRED,
        verified=True)


def envelope_provider_error(detail: Optional[str] = None) -> dict:
    """Wrap an upstream provider failure — NEVER return ``lost``."""
    return settlement_envelope(
        result=RESULT_UNRESOLVED, actual=None,
        reason=REASON_PROVIDER_ERROR, verified=False)


__all__ = [
    # results
    "RESULT_WON", "RESULT_LOST", "RESULT_PUSH", "RESULT_VOID",
    "RESULT_UNRESOLVED", "RESULT_PENDING",
    # reasons
    "REASON_GRADED_OK", "REASON_MISSING_ACTUAL", "REASON_MISSING_LINE",
    "REASON_MISSING_SIDE", "REASON_MISSING_COMPONENT",
    "REASON_PLAYER_NOT_IN_EVENT", "REASON_EVENT_NOT_FINAL",
    "REASON_PROVIDER_ERROR", "REASON_DNP", "REASON_DID_NOT_START",
    "REASON_VOID_MARKET", "REASON_CANCELLED", "REASON_POSTPONED",
    "REASON_SUSPENDED", "REASON_RETIRED", "REASON_NO_CONTEST",
    "REASON_AMBIGUOUS_EVENT", "REASON_AMBIGUOUS_PARTICIPANT",
    # functions
    "normalise_actual", "settlement_envelope",
    "grade_over_under", "grade_milestone", "grade_derived",
    "grade_moneyline", "grade_alt_lines_from_actual",
    "envelope_dnp", "envelope_did_not_start", "envelope_cancelled",
    "envelope_postponed", "envelope_no_contest", "envelope_retired",
    "envelope_provider_error",
    # exception
    "SettlementContractViolation",
]
