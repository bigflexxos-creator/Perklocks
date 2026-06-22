"""Simulator backtest — validate Monte Carlo calibration vs settled picks.

For every settled pick that has `sim_win_probability` stored, we compare
predicted P(win) against actual outcomes (WON/LOST). Computes:
  • Brier score, log-loss, Brier skill score vs naive-50%
  • 6-bucket expected-vs-observed calibration table
  • 4 strategy ROIs:
    - always_bet: every pick where sim_wp set
    - sim_confident_65: only bet sim_wp ≥ 65
    - sim_stronger_signal: only bet picks where sim > model by 5+
    - sim_weaker_signal_fade: fade picks the sim disagrees down on

Returns aggregate stats. When `sport` is None, also returns `by_sport`
breakdown for each supported sport.
"""
from __future__ import annotations
import math
from typing import Any

SUPPORTED_SPORTS = ["MLB", "Soccer", "NBA", "Tennis"]


def _bucket(p: float) -> str:
    if p < 50:
        return "<50"
    if p < 60:
        return "50-60"
    if p < 70:
        return "60-70"
    if p < 80:
        return "70-80"
    if p < 90:
        return "80-90"
    return "90+"


def _odds_to_payout(odds: float) -> float:
    try:
        o = float(odds)
    except (TypeError, ValueError):
        return 0.0
    if o == 0:
        return 0.0
    return o / 100.0 if o > 0 else 100.0 / abs(o)


def _compute_from_rows(rows: list[dict]) -> dict[str, Any]:
    """Pure compute step over a row set — no DB access."""
    buckets: dict[str, dict[str, Any]] = {}
    brier_sum = 0.0
    logloss_sum = 0.0
    sim_units = 0.0
    sim_bets = 0
    model_units = 0.0
    model_bets = 0
    stronger_units = 0.0
    stronger_bets = 0
    weaker_units = 0.0
    weaker_bets = 0
    n = 0

    for r in rows:
        sim_wp = float(r.get("sim_win_probability") or 0)
        actual = 1.0 if r.get("status") == "won" else 0.0
        odds = r.get("book_odds")
        payout = _odds_to_payout(odds)
        if payout <= 0:
            continue
        p = max(0.001, min(0.999, sim_wp / 100.0))
        n += 1
        brier_sum += (p - actual) ** 2
        logloss_sum += -(actual * math.log(p) + (1 - actual) * math.log(1 - p))
        b = _bucket(sim_wp)
        bucket = buckets.setdefault(b, {"n": 0, "wins": 0, "sum_pred": 0.0})
        bucket["n"] += 1
        bucket["wins"] += int(actual)
        bucket["sum_pred"] += sim_wp
        model_units += payout if actual == 1.0 else -1.0
        model_bets += 1
        if sim_wp >= 65.0:
            sim_units += payout if actual == 1.0 else -1.0
            sim_bets += 1
        sig = r.get("sim_signal") or ""
        if sig == "stronger":
            stronger_units += payout if actual == 1.0 else -1.0
            stronger_bets += 1
        elif sig == "weaker":
            weaker_units += payout if actual == 1.0 else -1.0
            weaker_bets += 1

    if n == 0:
        return {"n": 0}

    naive_brier = 0.25
    brier = brier_sum / n
    brier_skill = 1 - (brier / naive_brier)
    calibration = []
    for b in ["<50", "50-60", "60-70", "70-80", "80-90", "90+"]:
        if b not in buckets:
            continue
        bk = buckets[b]
        observed = (bk["wins"] / bk["n"]) * 100 if bk["n"] > 0 else 0.0
        expected = bk["sum_pred"] / bk["n"] if bk["n"] > 0 else 0.0
        calibration.append({
            "bucket": b,
            "n": bk["n"],
            "expected_pct": round(expected, 1),
            "observed_pct": round(observed, 1),
            "delta": round(observed - expected, 1),
        })

    return {
        "n": n,
        "brier": round(brier, 4),
        "log_loss": round(logloss_sum / n, 4),
        "brier_skill_score": round(brier_skill, 4),
        "calibration": calibration,
        "strategies": {
            "always_bet": {
                "bets": model_bets,
                "units": round(model_units, 2),
                "roi_pct": round((model_units / model_bets * 100) if model_bets else 0, 2),
            },
            "sim_confident_65": {
                "bets": sim_bets,
                "units": round(sim_units, 2),
                "roi_pct": round((sim_units / sim_bets * 100) if sim_bets else 0, 2),
            },
            "sim_stronger_signal": {
                "bets": stronger_bets,
                "units": round(stronger_units, 2),
                "roi_pct": round((stronger_units / stronger_bets * 100) if stronger_bets else 0, 2),
            },
            "sim_weaker_signal_fade": {
                "bets": weaker_bets,
                "units": round(weaker_units, 2),
                "roi_pct": round((weaker_units / weaker_bets * 100) if weaker_bets else 0, 2),
            },
        },
    }


async def run_sim_backtest(db, days: int = 30, sport: str | None = None) -> dict[str, Any]:
    """Walk settled picks that have sim_win_probability stored, compute
    calibration & strategy ROIs.

    Args:
        db:    Motor DB handle.
        days:  Lookback window.
        sport: If set, restrict to that sport. If None, returns aggregate +
               per-sport breakdown in `by_sport`.
    """
    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    base_query = {
        "status": {"$in": ["won", "lost"]},
        "sim_win_probability": {"$exists": True, "$ne": None},
        "settled_at": {"$gte": cutoff.isoformat()},
    }
    if sport:
        base_query["sport"] = sport
    else:
        base_query["sport"] = {"$in": SUPPORTED_SPORTS}

    cursor = db.picks.find(
        base_query,
        {
            "_id": 0, "id": 1, "sport": 1, "status": 1, "book_odds": 1, "market": 1,
            "sim_win_probability": 1, "sim_signal": 1,
            "win_probability": 1, "lock_score": 1,
        },
    ).limit(5000)

    all_rows = await cursor.to_list(length=5000)

    if not all_rows:
        empty_by_sport = {}
        if not sport:
            for sp in SUPPORTED_SPORTS:
                empty_by_sport[sp] = {"n": 0, "message": "No settled picks yet."}
        return {
            "n": 0,
            "days": days,
            "sport": sport,
            "message": "No settled picks with sim_win_probability yet. "
                       "Backtest will populate as picks settle.",
            "by_sport": empty_by_sport if not sport else None,
        }

    agg = _compute_from_rows(all_rows)
    agg["days"] = days
    agg["sport"] = sport

    if not sport:
        by_sport: dict[str, Any] = {}
        for sp in SUPPORTED_SPORTS:
            rows_sp = [r for r in all_rows if r.get("sport") == sp]
            if not rows_sp:
                by_sport[sp] = {"n": 0, "message": "No settled picks yet."}
                continue
            res = _compute_from_rows(rows_sp)
            by_sport[sp] = res
        agg["by_sport"] = by_sport

    return agg
