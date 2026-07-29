"""Per-engine performance tracker (2026-07-28).

Ranks the fusion sources — ML / Similar / Player H2H / Monte Carlo /
Fused — on accuracy, Brier, mean absolute error, and win-rate against
the actual outcome.  Also identifies best and worst markets.

Public API
──────────
    report = await build_engine_performance_report(
        db, sport=None, days=90, min_samples=10,
    )

    → {
        "n_graded":                int,
        "engines":                 [ {engine, n, accuracy, brier, mae,
                                       wins_vs_others, avg_probability} ],
        "engine_ranking":          [ engine, ... ]  # best → worst by Brier
        "best_markets":            [ {market, n, accuracy} ],
        "worst_markets":           [ {market, n, accuracy} ],
        "best_engine_by_market":   { market: engine },
        "generated_at":            str ISO,
      }

Zero writes. Never raises. Uses the same `fusion_predictions` docs
`calibration.build_calibration_report` reads.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Optional

_ENGINES = ("ml", "similar", "player_h2h", "simulator", "fused")


async def build_engine_performance_report(
    db,
    *,
    sport: Optional[str] = None,
    days: int = 90,
    min_samples: int = 10,
    top_k: int = 5,
) -> dict:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    q: dict = {"actual_value": {"$ne": None},
                "created_at": {"$gte": since}}
    if sport:
        q["sport"] = sport

    engine_agg: dict[str, dict] = {
        e: {"n": 0, "correct": 0, "brier_sum": 0.0,
              "err_sum": 0.0, "wins": 0, "prob_sum": 0.0}
        for e in _ENGINES
    }
    market_agg: dict[str, dict] = {}
    market_engine_wins: dict[str, dict[str, int]] = {}

    n_graded = 0
    async for d in db.fusion_predictions.find(q, {"_id": 0}):
        thr = d.get("threshold")
        actual = d.get("actual_value")
        p_fused = d.get("final_probability")
        if thr is None or actual is None or p_fused is None:
            continue
        try:
            thr = float(thr); actual = float(actual); p_fused = float(p_fused)
        except (TypeError, ValueError):
            continue
        y = 1.0 if actual > thr else 0.0
        n_graded += 1
        market = d.get("market") or "?"
        winning_engine = d.get("winning_component")

        # Per-market bookkeeping.
        row = market_agg.setdefault(market, {"n": 0, "correct": 0,
                                              "brier_sum": 0.0})
        row["n"] += 1
        row["brier_sum"] += (p_fused - y) ** 2
        if (p_fused >= 0.5) == (y >= 0.5):
            row["correct"] += 1
        if winning_engine:
            we = market_engine_wins.setdefault(market, {})
            we[winning_engine] = we.get(winning_engine, 0) + 1

        # Per-engine bookkeeping.
        comps = d.get("components") or {}
        for name in ("ml", "similar", "player_h2h", "simulator"):
            c = comps.get(name) or {}
            if not isinstance(c, dict) or not c.get("available"):
                continue
            p_e = c.get("probability")
            if p_e is None:
                continue
            try:
                p_e = float(p_e)
            except (TypeError, ValueError):
                continue
            engine_agg[name]["n"] += 1
            engine_agg[name]["prob_sum"] += p_e
            engine_agg[name]["brier_sum"] += (p_e - y) ** 2
            engine_agg[name]["err_sum"] += abs(p_e - y)
            if (p_e >= 0.5) == (y >= 0.5):
                engine_agg[name]["correct"] += 1
        # Fused row.
        engine_agg["fused"]["n"] += 1
        engine_agg["fused"]["prob_sum"] += p_fused
        engine_agg["fused"]["brier_sum"] += (p_fused - y) ** 2
        engine_agg["fused"]["err_sum"] += abs(p_fused - y)
        if (p_fused >= 0.5) == (y >= 0.5):
            engine_agg["fused"]["correct"] += 1
        if winning_engine and winning_engine in engine_agg:
            engine_agg[winning_engine]["wins"] += 1

    # Materialise engines.
    engines_out: list[dict] = []
    for e, v in engine_agg.items():
        n = v["n"]
        if n == 0:
            continue
        engines_out.append({
            "engine":              e,
            "n":                   n,
            "accuracy":            round(v["correct"] / n, 4),
            "brier":               round(v["brier_sum"] / n, 4),
            "mae":                 round(v["err_sum"] / n, 4),
            "wins_vs_others":      v["wins"],
            "avg_probability":     round(v["prob_sum"] / n, 4),
        })

    # Rank by Brier (lower = better). Only rank engines that hit the
    # `min_samples` floor.
    ranked = sorted([e for e in engines_out if e["n"] >= min_samples],
                     key=lambda x: x["brier"])
    ranking = [e["engine"] for e in ranked]

    # Market best/worst.
    market_rows = []
    for m, v in market_agg.items():
        if v["n"] < min_samples:
            continue
        market_rows.append({
            "market":   m,
            "n":        v["n"],
            "accuracy": round(v["correct"] / v["n"], 4),
            "brier":    round(v["brier_sum"] / v["n"], 4),
        })
    market_rows.sort(key=lambda r: r["accuracy"], reverse=True)
    best_markets = market_rows[:top_k]
    worst_markets = list(reversed(market_rows[-top_k:]))

    # Best engine per market.
    best_engine_by_market: dict[str, str] = {}
    for m, wins_map in market_engine_wins.items():
        if not wins_map:
            continue
        best_engine_by_market[m] = max(wins_map.items(),
                                        key=lambda kv: kv[1])[0]

    return {
        "n_graded":              n_graded,
        "engines":               engines_out,
        "engine_ranking":        ranking,
        "best_markets":          best_markets,
        "worst_markets":         worst_markets,
        "best_engine_by_market": best_engine_by_market,
        "window_days":           days,
        "min_samples":           min_samples,
        "generated_at":          datetime.now(timezone.utc).isoformat(),
    }


__all__ = ["build_engine_performance_report"]
