"""Soccer active-generation horizon — ONE authoritative helper.

User-approved 2026-09-18: active Soccer betting-generation work must
only touch events inside a rolling 48 h window from NOW.

Applies to:
- pre-generation fixture selection
- provider expansion / prop acquisition
- lineup / injury hydration
- model evaluation / candidate construction
- ATGS scorer regeneration

Does NOT apply to:
- historical storage
- settlement lookback
- analytics history
- completed-match research

Events automatically enter the active window as commence_time falls
inside the rolling 48 h.  No fixtures are deleted; older/further
events are simply not consumed by the active betting-generation path.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

# Single source of truth — every active-generation path imports this.
SOCCER_ACTIVE_HORIZON_HOURS = 48


def soccer_active_window_utc(
    now: Optional[datetime] = None,
) -> tuple[datetime, datetime]:
    """Return (start, end) UTC datetimes for the active Soccer window."""
    now = now or datetime.now(timezone.utc)
    return now, now + timedelta(hours=SOCCER_ACTIVE_HORIZON_HOURS)


def soccer_active_window_iso(
    now: Optional[datetime] = None,
) -> tuple[str, str]:
    """Return (start_iso, end_iso) — convenience for Mongo `$gte`/`$lte`."""
    start, end = soccer_active_window_utc(now)
    return start.isoformat(), end.isoformat()


def soccer_active_time_filter(
    fields: tuple[str, ...] = ("event_time", "kickoff_iso", "commence_time"),
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Build a Mongo `$or` filter that gates on the active 48 h window.

    Accepts multiple date fields (Soccer picks have historically used
    ``event_time``, ``kickoff_iso``, or ``commence_time`` depending on
    provider).  A pick matches if ANY of its date fields lies inside
    the rolling window.
    """
    start_iso, end_iso = soccer_active_window_iso(now)
    return {
        "$or": [
            {f: {"$gte": start_iso, "$lte": end_iso}}
            for f in fields
        ]
    }


def is_soccer_event_in_active_window(
    commence_time: Any,
    now: Optional[datetime] = None,
) -> bool:
    """True iff ``commence_time`` falls inside the rolling 48 h window.

    Accepts ISO strings ("2026-09-19T00:00:00Z"), plain dates, or
    datetime instances.  Silently returns False on unparseable input —
    never raises (active generation must be tolerant of garbage rows).
    """
    if commence_time is None or commence_time == "":
        return False
    if isinstance(commence_time, datetime):
        dt = commence_time
    else:
        try:
            s = str(commence_time).replace("Z", "+00:00")
            # Bare date "YYYY-MM-DD" → treat as UTC midnight
            if "T" not in s and len(s) == 10:
                s = f"{s}T00:00:00+00:00"
            dt = datetime.fromisoformat(s)
        except Exception:
            return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    start, end = soccer_active_window_utc(now)
    return start <= dt <= end


__all__ = [
    "SOCCER_ACTIVE_HORIZON_HOURS",
    "soccer_active_window_utc",
    "soccer_active_window_iso",
    "soccer_active_time_filter",
    "is_soccer_event_in_active_window",
]
