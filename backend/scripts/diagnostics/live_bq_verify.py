"""LIVE BQ AUTHORITY VERIFICATION — for the top MLB / CFB / NFL game
candidates, rescore in-memory (no DB writes) through the updated
``compute_lock_score`` and print the P/R/H/M/C/S/D + BQ ceiling
alongside the currently-persisted lock score.  Used to prove the
authority handoff in Part A now behaves correctly."""
from __future__ import annotations
import asyncio, os, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
load_dotenv()


async def _rows(db, q):
    return await db.picks.find(q).sort("lock_score", -1).limit(20).to_list(None)


def _norm_factors(pick: dict) -> tuple[dict, dict]:
    """Return (numeric_factors, raw_factors) with per-pick scale detection."""
    factors = pick.get("factors") or {}
    numeric = {
        k: float(v) for k, v in factors.items()
        if isinstance(v, (int, float)) and not isinstance(v, bool)
        and not (isinstance(k, str) and k.startswith("__"))
    }
    peak = max((abs(v) for v in numeric.values()), default=0.0)
    if peak > 1.5:
        numeric = {k: v / 100.0 for k, v in numeric.items()}
    return numeric, factors


async def rescore_report(db, sport: str, today: str):
    from sports_engine import compute_lock_score
    rows = await _rows(db, {"sport": sport, "pick_date": today,
                              "lock_score": {"$type": "number"}})
    print(f"\n=== {sport} TOP 20 (pick_date={today}) ===")
    for i, p in enumerate(rows):
        numeric, raw = _norm_factors(p)
        wp = float(p.get("win_probability") or 0.0)
        edge = float(p.get("edge_percent") or 0.0)
        market = p.get("market") or ""
        # Rebuild a fresh pick shim with sport / market for BQ enable.
        shim = {
            "sport": sport, "market": market,
            "book_odds": p.get("book_odds"),
            "win_probability": wp, "edge_percent": edge,
            "is_alt_line": bool(p.get("is_alt")),
            "data_quality": p.get("data_quality") or "",
            "probability_provenance": p.get("probability_provenance"),
            "sim_stability": p.get("sim_stability"),
            "exact_threshold_hit_rate": p.get("exact_threshold_hit_rate"),
            "historical_hit_rate": p.get("historical_hit_rate"),
        }
        try:
            new_ls, _ = compute_lock_score(numeric, win_prob=wp,
                                             pick=shim, edge_percent=edge)
        except Exception as e:
            new_ls = f"err:{e}"
        bq = shim.get("bet_quality_authority") or {}
        bqc = bq.get("components") or {}
        db_ls = p.get("lock_score")
        print(f'{i+1:>2}. DB_LS={db_ls!s:>5}  new_LS={new_ls!s:>5}  '
              f'WP={wp!s:>5}  BQ={bq.get("ceiling")!s:>5}  '
              f'| {market[:65]}')
        if bqc:
            print(f'    P={bqc.get("wp")!s:>4} R={bqc.get("reliability")!s:>4} '
                  f'H={bqc.get("history")!s:>4} M={bqc.get("matchup")!s:>4} '
                  f'C={bqc.get("convergence")!s:>4} S={bqc.get("distribution")!s:>4} '
                  f'D={bqc.get("data_quality")!s:>4}')


async def main():
    from services.perklocks_day import current_slate_day
    today = current_slate_day()
    c = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = c[os.environ.get("DB_NAME", "lockscore_db")]
    for sport in ("MLB", "CFB", "NFL"):
        await rescore_report(db, sport, today)


if __name__ == "__main__":
    asyncio.run(main())
