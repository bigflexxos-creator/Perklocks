"""NFL ALT REGRESSION VERIFICATION — compare current alt state to the
frozen snapshot to prove ingestion / thresholds / odds / WP / identity
were preserved through the surgical BQ authority fixes."""
from __future__ import annotations
import asyncio, json, os, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
load_dotenv()


async def main():
    from services.perklocks_day import current_slate_day
    today = current_slate_day()
    snapshot_path = f"/app/memory/nfl_alt_freeze_snapshot_{today}.json"
    if not os.path.exists(snapshot_path):
        print(f"MISS: no snapshot at {snapshot_path}")
        return
    with open(snapshot_path) as f:
        snap = json.load(f)
    c = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = c[os.environ.get("DB_NAME", "lockscore_db")]

    rows = await db.picks.find(
        {"sport": "NFL", "pick_date": today, "is_alt": True}
    ).sort([("_id", 1)]).to_list(None)
    key_fields = (
        "canonical_pick_id", "external_id", "player", "player_name",
        "canonical_player_id", "player_team", "canonical_team_id",
        "event", "event_time", "market", "line", "book_odds",
        "win_probability", "calibrated_win_probability", "is_alt",
        "is_alt_line", "selection", "side",
    )

    def _key(r):
        return {k: r.get(k) for k in key_fields}

    current = [_key(r) for r in rows]
    frozen = snap["rows"]

    # Compare
    frozen_map = {r.get("canonical_pick_id") or r.get("external_id"): r for r in frozen}
    current_map = {r.get("canonical_pick_id") or r.get("external_id"): r for r in current}

    frozen_ids = set(frozen_map)
    current_ids = set(current_map)
    added = current_ids - frozen_ids
    removed = frozen_ids - current_ids
    common = frozen_ids & current_ids

    field_diffs = []
    for cid in common:
        f = frozen_map[cid]
        c_ = current_map[cid]
        for k in key_fields:
            if f.get(k) != c_.get(k):
                field_diffs.append((cid, k, f.get(k), c_.get(k)))

    print(f"NFL ALT PRESERVATION")
    print(f"  frozen count:  {len(frozen)}")
    print(f"  current count: {len(current)}")
    print(f"  removed rows:  {len(removed)}")
    print(f"  added rows:    {len(added)}")
    print(f"  field diffs:   {len(field_diffs)}")
    if field_diffs:
        print("  first 10 diffs:")
        for d in field_diffs[:10]:
            print(f"    {d[0]}  {d[1]}  frozen={d[2]!r} current={d[3]!r}")


if __name__ == "__main__":
    asyncio.run(main())
