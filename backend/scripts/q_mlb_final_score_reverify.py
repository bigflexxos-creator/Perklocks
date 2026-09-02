"""One-shot verifier re-run for picks that already have a v-active
correction on the ledger but whose `final_score` mirror was written
by the buggy v1 grader — Root Closure 2026-06.

We call the same authoritative `_mlb_verify_prop` used at runtime,
capture the stashed actual, and mirror-write `pick.final_score`
onto the compat mirror.  Never mutates settlement_events (already
correct).  Idempotent.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

load_dotenv(os.path.join(_BACKEND, ".env"))


async def run() -> dict:
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = client[os.environ["DB_NAME"]]
    from grading_validator import _mlb_verify_prop, _LAST_MLB_VERIFY_ACTUALS
    now = datetime.now(timezone.utc)
    stats = {"picks_reverified": 0, "final_score_mirrored": 0, "verifier_null": 0}

    # Any MLB pick that has a v-active correction with no stored value,
    # OR where final_score_needs_reverify is set, OR where the stored
    # final_score contradicts the stored status.
    q = {
        "sport": "MLB",
        "status": {"$in": ["won", "lost", "push"]},
        "$or": [
            {"final_score_needs_reverify": True},
            {"final_score_source": {"$exists": False}},
        ],
        "market": {"$regex": "Over|Under", "$options": "i"},
    }
    total = await db.picks.count_documents(q)
    print(f"[reverify] MLB picks to consider: {total}")

    n = 0
    async for pick in db.picks.find(q):
        n += 1
        pid = pick.get("id")
        try:
            grade = await _mlb_verify_prop(pick)
        except Exception as e:
            print(f"  err on {pid}: {e}")
            continue
        if grade is None:
            stats["verifier_null"] += 1
            continue
        auth = _LAST_MLB_VERIFY_ACTUALS.get(pid) or {}
        if not auth.get("final_score"):
            continue
        upd = {
            "final_score":            auth["final_score"],
            "final_score_source":     auth.get("verifier_source", "mlb_statsapi"),
            "final_score_verified_at": now.isoformat(),
        }
        # If the newly-verified grade differs from stored status, do NOT
        # touch settlement_events here — that flow already handled the
        # canonical correction.  Only sync the derived mirror.
        if grade != pick.get("status") and pick.get("status") in ("won", "lost", "push"):
            upd["status"] = grade
            upd["final_score_status_synced_at"] = now.isoformat()
        await db.picks.update_one({"_id": pick["_id"]}, {"$set": upd,
                                                          "$unset": {"final_score_needs_reverify": ""}})
        stats["picks_reverified"] += 1
        stats["final_score_mirrored"] += 1
        if n % 50 == 0:
            print(f"[reverify] progress: {n}/{total}  mirrored={stats['final_score_mirrored']}")
    stats["scanned"] = n
    stats["finished_at"] = datetime.now(timezone.utc).isoformat()
    return stats


if __name__ == "__main__":
    out = asyncio.run(run())
    print()
    print(json.dumps(out, indent=2, default=str))
