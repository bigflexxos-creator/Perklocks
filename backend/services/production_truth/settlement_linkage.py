"""Settlement + Analytics Linkage (§9).

Production truth does not end at publication.  A pick can exist
in three distinct terminal states:

    PUBLISHED_UNSETTLED         — published, no settlement record
    SETTLED_NOT_MEASURABLE      — settled, but analytics could not
                                    attribute the result back to the
                                    original canonical prediction
    FULLY_MEASURABLE            — settled AND analytics linkage proven

This module classifies a pick + settlement + analytics tuple into
one of those buckets and preserves the linkage:

    canonical prediction
        → immutable pregame snapshot
        → authoritative settlement
        → correct analytics / history record
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Optional


class MeasurabilityState(str, enum.Enum):
    PUBLISHED_UNSETTLED     = "PUBLISHED_UNSETTLED"
    SETTLED_NOT_MEASURABLE  = "SETTLED_NOT_MEASURABLE"
    FULLY_MEASURABLE        = "FULLY_MEASURABLE"
    UNKNOWN                 = "UNKNOWN"


SETTLED_STATUSES = {"won", "lost", "push", "void"}


@dataclass
class LinkageResult:
    state:            MeasurabilityState
    canonical_prediction_id: Optional[str] = None
    pick_id:          Optional[str] = None
    snapshot_hash:    Optional[str] = None
    settlement_source: Optional[str] = None
    analytics_row_id: Optional[str] = None
    reasons:          list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "state":                   self.state.value,
            "canonical_prediction_id": self.canonical_prediction_id,
            "pick_id":                 self.pick_id,
            "snapshot_hash":           self.snapshot_hash,
            "settlement_source":       self.settlement_source,
            "analytics_row_id":        self.analytics_row_id,
            "reasons":                 self.reasons,
        }


def _is_settled(pick: dict, settlement: Optional[dict]) -> bool:
    if settlement:
        # A real settlement record is authoritative.
        return True
    status = (pick.get("settlement_status") or "").lower()
    return status in SETTLED_STATUSES


def _authoritative_source(settlement: Optional[dict]) -> Optional[str]:
    if not settlement:
        return None
    return (settlement.get("source")
             or settlement.get("provider")
             or settlement.get("authority"))


def classify_measurability(
    pick: dict,
    *,
    pregame_snapshot: Optional[dict] = None,
    settlement_record: Optional[dict] = None,
    analytics_row: Optional[dict] = None,
) -> LinkageResult:
    """Classify the terminal state of a canonical pick.

    * Both a real settlement record AND an analytics row that
      references the canonical prediction id → FULLY_MEASURABLE.
    * Settlement without analytics linkage → SETTLED_NOT_MEASURABLE.
    * Neither → PUBLISHED_UNSETTLED.
    * Missing prerequisites (e.g. no canonical_prediction_id on the
      pick) → UNKNOWN — never faked.
    """
    cpid = pick.get("canonical_prediction_id")
    pid = pick.get("id") or pick.get("external_id")
    result = LinkageResult(
        state=MeasurabilityState.UNKNOWN,
        canonical_prediction_id=cpid,
        pick_id=pid,
        snapshot_hash=(pregame_snapshot or {}).get("snapshot_hash"),
        settlement_source=_authoritative_source(settlement_record),
    )

    if not cpid:
        result.reasons.append("missing_canonical_prediction_id")
        return result

    settled = _is_settled(pick, settlement_record)
    if not settled:
        result.state = MeasurabilityState.PUBLISHED_UNSETTLED
        result.reasons.append("no_authoritative_settlement")
        return result

    # Settled → check analytics linkage.
    if analytics_row:
        # Analytics row must reference the same canonical prediction id.
        row_cpid = analytics_row.get("canonical_prediction_id")
        row_pid  = analytics_row.get("pick_id") or analytics_row.get("external_id")
        if row_cpid and row_cpid == cpid:
            result.analytics_row_id = str(analytics_row.get("_id")
                                            or analytics_row.get("id") or "")
            result.state = MeasurabilityState.FULLY_MEASURABLE
            result.reasons.append("analytics_linked_by_canonical_prediction_id")
            return result
        if row_pid and row_pid == pid:
            result.analytics_row_id = str(analytics_row.get("_id")
                                            or analytics_row.get("id") or "")
            result.state = MeasurabilityState.FULLY_MEASURABLE
            result.reasons.append("analytics_linked_by_pick_id")
            return result
        result.state = MeasurabilityState.SETTLED_NOT_MEASURABLE
        result.reasons.append("analytics_row_does_not_reference_prediction")
        return result

    # No analytics row supplied.  Cannot claim FULLY_MEASURABLE.
    result.state = MeasurabilityState.SETTLED_NOT_MEASURABLE
    result.reasons.append("no_analytics_row_supplied")
    return result


def linkage_summary(results: list[LinkageResult]) -> dict:
    by_state: dict[str, int] = {}
    for r in results:
        by_state[r.state.value] = by_state.get(r.state.value, 0) + 1
    return {
        "total":     len(results),
        "by_state":  by_state,
    }


__all__ = [
    "MeasurabilityState",
    "LinkageResult",
    "classify_measurability",
    "linkage_summary",
    "SETTLED_STATUSES",
]
