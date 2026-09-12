"""Unretire the v4 CFB rows that we've just reconstructed factors on
so canonical publication + the /api/picks/today filter can see them
again."""
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

    # Only touch v4 CFB rows in the CURRENT wagering window that were
    # retired for the R1/R2/R3 regen and now carry populated factors
    # (i.e. we've already reconstructed them).
    q = {
        "sport": "CFB",
        "event_time": {"$gte": ago, "$lte": later},
        "lock_score_version": {"$regex": "^v4"},
        "retirement_reason": {"$in": [
            "cfb_pre_fix_regen_r1_r2_r3",
            "cfb_pre_fix_stale_v2_factor_level",
            "cfb_pre_fix_empty_factors_high_lock_v2026_06_11",
        ]},
        "publication_state": "RETIRED_STALE_PRE_FIX",
        # Only rows that now carry populated factors from the repair
        "$expr": {"$gt": [
            {"$size": {"$ifNull": [{"$objectToArray": "$factors"}, []]}},
            0,
        ]},
    }
    total = await db.picks.count_documents(q)
    print(f"Retired v4 CFB rows to unretire (current-window, factors-repaired): {total}")

    if total == 0:
        return

    res = await db.picks.update_many(
        q,
        {
            "$set": {"off_board": False, "publication_state": "PUBLISHED"},
            "$unset": {
                "retired_at": "",
                "retirement_reason": "",
                "off_board_reasons": "",
            },
        },
    )
    print(f"Unretired: matched={res.matched_count} modified={res.modified_count}")

    # Future-window eligibility snapshot
    fut_ok = await db.picks.count_documents({
        "sport": "CFB",
        "event_time": {"$gte": now.isoformat(), "$lte": later},
        "off_board": {"$ne": True},
        "no_bet": {"$ne": True},
        "no_real_book_line": {"$ne": True},
        "book_odds": {"$exists": True, "$ne": None},
        "published_lock_score": {"$gte": 85},
    })
    print(f"CFB future-window on-board >=85 picks after unretire: {fut_ok}")


if __name__ == "__main__":
    asyncio.run(main())
