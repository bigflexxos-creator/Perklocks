"""Retire ALL current CFB picks whose engine version predates the sign-fix
correction. Next refresh cycle regenerates them with corrected SP+ math.

Safe: only sets off_board=True + retirement_reason. Never touches
Lock Score / Win Probability / factors on any surviving row.
"""
from __future__ import annotations
import asyncio, sys
from datetime import datetime, timezone

sys.path.insert(0, "/app/backend")
from deps import db

CURRENT_VERSION = "cfb_sp_game.v3.2026-06-signfix"


async def main():
    now_iso = datetime.now(timezone.utc).isoformat()
    match = {
        "sport": "CFB",
        "off_board": {"$ne": True},
        "cfb_engine_version": {"$ne": CURRENT_VERSION},
    }
    victims = await db.picks.find(
        match, {"_id": 0, "id": 1, "event": 1, "market": 1,
                "lock_score": 1, "win_probability": 1,
                "cfb_engine_version": 1}
    ).to_list(length=5000)
    print(f"Victims for retirement: {len(victims)}")
    if not victims:
        return
    result = await db.picks.update_many(
        match,
        {"$set": {
            "off_board":         True,
            "retired_at":        now_iso,
            "retirement_reason": "cfb_sp_signfix_v3_regen",
        }},
    )
    print(f"Retired {result.modified_count} CFB rows for sign-fix regeneration.")
    # Show sample of extreme values retired.
    extreme = [v for v in victims if (v.get("win_probability") or 0) >= 90.0]
    print(f"  · of which {len(extreme)} had WP >= 90% (candidates for extreme bug).")
    for v in extreme[:10]:
        print(f"    - {v.get('event')} | {v.get('market')} | WP={v.get('win_probability')} LS={v.get('lock_score')} eng={v.get('cfb_engine_version')}")


if __name__ == "__main__":
    asyncio.run(main())
