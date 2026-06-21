"""Parlay History — Save-on-Tap parlay tracker.

User taps "Save" on a generated parlay card → we persist it in
`parlay_history`. The settler resolves each saved parlay's status on
every settlement pass. Frontend can then list won / live / lost
parlays per user.

Collection schema (`parlay_history`):
  {
    id:           "p_<hash>",                # deterministic from leg set
    user_id:      "<userId>",                # owner
    created_at:   ISO 8601 UTC,
    mode:         "standard" | "advanced" | "high_risk" | "today",
    leg_ids:      [pick_id, pick_id, ...],
    legs:         [snapshot of pick objs],   # frozen at save time
    combined_odds: int (American moneyline),
    stake:        float (optional, default $1 unit),
    status:       "live" | "won" | "lost",
    legs_won:     int,
    legs_lost:    int,
    legs_pending: int,
    settled_at:   ISO or null,
    payout:       float or null,             # filled when won
  }
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("lockscore.parlay_history")


def _parlay_id(user_id: str, leg_ids: list[str]) -> str:
    """Deterministic id from sorted leg pick ids — prevents dupes if a
    user taps Save twice on the same combo."""
    raw = f"{user_id}|" + "|".join(sorted(leg_ids))
    return "p_" + hashlib.sha1(raw.encode()).hexdigest()[:14]


def _american_combine(odds: list[int]) -> int:
    """Combine American moneyline odds into one parlay number."""
    if not odds:
        return 0
    # Convert each to decimal, multiply, convert back to American.
    dec = 1.0
    for a in odds:
        if a >= 100:
            dec *= 1.0 + (a / 100.0)
        else:
            dec *= 1.0 + (100.0 / abs(a))
    if dec >= 2.0:
        return round((dec - 1.0) * 100.0)
    return -round(100.0 / (dec - 1.0))


def _payout_per_unit(combined_odds: int, stake: float = 1.0) -> float:
    """Profit (not return) on a 1-unit stake."""
    if combined_odds >= 100:
        return round(stake * combined_odds / 100.0, 2)
    return round(stake * 100.0 / abs(combined_odds), 2)


async def save_parlay(db, *, user_id: str, legs: list[dict],
                       mode: str = "standard",
                       stake: float = 1.0) -> dict:
    """Persist a user-tapped parlay. Idempotent (same legs → same id)."""
    if not legs or len(legs) < 2:
        raise ValueError("parlay must have at least 2 legs")
    leg_ids = [str(p.get("id") or p.get("pick_id")) for p in legs if (p.get("id") or p.get("pick_id"))]
    if len(leg_ids) != len(legs):
        raise ValueError("every leg must have an id")
    pid = _parlay_id(user_id, leg_ids)
    existing = await db.parlay_history.find_one({"id": pid})
    if existing:
        return existing  # idempotent
    odds = [int(p.get("book_odds") or 0) for p in legs]
    combined = _american_combine(odds)
    # Snapshot only the fields we'll display so we don't drag huge pick docs.
    leg_snapshots = []
    for p in legs:
        leg_snapshots.append({
            "pick_id": str(p.get("id") or p.get("pick_id")),
            "sport": p.get("sport"),
            "league": p.get("league"),
            "event": p.get("event"),
            "event_time": p.get("event_time"),
            "market": p.get("market"),
            "selection": p.get("selection"),
            "book_odds": int(p.get("book_odds") or 0),
            "lock_score": p.get("lock_score"),
            "status": "pending",  # filled later by resolver
        })
    now = datetime.now(timezone.utc).isoformat()
    doc = {
        "id": pid,
        "user_id": user_id,
        "created_at": now,
        "mode": mode,
        "leg_ids": leg_ids,
        "legs": leg_snapshots,
        "combined_odds": combined,
        "stake": stake,
        "status": "live",
        "legs_won": 0,
        "legs_lost": 0,
        "legs_pending": len(leg_ids),
        "settled_at": None,
        "payout": None,
    }
    await db.parlay_history.insert_one(doc)
    logger.info("Saved parlay %s for user %s (%d legs, %+d)",
                pid, user_id, len(leg_ids), combined)
    return doc


async def resolve_saved_parlays(db) -> dict:
    """Walk all `live` parlays and update each leg's status from picks.

    Status logic:
      • Any leg `lost` or `void` (anything not won/pending) → parlay lost
      • All legs `won` → parlay won (compute payout)
      • Otherwise → still live
    """
    won = lost = updated = 0
    now = datetime.now(timezone.utc).isoformat()
    cursor = db.parlay_history.find({"status": "live"})
    async for parlay in cursor:
        leg_ids = parlay.get("leg_ids") or []
        # Pull current status for each pick id.
        picks = await db.picks.find(
            {"id": {"$in": leg_ids}}, {"id": 1, "status": 1}
        ).to_list(length=len(leg_ids))
        status_by_id = {p["id"]: p.get("status") for p in picks}
        leg_status: list[str] = []
        for lid in leg_ids:
            leg_status.append(status_by_id.get(lid) or "pending")
        # Update snapshot leg statuses for display.
        legs_view = parlay.get("legs") or []
        for i, lid in enumerate(leg_ids):
            if i < len(legs_view):
                legs_view[i]["status"] = leg_status[i]
        wins = sum(1 for s in leg_status if s == "won")
        loss = sum(1 for s in leg_status if s in ("lost", "void"))
        pending = sum(1 for s in leg_status if s not in ("won", "lost", "void"))
        new_status = parlay["status"]
        settled_at = parlay.get("settled_at")
        payout = parlay.get("payout")
        if loss > 0:
            new_status = "lost"
            settled_at = now
            payout = 0.0
            lost += 1
        elif pending == 0 and wins == len(leg_ids):
            new_status = "won"
            settled_at = now
            payout = _payout_per_unit(parlay.get("combined_odds") or 0,
                                       parlay.get("stake") or 1.0)
            won += 1
        # Only persist if something changed.
        if (new_status != parlay["status"]
            or wins != parlay.get("legs_won")
            or loss != parlay.get("legs_lost")
            or pending != parlay.get("legs_pending")):
            await db.parlay_history.update_one(
                {"id": parlay["id"]},
                {"$set": {
                    "status": new_status,
                    "legs_won": wins,
                    "legs_lost": loss,
                    "legs_pending": pending,
                    "legs": legs_view,
                    "settled_at": settled_at,
                    "payout": payout,
                }},
            )
            updated += 1
    if won or lost:
        logger.info("Parlay resolver: %d updated, %d won, %d lost",
                    updated, won, lost)
    return {"updated": updated, "won": won, "lost": lost}


async def list_history(db, *, user_id: str, status_filter: Optional[str] = None,
                        limit: int = 50) -> list[dict]:
    q: dict = {"user_id": user_id}
    if status_filter and status_filter != "all":
        q["status"] = status_filter
    cursor = db.parlay_history.find(q, {"_id": 0}).sort("created_at", -1).limit(limit)
    return await cursor.to_list(length=limit)


async def get_parlay(db, *, user_id: str, parlay_id: str) -> Optional[dict]:
    return await db.parlay_history.find_one(
        {"id": parlay_id, "user_id": user_id}, {"_id": 0}
    )


async def delete_parlay(db, *, user_id: str, parlay_id: str) -> bool:
    r = await db.parlay_history.delete_one(
        {"id": parlay_id, "user_id": user_id}
    )
    return r.deleted_count > 0
