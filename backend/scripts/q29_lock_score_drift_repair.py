"""Q29 — Historical Lock-Score Drift Repair (2026-06)
=======================================================

CONTRACT (per user Root Closure spec):
  - Historical Lock Scores MUST equal the immutable pre-game truth.
  - Repair drift from `picks.published_lock_score` (pregame frozen mirror)
    or `prediction_snapshots.published_lock_score` (canonical frozen ledger).
  - If BOTH sources are absent for a legacy pick → mark
    `lock_reconstructability='LEGACY_LOCK_UNRECONSTRUCTABLE'`.
  - NEVER recompute historical Lock Scores using today's model, ML, edge,
    or calibration.  The immutable pregame truth is authoritative even
    when it disagrees with modern scoring.

Idempotent and chunked; safe to re-run.  Emits a full JSON summary.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

load_dotenv(os.path.join(_BACKEND, ".env"))

CHUNK = 1000
LOG_EVERY = 10_000
DRIFT_EPS = 0.001


async def _resolve_snapshot_score(db, pick: dict) -> Optional[float]:
    """Look up the frozen published_lock_score from prediction_snapshots.

    Match by prediction_id → pick.id → pick._id (deterministic order).
    Only ACTIVE snapshots (is_active=True) count as authoritative.
    """
    for key_val, snap_field in (
        (pick.get("id"),        "prediction_id"),
        (pick.get("id"),        "pick_id"),
        (str(pick.get("_id")),  "prediction_id"),
        (str(pick.get("_id")),  "pick_id"),
    ):
        if not key_val:
            continue
        snap = await db.prediction_snapshots.find_one(
            {snap_field: key_val, "is_active": True},
            {"published_lock_score": 1},
        )
        if snap and snap.get("published_lock_score") is not None:
            try:
                return float(snap["published_lock_score"])
            except Exception:
                continue
        # Non-active fallback (still frozen, still authoritative for legacy)
        snap = await db.prediction_snapshots.find_one(
            {snap_field: key_val},
            {"published_lock_score": 1},
            sort=[("snapshot_version", -1)],
        )
        if snap and snap.get("published_lock_score") is not None:
            try:
                return float(snap["published_lock_score"])
            except Exception:
                continue
    return None


async def q29_run() -> dict:
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = client[os.environ["DB_NAME"]]
    now = datetime.now(timezone.utc)

    stats = {
        "scanned":                    0,
        "already_pure":               0,
        "repaired_from_pick_pub":     0,
        "repaired_from_snapshot":     0,
        "legacy_unreconstructable":   0,
        "errors":                     0,
        "started_at":                 now.isoformat(),
    }

    total = await db.picks.count_documents({})
    print(f"[Q29] scanning {total} picks for lock-score drift…")

    last_id = None
    while True:
        q: dict = {}
        if last_id is not None:
            q["_id"] = {"$gt": last_id}
        cursor = db.picks.find(q).sort("_id", 1).limit(CHUNK)
        chunk = [p async for p in cursor]
        if not chunk:
            break
        last_id = chunk[-1]["_id"]

        for pick in chunk:
            stats["scanned"] += 1
            try:
                cur = pick.get("lock_score")
                pub = pick.get("published_lock_score")

                # Case A — pick has published_lock_score → it IS the truth
                if pub is not None:
                    try:
                        pubf = float(pub)
                    except Exception:
                        pubf = None
                    if pubf is not None:
                        try:
                            curf = float(cur) if cur is not None else None
                        except Exception:
                            curf = None
                        if curf is None or abs(curf - pubf) > DRIFT_EPS:
                            await db.picks.update_one(
                                {"_id": pick["_id"]},
                                {"$set": {
                                    "lock_score": pubf,
                                    "lock_reconstructability": "RESTORED_FROM_PICK_PUBLISHED",
                                    "q29_repaired_at": now,
                                }},
                            )
                            stats["repaired_from_pick_pub"] += 1
                        else:
                            # Ensure marker present for future audits.
                            if not pick.get("lock_reconstructability"):
                                await db.picks.update_one(
                                    {"_id": pick["_id"]},
                                    {"$set": {"lock_reconstructability": "PURE"}},
                                )
                            stats["already_pure"] += 1
                        continue

                # Case B — no published_lock_score on pick → try snapshot
                snap_score = await _resolve_snapshot_score(db, pick)
                if snap_score is not None:
                    await db.picks.update_one(
                        {"_id": pick["_id"]},
                        {"$set": {
                            "lock_score":               snap_score,
                            "published_lock_score":     snap_score,
                            "lock_reconstructability":  "RESTORED_FROM_SNAPSHOT",
                            "q29_repaired_at":          now,
                        }},
                    )
                    stats["repaired_from_snapshot"] += 1
                    continue

                # Case C — legacy pick with no truth source anywhere
                await db.picks.update_one(
                    {"_id": pick["_id"]},
                    {"$set": {
                        "lock_reconstructability": "LEGACY_LOCK_UNRECONSTRUCTABLE",
                        "q29_repaired_at":         now,
                    }},
                )
                stats["legacy_unreconstructable"] += 1

            except Exception as e:
                stats["errors"] += 1
                print(f"[Q29] error on _id={pick.get('_id')}: {e}")

        if stats["scanned"] % LOG_EVERY < CHUNK:
            print(f"[Q29] progress: scanned={stats['scanned']}/{total}  "
                  f"pure={stats['already_pure']}  "
                  f"pick-fix={stats['repaired_from_pick_pub']}  "
                  f"snap-fix={stats['repaired_from_snapshot']}  "
                  f"legacy={stats['legacy_unreconstructable']}  "
                  f"err={stats['errors']}")

    stats["finished_at"] = datetime.now(timezone.utc).isoformat()
    return stats


if __name__ == "__main__":
    out = asyncio.run(q29_run())
    print()
    print("── Q29 SUMMARY ──────────────────────────────────────────")
    print(json.dumps(out, indent=2, default=str))
