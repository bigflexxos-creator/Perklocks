"""Back-test loop — replay strategies against historical settled picks.

Phase 3 companion to bandit.py. Answers "what would my P&L curve have
looked like if I'd only played strategy X over the last N days?".

Used for:
  - Validating new arms before promoting them in the live bandit
  - Comparing the bandit's actual decisions against optimal hindsight
  - Auto-tuning strategy thresholds (lock-floor / edge-floor / odds-band)
  - Exposing a "Strategy Performance" analytics panel

Output per strategy:
  - n picks, decisive count, hit rate
  - units risked, units profit, ROI
  - max drawdown (in units)
  - Sharpe-like ratio (mean / stddev of unit returns)
  - cumulative P&L curve sampled at each pick
"""
from __future__ import annotations

import logging
import math
import statistics
from datetime import datetime, timezone, timedelta
from typing import Callable, Optional

from bandit import ARMS, matching_arms

logger = logging.getLogger("lockscore.backtest")


def _parse_ts(ts: str | None) -> Optional[datetime]:
    if not ts:
        return None
    try:
        iso = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


async def backtest_arms(db, days: int = 30) -> dict:
    """Replay every arm against the last `days` of settled picks.

    Returns: {arm: {n, wins, losses, hit_rate, roi, units_profit,
                    max_drawdown, sharpe, curve}}
    where `curve` is a list of (timestamp, cumulative_units_profit) tuples.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_iso = cutoff.isoformat()
    cursor = db.picks.find(
        {
            "status": {"$in": ["won", "lost", "push"]},
            "$or": [
                {"settled_at": {"$gte": cutoff_iso}},
                {"event_time": {"$gte": cutoff_iso}},
            ],
        },
        {"_id": 0, "sport": 1, "market": 1, "status": 1,
         "lock_score": 1, "edge_percent": 1, "book_odds": 1,
         "units_profit": 1, "units_risked": 1,
         "settled_at": 1, "event_time": 1},
    )
    picks = await cursor.to_list(length=20_000)
    if not picks:
        return {"window_days": days, "n_picks": 0, "arms": {}}

    # Sort by settle time for the equity curve.
    def _sort_key(p):
        dt = _parse_ts(p.get("settled_at")) or _parse_ts(p.get("event_time"))
        return dt or datetime.min.replace(tzinfo=timezone.utc)
    picks.sort(key=_sort_key)

    results: dict[str, dict] = {}
    for arm_name in ARMS.keys():
        arm_picks = [p for p in picks if arm_name in matching_arms(p)]
        decisive = [p for p in arm_picks if p["status"] in ("won", "lost")]
        if not arm_picks:
            results[arm_name] = {
                "description": ARMS[arm_name]["description"],
                "n": 0, "wins": 0, "losses": 0, "push": 0,
                "hit_rate": 0.0, "units_risked": 0.0, "units_profit": 0.0,
                "roi": 0.0, "max_drawdown": 0.0, "sharpe": 0.0, "curve": [],
            }
            continue

        wins = sum(1 for p in decisive if p["status"] == "won")
        losses = sum(1 for p in decisive if p["status"] == "lost")
        push = sum(1 for p in arm_picks if p["status"] == "push")
        hit_rate = (wins / len(decisive) * 100) if decisive else 0.0
        units_risked = sum(float(p.get("units_risked") or 0)
                           for p in arm_picks if p["status"] != "push")
        units_profit = sum(float(p.get("units_profit") or 0) for p in arm_picks)
        roi = (units_profit * 100 / units_risked) if units_risked else 0.0

        # Equity curve + max drawdown
        cum = 0.0
        peak = 0.0
        max_dd = 0.0
        curve: list[tuple[str, float]] = []
        per_pick_returns: list[float] = []
        for p in arm_picks:
            up = float(p.get("units_profit") or 0)
            cum += up
            peak = max(peak, cum)
            dd = peak - cum
            if dd > max_dd:
                max_dd = dd
            ts = p.get("settled_at") or p.get("event_time") or ""
            curve.append((ts, round(cum, 3)))
            ur = float(p.get("units_risked") or 0)
            if ur > 0:
                per_pick_returns.append(up / ur)

        # Sharpe-like ratio (mean / stddev of per-unit returns)
        if len(per_pick_returns) >= 2:
            mean_r = statistics.mean(per_pick_returns)
            std_r = statistics.pstdev(per_pick_returns) or 1e-6
            sharpe = round(mean_r / std_r * math.sqrt(len(per_pick_returns)), 3)
        else:
            sharpe = 0.0

        results[arm_name] = {
            "description": ARMS[arm_name]["description"],
            "n": len(arm_picks),
            "wins": wins,
            "losses": losses,
            "push": push,
            "hit_rate": round(hit_rate, 1),
            "units_risked": round(units_risked, 2),
            "units_profit": round(units_profit, 2),
            "roi": round(roi, 2),
            "max_drawdown": round(max_dd, 2),
            "sharpe": sharpe,
            "curve": curve[-50:],  # cap curve length for payload size
        }

    # Sort by ROI desc for easy ranking.
    ranked = sorted(results.items(), key=lambda kv: kv[1].get("roi", 0), reverse=True)
    return {
        "window_days": days,
        "n_picks": len(picks),
        "ranked": [name for name, _ in ranked],
        "arms": dict(ranked),
    }


async def backtest_custom(
    db, *,
    days: int = 30,
    lock_floor: float = 0,
    edge_floor: float = -100,
    odds_min: int = -10000,
    odds_max: int = 10000,
    sport: str | None = None,
    market_keyword: str | None = None,
) -> dict:
    """Ad-hoc backtest with explicit filters. Use to A/B-test new strategies
    before promoting them to a permanent arm."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    q: dict = {
        "status": {"$in": ["won", "lost", "push"]},
        "$or": [
            {"settled_at": {"$gte": cutoff.isoformat()}},
            {"event_time": {"$gte": cutoff.isoformat()}},
        ],
        "lock_score": {"$gte": lock_floor},
        "edge_percent": {"$gte": edge_floor},
        "book_odds": {"$gte": odds_min, "$lte": odds_max},
    }
    if sport:
        q["sport"] = sport
    if market_keyword:
        q["market"] = {"$regex": market_keyword, "$options": "i"}

    cursor = db.picks.find(q, {"_id": 0, "status": 1,
                                "units_profit": 1, "units_risked": 1})
    picks = await cursor.to_list(length=20_000)
    decisive = [p for p in picks if p["status"] in ("won", "lost")]
    wins = sum(1 for p in decisive if p["status"] == "won")
    units_risked = sum(float(p.get("units_risked") or 0)
                       for p in picks if p["status"] != "push")
    units_profit = sum(float(p.get("units_profit") or 0) for p in picks)
    return {
        "filters": {
            "days": days, "lock_floor": lock_floor, "edge_floor": edge_floor,
            "odds_min": odds_min, "odds_max": odds_max,
            "sport": sport, "market_keyword": market_keyword,
        },
        "n": len(picks),
        "wins": wins,
        "losses": len(decisive) - wins,
        "push": len(picks) - len(decisive),
        "hit_rate": round(wins / len(decisive) * 100, 1) if decisive else 0.0,
        "units_risked": round(units_risked, 2),
        "units_profit": round(units_profit, 2),
        "roi": round(units_profit * 100 / units_risked, 2) if units_risked else 0.0,
    }
