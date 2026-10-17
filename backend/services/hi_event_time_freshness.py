"""Event-time-based freshness verifier (B2 — item 21).

Replaces freshness checks that treated ``updated_at`` as authoritative.
``updated_at`` is OPERATIONAL metadata — a 2025 completed match
rewritten today by a projector is still a stale match for a 2026
model consumer.  The authoritative signal is the EVENT TIME: the real
timestamp of the completed upstream event.

Reads normalized event-time columns in priority order:

    event_time → game_date → match_date → event_date → date

The verifier returns BOTH values side-by-side so observers can see the
distinction:

    {
      "consumer_latest_event_time":  "<ISO or None>",
      "consumer_latest_updated_at":  "<ISO or None>",
    }

Downstream callers decide freshness on ``consumer_latest_event_time``.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

_EVENT_TIME_FIELDS: tuple[str, ...] = (
    "event_time", "game_date", "match_date", "event_date", "date",
)


def _row_event_time(row: dict) -> Optional[str]:
    for k in _EVENT_TIME_FIELDS:
        v = row.get(k)
        if v:
            return str(v)
    return None


def _row_updated_at(row: dict) -> Optional[str]:
    for k in ("updated_at", "ingested_at", "ingest_time"):
        v = row.get(k)
        if v:
            return str(v)
    return None


def latest_event_time(rows: Iterable[dict]) -> Optional[str]:
    """Return the maximum normalized event-time across rows.

    Returns ``None`` iff no row carries any normalized event-time
    column.  Falls back to NOTHING — not ``updated_at`` — so a
    projector-touched old event never masquerades as fresh.
    """
    best: Optional[str] = None
    for r in rows or []:
        et = _row_event_time(r)
        if not et:
            continue
        if best is None or et > best:
            best = et
    return best


def latest_updated_at(rows: Iterable[dict]) -> Optional[str]:
    """Return the maximum ``updated_at`` across rows (operational
    metadata only — NEVER a freshness decision input)."""
    best: Optional[str] = None
    for r in rows or []:
        u = _row_updated_at(r)
        if not u:
            continue
        if best is None or u > best:
            best = u
    return best


def consumer_freshness(rows: Iterable[dict]) -> dict[str, Any]:
    """Return the (event_time, updated_at) pair side-by-side."""
    rows_list = list(rows or [])
    return {
        "consumer_latest_event_time": latest_event_time(rows_list),
        "consumer_latest_updated_at": latest_updated_at(rows_list),
        "sample_size": len(rows_list),
    }


__all__ = [
    "latest_event_time",
    "latest_updated_at",
    "consumer_freshness",
]
