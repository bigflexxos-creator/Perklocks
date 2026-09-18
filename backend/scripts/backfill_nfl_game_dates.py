"""Backfill REAL NFL game dates onto player_game_actuals / team_game_actuals.

The nfl_player_weekly ingest stamped a placeholder event_time
("{season}-09-01T00:00:00Z") because weekly stats carry no gameday.  The
nflverse schedule (games.csv) carries the real gameday/gametime for every
game_id ("2025_07_NYG_DEN").  We join on canonical_event_id/event_id and
write the real kickoff (UTC-naive local → ISO date + time) plus
`event_time_source="nflverse_schedule"`.  No synthetic dates: rows whose
game_id is not in the schedule are left untouched and counted.

Usage: python scripts/backfill_nfl_game_dates.py /tmp/games.csv
"""
import asyncio, csv, os, sys
sys.path.insert(0, "/app/backend")
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import UpdateMany


async def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/games.csv"
    db = AsyncIOMotorClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]]
    sched: dict[str, str] = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            gid, day, tm = row.get("game_id"), row.get("gameday"), (row.get("gametime") or "13:00")
            if gid and day:
                sched[gid] = f"{day}T{tm}:00"
    print(f"schedule games={len(sched)}")
    total_matched = 0
    for coll in ("player_game_actuals", "team_game_actuals"):
        ids = set(await db[coll].distinct("canonical_event_id", {"sport": {"$in": ["nfl", "NFL"]}}))
        ids |= set(await db[coll].distinct("event_id", {"sport": {"$in": ["nfl", "NFL"]}}))
        ids.discard(None); ids.discard("")
        ops = []
        missing = 0
        for gid in ids:
            iso = sched.get(str(gid))
            if not iso:
                missing += 1
                continue
            ops.append(UpdateMany({"sport": {"$in": ["nfl", "NFL"]},
                                   "$or": [{"canonical_event_id": gid}, {"event_id": gid}],
                                   "event_time_source": {"$ne": "nflverse_schedule"}},
                                  {"$set": {"event_time": iso, "event_time_source": "nflverse_schedule"}}))
        modified = 0
        for i in range(0, len(ops), 500):
            r = await db[coll].bulk_write(ops[i:i + 500], ordered=False)
            modified += r.modified_count
        total_matched += modified
        print(f"{coll}: game_ids={len(ids)} not_in_schedule={missing} rows_updated={modified}")
    print("DONE", total_matched)

asyncio.run(main())
