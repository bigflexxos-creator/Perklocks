"""Universal season-active discovery (Blocks 37-43).

Decides whether a supported sport is "season-active" purely from
provider truth — not from a calendar date.  Replaces the hard-coded
October 21 NBA activation and the static per-sport refresh list.

Contract:
  sport is season-active ⇔
      (real_upcoming_events_count >= 1  OR  real_recent_events_count >= 1)
      AND capability_enabled(sport) is True

This does NOT change which market families are MODEL_UNAVAILABLE.
It only decides whether the orchestrator should REFRESH that sport.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Optional

# Supported seasonal sports.  CFB included (September-January).
SUPPORTED_SEASONAL_SPORTS: tuple[str, ...] = (
    "nfl", "mlb", "nba", "nhl", "cfb",
)
# Year-round / rolling sports.
SUPPORTED_ROLLING_SPORTS: tuple[str, ...] = (
    "soccer", "tennis",
)


async def season_active(db, sport: str,
                          *, lookahead_days: int = 10,
                          lookback_days: int = 3) -> dict:
    """Determine whether ``sport`` has real events RIGHT NOW.

    * Reads ``games`` for the sport within [-lookback, +lookahead].
    * Returns the counts + the active flag.
    """
    s = (sport or "").strip().lower()
    now = datetime.now(timezone.utc)
    lo = now - timedelta(days=lookback_days)
    hi = now + timedelta(days=lookahead_days)

    upcoming = 0
    recent   = 0
    try:
        upcoming = await db.games.count_documents({
            "sport": s,
            "game_date": {"$gte": now.isoformat(),
                           "$lte": hi.isoformat()},
        })
    except Exception:
        pass
    try:
        recent = await db.games.count_documents({
            "sport": s,
            "game_date": {"$gte": lo.isoformat(),
                           "$lt":  now.isoformat()},
        })
    except Exception:
        pass

    active = (upcoming + recent) > 0
    return {
        "sport": s,
        "season_active": active,
        "upcoming_events": upcoming,
        "recent_events":   recent,
        "window_lookahead_days": lookahead_days,
        "window_lookback_days":  lookback_days,
        "evaluated_at": now.isoformat(),
    }


async def resolve_refresh_eligibility(
    db, sports: Optional[list[str]] = None,
) -> dict[str, dict]:
    """Return per-sport eligibility flags for the orchestrator.
    Keys are lower-case sport slugs.  Each value includes
    ``season_active`` plus the raw counts so logs stay auditable.
    """
    sports = sports or list(SUPPORTED_SEASONAL_SPORTS + SUPPORTED_ROLLING_SPORTS)
    out: dict[str, dict] = {}
    for s in sports:
        out[s] = await season_active(db, s)
    return out


__all__ = [
    "SUPPORTED_SEASONAL_SPORTS", "SUPPORTED_ROLLING_SPORTS",
    "season_active", "resolve_refresh_eligibility",
]
