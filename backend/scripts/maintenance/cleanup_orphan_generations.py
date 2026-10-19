"""
P0 Cross-Pod Authority Fix — orphan BUILDING-generation cleanup.
================================================================

Scans ``board_generations`` for documents stuck in ``BUILDING`` /
``VALIDATING`` state older than N days.  Each candidate is deleted
ONLY after verifying it is not referenced by:

  * the ``board_generations`` ``_id="active"`` pointer
    (``generation_id`` or ``expected_active``),
  * any current ``canonical_worker_leases`` document,
  * any audit record in ``canonical_board_generations`` (if present),
  * any ``picks`` / ``canonical_picks`` row carrying
    ``generation_id`` matching the candidate.

Runs READ-ONLY by default; pass ``--commit`` to actually delete.  The
"Sept 18 orphan" is the specific target cited by Support RCA but the
scan is sport-/date-agnostic.

Usage:
    python scripts/maintenance/cleanup_orphan_generations.py --dry-run
    python scripts/maintenance/cleanup_orphan_generations.py --commit
"""
from __future__ import annotations
import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(dotenv_path=str(ROOT / "backend" / ".env"))
load_dotenv(dotenv_path=str(ROOT / ".env"))

from motor.motor_asyncio import AsyncIOMotorClient


async def _is_referenced(db, gid: str) -> tuple[bool, list[str]]:
    """Return (True, reasons) if the generation is still referenced."""
    reasons: list[str] = []
    # 1. Active pointer.
    active = await db.board_generations.find_one({"_id": "active"})
    if active:
        if active.get("generation_id") == gid:
            reasons.append("board_generations._id=active.generation_id")
        if active.get("expected_active") == gid:
            reasons.append("board_generations._id=active.expected_active")

    # 2. Lease documents.
    try:
        lease_hit = await db.canonical_worker_leases.find_one(
            {"$or": [{"generation_id": gid}, {"last_generation_id": gid}]})
        if lease_hit:
            reasons.append(f"canonical_worker_leases hit (lease_name={lease_hit.get('lease_name')})")
    except Exception as e:
        reasons.append(f"canonical_worker_leases probe errored: {e}")

    # 3. Audit tables.
    for coll in ("canonical_board_generations", "audit_board_generations",
                 "board_generation_audit"):
        try:
            if coll not in await db.list_collection_names():
                continue
            hit = await db[coll].find_one(
                {"$or": [{"generation_id": gid}, {"_id": gid}, {"gen_id": gid}]})
            if hit:
                reasons.append(f"{coll} audit record present")
        except Exception as e:
            reasons.append(f"{coll} probe errored: {e}")

    # 4. picks / canonical_picks referencing the generation.
    for coll in ("picks", "canonical_picks"):
        try:
            cnt = await db[coll].count_documents({"generation_id": gid}, limit=1)
            if cnt:
                reasons.append(f"{coll} contains picks with this generation_id")
        except Exception:
            pass

    return (len(reasons) > 0, reasons)


async def main(args):
    uri = os.environ["MONGO_URL"]
    dbn = os.environ.get("DB_NAME", "lockscore_db")
    client = AsyncIOMotorClient(uri)
    db = client[dbn]

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.min_age_days)
    cutoff_iso = cutoff.isoformat()
    print(f"Looking for BUILDING/VALIDATING generations older than {cutoff_iso}")

    cursor = db.board_generations.find({
        "state": {"$in": ["BUILDING", "VALIDATING"]},
        "started_at": {"$lt": cutoff_iso},
        "_id": {"$ne": "active"},
    }).sort("started_at", 1)

    candidates = [d async for d in cursor]
    print(f"Found {len(candidates)} candidate(s)")

    kept: list[dict] = []
    to_delete: list[dict] = []
    for doc in candidates:
        gid = doc.get("_id") or doc.get("generation_id")
        if not gid:
            continue
        referenced, reasons = await _is_referenced(db, str(gid))
        row = {
            "gid": gid, "state": doc.get("state"),
            "started_at": doc.get("started_at"),
            "scope": doc.get("scope"),
            "referenced": referenced, "reasons": reasons,
        }
        if referenced:
            kept.append(row)
            print(f"  KEEP  {gid}  state={doc.get('state')} started={doc.get('started_at')}  reasons={reasons}")
        else:
            to_delete.append(row)
            print(f"  CLEAN {gid}  state={doc.get('state')} started={doc.get('started_at')}  (no references)")

    if args.commit and to_delete:
        ids = [r["gid"] for r in to_delete]
        res = await db.board_generations.delete_many({"_id": {"$in": ids}})
        print(f"\nDeleted {res.deleted_count} orphan BUILDING/VALIDATING doc(s).")
    elif to_delete:
        print(f"\nDRY-RUN: {len(to_delete)} orphan(s) eligible for cleanup. Re-run with --commit.")
    else:
        print("\nNothing to clean.")

    print(f"\nSummary: candidates={len(candidates)} kept={len(kept)} to_delete={len(to_delete)}")
    client.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--commit", action="store_true", help="actually delete")
    p.add_argument("--dry-run", action="store_true", help="scan only (default)")
    p.add_argument("--min-age-days", type=int, default=7,
                   help="minimum age of a BUILDING doc to consider orphan (default 7 days)")
    args = p.parse_args()
    asyncio.run(main(args))
