"""Perklocks U.S. Betting-Day Contract (Block 2B, 2026-08).

Single source of truth for classifying "which U.S. betting day does an
event belong to?"  This module MUST be used anywhere the app decides
whether a game is TODAY, TOMORROW, or YESTERDAY.

The Perklocks betting day runs from **04:00 ET → 03:59 ET next day**.

Rationale:
    * A West Coast 10:10 PM PT = 01:10 ET next day = 05:10 UTC next day
      MUST remain part of the CURRENT US betting day, not tomorrow.
    * A game whose commence_time falls between 00:00-03:59 ET must be
      classified as YESTERDAY's slate (an East Coast late-night game
      that ran long is still the previous betting day).
    * DST-safe: uses zoneinfo.ZoneInfo("US/Eastern") which handles
      the March/November transitions automatically.

Contract:
    perklocks_day(dt_utc) -> "YYYY-MM-DD"
        Returns the U.S. betting-day for the given UTC datetime.

    is_in_current_slate(dt_utc, now_utc=None) -> bool
        True when the event belongs to the same betting day as ``now``.

    slate_bounds(day="YYYY-MM-DD") -> (start_utc, end_utc)
        UTC boundaries of the given betting day for query use.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("US/Eastern")
except Exception:  # pragma: no cover
    # zoneinfo missing → fall back to a fixed-offset ET-4 (no DST).
    _ET = timezone(timedelta(hours=-4))


# The Perklocks day rolls at 04:00 ET.  Chosen to include West Coast
# late-nighters (up to 12:59 AM PT = 03:59 ET) in the previous slate.
_DAY_ROLL_HOUR_ET = 4


def _as_utc(dt: datetime) -> datetime:
    """Coerce a datetime to UTC-aware.  Naive → assume UTC.

    NEVER compare naive to aware — this helper is the single place
    we normalize.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def perklocks_day(dt_utc: datetime) -> str:
    """Return the Perklocks U.S. betting-day string for ``dt_utc``.

    Rule: the betting day rolls at 04:00 ET.  A UTC timestamp at
    05:10 UTC (=01:10 ET) still belongs to the previous ET day's
    betting slate.
    """
    dt = _as_utc(dt_utc).astimezone(_ET)
    # Subtract the roll-hour so 00:00-03:59 ET → previous calendar day.
    shifted = dt - timedelta(hours=_DAY_ROLL_HOUR_ET)
    return shifted.date().isoformat()


def is_in_current_slate(dt_utc: datetime,
                        now_utc: Optional[datetime] = None) -> bool:
    """True when ``dt_utc`` belongs to the same Perklocks betting day
    as ``now_utc`` (or the current wall-clock time if unspecified)."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    return perklocks_day(dt_utc) == perklocks_day(now_utc)


def slate_bounds(day: str) -> tuple[datetime, datetime]:
    """Return (start_utc, end_utc) for the given betting day.

    Both bounds are UTC-aware.  Half-open interval: start ≤ event < end.
    """
    y, m, d = map(int, day.split("-"))
    start_et = datetime(y, m, d, _DAY_ROLL_HOUR_ET, 0, 0, tzinfo=_ET)
    end_et   = start_et + timedelta(days=1)
    return start_et.astimezone(timezone.utc), end_et.astimezone(timezone.utc)


def current_slate_day(now_utc: Optional[datetime] = None) -> str:
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    return perklocks_day(now_utc)


__all__ = [
    "perklocks_day",
    "is_in_current_slate",
    "slate_bounds",
    "current_slate_day",
]
