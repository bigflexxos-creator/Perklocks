"""Retire all current CFB picks lacking explicit cfb_engine_version
stamp so the next refresh cycle regenerates them through the fixed
production pipeline.

Read → mark → let production repopulate.  NEVER edits Lock Scores.
NEVER injects evidence.  NEVER modifies probabilities.

Run:
    cd /app/backend && python -m scripts.maintenance.retire_cfb_for_regen
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/app/backend")

from deps import db


async def main() -> None:
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    # Every current CFB pick with a future event_time AND no explicit
    # engine version stamp — those were emitted by the pre-fix pipeline
    # and must be regenerated end-to-end.
    q = {"sport": "CFB",
         "event_time": {"$gte": now_iso},
         "off_board": {"$ne": True},
         "cfb_engine_version": {"$in": [None, "", {"$exists": False}]}}
    # $in with $exists false doesn't compose cleanly — split into two.
    q_split_a = {"sport": "CFB",
                  "event_time": {"$gte": now_iso},
                  "off_board": {"$ne": True},
                  "cfb_engine_version": {"$exists": False}}
    q_split_b = {"sport": "CFB",
                  "event_time": {"$gte": now_iso},
                  "off_board": {"$ne": True},
                  "cfb_engine_version": None}
    match = {"$or": [q_split_a, q_split_b]}

    victims = await db.picks.find(match, {"_id": 0, "id": 1, "event": 1,
                                            "market": 1, "lock_score": 1}
                                    ).to_list(length=5000)
    if not victims:
        print("No pre-fix CFB picks found for regeneration.")
        return

    result = await db.picks.update_many(
        match,
        {"$set": {
            "off_board":         True,
            "retired_at":        now_iso,
            "retirement_reason": "cfb_pre_fix_regen_r1_r2_r3",
        }},
    )
    print(f"Retired {result.modified_count} of {len(victims)} pre-fix "
          f"CFB rows.  Next refresh cycle will regenerate with:")
    print("  • R1 factor-persistence merge (Model Fair Prob et al.)")
    print("  • R2 returning-prod + portal-net ctx pre-load")
    print("  • R3 probability_provenance + cfb_engine_version stamp")


if __name__ == "__main__":
    asyncio.run(main())
