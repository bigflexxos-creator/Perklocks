"""Final certification data collector for the MLB + CFB continuous
surgical closure.  Prints the numbers required by Parts A6/A7/A10 +
B1/B2/B3/B4/B7/B9 of the closure directive."""
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
    window_start = (now - timedelta(hours=1)).isoformat()
    window_end = (now + timedelta(hours=72)).isoformat()

    print("=" * 80)
    print(f"FINAL CERTIFICATION SNAPSHOT — {now.isoformat()}")
    print("=" * 80)

    for sport in ("MLB", "CFB"):
        print(f"\n─── {sport} ──────────────────────────────────────────────")
        total = await db.picks.count_documents({
            "sport": sport, "event_time": {"$gte": window_start, "$lte": window_end},
        })
        v4 = await db.picks.count_documents({
            "sport": sport, "event_time": {"$gte": window_start, "$lte": window_end},
            "lock_score_version": {"$regex": "^v4"},
        })
        unversioned = await db.picks.count_documents({
            "sport": sport, "event_time": {"$gte": window_start, "$lte": window_end},
            "lock_score_version": {"$in": [None, ""]},
        })
        eligible85 = await db.picks.count_documents({
            "sport": sport, "event_time": {"$gte": window_start, "$lte": window_end},
            "published_lock_score": {"$gte": 85},
        })
        published = await db.picks.count_documents({
            "sport": sport, "event_time": {"$gte": window_start, "$lte": window_end},
            "publication_source": {"$exists": True, "$ne": None},
        })
        print(f"  DB rows in window        = {total}")
        print(f"  v4-stamped               = {v4}")
        print(f"  unversioned              = {unversioned}")
        print(f"  eligible >=85            = {eligible85}")
        print(f"  published                = {published}")

        # Score distribution
        rows = await db.picks.aggregate([
            {"$match": {"sport": sport, "event_time": {"$gte": window_start, "$lte": window_end}}},
            {"$bucket": {
                "groupBy": "$lock_score",
                "boundaries": [0, 85, 90, 93, 96, 98, 99, 100, 101],
                "output": {"n":{"$sum":1}, "max":{"$max":"$lock_score"}}
            }}
        ]).to_list(20)
        print(f"  score distribution:")
        for r in rows: print(f"    [{r['_id']:>3}]  n={r['n']:5d}  max={r.get('max')}")

        # Alignment distribution
        agg = await db.picks.aggregate([
            {"$match": {"sport": sport, "event_time": {"$gte": window_start, "$lte": window_end}, "lock_score_version": {"$regex": "^v4"}}},
            {"$group": {"_id": None,
                "min":{"$min":"$lock_components.alignment"},
                "median":{"$median":{"input":"$lock_components.alignment","method":"approximate"}},
                "avg":{"$avg":"$lock_components.alignment"},
                "max":{"$max":"$lock_components.alignment"},
                "n":{"$sum":1},
                "n_zero":{"$sum":{"$cond":[{"$eq":["$lock_components.alignment",0]},1,0]}},
            }}
        ]).to_list(1)
        if agg:
            a = agg[0]
            print(f"  alignment stats: n={a['n']}  min={a['min']:.1f}  "
                  f"median={a['median']:.1f}  avg={a['avg']:.1f}  "
                  f"max={a['max']:.1f}  zeros={a['n_zero']}")

        # Top-3 example picks (proof of DB→wire parity)
        print(f"  top-3 picks:")
        cur = db.picks.find({"sport": sport, "event_time": {"$gte": window_start, "$lte": window_end}}).sort("lock_score", -1).limit(3)
        async for p in cur:
            lc = p.get("lock_components") or {}
            print(f"    · ls={p.get('lock_score')} pls={p.get('published_lock_score')} "
                  f"wp={p.get('win_probability')} edge={p.get('edge_percent')} "
                  f"align={lc.get('alignment')} v={p.get('lock_score_version')}")
            print(f"      market: {(p.get('market') or '')[:70]}")

    print("\n" + "=" * 80)
    print("CROSS-SPORT PARITY (Part C)")
    print("=" * 80)
    for sport in ("NFL", "Soccer"):
        top_score = await db.picks.find_one(
            {"sport": sport, "event_time": {"$gte": window_start, "$lte": window_end}, "lock_score_version": {"$regex": "^v4"}},
            sort=[("lock_score", -1)]
        )
        if top_score:
            print(f"  {sport} top v4 lock_score = {top_score.get('lock_score')} "
                  f"(mkt: {(top_score.get('market') or '')[:50]})")


if __name__ == "__main__":
    asyncio.run(main())
