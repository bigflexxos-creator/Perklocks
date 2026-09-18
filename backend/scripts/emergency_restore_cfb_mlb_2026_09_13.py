"""EMERGENCY RESTORE — undo the accidental CFB / MLB lock-score damage
caused by re-running ``scripts/rescore_bq_authority.py`` after fixing
its factor-scale detection.

The rescore was designed to STAMP ``bet_quality_authority`` on rows
that were scored before BQ shipped.  It reused the CURRENT scoring
path (``compute_lock_score``) to derive the ceiling, but as a side
effect the same path also rewrote the top-level ``lock_score`` from
the just-run composite — which was inevitably lower than the original
CFB-path score because the isolated helper cannot reconstruct the CFB
game-model context that populated the original composite.

This script restores ``lock_score`` (and ``published_lock_score``)
from the immutable ``lock_score_v3_snapshot`` / ``lock_score_peak``
audit fields, and clears the freshly-written ``bet_quality_authority``
so no false BQ authority claim remains on the DB.

READ-BACK ONLY — no BQ writes, no composite changes, no gate
adjustments.  Safe to re-run: it is idempotent (already-restored rows
are no-ops).
"""
from __future__ import annotations
# P5 QUARANTINE — legacy direct grade writer. Exits unless
# PERKLOCKS_ALLOW_LEGACY_SETTLEMENT_WRITE=1 (audited override).
import sys as _sys, os as _os
_sys.path.insert(0, "/app/backend")
from services.settlement_authority import require_legacy_override as _rlo
_rlo(__file__)

import asyncio
import os
import sys

sys.path.insert(0, "/app/backend")

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

load_dotenv("/app/backend/.env")


async def _restore(db, sport: str) -> dict:
    stats = {"scanned": 0, "restored": 0, "bq_cleared": 0, "no_snap": 0}
    q = {
        "sport": sport,
        "lock_score_v3_snapshot": {"$type": "number"},
    }
    async for p in db.picks.find(q):
        stats["scanned"] += 1
        snap = float(p.get("lock_score_v3_snapshot") or 0.0)
        peak = float(p.get("lock_score_peak") or 0.0)
        # Restore target = MAX(v3_snapshot, peak) — both are historical
        # authoritative values written before the current rescore.
        target = max(snap, peak) if peak else snap
        if target <= 0:
            stats["no_snap"] += 1
            continue
        current = float(p.get("lock_score") or 0.0)
        update: dict = {}
        if abs(current - target) >= 0.1:
            update["lock_score"] = round(target, 1)
            update["published_lock_score"] = round(target, 1)
            stats["restored"] += 1
        # The freshly-stamped BQ authority (with matchup=1.7 or the
        # convergence=0 collapse) is misleading — clear it so the live
        # scoring path can produce a proper BQ on the next refresh.
        if p.get("bet_quality_authority") is not None:
            update["bet_quality_authority"] = None
            stats["bq_cleared"] += 1
        if update:
            await db.picks.update_one({"_id": p["_id"]}, {"$set": update})
    return stats


async def main():
    client = AsyncIOMotorClient(
        os.environ.get("MONGO_URL") or "mongodb://localhost:27017"
    )
    db = client["lockscore_db"]
    for sport in ("CFB", "MLB"):
        s = await _restore(db, sport)
        print(f"{sport}: scanned={s['scanned']} restored={s['restored']} "
              f"bq_cleared={s['bq_cleared']} no_snap={s['no_snap']}")


if __name__ == "__main__":
    asyncio.run(main())
