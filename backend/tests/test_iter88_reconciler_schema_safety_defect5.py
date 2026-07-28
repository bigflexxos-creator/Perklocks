"""Regression test for Defect #5 — reconciler + no_bet schema safety.

Contract enforced after 2026-07-28:
  A. Every contradiction "loser" write goes through `_atomic_mark_no_bet`
     which sets `no_bet=True`, `no_bet_reason=<str>`, `status="blocked"`
     in a SINGLE `$set`. The trio is either fully present or entirely
     absent — never partially set.
  B. A startup sweep (`_enforce_no_bet_schema_invariant`) auto-heals
     any legacy inconsistent rows (no_bet_reason set but no_bet=False).
  C. `/api/picks/today` filters by `status ∈ {pending, open, None}` AND
     `no_bet != True`, so a `no_bet=True + status="blocked"` row can
     never surface on the active board — even if it lives on an OLD
     pick_date.
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone

os.environ.setdefault("MONGO_URL", os.environ.get("MONGO_URL", "mongodb://localhost:27017"))
os.environ.setdefault("DB_NAME", "lockscore_db")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _mk(pid: str, player: str, event: str, side: str, line: float, *,
        pick_date: str = "2026-07-28", book_odds: int = -110,
        lock_score: float = 90.0, edge_percent: float = 5.0,
        family_stat: str = "Strikeouts",
        event_time: str = "2026-07-28T22:00:00Z") -> dict:
    return {
        "id": pid,
        "sport": "MLB",
        "event": event,
        "selection": player,
        "market": f"{player} (TST) {side.capitalize()} {line} {family_stat}",
        "pick_date": pick_date,
        "event_time": event_time,
        "book_odds": book_odds,
        "lock_score": lock_score,
        "edge_percent": edge_percent,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "no_bet": False,
        "status": "pending",
    }


def test_contradiction_leaves_exactly_one_active_winner():
    """Insert Over + Under of same line, run reconciler, assert only
    one is active (`no_bet != True`) — the winner."""
    from motor.motor_asyncio import AsyncIOMotorClient
    import server

    async def _inner():
        client = AsyncIOMotorClient(os.environ["MONGO_URL"])
        db = client[os.environ["DB_NAME"]]
        server.db = db
        event = f"__d5_one_winner_{uuid.uuid4().hex[:8]}"
        over = _mk(f"o_{uuid.uuid4().hex[:8]}", "Wheeler", event, "over", 6.5,
                   lock_score=95.0, edge_percent=8.0)
        under = _mk(f"u_{uuid.uuid4().hex[:8]}", "Wheeler", event, "under", 6.5,
                    lock_score=88.0, edge_percent=3.0)
        try:
            await db.picks.insert_many([over, under])
            await server._reconcile_player_prop_contradictions([over, under], "2026-07-28")
            actives = await db.picks.find({
                "event": event,
                "no_bet": {"$ne": True},
            }).to_list(length=10)
            assert len(actives) == 1, (
                f"Expected exactly 1 active winner, got {len(actives)}: "
                f"{[(p['id'], p['market']) for p in actives]}"
            )
        finally:
            await db.picks.delete_many({"event": event})

    asyncio.run(_inner())


def test_loser_has_no_bet_true_reason_and_blocked_status():
    """Loser side must have ALL THREE fields set atomically:
       no_bet=True, no_bet_reason non-empty, status='blocked'."""
    from motor.motor_asyncio import AsyncIOMotorClient
    import server

    async def _inner():
        client = AsyncIOMotorClient(os.environ["MONGO_URL"])
        db = client[os.environ["DB_NAME"]]
        server.db = db
        event = f"__d5_trio_{uuid.uuid4().hex[:8]}"
        over = _mk(f"o_{uuid.uuid4().hex[:8]}", "Bieber", event, "over", 4.5,
                   lock_score=93.0, edge_percent=6.0)
        under = _mk(f"u_{uuid.uuid4().hex[:8]}", "Bieber", event, "under", 4.5,
                    lock_score=90.0, edge_percent=4.0)
        try:
            await db.picks.insert_many([over, under])
            await server._reconcile_player_prop_contradictions([over, under], "2026-07-28")
            loser = await db.picks.find_one({"event": event, "no_bet": True})
            assert loser is not None, "No loser flagged — reconciler didn't fire"
            assert loser.get("no_bet") is True
            assert loser.get("no_bet_reason"), (
                f"no_bet_reason missing: {loser.get('no_bet_reason')!r}"
            )
            assert loser.get("status") == "blocked", (
                f"status not blocked: got {loser.get('status')!r}"
            )
        finally:
            await db.picks.delete_many({"event": event})

    asyncio.run(_inner())


def test_atomic_mark_no_bet_helper_writes_all_three_fields_atomically():
    """Direct helper test — one call must set no_bet + reason + status
    in a single operation. No path can write reason without the flag."""
    from motor.motor_asyncio import AsyncIOMotorClient
    import server

    async def _inner():
        client = AsyncIOMotorClient(os.environ["MONGO_URL"])
        db = client[os.environ["DB_NAME"]]
        server.db = db
        pid = f"atomic_{uuid.uuid4().hex[:8]}"
        event = f"__d5_atomic_{uuid.uuid4().hex[:8]}"
        doc = _mk(pid, "TestPlayer", event, "over", 5.5)
        try:
            await db.picks.insert_one(doc)
            modified = await server._atomic_mark_no_bet(
                {"id": pid}, "test reason from helper"
            )
            assert modified == 1
            after = await db.picks.find_one({"id": pid})
            assert after["no_bet"] is True
            assert after["no_bet_reason"] == "test reason from helper"
            assert after["status"] == "blocked"
        finally:
            await db.picks.delete_many({"event": event})

    asyncio.run(_inner())


def test_startup_sweep_fixes_inconsistent_legacy_rows():
    """A row where `no_bet_reason` is set but `no_bet=False` (simulating
    a pre-helper write or crash-corruption) MUST be auto-healed by
    `_enforce_no_bet_schema_invariant`."""
    from motor.motor_asyncio import AsyncIOMotorClient
    import server

    async def _inner():
        client = AsyncIOMotorClient(os.environ["MONGO_URL"])
        db = client[os.environ["DB_NAME"]]
        server.db = db
        pid = f"legacy_{uuid.uuid4().hex[:8]}"
        event = f"__d5_legacy_{uuid.uuid4().hex[:8]}"
        # Deliberately INCONSISTENT: reason set + no_bet=False
        bad = _mk(pid, "Legacy Player", event, "over", 5.5)
        bad["no_bet"] = False
        bad["no_bet_reason"] = "legacy corruption — reason without flag"
        bad["status"] = "pending"
        try:
            await db.picks.insert_one(bad)
            # Sanity: bad state persisted
            pre = await db.picks.find_one({"id": pid})
            assert pre["no_bet"] is False and pre["no_bet_reason"]
            # Run sweep
            stats = await server._enforce_no_bet_schema_invariant()
            assert stats.get("fixed", 0) >= 1, (
                f"Sweep did not fix any legacy inconsistencies. stats={stats}"
            )
            after = await db.picks.find_one({"id": pid})
            assert after["no_bet"] is True, (
                f"Legacy row not fixed: no_bet={after.get('no_bet')}"
            )
            assert after["status"] == "blocked"
            assert after["no_bet_reason"]  # reason preserved
        finally:
            await db.picks.delete_many({"event": event})

    asyncio.run(_inner())


def test_old_date_flagged_contradiction_row_not_on_active_board():
    """A no_bet=True + status='blocked' pick sitting on an OLD
    pick_date must NOT surface via `/api/picks/today` because the
    endpoint filters by `status ∈ {pending, open, None}` AND
    `no_bet != True`."""
    from motor.motor_asyncio import AsyncIOMotorClient
    import server

    async def _inner():
        client = AsyncIOMotorClient(os.environ["MONGO_URL"])
        db = client[os.environ["DB_NAME"]]
        server.db = db
        event = f"__d5_old_flagged_{uuid.uuid4().hex[:8]}"
        # Insert a flagged old-date row (simulates yesterday's loser)
        old_loser = _mk(
            f"old_loser_{uuid.uuid4().hex[:8]}",
            "Old Loser Pitcher", event, "under", 6.5,
            pick_date="2026-07-27",
        )
        # Apply flag via the helper (guarantees the trio).
        try:
            await db.picks.insert_one(old_loser)
            await server._atomic_mark_no_bet(
                {"id": old_loser["id"]},
                "test — old contradiction loser",
            )
            # Query with the same filter shape `/api/picks/today` uses.
            active = await db.picks.count_documents({
                "event": event,
                "no_bet": {"$ne": True},
                "status": {"$in": ["pending", "open", None]},
            })
            assert active == 0, (
                f"Old-date flagged loser leaked onto active-board filter: "
                f"active_count={active}"
            )
        finally:
            await db.picks.delete_many({"event": event})

    asyncio.run(_inner())
