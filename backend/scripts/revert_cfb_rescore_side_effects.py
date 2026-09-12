"""Revert CFB rescore side effects. My boundary-repair rescore
recomputed compute_lock_score with a partial pick_shim (missing
odds_at_pick / closing_odds / data_quality provenance), producing
scores ~0.5 lower than the pre-session state. Since the same-input
replay proves the boundary is behaviourally identity for CFB, the
pre-session v4 score (preserved in ``published_lock_score``) is the
correct value.  Restore lock_score = published_lock_score for CFB
v4 rows where my rescore lowered them."""
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
    ago = (now - timedelta(hours=24)).isoformat()
    later = (now + timedelta(days=14)).isoformat()

    # CFB rows where my rescore lowered lock_score below published_lock_score.
    # Restore lock_score = published_lock_score (the pre-session v4 value).
    res = await db.picks.update_many(
        {
            "sport": "CFB",
            "event_time": {"$gte": ago, "$lte": later},
            "lock_score_version": {"$regex": "^v4"},
            "$expr": {"$lt": [
                "$lock_score",
                {"$ifNull": ["$published_lock_score", 0]},
            ]},
        },
        [
            {"$set": {"lock_score": "$published_lock_score"}},
        ],
    )
    print(f"CFB restored: matched={res.matched_count} modified={res.modified_count}")

    # Same audit for MLB — restore where my rescore accidentally lowered
    # below the pre-session snapshot (rare; boundary should only lift).
    res2 = await db.picks.update_many(
        {
            "sport": "MLB",
            "event_time": {"$gte": ago, "$lte": later},
            "lock_score_version": {"$regex": "^v4"},
            # Only restore when ls dropped by ≥ 1 point vs pre-session pls.
            "$expr": {"$lt": [
                "$lock_score",
                {"$subtract": [
                    {"$ifNull": ["$published_lock_score", 0]}, 1.0,
                ]},
            ]},
        },
        [
            {"$set": {"lock_score": "$published_lock_score"}},
        ],
    )
    print(f"MLB drops restored: matched={res2.matched_count} modified={res2.modified_count}")


if __name__ == "__main__":
    asyncio.run(main())
