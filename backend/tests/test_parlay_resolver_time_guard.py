"""Regression test for the parlay resolver's event-time proximity guard
(2026-07-04 fix).

User complaint: "when I add parlay to history it's grading games as wins
that haven't even played yet".

Root cause: `resolve_saved_parlays` used (sport, event, market,
selection) as a snapshot-match identity for legs whose source pick had
been wiped. Two games between the same teams (e.g. a 3-game MLB series;
tennis players meeting on consecutive days of a tournament) share this
identity — so a future leg would match yesterday's completed game and
inherit its `won`/`lost` status.

Fix: the snapshot fallback now requires the candidate pick's
`event_time` to be within ±36 h of the leg's stored event_time AND the
leg's own event_time must be ≤ now.

Run: python -m pytest backend/tests/test_parlay_resolver_time_guard.py -q
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402
from parlay_history import resolve_saved_parlays  # noqa: E402


def _db():
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    return client[os.environ.get("DB_NAME", "lockscore_db")]


def test_future_leg_never_inherits_stale_completed_pick_status():
    async def go():
        db = _db()
        now = datetime.now(timezone.utc)
        future = (now + timedelta(days=1)).isoformat()
        past = (now - timedelta(days=10)).isoformat()
        completed_pick = {
            "id": "test_completed_pick_regr_123",
            "sport": "MLB",
            "event": "Regr Yankees @ Regr Red Sox",
            "market": "Regr Yankees Moneyline",
            "selection": "Regr Yankees",
            "event_time": past,
            "status": "won",
        }
        await db.picks.replace_one({"id": completed_pick["id"]}, completed_pick, upsert=True)
        parlay = {
            "id": "p_regr_futurebug_test",
            "user_id": "test_user_regr_bugcheck",
            "leg_ids": ["ghost_pick_a", "ghost_pick_b"],
            "legs": [
                {"pick_id": "ghost_pick_a", "sport": "MLB",
                 "event": "Regr Yankees @ Regr Red Sox",
                 "market": "Regr Yankees Moneyline",
                 "selection": "Regr Yankees",
                 "event_time": future, "book_odds": -120, "status": "pending"},
                {"pick_id": "ghost_pick_b", "sport": "MLB",
                 "event": "Regr Cubs @ Regr Cardinals",
                 "market": "Regr Cubs Moneyline",
                 "selection": "Regr Cubs",
                 "event_time": future, "book_odds": -110, "status": "pending"},
            ],
            "status": "live",
            "created_at": now.isoformat(),
            "stake": 1.0,
            "combined_odds": 200,
        }
        await db.parlay_history.replace_one({"id": parlay["id"]}, parlay, upsert=True)
        try:
            await resolve_saved_parlays(db)
            got = await db.parlay_history.find_one({"id": parlay["id"]})
            assert got["status"] == "live", f"future parlay mis-graded: {got['status']}"
            for leg in got["legs"]:
                assert leg["status"] == "pending", f"future leg mis-graded: {leg['status']}"
        finally:
            await db.parlay_history.delete_one({"id": parlay["id"]})
            await db.picks.delete_one({"id": completed_pick["id"]})
    asyncio.run(go())


def test_past_leg_still_settles_from_snapshot_within_window():
    async def go():
        db = _db()
        now = datetime.now(timezone.utc)
        past = (now - timedelta(hours=6)).isoformat()
        completed_pick = {
            "id": "test_completed_within_win_123",
            "sport": "MLB",
            "event": "Win Yankees @ Win Red Sox",
            "market": "Win Yankees Moneyline",
            "selection": "Win Yankees",
            "event_time": past,
            "status": "won",
        }
        other_pick = {
            "id": "test_other_completed_pick_123",
            "sport": "MLB",
            "event": "Win Cubs @ Win Cardinals",
            "market": "Win Cubs Moneyline",
            "selection": "Win Cubs",
            "event_time": past,
            "status": "won",
        }
        await db.picks.replace_one({"id": completed_pick["id"]}, completed_pick, upsert=True)
        await db.picks.replace_one({"id": other_pick["id"]}, other_pick, upsert=True)
        parlay = {
            "id": "p_regr_pastwin_test",
            "user_id": "test_user_pastwin",
            "leg_ids": ["ghost_c", "ghost_d"],
            "legs": [
                {"pick_id": "ghost_c", "sport": "MLB",
                 "event": "Win Yankees @ Win Red Sox",
                 "market": "Win Yankees Moneyline",
                 "selection": "Win Yankees",
                 "event_time": past, "book_odds": -120, "status": "pending"},
                {"pick_id": "ghost_d", "sport": "MLB",
                 "event": "Win Cubs @ Win Cardinals",
                 "market": "Win Cubs Moneyline",
                 "selection": "Win Cubs",
                 "event_time": past, "book_odds": -110, "status": "pending"},
            ],
            "status": "live",
            "created_at": (now - timedelta(hours=8)).isoformat(),
            "stake": 1.0,
            "combined_odds": 250,
        }
        await db.parlay_history.replace_one({"id": parlay["id"]}, parlay, upsert=True)
        try:
            await resolve_saved_parlays(db)
            got = await db.parlay_history.find_one({"id": parlay["id"]})
            assert got["status"] == "won"
            for leg in got["legs"]:
                assert leg["status"] == "won"
        finally:
            await db.parlay_history.delete_one({"id": parlay["id"]})
            await db.picks.delete_one({"id": completed_pick["id"]})
            await db.picks.delete_one({"id": other_pick["id"]})
    asyncio.run(go())


def test_stale_older_pick_beyond_window_ignored():
    async def go():
        db = _db()
        now = datetime.now(timezone.utc)
        yesterday = (now - timedelta(hours=6)).isoformat()
        ten_days_ago = (now - timedelta(days=10)).isoformat()
        stale = {
            "id": "test_stale_pick_regr_123",
            "sport": "MLB",
            "event": "Stale Rays @ Stale Jays",
            "market": "Stale Rays Moneyline",
            "selection": "Stale Rays",
            "event_time": ten_days_ago,
            "status": "won",
        }
        await db.picks.replace_one({"id": stale["id"]}, stale, upsert=True)
        parlay = {
            "id": "p_regr_stale_test",
            "user_id": "test_user_stale",
            "leg_ids": ["ghost_e", "ghost_f"],
            "legs": [
                {"pick_id": "ghost_e", "sport": "MLB",
                 "event": "Stale Rays @ Stale Jays",
                 "market": "Stale Rays Moneyline",
                 "selection": "Stale Rays",
                 "event_time": yesterday, "book_odds": -120, "status": "pending"},
                {"pick_id": "ghost_f", "sport": "MLB",
                 "event": "Nada A @ Nada B",
                 "market": "Nada A Moneyline",
                 "selection": "Nada A",
                 "event_time": yesterday, "book_odds": -110, "status": "pending"},
            ],
            "status": "live",
            "created_at": (now - timedelta(hours=8)).isoformat(),
            "stake": 1.0,
            "combined_odds": 250,
        }
        await db.parlay_history.replace_one({"id": parlay["id"]}, parlay, upsert=True)
        try:
            await resolve_saved_parlays(db)
            got = await db.parlay_history.find_one({"id": parlay["id"]})
            assert got["legs"][0]["status"] == "pending"
            assert got["status"] == "live"
        finally:
            await db.parlay_history.delete_one({"id": parlay["id"]})
            await db.picks.delete_one({"id": stale["id"]})
    asyncio.run(go())
