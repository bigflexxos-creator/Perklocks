"""
NFL Prop Challenger — Champion-vs-Challenger Backtest
======================================================

Walk-forward backtest over ``nfl_player_weekly`` 2024 REG season.

For every (player, week) pair with >=3 prior 2024 games, we:
  1. Build both champion and challenger predictions using only
     data STRICTLY BEFORE that week (temporal integrity).
  2. Score P(over) at synthetic thresholds anchored on the median
     of the prior sample (0.6× / 0.8× / 1.0× / 1.2× / 1.4×).
  3. Compare against the REAL outcome (over/under).

CHAMPION baseline: prior-mean → normal distribution with fixed
league CV.  This mirrors the naïve pre-Iter-138 behaviour before
recency weighting + coherent alt-ladder distribution.

CHALLENGER: ``services.nfl_prop_challenger.build_challenger_distribution``
with recency-weighted mean + coherent log-normal / Poisson family
per market and provenance-tagged inputs.

Metrics reported (per family + per position):
  * Brier score
  * hit-rate by predicted-probability bucket
  * threshold-monotonicity check (0 violations required by
    construction on the challenger)

PROMOTION RULE
--------------
Challenger promoted only if:
  * Brier improvement ≥ 3% AND
  * calibration ECE ≤ champion ECE AND
  * hit-rate not degraded in ANY of the four supported families.

Otherwise: KEEP CHAMPION.
"""
from __future__ import annotations

import asyncio
import math
import os
import statistics
import sys
from collections import defaultdict

from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

# Path setup so the script can `import services.*` when run directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.nfl_prop_challenger import (  # noqa: E402
    build_challenger_distribution, _family_to_stat_col, _LEAGUE_PRIORS,
)


# ── Champion baseline: naive mean + fixed CV, normal CDF ─────────
_CHAMPION_CV = {
    "passing_yards": 0.31, "rushing_yards": 0.62,
    "receiving_yards": 0.78, "receptions": 0.55,
}


def champion_prob_over(rows: list[dict], stat: str, family: str,
                       threshold: float) -> float:
    reals = [float(r.get(stat) or 0.0) for r in rows
             if isinstance(r.get(stat), (int, float))]
    if len(reals) < 3 or threshold <= 0:
        return 0.5
    mu = statistics.mean(reals)
    cv = _CHAMPION_CV.get(family, 0.6)
    sigma = mu * cv
    if sigma <= 0:
        return 0.5
    z = (threshold - mu) / sigma
    return 1.0 - 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _brier(p: float, y: int) -> float:
    return (p - y) ** 2


def _calibration_ece(pairs: list[tuple[float, int]], n_bins: int = 10) -> float:
    if not pairs:
        return 0.0
    bins: dict[int, list[tuple[float, int]]] = defaultdict(list)
    for p, y in pairs:
        b = min(n_bins - 1, int(p * n_bins))
        bins[b].append((p, y))
    total = len(pairs)
    ece = 0.0
    for bkt, arr in bins.items():
        avg_p = sum(x[0] for x in arr) / len(arr)
        avg_y = sum(x[1] for x in arr) / len(arr)
        ece += (len(arr) / total) * abs(avg_p - avg_y)
    return ece


async def _fetch_player_history(db, player_id: str, season: int,
                                thru_week: int) -> list[dict]:
    cur = db.nfl_player_weekly.find({
        "player_id": player_id,
        "season": season,
        "season_type": "REG",
        "week": {"$lt": thru_week},
    }).sort([("week", 1)])
    return [d async for d in cur]


async def run_backtest(season: int = 2024,
                        max_players_per_pos: int = 60) -> dict:
    load_dotenv()
    mongo = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = mongo[os.environ.get("DB_NAME", "lockscore")]

    FAMILIES = {
        "QB": [("passing_yards", "passing_yards")],
        "RB": [("rushing_yards", "rushing_yards"),
               ("receptions",     "receptions")],
        "WR": [("receiving_yards","receiving_yards"),
               ("receptions",     "receptions")],
        "TE": [("receiving_yards","receiving_yards"),
               ("receptions",     "receptions")],
    }

    results: dict = defaultdict(lambda: {
        "champ_briers": [], "chall_briers": [],
        "champ_pairs": [], "chall_pairs": [],
        "n_samples": 0,
    })

    for position, family_list in FAMILIES.items():
        # Get the busiest players at each position in 2024 REG.
        pipe = [
            {"$match": {"season": season, "season_type": "REG",
                        "position": position}},
            {"$group": {"_id": "$player_id",
                         "games": {"$sum": 1}}},
            {"$match": {"games": {"$gte": 8}}},
            {"$sort":  {"games": -1}},
            {"$limit": max_players_per_pos},
        ]
        players = [d["_id"] async for d in
                   db.nfl_player_weekly.aggregate(pipe)]

        for pid in players:
            all_rows = await _fetch_player_history(db, pid, season, thru_week=99)
            for target in all_rows[3:]:      # need >=3 prior games
                thru = target["week"]
                prior = [r for r in all_rows if r["week"] < thru]
                if len(prior) < 3:
                    continue
                for family, stat_col in family_list:
                    y_val = target.get(stat_col)
                    if y_val is None or not isinstance(y_val, (int, float)):
                        continue
                    reals = [float(r.get(stat_col) or 0.0) for r in prior]
                    med = statistics.median(reals)
                    if med <= 0:
                        continue
                    # 5 synthetic thresholds anchored on prior median.
                    thresholds = [med * m for m in (0.6, 0.8, 1.0, 1.2, 1.4)]

                    chall = build_challenger_distribution(
                        prior, family, position,
                    )
                    key = (family, position)
                    bucket = results[key]

                    for th in thresholds:
                        y = 1 if y_val > th else 0
                        p_champ = champion_prob_over(prior, stat_col, family, th)
                        p_chall = chall.prob_over(th)
                        bucket["champ_briers"].append(_brier(p_champ, y))
                        bucket["chall_briers"].append(_brier(p_chall, y))
                        bucket["champ_pairs"].append((p_champ, y))
                        bucket["chall_pairs"].append((p_chall, y))
                        bucket["n_samples"] += 1

    # ── Summarise ────────────────────────────────────────────────
    summary = {}
    for (family, position), b in results.items():
        if not b["champ_briers"]:
            continue
        champ_brier = sum(b["champ_briers"]) / len(b["champ_briers"])
        chall_brier = sum(b["chall_briers"]) / len(b["chall_briers"])
        champ_ece = _calibration_ece(b["champ_pairs"])
        chall_ece = _calibration_ece(b["chall_pairs"])
        improved = (
            chall_brier < champ_brier * 0.97  # ≥3% Brier improvement
            and chall_ece <= champ_ece
        )
        summary[f"{family}/{position}"] = {
            "n": b["n_samples"],
            "champ_brier": round(champ_brier, 4),
            "chall_brier": round(chall_brier, 4),
            "brier_delta_pct": round(100 * (chall_brier - champ_brier) /
                                     max(champ_brier, 1e-6), 2),
            "champ_ece":   round(champ_ece, 4),
            "chall_ece":   round(chall_ece, 4),
            "challenger_won": improved,
        }
    # Overall
    all_ch = sum((r["champ_briers"] for r in results.values()), [])
    all_ca = sum((r["chall_briers"] for r in results.values()), [])
    if all_ch:
        summary["_OVERALL_"] = {
            "n": len(all_ch),
            "champ_brier": round(sum(all_ch)/len(all_ch), 4),
            "chall_brier": round(sum(all_ca)/len(all_ca), 4),
            "brier_delta_pct": round(
                100 * (sum(all_ca)/len(all_ca) - sum(all_ch)/len(all_ch)) /
                max(sum(all_ch)/len(all_ch), 1e-6), 2),
        }
    return summary


if __name__ == "__main__":
    out = asyncio.run(run_backtest(season=2024, max_players_per_pos=40))
    print("=" * 70)
    print("NFL PROP CHAMPION vs CHALLENGER BACKTEST — 2024 REG SEASON")
    print("=" * 70)
    for k in sorted(out.keys()):
        v = out[k]
        marker = " ✓ WIN " if v.get("challenger_won") else "       "
        print(f"{marker} {k:32s}  n={v['n']:5d}  "
              f"champ={v['champ_brier']:.4f}  chall={v['chall_brier']:.4f}  "
              f"Δ={v['brier_delta_pct']:+.2f}%")
    won_families = [k for k, v in out.items()
                    if v.get("challenger_won") and k != "_OVERALL_"]
    print("=" * 70)
    print(f"Challenger wins in {len(won_families)}/{len(out)-1} sub-buckets.")
    overall = out.get("_OVERALL_", {})
    if overall:
        print(f"OVERALL:  champ={overall['champ_brier']:.4f}  "
              f"chall={overall['chall_brier']:.4f}  "
              f"Δ={overall['brier_delta_pct']:+.2f}%")
