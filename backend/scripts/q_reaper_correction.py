"""Q-Reaper Correction — Un-void the stuck_pick_reaper's fabricated VOIDs
======================================================================

Root Closure 2026-06.

Historical defect:
    `stuck_pick_reaper.reap_stuck_picks` routed stuck picks through
    `SettlementService.settle_from_pick(result='void',
    authoritative_event_final=False)`, which inserted canonical
    `settlement_events` rows claiming those wagers were VOID.  In
    reality no authoritative outcome was ever observed — the pick was
    simply stuck past its settlement window.  VOID is a fabricated
    outcome that contaminates public History, hit-rate, ROI, and
    Analytics with false negatives.

This script:
  1. Finds every pick with `void_reason='auto_void_stuck_pick_reaper'`.
  2. Resets `status='unresolved'`, `settlement_status='UNRESOLVED'`,
     `unresolved_reason='stuck_past_settlement_window_reaper_reverted'`.
  3. Marks the corresponding `settlement_events` row(s)
     `is_active=False` with `supersedes_reason='reaper_fabrication_reverted_root_closure_2026_06'`.
  4. Preserves the immutable ledger — no rows are deleted.

Idempotent and safe to re-run.  Emits a JSON summary.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import UpdateOne

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

load_dotenv(os.path.join(_BACKEND, ".env"))


CORRECTION_TAG = "reaper_fabrication_reverted_root_closure_2026_06"
CHUNK = 500


async def run() -> dict:
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = client[os.environ["DB_NAME"]]
    now = datetime.now(timezone.utc)

    stats = {
        "picks_scanned":      0,
        "picks_reverted":     0,
        "ledger_deactivated": 0,
        "errors":             0,
        "started_at":         now.isoformat(),
    }

    # 1) Un-void every pick marked auto_void_stuck_pick_reaper.
    picks_query = {"void_reason": "auto_void_stuck_pick_reaper"}
    total = await db.picks.count_documents(picks_query)
    print(f"[Q-reaper] scanning {total} reaper-voided picks…")

    ops: list[UpdateOne] = []
    all_ids: list[str] = []
    async for p in db.picks.find(picks_query, {"id": 1}):
        pid = p.get("id")
        stats["picks_scanned"] += 1
        if not pid:
            continue
        all_ids.append(pid)
        ops.append(UpdateOne(
            {"_id": p["_id"]},
            {"$set": {
                "status":                 "unresolved",
                "settlement_status":      "UNRESOLVED",
                "unresolved_reason":      "stuck_past_settlement_window_reaper_reverted",
                "unresolved_by":          "q_reaper_correction",
                "unresolved_at":          now.isoformat(),
                "learning_excluded":      True,
                "q_reaper_reverted_at":   now,
            },
             "$unset": {"void_reason": ""}},
        ))
        if len(ops) >= CHUNK:
            r = await db.picks.bulk_write(ops, ordered=False)
            stats["picks_reverted"] += r.modified_count or 0
            ops = []
    if ops:
        r = await db.picks.bulk_write(ops, ordered=False)
        stats["picks_reverted"] += r.modified_count or 0

    # 2) Deactivate the fabricated settlement_events rows.
    #    Pass A — rows tied to picks we just reverted.
    print(f"[Q-reaper] deactivating fabricated ledger rows for {len(all_ids)} picks…")
    for i in range(0, len(all_ids), CHUNK):
        batch = all_ids[i:i + CHUNK]
        r = await db.settlement_events.update_many(
            {
                "prediction_id": {"$in": batch},
                "source": "stuck_pick_reaper",
                "is_active": True,
            },
            {"$set": {
                "is_active":            False,
                "supersedes_reason":    CORRECTION_TAG,
                "deactivated_at":       now.isoformat(),
            }},
        )
        stats["ledger_deactivated"] += r.modified_count or 0
    #    Pass B — orphan reaper rows whose underlying pick was
    #    atomically deleted by a later refresh (the ledger row must
    #    still be deactivated so it can't leak back into History).
    orphan = await db.settlement_events.update_many(
        {"source": "stuck_pick_reaper", "is_active": True},
        {"$set": {
            "is_active":            False,
            "supersedes_reason":    CORRECTION_TAG + "_orphan",
            "deactivated_at":       now.isoformat(),
        }},
    )
    stats["ledger_deactivated"] += orphan.modified_count or 0
    stats["ledger_orphan_deactivated"] = orphan.modified_count or 0

    stats["finished_at"] = datetime.now(timezone.utc).isoformat()
    return stats


if __name__ == "__main__":
    out = asyncio.run(run())
    print()
    print("── Q-REAPER CORRECTION SUMMARY ────────────────────────")
    print(json.dumps(out, indent=2, default=str))
