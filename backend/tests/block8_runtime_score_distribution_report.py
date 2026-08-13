"""Block 8 — Before/After score-distribution runtime measurement.

Feeds a **representative sample of live production picks** through the
new bounded Magic delta engine + explicit APEX gate, WITHOUT touching
DB state, and prints a side-by-side distribution report.

Read-only.  Safe to run any time.

Usage:
    cd /app/backend && python -m tests.block8_runtime_score_distribution_report
"""
from __future__ import annotations

import asyncio
import copy
import os
import statistics as stats
import sys
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient


async def _load_sample(db, limit: int = 3000) -> list[dict]:
    q = {"status": {"$nin": ["won", "lost", "void", "push"]}}
    cur = db.picks.find(q, {
        "_id": 0, "id": 1, "sport": 1, "market": 1, "market_type": 1,
        "lock_score": 1, "elite_player": 1, "confirmed_starter": 1,
        "expected_minutes": 1, "sim_win_probability": 1,
        "simulator_type": 1, "role": 1, "lineup_status": 1,
        "publication_gate": 1, "grade": 1,
    }).limit(limit)
    return [p async for p in cur]


def _distribution(scores: list[float]) -> dict[str, Any]:
    if not scores:
        return {"n": 0}
    scores = sorted(scores)
    def pct(p):
        i = int(round((p / 100.0) * (len(scores) - 1)))
        return scores[i]
    return {
        "n":       len(scores),
        "min":     round(scores[0], 1),
        "P10":     round(pct(10), 1),
        "P25":     round(pct(25), 1),
        "median":  round(pct(50), 1),
        "P75":     round(pct(75), 1),
        "P90":     round(pct(90), 1),
        "P95":     round(pct(95), 1),
        "P98":     round(pct(98), 1),
        "P99":     round(pct(99), 1),
        "max":     round(scores[-1], 1),
        "count_90plus": sum(1 for s in scores if s >= 90),
        "count_95plus": sum(1 for s in scores if s >= 95),
        "count_98plus": sum(1 for s in scores if s >= 98),
        "count_99":     sum(1 for s in scores if 98.99 <= s <= 99.01),
        "count_100":    sum(1 for s in scores if s >= 99.99),
    }


async def main():
    mongo_url = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
    db_name = os.environ.get("DB_NAME", "lockscore_db")
    cli = AsyncIOMotorClient(mongo_url)
    db = cli[db_name]

    picks = await _load_sample(db, limit=3000)
    if not picks:
        print("No unsettled picks in DB — nothing to measure.")
        return

    from services.magic.adapters import build_evidence
    from services.magic.lock_score_integrator import apply_magic_and_apex

    before_scores = [float(p["lock_score"]) for p in picks
                     if p.get("lock_score") is not None]
    after_scores  = []
    apex_picks    = []
    deltas        = []
    reasons_freq  = {}
    error_count   = 0

    for p in picks:
        base = p.get("lock_score")
        if base is None:
            continue
        working = copy.deepcopy(p)
        try:
            mo = await build_evidence(db, working)
        except Exception:
            error_count += 1
            continue
        if mo is None:
            error_count += 1
            continue
        try:
            audit = apply_magic_and_apex(working, mo)
        except Exception:
            error_count += 1
            continue
        final = working.get("lock_score")
        if final is None:
            continue
        after_scores.append(float(final))
        deltas.append(float(final) - float(base))
        if working.get("apex_lock"):
            apex_picks.append(working)
        # Track most common apex_block_reason (top-band picks only)
        if not working.get("apex_lock") and float(base) >= 90:
            reason = working.get("apex_block_reason") or ""
            key = reason.split(":")[0] if reason else ""
            if key:
                reasons_freq[key] = reasons_freq.get(key, 0) + 1

    print("\n══════════════════════════════════════════════════════════════")
    print(" BLOCK 8 — RUNTIME SCORE DISTRIBUTION REPORT")
    print(" (bounded Magic delta + explicit APEX gate)")
    print("══════════════════════════════════════════════════════════════\n")

    print("── BEFORE (base lock_score) ──")
    for k, v in _distribution(before_scores).items():
        print(f"  {k:16s} {v}")

    print("\n── AFTER (Magic-refined + APEX) ──")
    for k, v in _distribution(after_scores).items():
        print(f"  {k:16s} {v}")

    if deltas:
        print("\n── Magic delta distribution ──")
        deltas_sorted = sorted(deltas)
        def pct(l, p):
            i = int(round((p / 100.0) * (len(l) - 1)))
            return l[i]
        print(f"  min                 {round(deltas_sorted[0], 2)}")
        print(f"  P10                 {round(pct(deltas_sorted, 10), 2)}")
        print(f"  median              {round(pct(deltas_sorted, 50), 2)}")
        print(f"  mean                {round(stats.mean(deltas_sorted), 3)}")
        print(f"  P90                 {round(pct(deltas_sorted, 90), 2)}")
        print(f"  max                 {round(deltas_sorted[-1], 2)}")
        pos = sum(1 for d in deltas if d > 0)
        neg = sum(1 for d in deltas if d < 0)
        zer = sum(1 for d in deltas if d == 0)
        print(f"  positive_deltas     {pos} ({100*pos/len(deltas):.1f}%)")
        print(f"  negative_deltas     {neg} ({100*neg/len(deltas):.1f}%)")
        print(f"  zero_deltas         {zer} ({100*zer/len(deltas):.1f}%)")

    print("\n── APEX 100 summary ──")
    print(f"  apex_assigned       {len(apex_picks)}")
    if apex_picks:
        sport_ct = {}
        for p in apex_picks:
            sport_ct[p.get("sport") or "?"] = sport_ct.get(p.get("sport") or "?", 0) + 1
        for sp, ct in sorted(sport_ct.items(), key=lambda x: -x[1]):
            print(f"    {sp:12s} {ct}")

    if reasons_freq:
        print("\n── Top APEX block reasons (base >= 90) ──")
        for r, ct in sorted(reasons_freq.items(), key=lambda x: -x[1])[:10]:
            print(f"  {r:40s} {ct}")

    print(f"\n  errors: {error_count}")
    print("\n══════════════════════════════════════════════════════════════\n")


if __name__ == "__main__":
    asyncio.run(main())
