"""Rescore v4 MLB + CFB current-window picks under the BQ authority.
Only touches picks whose sport is opted-in via
BET_QUALITY_AUTHORITY_ENABLED_SPORTS and whose ``factors`` are
already populated on the DB row (post the CFB reconstruction repair)."""
from __future__ import annotations
import asyncio, os, sys
sys.path.insert(0, "/app/backend")
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")
from motor.motor_asyncio import AsyncIOMotorClient
from sports_engine import compute_lock_score


async def _rescore(db, sport: str):
    now = datetime.now(timezone.utc)
    ago = (now - timedelta(hours=24)).isoformat()
    later = (now + timedelta(days=14)).isoformat()
    q = {
        "sport": sport,
        "event_time": {"$gte": ago, "$lte": later},
        "lock_score_version": {"$regex": "^v4"},
        "$expr": {"$gt": [
            {"$size": {"$ifNull": [{"$objectToArray": "$factors"}, []]}},
            0,
        ]},
    }
    stats = {"scanned": 0, "updated": 0, "reached_85": 0,
             "max_before": 0.0, "max_after": 0.0}
    async for p in db.picks.find(q):
        stats["scanned"] += 1
        old_ls = float(p.get("lock_score") or 0.0)
        stats["max_before"] = max(stats["max_before"], old_ls)
        # ── FACTOR-SCALE DETECTION (2026-09-13 BQ RESCORE FIX) ─────
        # Historical bug: the rescore unconditionally divided every
        # persisted factor value by 100.  That is correct for MLB
        # (persisted values are 0-100 percent scale) but WRONG for
        # CFB / soccer, whose ``(norm)`` factors are ALREADY on the
        # [0,1] scale.  Dividing 0.9097 by 100 → 0.009097 collapsed
        # ``_matchup_component`` from ~92 to ~1.7 and pulled the BQ
        # ceiling of a 91% WP CFB moneyline down to 83, capping the
        # wire final score at exactly 92.0 across dozens of CFB
        # rows.  Detect the scale per-pick from the observed max
        # and only divide when values live on the 0-100 band.
        raw_numeric = {
            k: float(v)
            for k, v in (p.get("factors") or {}).items()
            if isinstance(v, (int, float))
            and not isinstance(v, bool)
            and not (isinstance(k, str) and k.startswith("__"))
        }
        _peak_val = max((abs(v) for v in raw_numeric.values()), default=0.0)
        _needs_div_100 = _peak_val > 1.5
        raw = {
            k: (v / 100.0) if _needs_div_100 else v
            for k, v in raw_numeric.items()
        }
        wp = float(p.get("win_probability") or 0.0)
        edge = float(p.get("edge_percent") or 0.0)
        pick_shim = {
            "sport": sport, "market": p.get("market") or "",
            "book_odds": p.get("book_odds") or 0,
            "win_probability": wp, "edge_percent": edge,
            "is_alt_line": bool("alt" in (p.get("market") or "").lower()),
            "data_quality": p.get("data_quality") or "",
            "probability_provenance": p.get("probability_provenance"),
            "odds_at_pick": p.get("odds_at_pick"),
            "closing_odds": p.get("closing_odds"),
            "sim_stability": p.get("sim_stability"),
            "exact_threshold_hit_rate": p.get("exact_threshold_hit_rate"),
            "historical_hit_rate": p.get("historical_hit_rate"),
        }
        new_ls, _ = compute_lock_score(raw, win_prob=wp, pick=pick_shim,
                                        edge_percent=edge)
        stats["max_after"] = max(stats["max_after"], new_ls)
        # ── BQ-ONLY WRITE (2026-09-13 SAFETY GUARDRAIL) ────────────
        # Previous version wrote ``lock_score`` + ``published_lock_score``
        # from this helper's isolated ``compute_lock_score`` run.  That
        # was dangerous — the standalone helper cannot reconstruct the
        # sport-specific composite context (CFB SP+ game model, NFL
        # Platinum sim, MLB pitcher intel etc.) that the real scorer
        # uses, so the recomputed composite was universally LOWER
        # than the authoritative production score and quietly demoted
        # legitimate high-tier picks.  This rescore now only STAMPS
        # the BQ authority payload so the historical evidence audit
        # trail exists; the persisted ``lock_score`` is left alone.
        _new_bq = pick_shim.get("bet_quality_authority")
        if _new_bq and p.get("bet_quality_authority") != _new_bq:
            stats["updated"] += 1
            await db.picks.update_one(
                {"_id": p["_id"]},
                {"$set": {"bet_quality_authority": _new_bq}},
            )
    return stats


async def main():
    client = AsyncIOMotorClient(os.environ.get("MONGO_URL") or "mongodb://localhost:27017")
    db = client["lockscore_db"]
    for sport in ("MLB", "CFB"):
        s = await _rescore(db, sport)
        print(f"{sport}: scanned={s['scanned']}  updated={s['updated']}  "
              f"crossed_85={s['reached_85']}  "
              f"max_before={s['max_before']:.1f}  max_after={s['max_after']:.1f}")


if __name__ == "__main__":
    asyncio.run(main())
