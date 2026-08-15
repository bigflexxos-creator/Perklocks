"""Soccer season resolver — Phase 2A.5D CLOSURE (2026-08).

Competition-specific season semantics.  Different Soccer competitions
use different season identifiers:

* MLS / Allsvenskan / Eliteserien / CSL / MLB     → calendar-year (2026)
* Premier League / La Liga / Bundesliga / Serie A
  / Ligue 1 / Eredivisie / Champions League etc.  → split-year (2025-2026)

The resolver returns a chain of season IDs for a competition + reference
date, ordered ``[current, prior, second_prior, third_prior]``.  This
never fabricates data; it returns identifiers only.  Consumers filter
their historical stores by the identifiers actually present.

Contract
--------
* No hardcoded "2024".  Everything is derived from the reference date.
* Future seasons work without code changes.
* Unknown competitions default to calendar-year semantics.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Optional


# ─── Competition families ───────────────────────────────────────────
_CALENDAR_YEAR_LEAGUES = frozenset({
    "mls", "major league soccer", "usl championship",
    "allsvenskan", "eliteserien", "veikkausliiga",
    "china super league", "csl", "chinese super league",
    "j1 league", "j-league", "k league 1", "k-league",
    "australian a-league", "a-league",
    "brasileirao", "brazilian serie a", "brasileirão", "brasileiro serie a",
    "argentine primera",
    "liga mx",  # LigaMX uses Apertura/Clausura but calendar-year is safe fallback
})

_SPLIT_YEAR_LEAGUES = frozenset({
    "epl", "premier league", "english premier league",
    "la liga", "la_liga", "primera division", "primera división",
    "bundesliga", "bundesliga 1",
    "serie a", "italian serie a",
    "ligue 1", "french ligue 1",
    "eredivisie",
    "primeira liga", "portuguese primeira",
    "belgian pro league",
    "scottish premiership",
    "championship", "efl championship",
    "champions league", "uefa champions league",
    "europa league", "uefa europa league",
    "conference league", "uefa conference league",
    "copa libertadores", "copa sudamericana",
})


def _norm(name: str) -> str:
    return (name or "").strip().lower()


def _is_calendar_year(competition: str) -> bool:
    n = _norm(competition)
    if not n:
        return True   # default
    if n in _CALENDAR_YEAR_LEAGUES:
        return True
    if n in _SPLIT_YEAR_LEAGUES:
        return False
    # Heuristic: North/South American leagues → calendar year.
    if any(k in n for k in ("mls", "brazil", "brasil", "argentin",
                              "colomb", "chile", "peru", "uruguay",
                              "australia", "japan", "korea", "china")):
        return True
    # European/African/others → split-year by default.
    return False


def _split_season_id(year_start: int) -> str:
    return f"{year_start}-{year_start + 1}"


def resolve_season_chain(
    competition: str,
    reference_date: Optional[datetime] = None,
    depth: int = 4,
) -> list[str]:
    """Return ordered season IDs [current, prior, second_prior, ...].

    depth=4 returns current + 3 completed prior seasons — matching the
    Phase 2A.5D directive's target.
    """
    d = reference_date or datetime.now(timezone.utc)
    year = d.year
    month = d.month

    if _is_calendar_year(competition):
        # MLS 2026 season runs Feb-Dec 2026.  If it's Jan of the next
        # year, the "current" season is still last calendar year.
        current = year if month >= 2 else year - 1
        return [str(current - k) for k in range(depth)]

    # Split-year: 2025-2026 season runs roughly Aug 2025 → May 2026.
    # If we're between Jan-Jul, the current season started previous
    # calendar year.  If Aug-Dec, current season starts this year.
    if month >= 7:                # Jul is close to season start
        start = year
    else:
        start = year - 1
    return [_split_season_id(start - k) for k in range(depth)]


def resolve_current_season(competition: str,
                            reference_date: Optional[datetime] = None) -> str:
    return resolve_season_chain(competition, reference_date, depth=1)[0]


def resolve_prior_season(competition: str,
                          reference_date: Optional[datetime] = None) -> str:
    chain = resolve_season_chain(competition, reference_date, depth=2)
    return chain[1]


def is_calendar_year_competition(competition: str) -> bool:
    """Public wrapper for competition classification."""
    return _is_calendar_year(competition)


__all__ = [
    "resolve_season_chain",
    "resolve_current_season",
    "resolve_prior_season",
    "is_calendar_year_competition",
]
