"""NFL ALT REGRESSION FREEZE (2026-09-13, Part K).

Before any authority repair lands, freeze the current NFL alt-line
candidate + published surface so we can prove post-fix that
canonical IDs, player identity, thresholds, odds, WP, and alt
classification are ALL byte-identical (or empty-list identical).
"""
from __future__ import annotations
import asyncio, json, os, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
load_dotenv()


async def snapshot(db, pick_date: str) -> dict:
    q = {"sport": "NFL", "pick_date": pick_date, "is_alt": True}
    rows = await db.picks.find(q).sort([("_id", 1)]).to_list(None)
    key_fields = (
        "canonical_pick_id", "external_id", "player", "player_name",
        "canonical_player_id", "player_team", "canonical_team_id",
        "event", "event_time", "market", "line", "book_odds",
        "win_probability", "calibrated_win_probability", "is_alt",
        "is_alt_line", "selection", "side",
    )
    def _key(r):
        return {k: r.get(k) for k in key_fields}
    return {
        "pick_date": pick_date,
        "count":     len(rows),
        "rows":      [_key(r) for r in rows],
    }


async def main():
    from services.perklocks_day import current_slate_day
    today = current_slate_day()
    c = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = c[os.environ.get("DB_NAME", "lockscore_db")]
    snap = await snapshot(db, today)
    out = f"/app/memory/nfl_alt_freeze_snapshot_{today}.json"
    with open(out, "w") as f:
        json.dump(snap, f, default=str, sort_keys=True, indent=1)
    print(f"snapshot: {out}  count={snap['count']}")


if __name__ == "__main__":
    asyncio.run(main())
