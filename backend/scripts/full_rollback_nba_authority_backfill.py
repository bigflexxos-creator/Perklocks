"""Full rollback of the NBA Prop Authority back-fill — restore ALL touched
picks to their pre-authority ``nba_prop_authority_prior`` lock_score and
remove the authority flags so the next refresh scores them from scratch
with the certified elite-evidence gate.
"""
from __future__ import annotations
import asyncio, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from motor.motor_asyncio import AsyncIOMotorClient  # noqa

def _grade(s):
    if s>=95: return "A+"
    if s>=90: return "A"
    if s>=85: return "A-"
    if s>=80: return "B+"
    if s>=75: return "B"
    if s>=70: return "B-"
    if s>=65: return "C+"
    if s>=60: return "C"
    return "D"

async def main():
    client = AsyncIOMotorClient(os.getenv("MONGO_URL","mongodb://localhost:27017"))
    for dbname in ("lockscore_db",):
        db = client[dbname]
        for coll_name in ("canonical_picks","picks"):
            if coll_name not in await db.list_collection_names(): continue
            coll = db[coll_name]
            scanned = restored = kept = 0
            cur = coll.find({"nba_prop_authority_prior":{"$exists":True}},
                            {"_id":1,"lock_score":1,"nba_prop_authority_prior":1,
                             "nba_prop_authority_rolled_back":1})
            async for d in cur:
                scanned += 1
                if d.get("nba_prop_authority_rolled_back") is True:
                    kept += 1  # already rolled back previously
                    continue
                prior = d.get("nba_prop_authority_prior")
                if prior is None: kept+=1; continue
                await coll.update_one({"_id":d["_id"]},
                    {"$set":{"lock_score":float(prior),"grade":_grade(float(prior)),
                             "nba_prop_authority_rolled_back":True,
                             "nba_prop_authority_applied":False},
                     "$unset":{"nba_prop_authority_wp":"",
                               "nba_prop_authority_ceiling":"",
                               "nba_prop_authority_evidence_mult":""}})
                restored += 1
            print(f"{dbname}.{coll_name}: scanned={scanned} restored={restored} kept={kept}")

if __name__=="__main__": asyncio.run(main())
