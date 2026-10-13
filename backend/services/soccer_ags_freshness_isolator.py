"""Soccer AGS freshness isolation (Block 18).

Family-level freshness for Soccer board must separate:
    * Anytime Goalscorer                (``goalscorer_anytime``)
    * Score or Assist                   (``goalscorer_score_or_assist``)
    * Assists / Shots / Shots on Target (future families)

A positive Score-or-Assist count MUST NOT make a zero AGS count
appear fresh.  Each family is evaluated independently.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Optional

STATUS_FRESH    = "FRESH"
STATUS_STALE    = "STALE"
STATUS_EMPTY    = "EMPTY"
STATUS_UNKNOWN  = "UNKNOWN"

SOCCER_PLAYER_FAMILIES = (
    "goalscorer_anytime",
    "goalscorer_score_or_assist",
    "player_assists",
    "player_shots",
    "player_shots_on_target",
)

_FRESH_WINDOW_MINUTES = 90


def _parse_iso(ts) -> Optional[datetime]:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None


async def family_freshness(db, family: str) -> dict:
    """Measure freshness INDEPENDENTLY for a single Soccer family."""
    try:
        count = await db.canonical_picks.count_documents({
            "sport": "soccer", "family": family,
            "active_revision": True,
        })
    except Exception as e:
        return {"family": family, "status": STATUS_UNKNOWN,
                "reason": f"count_error:{e}", "count": 0,
                "latest": None, "age_minutes": None}

    if count == 0:
        return {"family": family, "status": STATUS_EMPTY,
                "count": 0, "latest": None, "age_minutes": None}

    try:
        row = await db.canonical_picks.find_one(
            {"sport": "soccer", "family": family, "active_revision": True},
            sort=[("updated_at", -1)])
    except Exception:
        row = None
    latest = _parse_iso(row.get("updated_at") if row else None)
    age_minutes = None
    status = STATUS_UNKNOWN
    if latest:
        age_minutes = int((datetime.now(timezone.utc) - latest).total_seconds() / 60)
        status = STATUS_FRESH if age_minutes <= _FRESH_WINDOW_MINUTES else STATUS_STALE
    return {"family": family, "status": status, "count": count,
            "latest": latest.isoformat() if latest else None,
            "age_minutes": age_minutes}


async def soccer_player_freshness(db) -> dict[str, dict]:
    """Family-by-family freshness report for every Soccer player family."""
    out: dict[str, dict] = {}
    for fam in SOCCER_PLAYER_FAMILIES:
        out[fam] = await family_freshness(db, fam)
    return out


def ags_needs_refresh(report: dict[str, dict]) -> bool:
    """Specific invariant from Block 18: AGS refresh fires EVEN IF
    Score-or-Assist is fresh."""
    ags = report.get("goalscorer_anytime", {})
    return ags.get("status") in (STATUS_EMPTY, STATUS_STALE, STATUS_UNKNOWN)


__all__ = [
    "SOCCER_PLAYER_FAMILIES",
    "STATUS_FRESH", "STATUS_STALE", "STATUS_EMPTY", "STATUS_UNKNOWN",
    "family_freshness", "soccer_player_freshness", "ags_needs_refresh",
]
