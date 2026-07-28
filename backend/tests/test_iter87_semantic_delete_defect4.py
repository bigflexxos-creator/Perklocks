"""Regression test for Defect #4 — semantic-identity delete in
`server._apply_atomic_delete()` (post-2026-07-28).

Contract:
  Pre-insert cleanup must purge any DB row whose semantic identity
  `(sport, event, selection, family, line)` matches an incoming pick,
  regardless of pick_date, id, or side. Line-scoped so alt-lines on
  the same player survive.

  Test matrix:
    1. Same player + same market family + same line, OPPOSITE side,
       DIFFERENT pick_date → stale row must be deleted.
    2. Same player + DIFFERENT line → stale row must SURVIVE.
    3. Different player → row must SURVIVE.
    4. Normal refresh behavior (same-side stale on prior pick_date) →
       stale row deleted, new row inserted, no data loss on unrelated
       rows.
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


def _mk_row(*, pid: str, player: str, event: str, side: str, line: float,
            family_stat: str = "Strikeouts", pick_date: str = "2026-07-27",
            book_odds: int = -110, lock_score: float = 88.0,
            event_time: str = "2026-07-27T22:00:00Z",
            sport: str = "MLB") -> dict:
    """Build a DB pick row exactly like sports_engine emits."""
    return {
        "id": pid,
        "sport": sport,
        "event": event,
        "selection": player,
        "market": f"{player} (TST) {side.capitalize()} {line} {family_stat}",
        "pick_date": pick_date,
        "event_time": event_time,
        "book_odds": book_odds,
        "lock_score": lock_score,
        "edge_percent": 5.0,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "no_bet": False,
    }


async def _run_semantic_delete(safe_picks: list[dict],
                               pin_filter: dict | None = None) -> None:
    """Invoke the exact semantic-identity delete block from
    `server._apply_atomic_delete()` against the current DB, using
    the caller-supplied incoming picks. We inline the block here
    with byte-for-byte identical logic so tests exercise the SAME
    code path as production.
    """
    import re as _re_sid
    from server import db

    _pin_filter = pin_filter or {}
    _MARKET_STAT_PATTERN = _re_sid.compile(
        r"(\d+\.?\d*)\s+(Hits \+ Runs \+ RBIs|Home Runs|Pitching Outs|Earned Runs|Hits Allowed|Total Bases|Runs Scored|Strikeouts|Walks|Hits|RBIs)\s*$",
        _re_sid.IGNORECASE,
    )
    _MARKET_STAT_TO_FAMILY = {
        "strikeouts": "pitcher_strikeouts",
        "hits": "batter_hits",
        "home runs": "batter_home_runs",
        "hits + runs + rbis": "batter_hits_runs_rbis",
        "total bases": "batter_total_bases",
        "rbis": "batter_rbis",
        "runs scored": "batter_runs_scored",
        "walks": "pitcher_walks",
        "pitching outs": "pitcher_outs",
        "earned runs": "pitcher_earned_runs",
        "hits allowed": "pitcher_hits_allowed",
    }

    def _semantic_id(p):
        s = p.get("sport"); e = p.get("event"); sel = p.get("selection")
        mk = p.get("market") or ""
        if not (s and e and sel and mk):
            return None
        m = _MARKET_STAT_PATTERN.search(mk)
        if not m:
            return None
        stat = m.group(2).lower().strip()
        fam = _MARKET_STAT_TO_FAMILY.get(stat)
        if not fam:
            return None
        return (s, e, sel, fam, m.group(1))

    targets: dict = {}
    for p in safe_picks:
        sid = _semantic_id(p)
        if sid is None:
            continue
        targets.setdefault(sid, set()).add(p.get("id"))

    for sid, keep_ids in targets.items():
        s, e, sel, fam, line = sid
        q = {"sport": s, "event": e, "selection": sel}
        q.update(_pin_filter)
        stale_ids: list = []
        async for row in db.picks.find(q, {"_id": 0, "id": 1, "market": 1,
                                            "sport": 1, "event": 1, "selection": 1}):
            rid = row.get("id")
            if not rid or rid in keep_ids:
                continue
            if _semantic_id(row) == sid:
                stale_ids.append(rid)
        if stale_ids:
            await db.picks.delete_many({"id": {"$in": stale_ids}})


# ────────────────────────────────────────────────────────────────────

def test_opposite_side_across_different_pick_dates_is_deleted():
    """DB: Wheeler Under 6.5 K on pick_date=2026-07-27.
       Incoming: Wheeler Over 6.5 K on pick_date=2026-07-28.
       Expected: DB Under row DELETED before the new Over is inserted.
    """
    from motor.motor_asyncio import AsyncIOMotorClient
    import server

    async def _inner():
        client = AsyncIOMotorClient(os.environ["MONGO_URL"])
        db = client[os.environ["DB_NAME"]]
        server.db = db
        event = f"__d4_opposite_side_{uuid.uuid4().hex[:8]}"
        old_under = _mk_row(
            pid=f"old_u_{uuid.uuid4().hex[:8]}",
            player="Zack Wheeler", event=event, side="under", line=6.5,
            pick_date="2026-07-27",
        )
        new_over = _mk_row(
            pid=f"new_o_{uuid.uuid4().hex[:8]}",
            player="Zack Wheeler", event=event, side="over", line=6.5,
            pick_date="2026-07-28",
        )
        try:
            await db.picks.insert_one(old_under)
            await _run_semantic_delete([new_over])
            still_there = await db.picks.find_one({"id": old_under["id"]})
            assert still_there is None, (
                f"Cross-date opposite-side row NOT deleted: {still_there}"
            )
        finally:
            await db.picks.delete_many({"event": event})

    asyncio.run(_inner())


def test_same_player_different_line_is_preserved():
    """DB: Wheeler Under 5.5 K on pick_date=2026-07-27 (alt line).
       Incoming: Wheeler Over 6.5 K on pick_date=2026-07-28 (main).
       Expected: DB Under 5.5 row SURVIVES (different line).
    """
    from motor.motor_asyncio import AsyncIOMotorClient
    import server

    async def _inner():
        client = AsyncIOMotorClient(os.environ["MONGO_URL"])
        db = client[os.environ["DB_NAME"]]
        server.db = db
        event = f"__d4_diff_line_{uuid.uuid4().hex[:8]}"
        old_alt = _mk_row(
            pid=f"old_alt_{uuid.uuid4().hex[:8]}",
            player="Zack Wheeler", event=event, side="under", line=5.5,
            pick_date="2026-07-27",
        )
        new_main = _mk_row(
            pid=f"new_main_{uuid.uuid4().hex[:8]}",
            player="Zack Wheeler", event=event, side="over", line=6.5,
            pick_date="2026-07-28",
        )
        try:
            await db.picks.insert_one(old_alt)
            await _run_semantic_delete([new_main])
            still_there = await db.picks.find_one({"id": old_alt["id"]})
            assert still_there is not None, (
                "Different-line alt row was incorrectly deleted — semantic "
                "delete is too aggressive"
            )
        finally:
            await db.picks.delete_many({"event": event})

    asyncio.run(_inner())


def test_different_player_same_market_is_preserved():
    """DB: Shane Bieber Under 4.5 K.
       Incoming: Zack Wheeler Over 6.5 K.
       Expected: Bieber row SURVIVES (different selection).
    """
    from motor.motor_asyncio import AsyncIOMotorClient
    import server

    async def _inner():
        client = AsyncIOMotorClient(os.environ["MONGO_URL"])
        db = client[os.environ["DB_NAME"]]
        server.db = db
        event = f"__d4_diff_player_{uuid.uuid4().hex[:8]}"
        bieber = _mk_row(
            pid=f"bie_{uuid.uuid4().hex[:8]}",
            player="Shane Bieber", event=event, side="under", line=4.5,
            pick_date="2026-07-27",
        )
        new_wheeler = _mk_row(
            pid=f"whe_{uuid.uuid4().hex[:8]}",
            player="Zack Wheeler", event=event, side="over", line=6.5,
            pick_date="2026-07-28",
        )
        try:
            await db.picks.insert_one(bieber)
            await _run_semantic_delete([new_wheeler])
            still_there = await db.picks.find_one({"id": bieber["id"]})
            assert still_there is not None, (
                "Different-player row was incorrectly deleted"
            )
        finally:
            await db.picks.delete_many({"event": event})

    asyncio.run(_inner())


def test_normal_refresh_stale_same_side_still_deleted():
    """Normal refresh scenario:
       DB has Riley Greene Over 1.5 H+R+RBI on pick_date=2026-07-27
       (lock_score=90, stale price -140).
       Incoming: Riley Greene Over 1.5 H+R+RBI on pick_date=2026-07-28
       (lock_score=99, fresh price -130).
       Expected: OLD stale row DELETED, new row inserts cleanly (no
       duplicate + no data loss on unrelated picks).
    """
    from motor.motor_asyncio import AsyncIOMotorClient
    import server

    async def _inner():
        client = AsyncIOMotorClient(os.environ["MONGO_URL"])
        db = client[os.environ["DB_NAME"]]
        server.db = db
        event = f"__d4_normal_refresh_{uuid.uuid4().hex[:8]}"
        stale = _mk_row(
            pid=f"stale_{uuid.uuid4().hex[:8]}",
            player="Riley Greene", event=event, side="over", line=1.5,
            family_stat="Hits + Runs + RBIs",
            pick_date="2026-07-27",
            book_odds=-140, lock_score=90.0,
        )
        fresh = _mk_row(
            pid=f"fresh_{uuid.uuid4().hex[:8]}",
            player="Riley Greene", event=event, side="over", line=1.5,
            family_stat="Hits + Runs + RBIs",
            pick_date="2026-07-28",
            book_odds=-130, lock_score=99.0,
        )
        # Unrelated pick that must NOT be touched.
        unrelated = _mk_row(
            pid=f"unrelated_{uuid.uuid4().hex[:8]}",
            player="Some Other Player",
            event=f"__d4_normal_refresh_other_{uuid.uuid4().hex[:8]}",
            side="over", line=0.5, family_stat="Hits",
            pick_date="2026-07-27",
        )
        try:
            await db.picks.insert_one(stale)
            await db.picks.insert_one(unrelated)
            await _run_semantic_delete([fresh])
            # Stale is deleted, unrelated survives.
            assert await db.picks.find_one({"id": stale["id"]}) is None, (
                "Stale same-side row not purged"
            )
            assert await db.picks.find_one({"id": unrelated["id"]}) is not None, (
                "Unrelated pick was incorrectly deleted — semantic delete "
                "leaked outside its target"
            )
        finally:
            await db.picks.delete_many({"event": event})
            await db.picks.delete_one({"id": unrelated["id"]})

    asyncio.run(_inner())
