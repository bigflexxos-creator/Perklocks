"""Scheduled-snapshot helper (Phase A/B — Odds API burn reduction).

PerksLocks is a pick-generation app, not a live-odds app.  We take
periodic *snapshots* of upstream data (Odds API alt-lines, soccer
prop-injects, etc.) instead of polling continuously.

This module gives a tiny, dependable primitive:

    async for _ in schedule_utc_hours(name="alt_lines", hours=[12, 18, 23]):
        await do_snapshot_work()

It sleeps until the next UTC hour in the list, yields once, then
sleeps again.  All schedulers use this so operators can trivially
audit the app-wide fetch cadence.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator, Iterable, Optional

logger = logging.getLogger("lockscore.scheduled_snapshot")


def _seconds_until_next_slot(hours: Iterable[int], *,
                              now: Optional[datetime] = None) -> float:
    """Return seconds until the next UTC hour in `hours`.

    Hours are integers 0-23 in UTC.  If we're currently at 13:15 UTC
    and hours=[12, 18, 23], next slot is 18:00 UTC → 4h45m.
    """
    now = now or datetime.now(timezone.utc)
    slots = sorted({int(h) % 24 for h in hours if 0 <= int(h) < 24})
    if not slots:
        return 3600.0  # nothing scheduled — retry in an hour
    today = now.replace(minute=0, second=0, microsecond=0)
    for h in slots:
        candidate = today.replace(hour=h)
        if candidate > now:
            return (candidate - now).total_seconds()
    # None left today — next slot is the earliest hour tomorrow
    tomorrow = (today + timedelta(days=1)).replace(hour=slots[0])
    return (tomorrow - now).total_seconds()


async def schedule_utc_hours(
    *,
    name: str,
    hours: Iterable[int],
    run_immediately: bool = False,
    jitter_sec: float = 0.0,
) -> AsyncIterator[datetime]:
    """Async generator: yields once per scheduled UTC hour, sleeping
    between fires.  Use `run_immediately=True` to fire once at startup
    before entering the schedule (useful for cold-start snapshots).

    Any exception in the caller must not kill the schedule — wrap the
    body of the `async for` in try/except at the call site.
    """
    hours_list = list(hours)
    if run_immediately:
        yield datetime.now(timezone.utc)
    while True:
        wait_s = _seconds_until_next_slot(hours_list)
        if jitter_sec > 0:
            wait_s += min(jitter_sec, 60.0)
        logger.info(
            "[%s] snapshot scheduler sleeping %.0fs until next slot (hours=%s)",
            name, wait_s, hours_list,
        )
        await asyncio.sleep(max(1.0, wait_s))
        yield datetime.now(timezone.utc)


__all__ = ["schedule_utc_hours", "_seconds_until_next_slot"]
