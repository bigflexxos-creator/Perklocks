"""Sync published_lock_score to the current lock_score for MLB / CFB
picks that were rescored by the boundary repair.  Publication snapshot
is otherwise frozen at emission time and doesn't lift when we retro-
normalise the composite; without this sync, main-board eligibility
(services.main_board_eligibility.is_main_board_eligible) reads the
stale ``published_lock_score`` and hides the newly-lifted picks."""
from __future__ import annotations
import asyncio, os, sys
sys.path.insert(0, "/app/backend")
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")
from motor.motor_asyncio import AsyncIOMotorClient


async def main():
    client = AsyncIOMotorClient(os.environ.get("MONGO_URL") or "mongodb://localhost:27017")
    db = client["lockscore_db"]
    now = datetime.now(timezone.utc)
    window_start = (now - timedelta(hours=24)).isoformat()
    window_end = (now + timedelta(days=14)).isoformat()

    for sport in ("MLB", "CFB"):
        # Only sync where the divergence is large enough to matter
        # (Δ ≥ 1 point).  Keeps the update targeted.
        res = await db.picks.update_many(
            {
                "sport": sport,
                "event_time": {"$gte": window_start, "$lte": window_end},
                "lock_score_version": {"$regex": "^v4"},
                "$expr": {"$gte": [
                    {"$subtract": ["$lock_score",
                                    {"$ifNull": ["$published_lock_score", 0]}]},
                    1.0,
                ]},
            },
            [
                {"$set": {"published_lock_score": "$lock_score"}},
            ],
        )
        print(f"{sport}: matched={res.matched_count}  modified={res.modified_count}")


if __name__ == "__main__":
    asyncio.run(main())
