"""Shared helpers for the live game-log ingestors."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

logger = logging.getLogger("lockscore.live_gamelog")

BACKFILL_VERSION = "live-v1.0.0"
TARGET_COLLECTION = "player_game_actuals"


def _f(v) -> Optional[float]:
    """Numeric coercion.  None / '' / non-numeric → None (never 0)."""
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
        if f != f:  # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None


def _mlb_ip_to_outs(ip: Any) -> Optional[float]:
    """Baseball IP ('7.1' = 7⅓ = 22 outs).  Handles floats and strings."""
    if ip is None or ip == "":
        return None
    try:
        s = str(ip)
        whole, _, frac = s.partition(".")
        whole_i = int(whole)
        frac_i = int(frac) if frac else 0
        if frac_i not in (0, 1, 2):
            return round(float(ip) * 3)
        return whole_i * 3 + frac_i
    except (TypeError, ValueError):
        return None


def _iso(v: Any) -> Optional[str]:
    """Coerce a date-like value to an ISO string suitable for
    `event_time`.  Passes through anything that already looks ISO;
    turns "YYYY-MM-DD" into "YYYY-MM-DDT00:00:00Z"."""
    if v is None or v == "":
        return None
    if isinstance(v, str):
        return v if "T" in v else f"{v}T00:00:00Z"
    try:
        return v.isoformat()
    except Exception:
        return None


async def upsert_one(db, doc: dict) -> str:
    """Idempotent upsert on ``(sport, canonical_player_id, event_id)``.

    Returns "inserted" / "updated" / "skipped" so the runner can
    aggregate counters.  All exceptions are swallowed — game-log
    ingestion is best-effort.
    """
    try:
        filt = {
            "sport": doc["sport"],
            "canonical_player_id": doc["canonical_player_id"],
            "event_id": doc["event_id"],
        }
        existing = await db[TARGET_COLLECTION].find_one(filt, {"_id": 1})
        if existing:
            await db[TARGET_COLLECTION].update_one(filt, {"$set": doc})
            return "updated"
        await db[TARGET_COLLECTION].insert_one(doc)
        return "inserted"
    except Exception as e:                              # pragma: no cover
        logger.debug("upsert failure for %s/%s/%s: %s",
                     doc.get("sport"), doc.get("canonical_player_id"),
                     doc.get("event_id"), e)
        return "skipped"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def iter_active_players(db, sport: str) -> list[dict]:
    """Return all active rostered players for ``sport``.

    Falls back gracefully — if the players collection is empty for a
    sport, returns [] and the caller logs a warning.
    """
    cur = db.players.find(
        {"sport": sport, "active": True},
        {"player_id": 1, "mlb_id": 1, "espn_id": 1, "name": 1,
         "canonical_name": 1, "position": 1, "team": 1, "team_id": 1,
         "team_name": 1},
    )
    return [p async for p in cur]


async def pick_priority_ids(db, sport_display: str) -> set[str]:
    """IDs of players who appear in the current active board — refresh
    these FIRST so today's Pick Breakdown always has fresh game logs.

    ``sport_display`` is the display case used on picks ("MLB", "NFL",
    "NBA").  Returns a set of canonical_player_id strings (or empty
    set if no picks are present).
    """
    ids: set[str] = set()
    try:
        cur = db.picks.find(
            {"sport": sport_display,
             "canonical_player_id": {"$ne": None}},
            {"canonical_player_id": 1},
        )
        async for p in cur:
            cpid = p.get("canonical_player_id")
            if cpid:
                ids.add(str(cpid))
    except Exception:
        pass
    return ids


def sort_players_by_priority(players: list[dict],
                              priority: set[str],
                              id_key: str) -> list[dict]:
    """Sort so priority players come first — the loop can then bail
    early on time-budgeted refreshes without dropping today's picks."""
    def _key(p: dict) -> tuple[int, str]:
        pid = str(p.get(id_key) or p.get("player_id") or "")
        return (0 if pid in priority else 1, pid)
    return sorted(players, key=_key)


__all__ = [
    "BACKFILL_VERSION", "TARGET_COLLECTION", "logger",
    "_f", "_mlb_ip_to_outs", "_iso",
    "now_iso", "upsert_one",
    "iter_active_players", "pick_priority_ids", "sort_players_by_priority",
]
