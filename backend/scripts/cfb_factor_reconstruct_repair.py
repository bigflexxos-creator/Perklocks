"""CFB Fresh-Factor Repair — reconstruct populated factors on the
retired v4 CFB rows using their persisted ``cfb_game_sim`` metadata,
then rescore in-place through the CURRENT (fixed) compute_lock_score
path.

**Root cause closed here (in-memory-proven earlier via /tmp/trace_bp.py)**:
    ``_build_pick`` DOES preserve ``factors`` verbatim.  The retired
    v4 CFB rows in the DB have ``factors: {}`` because they were
    emitted BEFORE the R1 factor-persistence merge fix landed
    (sports_engine.py L3343-3345 / L2530-2562).  The fix is
    correctly wired for FUTURE emissions; this script performs a
    one-shot DB-side repair on the pre-fix rows so the current
    slate reflects the intended scoring.

**Contract**:
    * Only touches v4-stamped CFB rows in the current wagering
      window whose ``factors`` is empty AND whose ``cfb_game_sim``
      carries the ``_cfb_game_model`` provenance needed to
      reconstruct evidence.
    * Reconstructs the exact factor keys the R1 emission block would
      have produced.
    * Re-runs ``compute_lock_score`` and stamps
      ``lock_score / published_lock_score / lock_components /
      grade / confidence / factors`` on the row.
    * Never adds synthetic bonuses.  Never lowers the 85 floor.
      Never changes weights.  Never touches non-CFB rows.
    * Idempotent — running twice is a no-op.
"""
from __future__ import annotations
import asyncio, os, sys
sys.path.insert(0, "/app/backend")
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")
from motor.motor_asyncio import AsyncIOMotorClient
from sports_engine import compute_lock_score, _cfb_norm_margin, _cfb_norm_total  # noqa: E402


def _implied_prob(american: int | float | None) -> float:
    if american is None: return 0.0
    a = float(american)
    if a >= 100: return 100.0 / (a + 100.0)
    return -a / (-a + 100.0)


def _reconstruct_cfb_factors(pick: dict) -> dict | None:
    """Rebuild the factor dict the R1 emission block would have
    produced from the persisted ``cfb_game_sim`` provenance."""
    sim = pick.get("cfb_game_sim") or {}
    if not sim: return None
    market = str(pick.get("market") or "").lower()
    side = str(pick.get("selection") or pick.get("side") or "").strip()
    home = pick.get("home_team")
    is_home_side = (side == home)
    is_ml     = "moneyline" in market
    is_spread = "spread" in market
    is_total  = market.startswith("total")

    exp_margin = float(sim.get("expected_margin") or 0.0)
    exp_total  = float(sim.get("expected_total")  or 0.0)
    dq_str     = str(sim.get("data_quality") or "sp_plus")
    mp         = float(pick.get("win_probability") or 0.0) / 100.0
    book_odds  = pick.get("book_odds")
    book_impl  = _implied_prob(book_odds)

    # ML uses side-signed margin; Totals use raw expected_margin as
    # supplementary; Spread uses side-signed margin.
    if is_ml or is_spread:
        margin_side = exp_margin if is_home_side else -exp_margin
        sp_base = margin_side
    else:
        margin_side = exp_margin  # not side-flipped for totals
        sp_base = margin_side

    factors: dict = {
        # RAW EVIDENCE (UI/humans)
        "Projected Margin":         f"{margin_side:+.2f} pts",
        "Expected Total":           f"{exp_total:.2f} pts",
        "Model Fair Prob":          f"{mp * 100:.2f}%",
        "Sportsbook Implied Prob":  f"{book_impl * 100:.2f}%",
        "SP+ Margin Base":          f"{sp_base:+.2f} pts",
        "__data_quality":           dq_str,
        "__model_uncertainty_reason": "nominal",
        # NORMALIZED SCORING INPUTS ([0,1])
        "Projected Margin (norm)":   _cfb_norm_margin(margin_side),
        "Expected Total (norm)":     _cfb_norm_total(exp_total),
        "Model Fair Prob (norm)":    round(float(mp), 4),
        "Sportsbook Implied (norm)": round(float(book_impl), 4),
        "SP+ Rating Δ (norm)":       _cfb_norm_margin(sp_base),
    }
    return factors


async def main():
    client = AsyncIOMotorClient(os.environ.get("MONGO_URL") or "mongodb://localhost:27017")
    db = client["lockscore_db"]
    now = datetime.now(timezone.utc)
    ago = (now - timedelta(hours=24)).isoformat()
    later = (now + timedelta(days=14)).isoformat()

    q = {
        "sport": "CFB",
        "event_time": {"$gte": ago, "$lte": later},
        "lock_score_version": {"$regex": "^v4"},
        # Only rows whose factors is missing or empty
        "$or": [
            {"factors": {"$exists": False}},
            {"factors": {}},
            {"factors": None},
        ],
        # AND has cfb_game_sim provenance so we can reconstruct
        "cfb_game_sim": {"$exists": True, "$ne": None},
    }
    total = await db.picks.count_documents(q)
    print(f"Retired v4 CFB rows eligible for factor reconstruction: {total}")

    stats = {"scanned": 0, "repaired": 0, "reached_85": 0,
             "max_ls_before": 0.0, "max_ls_after": 0.0,
             "samples": []}

    cursor = db.picks.find(q)
    async for p in cursor:
        stats["scanned"] += 1
        old_ls = float(p.get("lock_score") or 0.0)
        stats["max_ls_before"] = max(stats["max_ls_before"], old_ls)

        reconstructed = _reconstruct_cfb_factors(p)
        if not reconstructed:
            continue

        wp_pct = float(p.get("win_probability") or 0.0)
        edge_pct = float(p.get("edge_percent") or 0.0)
        pick_shim = {
            "sport": "CFB",
            "market": p.get("market") or "",
            "book_odds": p.get("book_odds") or 0,
            "win_probability": wp_pct,
            "edge_percent": edge_pct,
            "is_alt_line": False,
            "data_quality": p.get("data_quality") or "",
            "probability_provenance": p.get("probability_provenance"),
            "odds_at_pick": p.get("odds_at_pick"),
            "closing_odds": p.get("closing_odds"),
        }
        new_ls, weighted = compute_lock_score(
            reconstructed, win_prob=wp_pct, pick=pick_shim,
            edge_percent=edge_pct,
        )
        stats["max_ls_after"] = max(stats["max_ls_after"], new_ls)
        # Emission-side merge (mirror of L3343-3345):
        persisted_factors = {**weighted, **reconstructed}
        # Pop provenance __-keys stashed by the v4 bridge so the
        # persisted factors dict cleanly separates provenance
        # (top-level fields) from evidence (factor keys).
        for k in ("__lock_score_version",
                  "__calibrated_win_probability",
                  "__effective_weights",
                  "__confidence_component"):
            persisted_factors.pop(k, None)
        set_payload = {
            "lock_score": new_ls,
            "published_lock_score": new_ls,
            "lock_components": pick_shim.get("lock_components"),
            "grade": pick_shim.get("grade"),
            "confidence": pick_shim.get("confidence"),
            "factors": persisted_factors,
            "lock_score_version": pick_shim.get(
                "lock_score_version",
                "v4.confidence_first.2026-06-14"),
        }
        await db.picks.update_one(
            {"_id": p["_id"]},
            {"$set": set_payload},
        )
        stats["repaired"] += 1
        if new_ls >= 85.0:
            stats["reached_85"] += 1
        if len(stats["samples"]) < 6:
            stats["samples"].append({
                "market": (p.get("market") or "")[:55],
                "before": old_ls,
                "after":  new_ls,
                "align_before": (p.get("lock_components") or {}).get("alignment"),
                "align_after":  (pick_shim.get("lock_components") or {}).get("alignment"),
                "factor_count": len(persisted_factors),
            })
    print(f"scanned={stats['scanned']}  repaired={stats['repaired']}")
    print(f"max_ls before={stats['max_ls_before']:.1f}  "
          f"max_ls after={stats['max_ls_after']:.1f}")
    print(f"reached >=85: {stats['reached_85']}")
    for s in stats["samples"]:
        print(f"  · {s['market']}   {s['before']} → {s['after']}   "
              f"(align {s['align_before']} → {s['align_after']}, "
              f"factors={s['factor_count']})")


if __name__ == "__main__":
    asyncio.run(main())
