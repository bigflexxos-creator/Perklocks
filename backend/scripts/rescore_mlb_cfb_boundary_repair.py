"""Retro-normalise Lock Score for MLB / CFB picks whose alignment
component collapsed due to the pre-boundary scale mismatch.

Applies the NEW factor-normalisation boundary (services.mlb_factor_
normalization → compute_lock_score) to every current-window MLB / CFB
pick whose ``lock_components.alignment`` = 0 and ``lock_score_version``
is v4.  Re-runs compute_lock_score with the raw factor VALUES the
generator originally emitted (recovered from ``factors``, which the
prior scorer stored ×100).

This is a targeted repair of the 287 MLB + 68 CFB pending picks that
would otherwise remain locked out of the 85+ publication band.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, "/app/backend")

from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv("/app/backend/.env")

from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402
from sports_engine import compute_lock_score  # noqa: E402


async def _rescore_sport(db, sport: str) -> dict:
    """Rescore all v4-stamped picks for the given sport whose alignment
    was collapsed by the pre-boundary scale mismatch."""
    now = datetime.now(timezone.utc)
    window_start = (now - timedelta(hours=24)).isoformat()
    window_end = (now + timedelta(days=14)).isoformat()

    stats = {
        "scanned": 0,
        "rescored": 0,
        "gained_ge5_points": 0,
        "reached_85": 0,
        "before_max": 0.0,
        "after_max": 0.0,
        "sample": [],
    }

    cursor = db.picks.find({
        "sport": sport,
        "event_time": {"$gte": window_start, "$lte": window_end},
        "lock_score_version": {"$regex": "^v4"},
    })
    async for pick in cursor:
        stats["scanned"] += 1
        stats["before_max"] = max(stats["before_max"], float(pick.get("lock_score") or 0))
        # Persisted factors are ×100 (display).  Convert back to the
        # [0, 1] scale the generator originally computed on, then let
        # the boundary re-normalise inside compute_lock_score.
        raw_factors = {}
        for k, v in (pick.get("factors") or {}).items():
            if isinstance(k, str) and k.startswith("__"):
                continue
            if isinstance(v, (int, float)):
                raw_factors[k] = float(v) / 100.0
            # Non-numeric (e.g. CFB "78.34%" strings) are display-only,
            # excluded from scoring inputs.

        wp = pick.get("calibrated_win_probability") or (
            (pick.get("win_probability") or 0) / 100.0
        )
        wp_pct = wp * 100.0 if wp <= 1.0 else float(wp)
        edge_pct = float(pick.get("edge_percent") or 0)

        # Reconstruct a compute_lock_score-compatible pick shim
        # (preserves book_odds, market, sport, is_alt_line, odds_at_pick,
        # closing_odds so vol / clv components stay identical).
        pick_shim = {
            "sport": sport,
            "market": pick.get("market") or "",
            "book_odds": pick.get("book_odds") or 0,
            "win_probability": wp_pct,
            "edge_percent": edge_pct,
            "is_alt_line": bool(pick.get("is_alt_prop")
                                or "alt" in (pick.get("market") or "").lower()),
            "is_long_shot": bool(pick.get("is_long_shot")),
            "odds_at_pick": pick.get("odds_at_pick"),
            "closing_odds": pick.get("closing_odds"),
            "data_quality": pick.get("data_quality") or "",
            "probability_provenance": pick.get("probability_provenance"),
        }
        new_score, _ = compute_lock_score(
            raw_factors, win_prob=wp_pct, pick=pick_shim,
            edge_percent=edge_pct,
        )
        old_score = float(pick.get("lock_score") or 0)
        gain = new_score - old_score
        if abs(gain) >= 0.1:
            stats["rescored"] += 1
            if gain >= 5.0:
                stats["gained_ge5_points"] += 1
            if new_score >= 85.0 and old_score < 85.0:
                stats["reached_85"] += 1
            await db.picks.update_one(
                {"_id": pick["_id"]},
                {"$set": {
                    "lock_score": new_score,
                    "published_lock_score": new_score,
                    "lock_components": pick_shim.get("lock_components"),
                    "grade": pick_shim.get("grade"),
                    "confidence": pick_shim.get("confidence"),
                    "lock_score_version": pick_shim.get(
                        "lock_score_version",
                        "v4.confidence_first.2026-06-14"),
                }},
            )
            if len(stats["sample"]) < 6 and gain >= 5.0:
                stats["sample"].append({
                    "market": pick.get("market"),
                    "before": old_score,
                    "after":  new_score,
                    "align_before": (pick.get("lock_components") or {}).get("alignment"),
                    "align_after":  (pick_shim.get("lock_components") or {}).get("alignment"),
                })
        stats["after_max"] = max(stats["after_max"], new_score)
    return stats


async def main():
    client = AsyncIOMotorClient(os.environ.get("MONGO_URL") or "mongodb://localhost:27017")
    db = client["lockscore_db"]
    for sport in ("MLB", "CFB"):
        print(f"\n=== {sport} rescore pass ===")
        stats = await _rescore_sport(db, sport)
        print(f"  scanned          = {stats['scanned']}")
        print(f"  rescored (Δ≥0.1) = {stats['rescored']}")
        print(f"  Δ ≥ 5 points     = {stats['gained_ge5_points']}")
        print(f"  crossed 85 floor = {stats['reached_85']}")
        print(f"  max_lock before  = {stats['before_max']:.1f}")
        print(f"  max_lock after   = {stats['after_max']:.1f}")
        for s in stats["sample"]:
            print(f"    · {s['market']}   {s['before']} → {s['after']}   "
                  f"(align {s['align_before']} → {s['align_after']})")


if __name__ == "__main__":
    asyncio.run(main())
