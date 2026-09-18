"""P6 — Rollover official slate: immutable Top 3 with an append-only audit.

Collections
  rollover_slates        one doc per (slate_date, scope) — official membership
  rollover_slate_events  append-only: FROZEN / LEG_REPLACED / LEG_INVALIDATED

Rules
  * Once frozen, game start does NOT remove a leg, settlement does NOT
    replace it, refresh does NOT rerank it.
  * Only legitimate PREGAME invalidation (pick pulled off board / no_bet /
    voided / line gone BEFORE kickoff) may replace a leg; the replacement
    appends an audit event {old, new, time, reason}.  No silent change.
  * Filtered rollover requests never touch the official slate.
  * Historical replay (rollover_history_tagger) is RESEARCH_REPLAY only.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

SLATES = "rollover_slates"
EVENTS = "rollover_slate_events"
SCOPE_OFFICIAL = "official"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _leg(p: dict, rank: int) -> dict:
    return {
        "rank": rank,
        "canonical_pick_id": p.get("id"),
        "publication_version": p.get("snapshot_version") or p.get("publication_version"),
        "sport": p.get("sport"),
        "event": p.get("event"),
        "canonical_event_id": p.get("canonical_event_id") or p.get("event_id") or p.get("event"),
        "event_time": p.get("event_time"),
        "market": p.get("market"),
        "selection": p.get("selection"),
        "line": p.get("line"),
        "odds": p.get("book_odds"),
        "win_probability": p.get("win_probability"),
        "lock_score": p.get("lock_score"),
        "grade": p.get("grade"),
        "ev_score": p.get("rollover_ev_score"),
    }


async def get_official_slate(db, slate_date: str) -> Optional[dict]:
    return await db[SLATES].find_one({"slate_date": slate_date, "scope": SCOPE_OFFICIAL}, {"_id": 0})


async def freeze_official_slate(db, slate_date: str, picks: list[dict], *,
                                selector_version: str, board_version: Optional[str]) -> dict:
    """Freeze Top-N as the official slate for the date.  Idempotent: if a
    slate already exists it is returned untouched (never reranked)."""
    existing = await get_official_slate(db, slate_date)
    if existing:
        return existing
    legs = [_leg(p, i + 1) for i, p in enumerate(picks)]
    doc = {
        "slate_id": f"rollover:{slate_date}:{SCOPE_OFFICIAL}",
        "slate_date": slate_date,
        "scope": SCOPE_OFFICIAL,
        "legs": legs,
        "leg_count": len(legs),
        "selector_version": selector_version,
        "board_version": board_version,
        "frozen_at": _now_iso(),
        "version": 1,
    }
    try:
        await db[SLATES].insert_one(dict(doc))
    except Exception:
        # Concurrent freeze — the first writer wins.
        return (await get_official_slate(db, slate_date)) or doc
    await db[EVENTS].insert_one({
        "slate_id": doc["slate_id"], "slate_date": slate_date, "event": "FROZEN",
        "at": doc["frozen_at"], "legs": legs, "selector_version": selector_version,
        "version": 1,
    })
    return doc


def _pregame_invalid_reason(pick: Optional[dict], now: datetime) -> Optional[str]:
    """Return an invalidation reason ONLY for legitimate pregame reasons."""
    if pick is None:
        return "PICK_MISSING"
    et = pick.get("event_time")
    try:
        started = bool(et) and datetime.fromisoformat(str(et).replace("Z", "+00:00")) <= now
    except Exception:
        started = False
    if started:
        return None  # post-kickoff: nothing may replace the leg
    if pick.get("off_board"):
        return "OFF_BOARD_PREGAME"
    if pick.get("no_bet"):
        return "NO_BET_PREGAME"
    if pick.get("no_real_book_line") or pick.get("book_odds") is None:
        return "LINE_REMOVED_PREGAME"
    if (pick.get("status") or "pending") in ("void", "cancelled", "canceled"):
        return "VOID_PREGAME"
    return None


async def reconcile_official_slate(db, slate: dict, candidates: list[dict], *,
                                   selector_version: str) -> dict:
    """Replace ONLY legitimately pregame-invalidated legs, appending an audit
    event per replacement.  Returns the (possibly updated) slate."""
    now = datetime.now(timezone.utc)
    legs = list(slate.get("legs") or [])
    used_events = {l.get("canonical_event_id") for l in legs}
    changed = False
    for i, leg in enumerate(legs):
        pick = await db.picks.find_one({"id": leg.get("canonical_pick_id")}, {"_id": 0})
        reason = _pregame_invalid_reason(pick, now)
        if not reason:
            continue
        repl = next((c for c in candidates
                     if c.get("id") not in {l.get("canonical_pick_id") for l in legs}
                     and (c.get("canonical_event_id") or c.get("event_id") or c.get("event")) not in used_events), None)
        event_doc = {
            "slate_id": slate["slate_id"], "slate_date": slate["slate_date"],
            "event": "LEG_REPLACED" if repl else "LEG_INVALIDATED",
            "at": _now_iso(), "reason": reason, "rank": leg.get("rank"),
            "old_pick": leg, "new_pick": _leg(repl, leg.get("rank") or i + 1) if repl else None,
            "selector_version": selector_version,
        }
        await db[EVENTS].insert_one(event_doc)
        if repl:
            legs[i] = event_doc["new_pick"]
            used_events.add(legs[i].get("canonical_event_id"))
        else:
            legs[i] = {**leg, "invalidated": True, "invalidated_reason": reason}
        changed = True
    if changed:
        new_version = int(slate.get("version") or 1) + 1
        await db[SLATES].update_one(
            {"slate_id": slate["slate_id"]},
            {"$set": {"legs": legs, "version": new_version, "updated_at": _now_iso()}},
        )
        slate = {**slate, "legs": legs, "version": new_version}
    return slate


async def slate_events(db, slate_date: str) -> list[dict]:
    return [e async for e in db[EVENTS].find({"slate_date": slate_date}, {"_id": 0}).sort("at", 1)]


__all__ = ["get_official_slate", "freeze_official_slate", "reconcile_official_slate",
           "slate_events", "SLATES", "EVENTS"]
