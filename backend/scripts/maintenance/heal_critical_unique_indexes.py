"""
heal_critical_unique_indexes — EXPLICIT operator maintenance script.
========================================================================

Purpose
-------
Repair the pre-existing MongoDB data state so every ``critical AND
unique`` IndexSpec declared in :mod:`services.index_registry` can be
built cleanly on the next backend boot.

This script is NEVER invoked by the backend application lifecycle.  It
is strictly an operator-run tool, hence the mandatory ``--commit``
flag; a dry-run is the default so an operator can preview the actions
before touching data.

Why not auto-heal?
------------------
The deployment-policy health check (and good engineering practice)
flags any startup path that can hard-delete rows from a production
collection.  Even though this heal preserves losers in a quarantine
collection, it still removes rows from the source — a destructive
automatic action we must not take at boot.  Hence: explicit operator
command, dry-run default, loud logs.

What it does (per critical-unique IndexSpec)
--------------------------------------------
1. Null-key rows — rows whose indexed key fields are null or missing
   are MOVED (not duplicated-then-deleted; a transactional
   insert-into-quarantine ``+ delete-from-source`` cycle) to
   ``<collection>__dup_quarantine`` with the following stamp:

       _quarantine_reason: "null_critical_unique_key"
       _quarantine_spec:   <IndexSpec.name>
       _quarantine_at:     ISO UTC timestamp

2. Duplicate rows on the key-set — the newest ``_id`` is retained in
   the source collection; every loser is MOVED to the quarantine
   collection with ``_quarantine_reason="duplicate_critical_unique_key"``.

Zero rows are destroyed — every removed row survives in the
quarantine collection and can be reinstated or inspected by the
operator at any time.

Usage
-----
    # dry run (default) — preview what WOULD be moved
    python backend/scripts/maintenance/heal_critical_unique_indexes.py

    # actually move rows into quarantine
    python backend/scripts/maintenance/heal_critical_unique_indexes.py --commit
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(dotenv_path=str(ROOT / "backend" / ".env"))
load_dotenv(dotenv_path=str(ROOT / ".env"))

from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402

from services.index_registry import _INDEX_SPECS  # noqa: E402


async def heal(db, *, commit: bool) -> dict[str, Any]:
    out: dict[str, Any] = {}
    critical_unique_specs = [
        s for s in _INDEX_SPECS if s.critical and s.unique
    ]
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    for spec in critical_unique_specs:
        key_fields = [k for k, _dir in spec.keys]
        coll = db[spec.collection]
        actions: dict[str, Any] = {
            "collection": spec.collection,
            "spec":       spec.name,
            "key_fields": key_fields,
            "null_rows_moved":        0,
            "duplicate_rows_moved":   0,
        }
        quarantine = db[f"{spec.collection}__dup_quarantine"]

        # 1. Null-key rows.
        null_filter: dict[str, Any] = {
            "$or": [
                {f: {"$in": [None]}} for f in key_fields
            ] + [
                {f: {"$exists": False}} for f in key_fields
            ]
        }
        try:
            null_count = await coll.count_documents(null_filter)
            if null_count:
                moved = 0
                async for doc in coll.find(null_filter):
                    doc["_quarantine_reason"] = "null_critical_unique_key"
                    doc["_quarantine_spec"]   = spec.name
                    doc["_quarantine_at"]     = now
                    if commit:
                        try:
                            await quarantine.insert_one(doc)
                        except Exception as e:
                            print(f"  [warn] quarantine insert failed in "
                                  f"{spec.collection}: {e}")
                            continue
                    moved += 1
                if commit and moved:
                    deleted = await coll.delete_many(null_filter)
                    actions["null_rows_moved"] = int(deleted.deleted_count or 0)
                else:
                    actions["null_rows_moved"] = moved
        except Exception as e:
            print(f"  [warn] null-row scan failed for "
                  f"{spec.collection}.{spec.name}: {e}")

        # 2. Duplicates on the key-set.
        try:
            group_id = {f: f"${f}" for f in key_fields}
            pipeline = [
                {"$group": {
                    "_id":  group_id,
                    "ids":  {"$push": "$_id"},
                    "n":    {"$sum": 1},
                }},
                {"$match": {"n": {"$gt": 1}}},
            ]
            dup_losers: list[Any] = []
            async for g in coll.aggregate(pipeline, allowDiskUse=True):
                ids = g.get("ids") or []
                if len(ids) <= 1:
                    continue
                try:
                    winner = max(ids)
                except TypeError:
                    winner = ids[-1]
                dup_losers.extend([i for i in ids if i != winner])
            if dup_losers:
                moved = 0
                async for doc in coll.find({"_id": {"$in": dup_losers}}):
                    doc["_quarantine_reason"] = "duplicate_critical_unique_key"
                    doc["_quarantine_spec"]   = spec.name
                    doc["_quarantine_at"]     = now
                    if commit:
                        try:
                            await quarantine.insert_one(doc)
                        except Exception as e:
                            print(f"  [warn] quarantine insert failed in "
                                  f"{spec.collection}: {e}")
                            continue
                    moved += 1
                if commit and moved:
                    deleted = await coll.delete_many(
                        {"_id": {"$in": dup_losers}},
                    )
                    actions["duplicate_rows_moved"] = int(
                        deleted.deleted_count or 0,
                    )
                else:
                    actions["duplicate_rows_moved"] = moved
        except Exception as e:
            print(f"  [warn] dup scan failed for "
                  f"{spec.collection}.{spec.name}: {e}")

        if actions["null_rows_moved"] or actions["duplicate_rows_moved"]:
            out.setdefault(spec.collection, []).append(actions)
    return out


async def main(args):
    uri = os.environ["MONGO_URL"]
    dbn = os.environ.get("DB_NAME", "lockscore_db")
    client = AsyncIOMotorClient(uri)
    db = client[dbn]

    mode = "COMMIT" if args.commit else "DRY-RUN"
    print(f"── heal_critical_unique_indexes · mode={mode} · "
          f"db={dbn} ──\n")
    report = await heal(db, commit=args.commit)
    if not report:
        print("Nothing to heal — every critical+unique IndexSpec is "
              "satisfied on this cluster.")
    else:
        for coll, items in report.items():
            print(f"── {coll} ──")
            for a in items:
                print(f"  spec={a['spec']} "
                      f"null_moved={a['null_rows_moved']} "
                      f"dup_moved={a['duplicate_rows_moved']}")
    if not args.commit:
        print("\n(dry-run) Re-run with --commit to actually move rows "
              "into the quarantine collection.")
    client.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--commit", action="store_true",
                   help="actually move rows (default is dry-run)")
    asyncio.run(main(p.parse_args()))
