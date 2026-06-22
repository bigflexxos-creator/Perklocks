"""MLB Simulator backtest — validate Monte Carlo calibration vs settled picks.

For every settled MLB pick that has `sim_win_probability` stored, we compare
the predicted P(win) against the actual outcome (WON/LOST). We bucket picks
into 5 confidence bands (50–60, 60–70, 70–80, 80–90, 90+) and compute the
*observed* hit rate per band. A well-calibrated simulator's observed rate
should land inside the band (e.g., 60–70% band → ~65% hit rate).

Also computes:
  • Brier score (lower is better, 0.25 = naive)
  • Log loss (lower is better)
  • Brier skill score vs. always-50% baseline
  • ROI when betting only sim-confident picks (sim_wp ≥ 65)
  • ROI when betting only model-confident picks (no sim)
"""
from __future__ import annotations
import math
from typing import Any


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
    """American odds → net payout per 1u staked (e.g. +150 → 1.50, -110 → 0.909)."""
    try:
        o = float(odds)
    except (TypeError, ValueError):
        return 0.0
    if o == 0:
        return 0.0
    return o / 100.0 if o > 0 else 100.0 / abs(o)


async def run_sim_backtest(db, days: int = 30) -> dict[str, Any]:
    """Walk settled MLB picks that have sim_win_probability, compute calibration."""
    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    cursor = db.picks.find(
        {
            "sport": "MLB",
            "status": {"$in": ["won", "lost"]},
            "sim_win_probability": {"$exists": True, "$ne": None},
            "settled_at": {"$gte": cutoff.isoformat()},
        },
        {
            "_id": 0, "id": 1, "status": 1, "book_odds": 1, "market": 1,
            "sim_win_probability": 1, "sim_signal": 1,
            "win_probability": 1, "lock_score": 1,
        },
    ).limit(2000)

    rows = await cursor.to_list(length=2000)

    buckets: dict[str, dict[str, Any]] = {}
    brier_sum = 0.0
    logloss_sum = 0.0
    sim_units = 0.0   # P&L when betting sim-confident picks (sim_wp ≥ 65)
    sim_bets = 0
    model_units = 0.0  # P&L betting all (baseline)
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

        # Brier + log-loss
        brier_sum += (p - actual) ** 2
        logloss_sum += -(actual * math.log(p) + (1 - actual) * math.log(1 - p))

        b = _bucket(sim_wp)
        bucket = buckets.setdefault(b, {"n": 0, "wins": 0, "sum_pred": 0.0})
        bucket["n"] += 1
        bucket["wins"] += int(actual)
        bucket["sum_pred"] += sim_wp

        # Always bet (model baseline)
        model_units += payout if actual == 1.0 else -1.0
        model_bets += 1

        # Sim-confident strategy: only bet picks where sim_wp ≥ 65
        if sim_wp >= 65.0:
            sim_units += payout if actual == 1.0 else -1.0
            sim_bets += 1

        # Sim-disagreement strategies
        sig = r.get("sim_signal") or ""
        if sig == "stronger":
            stronger_units += payout if actual == 1.0 else -1.0
            stronger_bets += 1
        elif sig == "weaker":
            weaker_units += payout if actual == 1.0 else -1.0
            weaker_bets += 1

    if n == 0:
        return {
            "n": 0,
            "message": "No settled MLB picks with sim_win_probability yet. "
                       "Backtest will populate as MLB picks settle.",
            "days": days,
        }

    # Naive Brier baseline: predict 0.5 always
    naive_brier = 0.25
    brier = brier_sum / n
    brier_skill = 1 - (brier / naive_brier)

    # Bucket calibration table
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
        "days": days,
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
