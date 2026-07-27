"""Regression tests for the 2026-07-28 consolidation of the MLB K
family Over/Under contradiction resolver.

Ensures:
  A. Shared helper `services.k_conflict_resolver.resolve_k_family_winner`
     picks the correct side using k_math_expected_k vs line.
  B. `_reconcile_player_prop_contradictions` in server.py consults the
     helper for MLB_K family conflicts.
  C. Atomic no_bet writes always set BOTH `no_bet` and `no_bet_reason`
     in a single MongoDB $set (never one without the other).
  D. Cross pick_date "update in place" — when a corrected pick lands on
     a later pick_date and the wrong-side row still sits on an earlier
     pick_date, the earlier row is UPDATED in place and the redundant
     newer row is DELETED.
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone

os.environ.setdefault(
    "MONGO_URL",
    os.environ.get("MONGO_URL", "mongodb://localhost:27017"),
)
os.environ.setdefault("DB_NAME", "lockscore_db")

# Make backend/ importable when running via pytest from /app/backend.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_k_math_resolver_over_wins():
    """expected_k = 7.2, line = 6.5 → OVER wins."""
    from services.k_conflict_resolver import resolve_k_family_winner
    over = {"k_math_expected_k": 7.2, "edge_percent": 5, "lock_score": 90}
    under = {"k_math_expected_k": 7.2, "edge_percent": 4, "lock_score": 85}
    side, reason = resolve_k_family_winner(over, under, 6.5)
    assert side == "over", f"got side={side} reason={reason}"
    assert reason == "kmath_over"


def test_k_math_resolver_under_wins():
    """expected_k = 3.4, line = 4.5 → UNDER wins."""
    from services.k_conflict_resolver import resolve_k_family_winner
    over = {"k_math_expected_k": 3.4, "edge_percent": 4, "lock_score": 90}
    under = {"k_math_expected_k": 3.4, "edge_percent": 5, "lock_score": 85}
    side, reason = resolve_k_family_winner(over, under, 4.5)
    assert side == "under", f"got side={side} reason={reason}"
    assert reason == "kmath_under"


def test_k_math_indeterminate_falls_back_to_edge_lock():
    """expected_k = 6.4, line = 6.5, both within tolerance → fallback."""
    from services.k_conflict_resolver import resolve_k_family_winner
    over = {"k_math_expected_k": 6.4, "edge_percent": 7, "lock_score": 90}
    under = {"k_math_expected_k": 6.4, "edge_percent": 4, "lock_score": 85}
    side, reason = resolve_k_family_winner(over, under, 6.5)
    # 6.4 vs 6.5 → within ±0.3 tolerance → K-math indeterminate → fallback
    assert side == "over"
    assert reason == "edge_lock_over"


def test_k_math_missing_signal_falls_back():
    """No k_math_expected_k → straight edge/lock fallback."""
    from services.k_conflict_resolver import resolve_k_family_winner
    over = {"edge_percent": 2, "lock_score": 88}
    under = {"edge_percent": 6, "lock_score": 92}
    side, reason = resolve_k_family_winner(over, under, 5.5)
    assert side == "under"
    assert reason == "edge_lock_under"


def test_reconciler_atomic_no_bet_write_writes_both_fields():
    """`no_bet` and `no_bet_reason` must be set together in a single
    MongoDB update — neither can ever be persisted without the other.
    """
    from motor.motor_asyncio import AsyncIOMotorClient
    import server

    async def _inner():
        client = AsyncIOMotorClient(os.environ["MONGO_URL"])
        db = client[os.environ["DB_NAME"]]
        server.db = db  # monkey-patch reconciler to use test connection

        event = f"__test_atomic_{uuid.uuid4().hex[:8]}"
        player = "Test Pitcher"
        base = {
            "sport": "MLB",
            "event": event,
            "selection": player,
            "event_time": "2026-07-28T21:00:00Z",
            "book_odds": -110,
            "pick_date": "2026-07-28",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "no_bet": False,
        }
        over_id = f"over_{uuid.uuid4().hex[:8]}"
        under_id = f"under_{uuid.uuid4().hex[:8]}"
        docs = [
            {**base, "id": over_id,
             "market": f"{player} (TST) Over 6.5 Strikeouts",
             "edge_percent": 5.0, "lock_score": 90.0,
             "k_math_expected_k": 8.1, "k_math_gate": "passed"},
            {**base, "id": under_id,
             "market": f"{player} (TST) Under 6.5 Strikeouts",
             "edge_percent": 3.0, "lock_score": 88.0,
             "k_math_expected_k": 8.1, "k_math_gate": "passed"},
        ]
        try:
            await db.picks.insert_many(docs)
            await server._reconcile_player_prop_contradictions(docs, "2026-07-28")
            # Under (loser) must be no_bet=True with reason set.
            under_doc = await db.picks.find_one({"id": under_id})
            over_doc = await db.picks.find_one({"id": over_id})
            assert under_doc is not None
            assert over_doc is not None, "Over (winner) should still exist"
            # Atomic: both fields set (or neither).
            has_flag = under_doc.get("no_bet") is True
            has_reason = bool(under_doc.get("no_bet_reason"))
            assert has_flag and has_reason, (
                f"Atomic invariant violated: no_bet={under_doc.get('no_bet')}, "
                f"no_bet_reason={under_doc.get('no_bet_reason')!r}"
            )
            reason = under_doc["no_bet_reason"].lower()
            assert "over" in reason and "mlb_k" in reason
            # Winner Over must remain active (not no_bet).
            assert over_doc.get("no_bet") is not True, (
                f"Winner over got no_bet=True: {over_doc.get('no_bet_reason')!r}"
            )
        finally:
            await db.picks.delete_many({"event": event})

    asyncio.run(_inner())


def test_reconciler_cross_pick_date_updates_in_place():
    """When wrong-side pick sits on an EARLIER pick_date and the
    corrected pick lands on a LATER pick_date, the earlier row must be
    UPDATED in place (market/side/pick_date rewritten) and the newer
    row DELETED.
    """
    from motor.motor_asyncio import AsyncIOMotorClient
    import server

    async def _inner():
        client = AsyncIOMotorClient(os.environ["MONGO_URL"])
        db = client[os.environ["DB_NAME"]]
        server.db = db

        event = f"__test_inplace_{uuid.uuid4().hex[:8]}"
        player = "Test Ace"
        older_id = f"older_{uuid.uuid4().hex[:8]}"
        newer_id = f"newer_{uuid.uuid4().hex[:8]}"
        older_docs = [
            # Wrong side (Under 6.5) inserted YESTERDAY.
            {"id": older_id, "sport": "MLB", "event": event,
             "selection": player, "pick_date": "2026-07-27",
             "market": f"{player} (TST) Under 6.5 Strikeouts",
             "book_odds": -110, "event_time": "2026-07-27T23:00:00Z",
             "edge_percent": 2.0, "lock_score": 87.0,
             "k_math_expected_k": 8.2, "k_math_gate": "passed",
             "created_at": datetime.now(timezone.utc).isoformat(),
             "no_bet": False},
        ]
        newer_doc = {
            "id": newer_id, "sport": "MLB", "event": event,
            "selection": player, "pick_date": "2026-07-28",
            "market": f"{player} (TST) Over 6.5 Strikeouts",
            "book_odds": -105, "event_time": "2026-07-27T23:00:00Z",
            "edge_percent": 6.0, "lock_score": 93.0,
            "k_math_expected_k": 8.2, "k_math_gate": "passed",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "no_bet": False,
        }
        try:
            await db.picks.insert_many(older_docs + [newer_doc])
            # Reconciler is triggered by the just-inserted "newer" pick.
            await server._reconcile_player_prop_contradictions(
                [newer_doc], "2026-07-28",
            )
            older_after = await db.picks.find_one({"id": older_id})
            newer_after = await db.picks.find_one({"id": newer_id})
            # Older row should have been updated IN PLACE with winner's data.
            assert older_after is not None, "Older row must not be deleted"
            assert "Over 6.5" in older_after.get("market", ""), (
                f"Older row market not flipped to Over. got: "
                f"{older_after.get('market')!r}"
            )
            assert older_after.get("pick_date") == "2026-07-28", (
                f"Older row pick_date not updated. got: "
                f"{older_after.get('pick_date')!r}"
            )
            assert older_after.get("corrected_from_side") == "under", (
                "audit tag missing"
            )
            assert older_after.get("no_bet") is not True, (
                "In-place-updated keeper must be active, not no_bet"
            )
            # Newer duplicate must be deleted.
            assert newer_after is None, (
                f"Redundant newer winner row not deleted: {newer_after}"
            )
        finally:
            await db.picks.delete_many({"event": event})

    asyncio.run(_inner())
