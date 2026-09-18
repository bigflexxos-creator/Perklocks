"""P5 — ONE settlement authority (façade) + legacy direct-writer guard.

Architecture
  canonical publication → canonical event/participant identity → authoritative
  actual → ONE deterministic market-rule grader → versioned settlement_event
  (SettlementService.record) → History projection.

Sport/provider adapters supply FACTS (``actual``); they never write a grade.
Every grade mutation of settlement truth passes through ``settle_pick_canonical``
below, which delegates to the single ledger writer ``SettlementService``.

States: PENDING · WON · LOST · PUSH · VOID · UNRESOLVED · OFF_BOARD · NO_BET
  provider failure / identity unresolved / actual unavailable → UNRESOLVED
  OFF_BOARD ≠ VOID · NO_BET ≠ LOSS · never a false LOSS, never a false VOID.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Optional

STATES = ("pending", "won", "lost", "push", "void", "unresolved", "off_board", "no_bet")


class LegacySettlementWriteBlocked(RuntimeError):
    pass


def require_legacy_override(script_path: str) -> None:
    """Legacy one-off scripts that used to write pick grades directly call
    this at import time.  They are QUARANTINED: they exit unless the
    operator sets PERKLOCKS_ALLOW_LEGACY_SETTLEMENT_WRITE=1 explicitly."""
    if os.environ.get("PERKLOCKS_ALLOW_LEGACY_SETTLEMENT_WRITE") == "1":
        return
    msg = (f"[settlement_authority] {os.path.basename(script_path)} is a QUARANTINED "
           "legacy direct grade writer. Route grades through "
           "services.settlement_authority.settle_pick_canonical or set "
           "PERKLOCKS_ALLOW_LEGACY_SETTLEMENT_WRITE=1 for an audited override.")
    print(msg, file=sys.stderr)
    raise SystemExit(2)


def deterministic_market_rule(pick: dict, actual: Optional[dict]) -> tuple[str, Optional[str]]:
    """Return (result, reason).  Pure function; no I/O.

    ``actual`` is the authoritative FACT payload for the pick's event:
      game markets  → {"scores": [{"name": team, "score": n}, ...], "completed": bool}
      player props  → {"value": number|None, "completed": bool}
    Any missing fact → UNRESOLVED (never LOST, never VOID)."""
    if not actual:
        return "unresolved", "ACTUAL_UNAVAILABLE"
    if actual.get("error") or actual.get("provider_failed"):
        return "unresolved", "PROVIDER_FAILURE"
    if actual.get("identity_unresolved"):
        return "unresolved", "IDENTITY_UNRESOLVED"
    if not actual.get("completed", True):
        return "pending", "EVENT_NOT_FINAL"
    if pick.get("off_board"):
        return "off_board", "OFF_BOARD_PREGAME"
    if pick.get("no_bet"):
        return "no_bet", "NO_BET_PREGAME"

    # Player props: numeric comparison against the published line.
    if "value" in actual:
        v = actual.get("value")
        if v is None:
            return "unresolved", "ACTUAL_UNAVAILABLE"
        line = pick.get("published_line", pick.get("line"))
        side = (pick.get("side") or pick.get("selection") or pick.get("market") or "").lower()
        if line is None:
            # yes/no props (anytime scorer etc.): value>0 ⇒ WON
            return ("won" if float(v) > 0 else "lost"), "YES_NO_RULE"
        try:
            v, ln = float(v), float(line)
        except (TypeError, ValueError):
            return "unresolved", "ACTUAL_UNPARSEABLE"
        if v == ln:
            return "push", "EXACT_LINE"
        is_over = "over" in side or side.startswith("o ")
        is_under = "under" in side or side.startswith("u ")
        if not (is_over or is_under):
            return "unresolved", "SIDE_UNRESOLVED"
        return ("won" if ((v > ln) == is_over) else "lost"), "OVER_UNDER_RULE"

    # Game markets: reuse the existing deterministic rule set.
    try:
        from settlement_engine import settle_pick as _legacy_rule
        res = _legacy_rule(pick, actual)
    except Exception:
        return "unresolved", "RULE_ERROR"
    if res in ("won", "lost", "push", "void"):
        return res, "GAME_MARKET_RULE"
    return "unresolved", "RULE_INDETERMINATE"


async def settle_pick_canonical(db, pick: dict, actual: Optional[dict], *, source: str,
                                canonical_event_id: Optional[str] = None,
                                authoritative_event_final: bool = True) -> dict:
    """The ONLY sanctioned grade mutation path.  Computes the result with
    the deterministic rule and appends a versioned settlement event."""
    from services.settlement_service import SettlementService
    result, reason = deterministic_market_rule(pick, actual)
    if result == "pending":
        return {"status": "PENDING", "reason": reason, "prediction_id": pick.get("id")}
    if result in ("off_board", "no_bet"):
        # Pregame dispositions are recorded as facts on the pick, not as
        # wager outcomes; History projects them explicitly.
        await db.picks.update_one({"id": pick.get("id")},
                                  {"$set": {"status": result, "settlement_reason": reason,
                                            "settlement_authority": "settlement_authority.v1"}})
        return {"status": result.upper(), "reason": reason, "prediction_id": pick.get("id")}
    svc = SettlementService(db)
    out = await svc.record(
        prediction_id=pick.get("id"), result=result, source=source,
        actual_result={**(actual or {}), "rule_reason": reason},
        authoritative_event_final=authoritative_event_final,
        canonical_event_id=canonical_event_id or pick.get("canonical_event_id") or pick.get("event_id"),
        market=pick.get("market"), side=pick.get("side") or pick.get("selection"),
        line=pick.get("published_line", pick.get("line")),
        expected_pick_id=pick.get("id"),
        expected_event_id=pick.get("canonical_event_id") or pick.get("event_id"),
        expected_market=pick.get("market"), expected_side=pick.get("side") or pick.get("selection"),
    )
    if isinstance(out, dict):
        out.setdefault("rule_reason", reason)
    return out


__all__ = ["settle_pick_canonical", "deterministic_market_rule", "require_legacy_override",
           "STATES", "LegacySettlementWriteBlocked"]
