"""
Back-fill the NHL alt-goal cap, NHL anytime-scorer floor, and NBA ALT LOCK
reliability floor onto picks ALREADY published to the DB.

Mirrors the logic in `services/pick_refresh_orchestrator.py` (same thresholds)
so the fixes take effect immediately without waiting for the next publish pass.

Usage:
    python -m scripts.backfill_nhl_nba_lock_scores
"""
from __future__ import annotations

import asyncio
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402

MONGO_URL = os.getenv("MONGO_URL", "mongodb://localhost:27017")
DB_NAMES = ("lockscore_db",)

NHL_ALT_GOAL_PAT = re.compile(r"\bover\s+(\d+)\.5\s+goals\b", re.IGNORECASE)
NHL_ANYTIME_PAT  = re.compile(r"\bover\s+0\.5\s+goals\b", re.IGNORECASE)


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


async def _process_coll(coll) -> dict:
    stats = {
        "nhl_alt_capped": 0,
        "nhl_anytime_lifted": 0,
        "nba_alt_lifted": 0,
        "scanned": 0,
    }

    # NHL alt-goal cap (Over N.5 Goals, N >= 1 → cap at 84.9)
    cur = coll.find(
        {"sport": "NHL",
         "market": {"$regex": r"Over \d+\.5 Goals", "$options": "i"},
         "lock_score": {"$gte": 85.0}},
        {"id": 1, "market": 1, "lock_score": 1, "lock_score_v2": 1,
         "lock_score_peak": 1},
    )
    async for d in cur:
        stats["scanned"] += 1
        m = NHL_ALT_GOAL_PAT.search(d.get("market") or "")
        if not m:
            continue
        try:
            n = int(m.group(1))
        except (TypeError, ValueError):
            continue
        if n < 1:
            continue
        new_lock = 84.9
        try:
            lv2 = min(float(d.get("lock_score_v2") or new_lock), new_lock)
            lpk = min(float(d.get("lock_score_peak") or new_lock), new_lock)
        except (TypeError, ValueError):
            lv2 = lpk = new_lock
        await coll.update_one(
            {"_id": d["_id"]},
            {"$set": {
                "lock_score": new_lock,
                "lock_score_v2": lv2,
                "lock_score_peak": lpk,
                "apex_lock": False,
                "apex_score": new_lock,
                "apex_status": "NOT_APEX",
                "apex_reason": "nhl_alt_goal_overline_improbable",
                "nhl_alt_goal_cap_applied": True,
                "nhl_alt_goal_cap_reason": (
                    f"ALT-GOAL IMPROBABLE: Over {n}.5 Goals requires skater "
                    f"to score {n+1}+ goals in one game — league base-rate "
                    f"~3-5 %. Capped below 85 by back-fill."
                ),
                "grade": _grade(new_lock),
            }}
        )
        stats["nhl_alt_capped"] += 1

    # NHL anytime-scorer floor (Over 0.5 Goals, lock >= 85 → ip-based floor)
    cur = coll.find(
        {"sport": "NHL",
         "market": {"$regex": r"Over 0\.5 Goals", "$options": "i"},
         "lock_score": {"$gte": 85.0},
         "implied_probability": {"$ne": None}},
        {"id": 1, "market": 1, "lock_score": 1, "lock_score_v2": 1,
         "lock_score_peak": 1, "implied_probability": 1},
    )
    async for d in cur:
        stats["scanned"] += 1
        if not NHL_ANYTIME_PAT.search(d.get("market") or ""):
            continue
        try:
            lock = float(d.get("lock_score") or 0.0)
            ip = d.get("implied_probability")
            if ip is None:
                continue
            ip = float(ip)
            if ip <= 1.0:
                ip = ip * 100.0
        except (TypeError, ValueError):
            continue
        if lock < 85.0:
            continue
        floor = min(95.0, 85.0 + (ip - 25.0) * 0.5)
        if floor <= lock:
            continue
        new_lock = round(floor, 1)
        try:
            lv2 = max(float(d.get("lock_score_v2") or 0.0), new_lock)
            lpk = max(float(d.get("lock_score_peak") or 0.0), new_lock)
        except (TypeError, ValueError):
            lv2 = lpk = new_lock
        await coll.update_one(
            {"_id": d["_id"]},
            {"$set": {
                "lock_score": new_lock,
                "lock_score_v2": lv2,
                "lock_score_peak": lpk,
                "nhl_anytime_floor_applied": True,
                "nhl_anytime_floor_prior": lock,
                "nhl_anytime_floor_ip": round(ip, 2),
                "grade": _grade(new_lock),
            }}
        )
        stats["nhl_anytime_lifted"] += 1

    # NBA ALT LOCK floor (wp >= 0.70 AND edge >= 0 → floor = 60 + wp*40, cap 96)
    cur = coll.find(
        {"sport": "NBA",
         "market": {"$regex": "ALT LOCK"},
         "win_probability": {"$gte": 70.0}},
        {"id": 1, "market": 1, "lock_score": 1, "lock_score_v2": 1,
         "lock_score_peak": 1, "win_probability": 1, "edge_percent": 1},
    )
    async for d in cur:
        stats["scanned"] += 1
        try:
            wp = float(d.get("win_probability") or 0.0)
            if wp > 1.0: wp = wp / 100.0
            lock = float(d.get("lock_score") or 0.0)
            edge = d.get("edge_percent")
            edge = float(edge) if edge is not None else 0.0
        except (TypeError, ValueError):
            continue
        if wp < 0.70 or edge < 0.0:
            continue
        floor = min(96.0, 60.0 + max(0.0, min(1.0, wp)) * 40.0)
        if floor <= lock:
            continue
        new_lock = round(floor, 1)
        try:
            lv2 = max(float(d.get("lock_score_v2") or 0.0), new_lock)
            lpk = max(float(d.get("lock_score_peak") or 0.0), new_lock)
        except (TypeError, ValueError):
            lv2 = lpk = new_lock
        await coll.update_one(
            {"_id": d["_id"]},
            {"$set": {
                "lock_score": new_lock,
                "lock_score_v2": lv2,
                "lock_score_peak": lpk,
                "nba_alt_lock_floor_applied": True,
                "nba_alt_lock_floor_prior": lock,
                "nba_alt_lock_floor_wp": round(wp, 4),
                "nba_alt_lock_floor_edge": round(edge, 2),
                "grade": _grade(new_lock),
            }}
        )
        stats["nba_alt_lifted"] += 1

    return stats


async def main():
    client = AsyncIOMotorClient(MONGO_URL)
    total = {"nhl_alt_capped": 0, "nhl_anytime_lifted": 0,
             "nba_alt_lifted": 0, "scanned": 0}
    for dbname in DB_NAMES:
        db = client[dbname]
        for coll_name in ("canonical_picks", "picks"):
            if coll_name not in await db.list_collection_names():
                continue
            coll = db[coll_name]
            print(f"--- Processing {dbname}.{coll_name} ---")
            s = await _process_coll(coll)
            print(f"  scanned={s['scanned']} nhl_alt_capped={s['nhl_alt_capped']} "
                  f"nhl_anytime_lifted={s['nhl_anytime_lifted']} "
                  f"nba_alt_lifted={s['nba_alt_lifted']}")
            for k in total:
                total[k] += s[k]
    print(f"\n=== TOTAL === {total}")

if __name__ == "__main__":
    asyncio.run(main())
