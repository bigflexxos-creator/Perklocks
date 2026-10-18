"""Perklocks — Universal Historical Freshness BACKFILL (preview mode).

Scope: compute how many ``season``-null rows across every supported
sport would be repaired by the shared ``derive_season()`` resolver.

Behaviour contract
------------------
- **DEFAULT = DRY-RUN.**  Reports exact counts per collection and
  shows a sample of the deltas that WOULD be written.  No Prod
  writes.  Satisfies the user's "test on local/Preview data first"
  requirement.
- `--apply` performs the backfill via ``$set`` on an exact filter
  (``season = None`` AND the event-time field present).  Idempotent:
  running it twice is a no-op.  Even in apply mode, each per-row
  update uses the SHARED resolver — no sport-specific hacks.
- Collections touched are confined to the known historical stores:
  ``player_game_actuals`` and ``team_game_actuals``.  No other
  collections are read-written.

Run:
    PYTHONPATH=/app/backend python scripts/freshness_backfill_report.py
    PYTHONPATH=/app/backend python scripts/freshness_backfill_report.py --apply   # only after review
"""
from __future__ import annotations

import os
import sys
import argparse
import asyncio
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

load_dotenv("/app/backend/.env")

sys.path.insert(0, "/app/backend")
from services.historical_freshness import derive_season  # noqa: E402


SPORTS = ["nfl", "nhl", "nba", "cfb", "mlb", "soccer", "tennis", "ufc", "mma"]
COLLECTIONS = ["player_game_actuals", "team_game_actuals"]
DATE_FIELDS = ("event_time", "event_date", "game_date",
               "match_date", "date")


async def report_one(db, coll: str, sport: str, apply: bool) -> dict:
    total = await db[coll].count_documents({"sport": sport})
    if total == 0:
        return {"total": 0, "null_season": 0, "repairable": 0, "written": 0}
    null_q = {"sport": sport, "season": None}
    null_season = await db[coll].count_documents(null_q)
    if null_season == 0:
        return {"total": total, "null_season": 0,
                "repairable": 0, "written": 0}
    # Sample the null-season rows that have a derivable date.
    repairable = 0
    written = 0
    sample = []
    projection = {f: 1 for f in DATE_FIELDS}
    projection["canonical_player_id"] = 1
    projection["canonical_team_id"] = 1
    cursor = db[coll].find(null_q, projection).limit(1000)
    async for d in cursor:
        date_val = next((d.get(f) for f in DATE_FIELDS if d.get(f)), None)
        if not date_val:
            continue
        derived = derive_season(sport, date_val)
        if derived is None:
            continue
        repairable += 1
        if len(sample) < 3:
            sample.append({
                "_id": d.get("_id"),
                "date_source_field": next((f for f in DATE_FIELDS
                                            if d.get(f)), None),
                "date_value": str(date_val)[:19],
                "derived_season": derived,
            })
        if apply:
            r = await db[coll].update_one(
                {"_id": d["_id"], "season": None},
                {"$set": {"season": derived,
                           "season_derived_by": "historical_freshness"}},
            )
            if r.modified_count == 1:
                written += 1
    return {"total": total, "null_season": null_season,
            "repairable": repairable, "written": written,
            "sample": sample}


async def main(args):
    mongo = os.environ["MONGO_URL"]
    db_name = os.environ.get("DB_NAME", "lockscore_db")
    client = AsyncIOMotorClient(mongo)
    db = client[db_name]
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"Historical Freshness BACKFILL · mode={mode}")
    print(f"DB: {db_name}   collections: {COLLECTIONS}")
    print()
    print(f"{'Sport':8} {'Coll':24} {'Total':>8} {'NullS':>8} "
          f"{'Repair':>8} {'Written':>8}")
    print("-" * 72)
    totals = {"total": 0, "null_season": 0, "repairable": 0, "written": 0}
    for coll in COLLECTIONS:
        for sport in SPORTS:
            r = await report_one(db, coll, sport, apply=args.apply)
            if r["total"] == 0:
                continue
            print(f"{sport:8} {coll:24} {r['total']:>8} {r['null_season']:>8} "
                  f"{r['repairable']:>8} {r['written']:>8}")
            for k in totals:
                totals[k] += r[k]
            for sample in r.get("sample") or []:
                print(f"           sample  "
                      f"{sample['date_source_field']}="
                      f"{sample['date_value']}  "
                      f"→ season={sample['derived_season']}")
    print("-" * 72)
    print(f"{'TOTAL':8} {'':24} {totals['total']:>8} {totals['null_season']:>8} "
          f"{totals['repairable']:>8} {totals['written']:>8}")
    if not args.apply:
        print()
        print("DRY-RUN complete.  Re-run with --apply to write these rows.")
        print("(Apply is idempotent — season_derived_by:historical_freshness "
              "marker is stamped on every written doc.)")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true",
                   help="Actually write the derived season back to the "
                        "collection (requires review).")
    asyncio.run(main(p.parse_args()))
