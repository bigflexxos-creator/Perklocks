"""
UEA BACKFILL — one-time provenance stamper for existing picks.

Iterates over all on-board picks in the current slate and stamps
``evidence_authority`` from persisted factors — READ-ONLY on
``lock_score`` / ``published_lock_score``.  Never mutates the
authoritative score, only adds the audit-block.

Usage:
    cd /app/backend
    python -m services.uea_backfill_provenance
"""
from __future__ import annotations
import asyncio, os, sys

async def backfill():
    from motor.motor_asyncio import AsyncIOMotorClient
    from services.evidence_authority_contract import (
        compute_authority_score, peak_non_apex_eligible,
        enabled as _uea_enabled,
    )
    from services.evidence_authority_adapters import (
        build_contract_for_pick,
    )
    url = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
    client = AsyncIOMotorClient(url)
    db = client["lockscore_db"]
    IN_SCOPE = {"MLB", "NFL", "CFB", "SOCCER", "TENNIS"}
    stamped = 0
    scanned = 0
    q = {"evidence_authority": {"$exists": False}}
    cursor = db.picks.find(q,
        projection={"_id": 1, "sport": 1, "market": 1, "factors": 1,
                     "win_probability": 1, "model_win_probability": 1,
                     "probability_provenance": 1, "data_quality": 1,
                     "sim_stability": 1, "no_vig_implied_pct": 1,
                     "lock_score": 1, "matchup_score": 1,
                     "exact_threshold_hit_rate": 1,
                     "history_sample_size": 1})
    async for p in cursor:
        scanned += 1
        sport_up = str(p.get("sport") or "").upper()
        if sport_up not in IN_SCOPE:
            continue
        p["sport"] = sport_up
        f = p.get("factors") or {}
        sf = {k: v for k, v in f.items() if isinstance(v, (int, float))}
        c = build_contract_for_pick(p, f, sf)
        if c is None:
            continue
        r = compute_authority_score(c)
        upd = {"evidence_authority": r}
        elig, why = peak_non_apex_eligible(r)
        if elig:
            upd["peak_non_apex_eligible"] = True
        elif float(p.get("lock_score") or 0.0) >= 99.0:
            upd["peak_non_apex_denied_reason"] = why
        await db.picks.update_one({"_id": p["_id"]}, {"$set": upd})
        stamped += 1
        if stamped % 5000 == 0:
            print(f"[{stamped}] stamped ({scanned} scanned)", flush=True)
    print(f"[final] stamped={stamped} scanned={scanned}")
    client.close()

if __name__ == "__main__":
    asyncio.run(backfill())
