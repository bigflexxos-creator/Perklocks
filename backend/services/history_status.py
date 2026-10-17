"""Universal history-status contract (B2 — items 17, 32).

A single enum of TRUTHFUL statuses used by every historical-intelligence
response and every freshness-verifier output.  The invariant from
item 17:

    CURRENT COMPLETED UPSTREAM EVENT
      → CANONICAL IDENTITY
      → CANONICAL HISTORY
      → REAL EVENT TIME
      → REAL OPPONENT IDENTITY
      → MODEL CONSUMER
      → HI CONSUMER
      → CURRENT L5/L10/L20
      → DIRECT VS OPP

If upstream contains current history but the consumer cannot see it,
the response MUST declare one of the lag/limited statuses below —
never ``provider_coverage_absence`` and never a silent ``CURRENT``.
"""
from __future__ import annotations

import enum
from typing import Any


class HistoryStatus(str, enum.Enum):
    """Universal history-consumer status.  Return exactly one."""

    #: Consumer sees the real latest completed upstream event.
    CURRENT                   = "CURRENT"

    #: Upstream has newer completed events than the consumer sees.
    #: The ingestion pipeline has not run / finished / caught up.
    INGESTION_LAG             = "INGESTION_LAG"

    #: Rows exist upstream and are ingested, but the consumer cannot
    #: resolve the player/team/event identity to its canonical ID.
    IDENTITY_LAG              = "IDENTITY_LAG"

    #: Rows exist but failed to normalize into the consumer's
    #: expected market / entity schema.
    NORMALIZATION_LAG         = "NORMALIZATION_LAG"

    #: Provider genuinely has limited coverage for this sport /
    #: league / player (NOT a Perklocks defect).  Use with
    #: evidence — not as a catch-all fallback.
    PROVIDER_COVERAGE_LIMITED = "PROVIDER_COVERAGE_LIMITED"

    #: Historical rows exist but the latest completed event is
    #: materially older than the current slate.  Different from
    #: INGESTION_LAG because no newer upstream event is known.
    STALE                     = "STALE"

    #: Partial truth states (item 32) — surface specific failure
    #: modes without conflating them with CURRENT.
    HISTORY_SYNCING           = "HISTORY_SYNCING"
    CURRENT_SEASON_PARTIAL    = "CURRENT_SEASON_PARTIAL"
    IDENTITY_SYNC_NEEDED      = "IDENTITY_SYNC_NEEDED"

    #: Consumer cannot determine current state from available data.
    UNKNOWN                   = "UNKNOWN"


def classify_history_status(*,
                             upstream_latest_event_time: str | None,
                             consumer_latest_event_time: str | None,
                             consumer_latest_updated_at: str | None = None,
                             identity_resolved: bool | None = None,
                             normalized: bool | None = None,
                             provider_has_data: bool | None = None,
                             as_of: str | None = None,
                             stale_threshold_days: int = 7,
                             ) -> HistoryStatus:
    """Return a truthful HistoryStatus given the inputs.

    Precedence (first match wins):

      1. upstream None AND provider_has_data False
         → PROVIDER_COVERAGE_LIMITED
      2. upstream None AND consumer None  → UNKNOWN
      3. consumer None (no history rows at all)  → INGESTION_LAG
      4. identity_resolved explicitly False       → IDENTITY_LAG
      5. normalized explicitly False               → NORMALIZATION_LAG
      6. upstream > consumer                       → INGESTION_LAG
      7. upstream == consumer AND consumer recent  → CURRENT
      8. consumer older than ``stale_threshold_days`` from ``as_of``
                                                   → STALE
      9. otherwise                                 → CURRENT
    """
    if provider_has_data is False and not upstream_latest_event_time:
        return HistoryStatus.PROVIDER_COVERAGE_LIMITED
    if not upstream_latest_event_time and not consumer_latest_event_time:
        return HistoryStatus.UNKNOWN
    if not consumer_latest_event_time:
        return HistoryStatus.INGESTION_LAG
    if identity_resolved is False:
        return HistoryStatus.IDENTITY_LAG
    if normalized is False:
        return HistoryStatus.NORMALIZATION_LAG
    if (upstream_latest_event_time
            and upstream_latest_event_time > consumer_latest_event_time):
        return HistoryStatus.INGESTION_LAG
    if as_of and consumer_latest_event_time:
        try:
            from datetime import datetime, timedelta, timezone
            _s = str(consumer_latest_event_time)[:19]
            _a = str(as_of)[:19]
            _ds = datetime.fromisoformat(_s.replace("Z", ""))
            _da = datetime.fromisoformat(_a.replace("Z", ""))
            if (_da - _ds) > timedelta(days=stale_threshold_days):
                return HistoryStatus.STALE
        except Exception:
            pass
    return HistoryStatus.CURRENT


def status_dict(status: HistoryStatus,
                *, lag_reason: str | None = None,
                detail: dict | None = None) -> dict[str, Any]:
    """Serialize a HistoryStatus into a response-DTO fragment."""
    out: dict[str, Any] = {"history_status": status.value}
    if lag_reason:
        out["lag_reason"] = lag_reason
    if detail:
        out["history_detail"] = dict(detail)
    return out


__all__ = ["HistoryStatus", "classify_history_status", "status_dict"]
