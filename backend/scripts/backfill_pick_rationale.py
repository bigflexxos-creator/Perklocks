"""One-shot backfill: walk every pick that has no `pick_rationale` yet,
run the universal enrichment, and persist the result.

Why this exists:
  The daily refresh pipeline calls `pick_enrichment.enrich_picks_with_active_registry`
  BEFORE `db.picks.insert_many(... ordered=False)`. But the picks
  collection has a unique index, so any pick whose key already exists is
  silently skipped (`code=11000`) — meaning the freshly-enriched payload
  never lands. This script closes that gap on the existing fleet:
  loads each pick in-place, runs enrichment, and writes the
  `pick_rationale` block (and `validation_block` flags) back via
  `update_one`.

Usage (from /app/backend):
    python3 scripts/backfill_pick_rationale.py            # today only
    python3 scripts/backfill_pick_rationale.py --all      # entire collection
    python3 scripts/backfill_pick_rationale.py --date 2026-06-28

Safe to re-run — picks that already carry a non-empty rationale are
skipped unless `--force` is passed.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import date
from typing import Any

# Add backend root to sys.path so `pick_enrichment` and `services.*`
# resolve regardless of where the script is invoked from.
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(ROOT, ".env"))

from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402
from pick_enrichment import enrich_picks_with_active_registry  # noqa: E402


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--all", action="store_true",
                   help="Backfill the entire picks collection (default: today only)")
    p.add_argument("--date", default=None,
                   help="Backfill a specific pick_date (YYYY-MM-DD)")
    p.add_argument("--force", action="store_true",
                   help="Re-enrich picks that already have a pick_rationale")
    p.add_argument("--limit", type=int, default=0,
                   help="Stop after N picks (0 = no cap)")
    args = p.parse_args()

    mongo_url = os.environ["MONGO_URL"]
    db_name = os.environ.get("DB_NAME", "test_database")
    client = AsyncIOMotorClient(mongo_url)
    db = client[db_name]

    q: dict[str, Any] = {}
    if not args.all:
        q["pick_date"] = args.date or date.today().isoformat()
    if not args.force:
        q["$or"] = [
            {"pick_rationale": {"$exists": False}},
            {"pick_rationale": None},
            {"pick_rationale": {}},
        ]

    cursor = db.picks.find(q)
    batch: list[dict] = []
    BATCH = 200
    enriched = 0
    blocked = 0
    skipped = 0
    mlb_intel = 0
    seen = 0

    async def flush(batch: list[dict]) -> None:
        nonlocal enriched, blocked, skipped, mlb_intel
        if not batch:
            return
        # Run the in-memory enricher on the batch
        counts = enrich_picks_with_active_registry(batch)
        enriched += counts.get("enriched", 0)
        blocked += counts.get("blocked_inactive", 0)
        skipped += counts.get("skipped_team_pick", 0)
        mlb_intel += counts.get("mlb_intel", 0)
        # Persist each pick's new rationale + validation flags.
        for pick in batch:
            update: dict[str, Any] = {}
            if isinstance(pick.get("pick_rationale"), dict) and pick["pick_rationale"]:
                update["pick_rationale"] = pick["pick_rationale"]
            if pick.get("validation_block"):
                update["validation_block"] = pick["validation_block"]
                update["validation_block_reason"] = pick.get("validation_block_reason")
            if not update:
                continue
            await db.picks.update_one({"id": pick["id"]}, {"$set": update})

    async for doc in cursor:
        seen += 1
        # Drop Mongo's _id so the enricher and update_one don't choke.
        doc.pop("_id", None)
        batch.append(doc)
        if len(batch) >= BATCH:
            await flush(batch)
            batch = []
            print(f"  …processed {seen} picks (enriched={enriched}, blocked={blocked})")
        if args.limit and seen >= args.limit:
            break
    await flush(batch)

    print("=" * 50)
    print(f"Backfill complete:")
    print(f"  scanned       : {seen}")
    print(f"  enriched      : {enriched}")
    print(f"  blocked inactive: {blocked}")
    print(f"  team picks    : {skipped}")
    print(f"  MLB intel deep : {mlb_intel}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
