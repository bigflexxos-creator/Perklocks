"""Phase 5 (2026-08-11) — Player History Linkage Contract.

A history row is one atomic observation: "player X posted value Y in
market M on date D".  Every history row MUST attach to a canonical
player id — never to a name, provider id, or team-string.

This module is the WRITE-side contract; ``services.universal_player_identity.get_history``
is the READ side.  Both live behind ``PLAYER_HISTORY_COLLECTION``.

Contract:

    {
      "canonical_player_id": str        # REQUIRED — the identity key
      "sport":               str        # REQUIRED — one of ENABLED_SPORTS
      "date":                iso_date   # REQUIRED — "YYYY-MM-DD"
      "event_id":            str        # REQUIRED — provider event id
      "market":              str        # REQUIRED — normalized market key
      "value":               number     # REQUIRED — the observed value
      "team_at_time":        Optional[str]   # team when the row happened
      "opponent":            Optional[str]
      "home":                Optional[bool]
      "season":              Optional[str]
      "source":              Optional[str]
      "linked_at":           iso_datetime    # written by upsert
    }

Sport adapters may extend the row with sport-specific keys (e.g.
``pitcher_faced`` for MLB, ``surface`` for Tennis, ``round_finished``
for UFC) — those keys ride alongside the required fields and are
harmless to ignore.

Threshold-history readiness:
    ``get_history(cpid, sport=..., limit=N)`` returns rows ordered by
    date descending.  Downstream (Magic Layer 2.0) filters these to
    compute "N of last M ≥ threshold".  For this to work the writer
    MUST populate `value` and MUST NOT invent missing values.
"""
from __future__ import annotations

from typing import Any


REQUIRED_FIELDS: tuple[str, ...] = (
    "canonical_player_id", "sport", "date",
    "event_id", "market", "value",
)


class HistoryContractViolation(ValueError):
    """Raised when a history row fails the linkage contract."""


def validate_history_row(row: dict[str, Any]) -> None:
    """Raise if a history row is missing REQUIRED_FIELDS or has a
    NaN / None / empty required value.  Callers should NEVER fill
    missing values — return early instead."""
    for k in REQUIRED_FIELDS:
        v = row.get(k)
        if v is None or v == "":
            raise HistoryContractViolation(
                f"history row missing required field: {k}")
    # Value can be int/float/bool; disallow NaN.
    val = row.get("value")
    try:
        if isinstance(val, float) and val != val:  # NaN
            raise HistoryContractViolation("history row value is NaN")
    except TypeError:
        pass


def is_threshold_ready(row: dict[str, Any], threshold: float) -> bool:
    """True iff the row is safely comparable to a numeric threshold."""
    v = row.get("value")
    if v is None:
        return False
    try:
        return float(v) >= float(threshold)
    except (TypeError, ValueError):
        return False


__all__ = [
    "REQUIRED_FIELDS", "HistoryContractViolation",
    "validate_history_row", "is_threshold_ready",
]
