"""Rollback NBA Prop Authority back-fill on past-event / immutable picks.

The initial back-fill filtered on sport + wp + identity + is-nba-prop,
but did NOT exclude rows whose event_time / commence_time is in the past.
Per the certification contract, historical published picks must have
immutable lock scores — this script restores `lock_score` from
`nba_prop_authority_prior` for every touched row whose event is past.

Does NOT touch rows that are still upcoming — those keep the new score.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402

MONGO_URL = os.getenv("MONGO_URL", "mongodb://localhost:27017")
DB_NAMES = ("lockscore_db",)


def _grade(score: float) -> str:
    if score >= 95: return "A+"
    if score >= 90: return "A"
    if score >= 85: return "A-"
    if score >= 80: return "B+"
    if score >= 75: return "B"
    if score >= 70: return "B-"
    if score >= 65: return "C+"
    if score >= 60: return "C"
    return "D"


async def _rollback_coll(coll, now_iso: str) -> dict:
    stats = {"scanned": 0, "rolled_back": 0, "kept": 0}
    cur = coll.find(
        {"nba_prop_authority_prior": {"$exists": True},
         "$or": [
             {"event_time":    {"$lt": now_iso}},
             {"commence_time": {"$lt": now_iso}},
         ]},
        {"id": 1, "lock_score": 1,
         "nba_prop_authority_prior": 1},
    )
    async for d in cur:
        stats["scanned"] += 1
        prior = d.get("nba_prop_authority_prior")
        if prior is None:
            stats["kept"] += 1
            continue
        await coll.update_one(
            {"_id": d["_id"]},
            {"$set": {
                "lock_score": float(prior),
                "grade": _grade(float(prior)),
                "nba_prop_authority_rolled_back": True,
                "nba_prop_authority_applied": False,
            },
             "$unset": {
                 "nba_prop_authority_wp": "",
                 "nba_prop_authority_ceiling": "",
                 "nba_prop_authority_evidence_mult": "",
             }}
        )
        stats["rolled_back"] += 1
    return stats


async def main():
    client = AsyncIOMotorClient(MONGO_URL)
    now_iso = datetime.now(timezone.utc).isoformat()
    for dbname in DB_NAMES:
        db = client[dbname]
        for coll_name in ("canonical_picks", "picks"):
            if coll_name not in await db.list_collection_names():
                continue
            coll = db[coll_name]
            s = await _rollback_coll(coll, now_iso)
            print(f"{dbname}.{coll_name}: {s}")


if __name__ == "__main__":
    asyncio.run(main())
