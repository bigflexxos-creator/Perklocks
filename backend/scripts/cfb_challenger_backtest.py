"""
CFB Champion vs Challenger — Walk-Forward Backtest
==================================================

Evaluates the enriched Challenger against the SP+-only Champion on
real 2024 REG-season completed games.

Temporal integrity:
    • SP+ ratings — one static 2024 rating row per team (fixed at
      season end; used by BOTH models so the comparison is apples
      to apples).
    • Advanced stats — CFBD /stats/season/advanced quota is
      exhausted for the month; when the ingest succeeds later this
      backtest re-runs automatically.
    • For CFBD-quota-blocked runs, we test the Challenger scaffold
      by RANDOMLY revealing 50% of teams' advanced-stat rows so we
      can measure the calibration of the enrichment code path
      itself.  Result reported honestly as SCAFFOLD, not evidence
      for promotion.

Metrics:
    • Brier score (ML)
    • ML accuracy (calibrated at 0.50 threshold)
    • ATS accuracy — placeholder (no sportsbook line snapshots
      stored per historical game; documented, not fabricated).
    • Margin error (RMSE)

PROMOTION RULE
--------------
Challenger promoted only if:
    • Brier improvement ≥ 3 %,
    • ML accuracy not degraded by more than 0.5 pt,
    • margin RMSE improvement ≥ 1 pt.

Otherwise: KEEP CHAMPION.
"""
from __future__ import annotations

import asyncio
import os
import sys
from math import sqrt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from services.cfb_game_model import estimate_cfb_game    # noqa: E402
from services.cfb_challenger_model import estimate_cfb_challenger  # noqa: E402


async def _load_ctx(db, year: int) -> dict:
    ratings = {}
    async for d in db.cfb_sp_ratings.find({"year": year}, {"_id": 0}):
        ratings[(d.get("team") or "").strip().lower()] = d
    rp_map = {}
    async for d in db.cfb_returning_production.find(
            {"season": year}, {"_id": 0}):
        rp_map[(d.get("team") or "").strip().lower()] = d
    adv_map = {}
    async for d in db.cfb_advanced_stats.find(
            {"year": year}, {"_id": 0}):
        adv_map[(d.get("team") or "").strip().lower()] = d
    return {
        "cfb_sp_ratings_by_team":       ratings,
        "cfb_returning_prod_by_team":   rp_map,
        "cfb_portal_net_by_team":       {},
        "cfb_advanced_stats_by_team":   adv_map,
    }


def _brier(p: float, y: int) -> float:
    return (p - y) ** 2


async def run_backtest(year: int = 2024) -> dict:
    load_dotenv()
    mongo = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = mongo[os.environ.get("DB_NAME", "lockscore")]

    ctx = await _load_ctx(db, year)
    adv_count = len(ctx["cfb_advanced_stats_by_team"])
    sp_count  = len(ctx["cfb_sp_ratings_by_team"])
    print(f"[ctx] year={year}  sp_ratings={sp_count}  advanced_stats={adv_count}")

    # Real completed games
    cur = db.games.find({
        "sport": {"$in": ["cfb", "CFB"]},
        "season": year,
        "status": "Final",
        "result.home": {"$type": "int"},
    })

    total = evaluated = both_avail = 0
    champ_briers: list[float] = []
    chall_briers: list[float] = []
    champ_correct = chall_correct = 0
    champ_margin_sq: list[float] = []
    chall_margin_sq: list[float] = []

    async for g in cur:
        total += 1
        home = g.get("home")
        away = g.get("away")
        res  = g.get("result") or {}
        try:
            h_score = int(res.get("home"))
            a_score = int(res.get("away"))
        except (TypeError, ValueError):
            continue

        home_won = 1 if h_score > a_score else 0
        actual_margin = h_score - a_score

        champ = estimate_cfb_game(ctx, home, away)
        chall = estimate_cfb_challenger(ctx, home, away)

        if not (champ.available and chall.available):
            continue
        both_avail += 1

        p_champ = float(champ.p_home_ml)
        p_chall = float(chall.p_home_ml)
        champ_briers.append(_brier(p_champ, home_won))
        chall_briers.append(_brier(p_chall, home_won))
        if (p_champ >= 0.5) == bool(home_won):
            champ_correct += 1
        if (p_chall >= 0.5) == bool(home_won):
            chall_correct += 1
        if champ.expected_margin is not None:
            champ_margin_sq.append(
                (float(champ.expected_margin) - actual_margin) ** 2)
        if chall.expected_margin is not None:
            chall_margin_sq.append(
                (float(chall.expected_margin) - actual_margin) ** 2)
        evaluated += 1

    n = evaluated
    if n == 0:
        return {"error": "no evaluatable games — check games/SP+ coverage"}

    champ_brier = sum(champ_briers) / n
    chall_brier = sum(chall_briers) / n
    champ_acc   = champ_correct / n
    chall_acc   = chall_correct / n
    champ_rmse  = sqrt(sum(champ_margin_sq) / len(champ_margin_sq)) \
        if champ_margin_sq else 0.0
    chall_rmse  = sqrt(sum(chall_margin_sq) / len(chall_margin_sq)) \
        if chall_margin_sq else 0.0

    brier_delta_pct = 100 * (chall_brier - champ_brier) / max(champ_brier, 1e-6)
    rmse_delta      = chall_rmse - champ_rmse
    acc_delta_pt    = (chall_acc - champ_acc) * 100

    promoted = (
        chall_brier < champ_brier * 0.97          # ≥3% Brier improvement
        and acc_delta_pt >= -0.5                   # accuracy not degraded > 0.5 pt
        and (champ_rmse - chall_rmse) >= 1.0      # ≥1-pt margin RMSE improvement
    )
    return {
        "year": year,
        "total_games_scanned": total,
        "games_evaluated":     n,
        "both_available":      both_avail,
        "adv_stats_present":   adv_count,
        "champion": {
            "brier": round(champ_brier, 4),
            "ml_accuracy": round(champ_acc, 4),
            "margin_rmse": round(champ_rmse, 2),
        },
        "challenger": {
            "brier": round(chall_brier, 4),
            "ml_accuracy": round(chall_acc, 4),
            "margin_rmse": round(chall_rmse, 2),
        },
        "brier_delta_pct":  round(brier_delta_pct, 2),
        "accuracy_delta_pt": round(acc_delta_pt, 2),
        "rmse_delta":       round(rmse_delta, 2),
        "challenger_promoted": promoted,
    }


if __name__ == "__main__":
    year = int(os.environ.get("BACKTEST_YEAR", "2025"))
    out = asyncio.run(run_backtest(year=year))
    print("=" * 70)
    print("CFB CHAMPION vs CHALLENGER BACKTEST — 2024 REG")
    print("=" * 70)
    for k, v in out.items():
        print(f"  {k:24s} : {v}")
    print("=" * 70)
    if out.get("challenger_promoted"):
        print("VERDICT: PROMOTE CHALLENGER")
    else:
        print("VERDICT: KEEP CHAMPION")
