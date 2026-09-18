"""Backfill event_time in player_game_actuals (MLB) from team_game_actuals.

Root cause of "08/11 for every game log" bug:
- MLB player_game_actuals docs have event_time=None on 64k+ rows.
- MLBPlayerHistoricalAdapter falls back to ingested_at, which is the
  batch-ingestion timestamp — identical across the whole batch.
- team_game_actuals has the real event_time keyed by the same event_id.

This one-shot script joins them and populates event_time so game logs
show real, varied dates.  Safe to re-run — only writes when event_time
is currently null AND team_game_actuals has a value.
"""
import asyncio, os
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import UpdateOne
from dotenv import load_dotenv

load_dotenv()

async def main():
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = client[os.environ["DB_NAME"]]

    print("Loading event_id → event_time from team_game_actuals (MLB)…")
    ev_map: dict[str, str] = {}
    cur = db.team_game_actuals.find(
        {"sport": "mlb", "event_time": {"$ne": None}},
        {"event_id": 1, "event_time": 1},
    )
    async for d in cur:
        eid = str(d.get("event_id") or "")
        et = d.get("event_time")
        if eid and et and eid not in ev_map:
            ev_map[eid] = et
    print(f"  loaded {len(ev_map):,} event_id → event_time mappings")

    print("Scanning player_game_actuals (MLB, event_time is null)…")
    batch: list[UpdateOne] = []
    total = 0
    updated = 0
    missing = 0
    cur = db.player_game_actuals.find(
        {"sport": "mlb", "event_time": None},
        {"_id": 1, "event_id": 1},
    )
    async for d in cur:
        total += 1
        eid = str(d.get("event_id") or "")
        et = ev_map.get(eid)
        if not et:
            missing += 1
            continue
        batch.append(UpdateOne(
            {"_id": d["_id"]},
            {"$set": {"event_time": et,
                      "event_time_backfill_source": "team_game_actuals_join_v1"}},
        ))
        if len(batch) >= 1000:
            r = await db.player_game_actuals.bulk_write(batch, ordered=False)
            updated += r.modified_count
            batch.clear()
            if total % 10000 == 0:
                print(f"  processed {total:,} · updated {updated:,} · missing_ref {missing:,}")
    if batch:
        r = await db.player_game_actuals.bulk_write(batch, ordered=False)
        updated += r.modified_count

    print(f"\nDONE · scanned {total:,} · updated {updated:,} · missing_ref {missing:,}")
    remaining = await db.player_game_actuals.count_documents(
        {"sport": "mlb", "event_time": None}
    )
    print(f"Remaining MLB docs with null event_time: {remaining:,}")

if __name__ == "__main__":
    asyncio.run(main())
