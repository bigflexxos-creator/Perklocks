"""Universal backfill: populate event_time in player_game_actuals
from team_game_actuals across ALL sports (MLB / NFL / NBA / NHL).

Root cause pattern (originally observed on MLB):
- player_game_actuals docs land with event_time=None when the
  legacy ingestion pipeline only had game_pk and no game_datetime.
- Historical Intelligence then falls back to ingested_at
  (batch timestamp) making every "Game Log" date collapse to the
  ingestion date.
- The authoritative event date exists in team_game_actuals keyed
  by the SAME event_id, so we can bulk-join and fix it once.

Safe to re-run — only writes when event_time is currently null AND
team_game_actuals has a matching value.
"""
import asyncio, os
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import UpdateOne
from dotenv import load_dotenv

load_dotenv()

SPORTS = ["mlb", "nfl", "nba", "nhl"]


async def backfill_sport(db, sport: str) -> dict:
    """Backfill one sport.  Returns stats dict."""
    stats = {"sport": sport, "team_mappings": 0, "scanned": 0,
             "updated": 0, "missing_ref": 0}

    # Team map lookup
    ev_map: dict[str, str] = {}
    async for d in db.team_game_actuals.find(
        {"sport": sport, "event_time": {"$ne": None}},
        {"event_id": 1, "event_time": 1},
    ):
        eid = str(d.get("event_id") or "")
        et = d.get("event_time")
        if eid and et and eid not in ev_map:
            ev_map[eid] = et
    stats["team_mappings"] = len(ev_map)

    if not ev_map:
        return stats

    # Backfill player rows
    batch: list[UpdateOne] = []
    async for d in db.player_game_actuals.find(
        {"sport": sport, "event_time": None},
        {"_id": 1, "event_id": 1},
    ):
        stats["scanned"] += 1
        eid = str(d.get("event_id") or "")
        et = ev_map.get(eid)
        if not et:
            stats["missing_ref"] += 1
            continue
        batch.append(UpdateOne(
            {"_id": d["_id"]},
            {"$set": {"event_time": et,
                      "event_time_backfill_source": "team_game_actuals_join_v1"}},
        ))
        if len(batch) >= 1000:
            r = await db.player_game_actuals.bulk_write(batch, ordered=False)
            stats["updated"] += r.modified_count
            batch.clear()
    if batch:
        r = await db.player_game_actuals.bulk_write(batch, ordered=False)
        stats["updated"] += r.modified_count
    return stats


async def main():
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = client[os.environ["DB_NAME"]]

    print("Universal event_time backfill (player_game_actuals ⟵ team_game_actuals)")
    print("=" * 72)
    for sport in SPORTS:
        s = await backfill_sport(db, sport)
        remaining = await db.player_game_actuals.count_documents(
            {"sport": sport, "event_time": None}
        )
        print(f"{sport.upper():<6} · team_map={s['team_mappings']:>6,} · "
              f"scanned={s['scanned']:>6,} · updated={s['updated']:>6,} · "
              f"missing_ref={s['missing_ref']:>5,} · remaining_null={remaining:>5,}")
    print("=" * 72)
    print("DONE")


if __name__ == "__main__":
    asyncio.run(main())
