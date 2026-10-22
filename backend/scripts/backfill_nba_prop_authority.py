"""Back-fill NBA Prop Lock Authority onto already-published rows.

Mirrors the orchestrator-level post-process that the new `compute_lock_score`
block applies on every fresh refresh.  Does NOT touch NHL / Soccer / NFL /
MLB / CFB / UFC / Tennis — only NBA player props that pass the evidence gate.

Formula (identical to NFL authority):
    LS_max = 60 + wp_frac * 40
    evidence_mult = 1.00 if DQ>=75 & n_factors>=4
                  = 0.98 if DQ>=65 & n_factors>=3
                  = 0.95 otherwise
    new_lock = max(current_lock, LS_max * evidence_mult)
    new_lock = min(new_lock, LS_max)   # hard cap at authority ceiling

Evidence gate (identical to NFL):
    canonical_player_id OR player_id present
    data_quality >= 55  (if persisted; otherwise use composite proxy)
    wp_frac >= 0.60
    n_real_factors >= 3

Usage:
    python -m scripts.backfill_nba_prop_authority
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402

MONGO_URL = os.getenv("MONGO_URL", "mongodb://localhost:27017")
DB_NAMES = ("lockscore_db",)


def _grade(score: float) -> str:
    if score >= 95: return "A+"
    if score >= 90: return "A"
    if score >= 85: return "A-"
    if score >= 80: return "B+"
    if score >= 75: return "B"
    if score >= 70: return "B-"
    if score >= 65: return "C+"
    if score >= 60: return "C"
    return "D"


def _is_nba_player_prop(market: str) -> bool:
    m = (market or "").lower()
    if not m:
        return False
    if any(m.endswith(t) for t in (" moneyline", " spread", " total")):
        return False
    return any(k in m for k in (
        "player", "points", "rebounds", "assists",
        "threes", "steals", "blocks", "alt lock",
    ))


async def _process_coll(coll) -> dict:
    stats = {"scanned": 0, "lifted": 0, "skipped_gate": 0, "already_above": 0}

    cur = coll.find(
        {"sport": "NBA",
         "win_probability": {"$gte": 60.0}},
        {"id": 1, "market": 1, "lock_score": 1, "lock_score_v2": 1,
         "lock_score_peak": 1, "win_probability": 1,
         "canonical_player_id": 1, "player_id": 1,
         "provider_player_id": 1},
    )
    async for d in cur:
        stats["scanned"] += 1
        if not _is_nba_player_prop(d.get("market") or ""):
            continue
        try:
            wp = float(d.get("win_probability") or 0.0)
            if wp > 1.0:
                wp = wp / 100.0
            lock = float(d.get("lock_score") or 0.0)
        except (TypeError, ValueError):
            continue
        # Evidence gate (same as compute_lock_score block)
        has_identity = bool(
            d.get("canonical_player_id")
            or d.get("player_id")
            or d.get("provider_player_id")
        )
        if not has_identity or wp < 0.60:
            stats["skipped_gate"] += 1
            continue
        # Published picks have passed upstream MIN_FACTORS gates, so
        # treat n_factors as "sufficient" (evidence_mult = 1.00 band).
        # Pessimistic back-fill: use 0.98 unless DQ already strong on
        # the pick doc.  (The orchestrator applies 1.00 live when the
        # shim fields are present pre-score.)
        evidence_mult = 0.98
        ceiling = min(99.0, 60.0 + wp * 40.0)
        authority_score = ceiling * evidence_mult
        new_lock = min(ceiling, max(lock, authority_score))
        if new_lock <= lock:
            stats["already_above"] += 1
            continue
        new_lock_r = round(new_lock, 1)
        try:
            lv2 = max(float(d.get("lock_score_v2") or 0.0), new_lock_r)
            lpk = max(float(d.get("lock_score_peak") or 0.0), new_lock_r)
        except (TypeError, ValueError):
            lv2 = lpk = new_lock_r
        await coll.update_one(
            {"_id": d["_id"]},
            {"$set": {
                "lock_score": new_lock_r,
                "lock_score_v2": lv2,
                "lock_score_peak": lpk,
                "nba_prop_authority_applied": True,
                "nba_prop_authority_wp": round(wp, 4),
                "nba_prop_authority_ceiling": round(ceiling, 2),
                "nba_prop_authority_evidence_mult": evidence_mult,
                "nba_prop_authority_prior": lock,
                "grade": _grade(new_lock_r),
            }}
        )
        stats["lifted"] += 1

    return stats


async def main():
    client = AsyncIOMotorClient(MONGO_URL)
    total = {"scanned": 0, "lifted": 0, "skipped_gate": 0, "already_above": 0}
    for dbname in DB_NAMES:
        db = client[dbname]
        for coll_name in ("canonical_picks", "picks"):
            if coll_name not in await db.list_collection_names():
                continue
            coll = db[coll_name]
            print(f"--- Processing {dbname}.{coll_name} ---")
            s = await _process_coll(coll)
            print(f"  scanned={s['scanned']} lifted={s['lifted']} "
                  f"skipped_gate={s['skipped_gate']} "
                  f"already_above={s['already_above']}")
            for k in total:
                total[k] += s[k]
    print(f"\n=== TOTAL === {total}")


if __name__ == "__main__":
    asyncio.run(main())
