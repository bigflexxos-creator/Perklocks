"""Phase 3K — publication_mismatch_report `logged_at_dt` backfill.

Idempotent, dry-run-first, resumable.  Adds a BSON Date `logged_at_dt`
field to every row whose existing `logged_at` string parses cleanly.
NEVER deletes documents.  NEVER invents timestamps for invalid rows.

Usage:
  # 1. dry-run (default — no writes)
  python -m backend.scripts.backfills.backfill_publication_mismatch_logged_at_dt

  # 2. execute (mutates)
  python -m backend.scripts.backfills.backfill_publication_mismatch_logged_at_dt --execute

  # 3. resume from a checkpoint _id
  python -m backend.scripts.backfills.backfill_publication_mismatch_logged_at_dt --execute --resume-from <ObjectId>
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv


def _parse_iso(raw: str):
    """Parse an ISO-8601 string to a timezone-aware UTC datetime.  Raises
    on failure."""
    from dateutil.parser import isoparse
    dt = isoparse(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


async def run(*, execute: bool, batch_size: int, resume_from: str | None,
              limit: int | None, report_path: str | None) -> dict:
    load_dotenv("/app/backend/.env")
    sys.path.insert(0, "/app/backend")
    from motor.motor_asyncio import AsyncIOMotorClient
    from bson import ObjectId

    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db     = client[os.environ["DB_NAME"]]
    coll   = db.publication_mismatch_report

    query: dict = {
        "logged_at":    {"$type": "string"},
        "logged_at_dt": {"$exists": False},
    }
    if resume_from:
        query["_id"] = {"$gt": ObjectId(resume_from)}

    total_pending = await coll.count_documents(query)
    print(f"[phase3k] pending={total_pending} execute={execute} batch_size={batch_size}")

    scanned = 0
    migrated = 0
    invalid: list[dict] = []
    last_id = resume_from

    cursor = coll.find(query, {"_id": 1, "logged_at": 1}).sort("_id", 1)
    if limit:
        cursor = cursor.limit(limit)

    batch: list = []
    async for doc in cursor:
        scanned += 1
        try:
            dt = _parse_iso(doc["logged_at"])
        except Exception as e:
            invalid.append({
                "_id": str(doc["_id"]),
                "raw": doc.get("logged_at"),
                "error": f"{type(e).__name__}: {e}",
            })
            continue
        batch.append((doc["_id"], dt))
        last_id = str(doc["_id"])
        if len(batch) >= batch_size:
            if execute:
                await asyncio.gather(*[
                    coll.update_one({"_id": _id}, {"$set": {"logged_at_dt": dt}})
                    for _id, dt in batch
                ])
            migrated += len(batch)
            batch.clear()
            if scanned % (batch_size * 10) == 0:
                print(f"[phase3k] scanned={scanned} migrated={migrated} invalid={len(invalid)} last_id={last_id}")

    if batch:
        if execute:
            await asyncio.gather(*[
                coll.update_one({"_id": _id}, {"$set": {"logged_at_dt": dt}})
                for _id, dt in batch
            ])
        migrated += len(batch)

    result = {
        "phase":          "3K_backfill",
        "execute":        execute,
        "scanned":        scanned,
        "would_migrate":  migrated if not execute else 0,
        "migrated":       migrated if execute else 0,
        "invalid_count":  len(invalid),
        "invalid_sample": invalid[:20],
        "last_id":        last_id,
        "resume_hint":    last_id,
    }
    if report_path:
        Path(report_path).write_text(json.dumps(result, default=str, indent=2))
    print(json.dumps({k: v for k, v in result.items() if k != "invalid_sample"}, default=str))
    client.close()
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute",     action="store_true", help="mutate (default: dry-run)")
    ap.add_argument("--dry-run",     action="store_true", help="explicit dry-run (default)")
    ap.add_argument("--batch-size",  type=int, default=500)
    ap.add_argument("--resume-from", type=str, default=None)
    ap.add_argument("--limit",       type=int, default=None)
    ap.add_argument("--report-path", type=str, default=None)
    a = ap.parse_args()
    execute = a.execute and not a.dry_run
    asyncio.run(run(
        execute=execute,
        batch_size=a.batch_size,
        resume_from=a.resume_from,
        limit=a.limit,
        report_path=a.report_path,
    ))


if __name__ == "__main__":
    main()
