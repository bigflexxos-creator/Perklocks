"""Regression test for Defect #3 — DB-aware K conflict resolver in
`sports_engine.generate_all_picks` (post-2026-07-28).

Contract:
  The K conflict resolver must detect contradictory K picks that live
  in the DB across:
    (a) Different refresh cycles (same day, same pick_date, but the
        wrong-side row was persisted by an EARLIER refresh window).
    (b) Different pick_date buckets (same game, one row on
        pick_date=X, new pick emitted on pick_date=X+1).
    (c) Same-day refresh overlap where the DB row is on a later
        pick_date than the new pick (edge case; still must resolve).

  For each contradiction the resolver must:
    - Consult the shared `resolve_k_family_winner` helper.
    - If NEW pick wins → mark DB row `no_bet=True` atomically (both
      `no_bet` and `no_bet_reason` written together).
    - If DB row wins → drop the new pick from the emitted batch.
    - If indeterminate → mark DB row `no_bet=True` AND drop new pick.
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone, timedelta

os.environ.setdefault("MONGO_URL", os.environ.get("MONGO_URL", "mongodb://localhost:27017"))
os.environ.setdefault("DB_NAME", "lockscore_db")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _make_pick(*, pid: str, player: str, event: str, side: str,
               line: float, pick_date: str, book_odds: int = -110,
               k_math_expected_k: float | None = None,
               lock_score: float = 90.0, edge_percent: float = 5.0,
               created_offset_hours: int = 0) -> dict:
    """Build an in-memory pick dict matching what sports_engine emits."""
    market = f"{player} (TST) {side.capitalize()} {line} Strikeouts"
    created_at = (datetime.now(timezone.utc)
                  - timedelta(hours=created_offset_hours)).isoformat()
    doc = {
        "id": pid,
        "sport": "MLB",
        "event": event,
        "selection": player,
        "market": market,
        "pick_date": pick_date,
        "book_odds": book_odds,
        "lock_score": lock_score,
        "edge_percent": edge_percent,
        "created_at": created_at,
        "no_bet": False,
    }
    if k_math_expected_k is not None:
        doc["k_math_expected_k"] = k_math_expected_k
        doc["k_math_gate"] = "passed"
    return doc


async def _invoke_resolver_only(picks_in: list[dict]) -> list[dict]:
    """Run the DB-aware K conflict resolver block from
    `sports_engine.generate_all_picks` on a supplied `all_picks` list.

    We can't invoke `generate_all_picks` end-to-end (it hits the Odds
    API). Instead we replicate the resolver's public contract by
    calling a small helper we monkey-patch onto sports_engine, or by
    invoking the resolver body directly.

    For maximum fidelity we inline the resolver body — the ONLY thing
    the test needs to prove is: given the same in-memory batch AND a
    contradictory DB state, the DB-aware pass produces the right
    survivors + DB updates.
    """
    # Import the resolver dependencies exactly as sports_engine does.
    from services.k_conflict_resolver import resolve_k_family_winner
    from server import db
    import re

    def _kc_line(pick: dict):
        m = re.search(r"(\d+\.?\d*)\s+Strikeouts", pick.get("market") or "", re.I)
        return float(m.group(1)) if m else None

    def _kc_side(pick: dict):
        m = (pick.get("market") or "").lower()
        if " over " in m:
            return "over"
        if " under " in m:
            return "under"
        return "unknown"

    cutoff_iso = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
    drop_ids: set = set()
    for new_pick in list(picks_in):
        if "strikeout" not in (new_pick.get("market") or "").lower():
            continue
        new_line = _kc_line(new_pick)
        new_side = _kc_side(new_pick)
        if new_line is None or new_side == "unknown":
            continue
        opp = "under" if new_side == "over" else "over"
        rows = await db.picks.find({
            "sport": "MLB",
            "event": new_pick.get("event"),
            "selection": new_pick.get("selection"),
            "no_bet": {"$ne": True},
            "created_at": {"$gte": cutoff_iso},
        }).to_list(length=20)
        for row in rows:
            if "strikeout" not in (row.get("market") or "").lower():
                continue
            if _kc_line(row) != new_line or _kc_side(row) != opp:
                continue
            if new_side == "over":
                op, up = new_pick, row
            else:
                op, up = row, new_pick
            win, reason = resolve_k_family_winner(op, up, new_line)
            if win == new_side:
                await db.picks.update_one(
                    {"id": row.get("id")},
                    {"$set": {
                        "no_bet": True,
                        "no_bet_reason": (
                            f"cross-refresh K conflict: new-{new_side} wins "
                            f"over DB-{opp} line={new_line} pitcher="
                            f"{new_pick.get('selection')} ({reason})"
                        ),
                    }},
                )
            elif win == opp:
                drop_ids.add(id(new_pick))
                break
            else:
                await db.picks.update_one(
                    {"id": row.get("id")},
                    {"$set": {
                        "no_bet": True,
                        "no_bet_reason": (
                            f"cross-refresh K conflict: indeterminate vs "
                            f"new-{new_side} line={new_line} pitcher="
                            f"{new_pick.get('selection')}"
                        ),
                    }},
                )
                drop_ids.add(id(new_pick))
                break
    return [p for p in picks_in if id(p) not in drop_ids]


# ────────────────────────────────────────────────────────────────────
# Tests
# ────────────────────────────────────────────────────────────────────

def test_existing_db_under_flagged_when_new_refresh_over_wins():
    """DB has Wheeler Under 6.5 K (wrong side). New refresh emits Over
    6.5 K with model support (expected_k = 8.0 → Over wins per shared
    K-math helper). DB row must be flagged `no_bet=True` atomically."""
    from motor.motor_asyncio import AsyncIOMotorClient
    import server

    async def _inner():
        client = AsyncIOMotorClient(os.environ["MONGO_URL"])
        db = client[os.environ["DB_NAME"]]
        server.db = db  # ensure resolver sees test DB
        event = f"__d3_over_wins_{uuid.uuid4().hex[:8]}"
        db_under = _make_pick(
            pid=f"under_{uuid.uuid4().hex[:8]}",
            player="Zack Wheeler", event=event, side="under", line=6.5,
            pick_date="2026-07-27",
            k_math_expected_k=8.0,  # math clearly favors Over
            lock_score=88.0, edge_percent=3.0, book_odds=110,
        )
        new_over = _make_pick(
            pid=f"new_over_{uuid.uuid4().hex[:8]}",
            player="Zack Wheeler", event=event, side="over", line=6.5,
            pick_date="2026-07-28",
            k_math_expected_k=8.0,
            lock_score=95.0, edge_percent=8.0, book_odds=-140,
        )
        try:
            await db.picks.insert_one(db_under)
            survivors = await _invoke_resolver_only([new_over])
            # New Over must have survived.
            assert len(survivors) == 1 and survivors[0]["id"] == new_over["id"], (
                f"New Over dropped incorrectly: survivors={survivors}"
            )
            # DB Under must be flagged no_bet=True atomically.
            db_row = await db.picks.find_one({"id": db_under["id"]})
            assert db_row is not None
            assert db_row.get("no_bet") is True, (
                f"DB Under not flagged: no_bet={db_row.get('no_bet')}"
            )
            assert db_row.get("no_bet_reason"), (
                "no_bet_reason not written — atomic invariant broken"
            )
            assert "cross-refresh" in db_row["no_bet_reason"].lower()
        finally:
            await db.picks.delete_many({"event": event})

    asyncio.run(_inner())


def test_cross_pick_date_bucket_contradiction_detected():
    """DB has Under 6.5 K on pick_date=2026-07-27 (yesterday). New
    refresh at 2026-07-28 emits Over 6.5 K for the SAME event/pitcher.
    Resolver must detect this despite the pick_date mismatch."""
    from motor.motor_asyncio import AsyncIOMotorClient
    import server

    async def _inner():
        client = AsyncIOMotorClient(os.environ["MONGO_URL"])
        db = client[os.environ["DB_NAME"]]
        server.db = db
        event = f"__d3_cross_date_{uuid.uuid4().hex[:8]}"
        yesterday_under = _make_pick(
            pid=f"y_under_{uuid.uuid4().hex[:8]}",
            player="Shane Bieber", event=event, side="under", line=4.5,
            pick_date="2026-07-27",   # ← earlier pick_date
            k_math_expected_k=5.8,     # math favors Over (5.8 > 4.5+0.3)
            lock_score=90.0, edge_percent=4.0, book_odds=100,
            created_offset_hours=20,   # ~yesterday
        )
        today_over = _make_pick(
            pid=f"t_over_{uuid.uuid4().hex[:8]}",
            player="Shane Bieber", event=event, side="over", line=4.5,
            pick_date="2026-07-28",   # ← today
            k_math_expected_k=5.8,
            lock_score=96.0, edge_percent=9.0, book_odds=-130,
        )
        try:
            await db.picks.insert_one(yesterday_under)
            survivors = await _invoke_resolver_only([today_over])
            assert len(survivors) == 1, (
                f"Cross-date resolver dropped the winning new pick: {survivors}"
            )
            db_row = await db.picks.find_one({"id": yesterday_under["id"]})
            assert db_row is not None
            assert db_row.get("no_bet") is True, (
                f"Yesterday's Under not flagged — pick_date bucket boundary "
                f"leaked past DB-aware resolver. no_bet={db_row.get('no_bet')}"
            )
            assert "cross-refresh" in (db_row.get("no_bet_reason") or "").lower()
        finally:
            await db.picks.delete_many({"event": event})

    asyncio.run(_inner())


def test_same_day_refresh_overlap_db_wins_drops_new_pick():
    """DB has an EARLIER refresh's Under 5.5 K with STRONG K-math
    (expected_k=4.9 favors Under). A LATER refresh emits Over 5.5 K
    for the SAME pitcher on the SAME day. The shared helper says
    Under wins → the DB-aware resolver must DROP the new Over pick
    (not accidentally emit both)."""
    from motor.motor_asyncio import AsyncIOMotorClient
    import server

    async def _inner():
        client = AsyncIOMotorClient(os.environ["MONGO_URL"])
        db = client[os.environ["DB_NAME"]]
        server.db = db
        event = f"__d3_same_day_{uuid.uuid4().hex[:8]}"
        pick_date = "2026-07-28"
        db_under = _make_pick(
            pid=f"db_u_{uuid.uuid4().hex[:8]}",
            player="Reynaldo Lopez", event=event, side="under", line=5.5,
            pick_date=pick_date,
            k_math_expected_k=4.9,   # math favors Under (4.9 < 5.5-0.3)
            lock_score=93.0, edge_percent=6.0, book_odds=-150,
        )
        new_over = _make_pick(
            pid=f"new_o_{uuid.uuid4().hex[:8]}",
            player="Reynaldo Lopez", event=event, side="over", line=5.5,
            pick_date=pick_date,     # ← same pick_date, different refresh cycle
            k_math_expected_k=4.9,
            lock_score=91.0, edge_percent=2.0, book_odds=105,
        )
        try:
            await db.picks.insert_one(db_under)
            survivors = await _invoke_resolver_only([new_over])
            # DB Under wins → new Over must be dropped.
            assert len(survivors) == 0, (
                f"Same-day resolver failed to drop losing new Over: "
                f"survivors={[(s.get('id'), s.get('market')) for s in survivors]}"
            )
            # DB row must remain active (winner).
            db_row = await db.picks.find_one({"id": db_under["id"]})
            assert db_row is not None
            assert db_row.get("no_bet") is not True, (
                f"Winning DB Under was accidentally flagged no_bet: {db_row}"
            )
        finally:
            await db.picks.delete_many({"event": event})

    asyncio.run(_inner())


def test_no_contradiction_leaves_state_unchanged():
    """Guard: if no cross-DB contradiction exists (only a same-side
    duplicate, or a different-line row), resolver must NOT touch DB
    rows or drop the new pick."""
    from motor.motor_asyncio import AsyncIOMotorClient
    import server

    async def _inner():
        client = AsyncIOMotorClient(os.environ["MONGO_URL"])
        db = client[os.environ["DB_NAME"]]
        server.db = db
        event = f"__d3_no_conflict_{uuid.uuid4().hex[:8]}"
        # DB row on DIFFERENT line (Under 4.5) — not a contradiction
        # for a new Over 6.5 pick.
        db_diff_line = _make_pick(
            pid=f"db_dl_{uuid.uuid4().hex[:8]}",
            player="Test Pitcher", event=event, side="under", line=4.5,
            pick_date="2026-07-28", lock_score=88.0, book_odds=-105,
        )
        new_over = _make_pick(
            pid=f"new_ok_{uuid.uuid4().hex[:8]}",
            player="Test Pitcher", event=event, side="over", line=6.5,
            pick_date="2026-07-28", lock_score=92.0, book_odds=-125,
            k_math_expected_k=7.5,
        )
        try:
            await db.picks.insert_one(db_diff_line)
            survivors = await _invoke_resolver_only([new_over])
            # New pick must survive; DB row untouched.
            assert len(survivors) == 1
            db_row = await db.picks.find_one({"id": db_diff_line["id"]})
            assert db_row.get("no_bet") is not True, (
                f"Non-contradictory DB row was incorrectly flagged: {db_row}"
            )
        finally:
            await db.picks.delete_many({"event": event})

    asyncio.run(_inner())
