"""Phase 1B — persistent production funnel telemetry.

Perklocks Phase 1 §17 requires that every candidate rejection in the
production pick pipeline carries a machine-readable reason and that
those reasons survive process restarts (the legacy
``services.pipeline_diagnostic.log_reason`` ring buffer holds 512
in-memory entries and is lost on restart).

Design
──────
* ``record(...)`` is **synchronous** and allocation-cheap so it can be
  called from the sync hot path (``sports_engine._picks_from_game``).
  Records buffer in-process.
* ``flush(db)`` is called once per refresh cycle by
  ``PickRefreshOrchestrator.refresh`` and bulk-inserts the buffered
  records into the ``funnel_telemetry`` Mongo collection.
* Every ``record`` also mirrors into the legacy in-memory
  ``pipeline_diagnostic.log_reason`` so existing admin diagnostics keep
  working.

This module intentionally has NO opinion about eligibility — it only
observes.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("lockscore.funnel")

# ── Machine-readable reason codes (Phase 1B vocabulary) ──────────────
MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
SIM_FAILED = "SIM_FAILED"
GAP_FILL_EVENT_COVERED_BY_PRIMARY = "GAP_FILL_EVENT_COVERED_BY_PRIMARY"
GAP_FILL_NO_REAL_BOOK_LINE = "GAP_FILL_NO_REAL_BOOK_LINE"
SYNTHETIC_SCORER_RESEARCH_ONLY = "SYNTHETIC_SCORER_RESEARCH_ONLY"
LEGACY_PIPELINE_RETIRED = "LEGACY_PIPELINE_RETIRED"
REAL_PLAYER_MARKET_UNAVAILABLE = "REAL_PLAYER_MARKET_UNAVAILABLE"
PROVIDER_MARKET_MISSING = "PROVIDER_MARKET_MISSING"
# ── Phase 1C — infrastructure reason codes ───────────────────────────
PROVIDER_QUOTA_BLOCKED = "PROVIDER_QUOTA_BLOCKED"
BUDGET_GOVERNOR_BLOCKED = "BUDGET_GOVERNOR_BLOCKED"
CIRCUIT_BREAKER_OPEN = "CIRCUIT_BREAKER_OPEN"
PROVIDER_AUTH_FAILURE = "PROVIDER_AUTH_FAILURE"
PROVIDER_RATE_LIMITED = "PROVIDER_RATE_LIMITED"
PROVIDER_REQUEST_FAILED = "PROVIDER_REQUEST_FAILED"
REFRESH_RUNTIME_FAILURE = "REFRESH_RUNTIME_FAILURE"
SYNTHETIC_ODDS_RESEARCH_ONLY = "SYNTHETIC_ODDS_RESEARCH_ONLY"
NO_REAL_BOOK_LINE_RESEARCH_ONLY = "NO_REAL_BOOK_LINE_RESEARCH_ONLY"
EVIDENCE_THRESHOLD = "EVIDENCE_THRESHOLD"
INTEGRITY_CHECK_FAILED = "INTEGRITY_CHECK_FAILED"

_BUFFER: list[dict] = []
# Hard cap so a pathological loop can't OOM the process. 20k records
# per refresh cycle is far beyond a normal slate.
_BUFFER_CAP = 20_000


def record(*, sport: str, market: str, stage: str, reason: str,
           event: Optional[str] = None, side: Optional[str] = None,
           detail: Optional[str] = None,
           extra: Optional[dict[str, Any]] = None) -> None:
    """Buffer one funnel record. Sync + non-raising by contract."""
    try:
        rec: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "sport": sport,
            "market": market,
            "stage": stage,
            "reason": reason,
        }
        if event:
            rec["event"] = event
        if side:
            rec["side"] = side
        if detail:
            rec["detail"] = detail
        if extra:
            rec["extra"] = extra
        if len(_BUFFER) < _BUFFER_CAP:
            _BUFFER.append(rec)
        # Mirror into the legacy in-memory diagnostics ring buffer.
        try:
            from services.pipeline_diagnostic import log_reason
            log_reason(sport=sport, market=market, event=event or "",
                       reason=reason)
        except Exception:
            pass
    except Exception:  # never let telemetry break the pipeline
        pass


def buffered_count() -> int:
    return len(_BUFFER)


def drain() -> list[dict]:
    """Return + clear the buffer (used by flush and tests)."""
    global _BUFFER
    out, _BUFFER = _BUFFER, []
    return out


def peek(*, reason: Optional[str] = None,
         sport: Optional[str] = None) -> list[dict]:
    """Non-destructive filtered view of the buffer (tests/diagnostics)."""
    out = list(_BUFFER)
    if reason:
        out = [r for r in out if r.get("reason") == reason]
    if sport:
        out = [r for r in out if r.get("sport") == sport]
    return out


async def flush(db, cycle_id: str) -> int:
    """Persist buffered records to Mongo. Returns records written."""
    records = drain()
    if not records:
        return 0
    for r in records:
        r["cycle_id"] = cycle_id
    try:
        await db.funnel_telemetry.insert_many(records, ordered=False)
        logger.info("Funnel telemetry: %d records persisted (cycle=%s)",
                    len(records), cycle_id)
        return len(records)
    except Exception as e:
        logger.warning("Funnel telemetry flush failed (%d records): %s",
                       len(records), e)
        return 0
