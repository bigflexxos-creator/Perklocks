"""SURGICAL RESTORATION — un-retire legacy CFB picks retired by
`scripts.maintenance.retire_cfb_pre_fix_v2` during the CFB naming/
emission repair sequence.

**Root cause identified (2026-06-15)**
    ``scripts/maintenance/retire_cfb_pre_fix_v2.py`` (invoked at
    2026-09-12T15:42:33Z, BEFORE this session) mass-marked
    ``off_board = True`` on legacy CFB picks with ``lock_score >= 95``
    that lacked current-engine factor-key provenance
    (``Model Fair Prob``, ``__data_quality``, ``SP+ Margin Base``,
    ``Sportsbook Implied Prob``).  The retirement criterion
    ``_has_current_engine_factors`` returned False because the CFB
    persistence layer stores ``factors={}`` for both legacy AND
    current v4-stamped rows — meaning the retirement should have hit
    ZERO rows but hit the entire legacy population instead.

    Consequence: legacy CFB v3 rows scored at LS ≥ 95 (up to 98.0) were
    hidden from the live board, while the fresh v4 CFB rows scored
    with the same empty ``factors`` dict cap at 78.4 (confidence-first
    composite with alignment default = 50).  Net effect: CFB Lock
    confidence distribution suppressed from historical maxima of
    95–98 down to ~78, contradicting the user directive that this
    repair sequence should NOT change the CFB confidence distribution.

**Surgical restoration contract**
    Un-retire ONLY rows tagged ``retirement_reason ==
    "cfb_pre_fix_stale_v2_factor_level"``.  Do NOT touch:
        * any weight, floor, bonus, or model formula
        * legit off_board flags (chalk_trap / longshot_trap /
          player_team_invalid / lock<85 for real v4 low-score rows)
        * MLB normalisation boundary or MLB rescored rows
        * NFL / Soccer / Tennis / NHL / NBA rows
    The unretire clears ``off_board``, ``retired_at``, and
    ``retirement_reason`` so ``board_visibility.tag_board_visibility``
    reconsiders the row on its next canonical cycle.
"""
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

    # Only touch CFB rows retired by the specific pre-fix script and
    # still inside the current wagering window.
    q = {
        "sport": "CFB",
        "retirement_reason": "cfb_pre_fix_stale_v2_factor_level",
        "event_time": {"$gte": ago, "$lte": later},
    }
    before = await db.picks.count_documents(q)
    print(f"Legacy CFB rows retired by pre_fix_v2 in current window: {before}")

    if before == 0:
        print("Nothing to restore.")
        return

    res = await db.picks.update_many(
        q,
        {
            "$set": {"off_board": False},
            "$unset": {
                "retired_at": "",
                "retirement_reason": "",
                "off_board_reasons": "",
            },
        },
    )
    print(f"Restored: matched={res.matched_count} modified={res.modified_count}")

    # Post-condition audit: how many CFB rows now have published_lock_score >= 85?
    now_iso = now.isoformat()
    future_eligible = await db.picks.count_documents({
        "sport": "CFB",
        "event_time": {"$gte": now_iso, "$lte": later},
        "off_board": {"$ne": True},
        "no_bet": {"$ne": True},
        "published_lock_score": {"$gte": 85},
    })
    print(f"CFB future-window on-board >=85 picks after restore: {future_eligible}")


if __name__ == "__main__":
    asyncio.run(main())
