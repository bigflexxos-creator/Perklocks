"""P0.2 (2026-08-11) — Shared settler write-gate helper.

Single choke-point that every live settler calls before writing a
final W/L to Mongo.  Enforces the Universal Settlement Contract:

  * outcome ∈ {won, lost} + evidence missing → refuse, return False
  * outcome=lost + actual==0 + not authoritative_zero → refuse
  * void / push / unresolved / pending → allowed always

Callers pattern:

    from services.settler_write_gate import guard_final_write
    if not guard_final_write(pick, outcome, evidence):
        # skip this write — will be retried next settle cycle
        return
    await db.picks.update_one({"_id": p["_id"]},
                                {"$set": {"status": outcome, ...}})
"""
from __future__ import annotations

from typing import Any, Mapping, Optional


def guard_final_write(
    pick: Mapping[str, Any],
    outcome: str,
    evidence: Optional[Mapping[str, Any]] = None,
) -> bool:
    """Return True iff the settler MAY write this outcome.

    Non-final outcomes (void / push / pending / unresolved) are
    always allowed.  Only final W/L writes are gated.
    """
    outcome_l = (outcome or "").lower()
    if outcome_l not in ("won", "lost"):
        return True  # non-final outcomes pass

    ev = evidence or {}

    # Missing actual → refuse.
    if "value" in ev and ev.get("value") is None:
        return False

    # Missing scoreboard payload (individual-sport settlers).
    if "ref" in ev and not ev.get("ref"):
        return False

    # No positive winner signal on a game-line settlement.
    if "winner_signal_present" in ev and not ev.get("winner_signal_present"):
        return False

    # Confirmed player did NOT participate — but market says loss
    # from missing data.  Refuse until we have a positive signal.
    if ev.get("participant_confirmed_absent") \
            and not ev.get("authoritative_result_confirmed"):
        return False

    # Zero actual on a loss requires authoritative_zero.
    if outcome_l == "lost":
        v = ev.get("value")
        try:
            if v is not None and float(v) == 0.0 \
                    and not ev.get("authoritative_zero"):
                return False
        except (TypeError, ValueError):
            pass

    return True


__all__ = ["guard_final_write"]
