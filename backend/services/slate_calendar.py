"""slate_calendar — Phase 3E central date/season service.

One authoritative module for every date/season/timezone concern in
the PerksLocks backend.  All existing per-module helpers (five
distinct ``_today_str`` implementations at last count) should
delegate here.  Backward-compatible adapters can remain during the
Phase 3 continuation.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

ET = ZoneInfo("America/New_York")


# ── Now / basic conversions ─────────────────────────────────────────
def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def et_now() -> datetime:
    return datetime.now(ET)


def to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_et(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ET)


# ── Slate / board dates (ET business day) ────────────────────────────
def slate_date_et(now: datetime | None = None) -> date:
    """Return the ET calendar date used as the betting-board slate
    date.  Late-night UTC still returns yesterday's ET date until
    the ET midnight rollover — this is the correct behaviour for a
    US-facing product."""
    return to_et(now or utc_now()).date()


def slate_date_str(now: datetime | None = None) -> str:
    return slate_date_et(now).strftime("%Y-%m-%d")


def board_date_utc(now: datetime | None = None) -> str:
    """UTC calendar date — what most of the picks pipeline still uses
    for ``pick_date``.  Kept as a distinct helper so future ET-slate
    migration is a one-line switch."""
    return (now or utc_now()).astimezone(timezone.utc).strftime("%Y-%m-%d")


def tomorrow_utc(now: datetime | None = None) -> str:
    return ((now or utc_now()).astimezone(timezone.utc)
            + timedelta(days=1)).strftime("%Y-%m-%d")


# ── League-specific season helpers ──────────────────────────────────
def mlb_season(dt: datetime | None = None) -> int:
    """MLB regular season = single calendar year; postseason ends by
    early November."""
    return (dt or utc_now()).astimezone(timezone.utc).year


def nfl_season(dt: datetime | None = None) -> int:
    """NFL season starts in September; January playoffs count toward
    the PRIOR calendar year's season."""
    d = (dt or utc_now()).astimezone(timezone.utc)
    return d.year if d.month >= 8 else d.year - 1


def nba_season(dt: datetime | None = None) -> int:
    """NBA regular season starts October; finals end June — season
    is labelled by the calendar year in which it BEGAN."""
    d = (dt or utc_now()).astimezone(timezone.utc)
    return d.year if d.month >= 8 else d.year - 1


def nhl_season(dt: datetime | None = None) -> int:
    """NHL regular season starts October; finals end June."""
    d = (dt or utc_now()).astimezone(timezone.utc)
    return d.year if d.month >= 9 else d.year - 1


def soccer_split_season(dt: datetime | None = None) -> str:
    """European soccer split-year season string, e.g. ``2025-26``.

    Season rollover convention (July 1)."""
    d = (dt or utc_now()).astimezone(timezone.utc)
    start = d.year if d.month >= 7 else d.year - 1
    end   = (start + 1) % 100
    return f"{start}-{end:02d}"


def cfb_season(dt: datetime | None = None) -> int:
    d = (dt or utc_now()).astimezone(timezone.utc)
    return d.year if d.month >= 8 else d.year - 1


__all__ = [
    "ET", "utc_now", "et_now", "to_utc", "to_et",
    "slate_date_et", "slate_date_str", "board_date_utc", "tomorrow_utc",
    "mlb_season", "nfl_season", "nba_season", "nhl_season",
    "soccer_split_season", "cfb_season",
]
