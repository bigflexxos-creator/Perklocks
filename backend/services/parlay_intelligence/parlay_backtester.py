"""Parlay Backtester (Phase 5, 2026-06-30).

Aggregates settled parlays from the `parlay_history` collection into a
performance report used by the learning loop and by the frontend
"parlay stats" surface (existing UI — no new screens).

Metrics
───────
  • Overall: n_parlays, wins, losses, pushes, win_rate, roi_proxy
  • By leg count: hit rate per leg-count bucket (2..10)
  • Best combos: top (sport, market_family) pairs by win_rate w/ ≥3 samples
  • Common losing legs: (sport, market_family) rows with highest lose rate
  • Confidence accuracy: predicted survival vs actual, binned

Never modifies simulators. Never uses sportsbook odds as features —
`parlay_history` never captured book prices.
"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import Optional

logger = logging.getLogger("lockscore.services.parlay_intelligence.backtester")

HISTORY_COLL = "parlay_history"
SNAPSHOT_COLL = "parlay_backtest_snapshots"

DEFAULT_LOOKBACK_DAYS = 60
MIN_SAMPLE_FOR_COMBO = 3


def _cutoff_iso(days: int) -> str:
    return (_dt.datetime.now(_dt.timezone.utc)
            - _dt.timedelta(days=days)).isoformat()


async def _collect_settled(db, days: int) -> list[dict]:
    if db is None:
        return []
    cutoff = _cutoff_iso(days)
    rows: list[dict] = []
    try:
        cursor = db[HISTORY_COLL].find(
            {"status": {"$in": ["won", "lost", "push"]},
             "shown_at": {"$gte": cutoff}},
            {"_id": 0, "legs": 1, "status": 1, "leg_count": 1,
             "survival_pct": 1, "shown_at": 1, "settled_at": 1,
             "signature": 1, "mode": 1},
        )
        async for row in cursor:
            rows.append(row)
    except Exception as e:
        logger.warning("backtester collect failed: %s", e)
    return rows


def _leg_key(leg: dict) -> tuple[str, str]:
    sport = (leg.get("sport") or "").lower()
    fam = (leg.get("market_family") or "other").lower()
    return (sport, fam)


# ═════════════════════════════════════════════════════════════════════
# Public
# ═════════════════════════════════════════════════════════════════════
async def backtest_parlays(db, *, days: int = DEFAULT_LOOKBACK_DAYS,
                           persist: bool = False) -> dict:
    """Run a full backtest snapshot. Optionally persist to
    `parlay_backtest_snapshots`. Returns the report dict."""
    parlays = await _collect_settled(db, days)
    if not parlays:
        return {
            "n_parlays": 0, "wins": 0, "losses": 0, "pushes": 0,
            "win_rate": 0.0, "by_leg_count": {},
            "best_combos": [], "common_losing_legs": [],
            "confidence_accuracy": [],
            "lookback_days": days,
        }

    n = len(parlays)
    wins = sum(1 for p in parlays if p.get("status") == "won")
    losses = sum(1 for p in parlays if p.get("status") == "lost")
    pushes = sum(1 for p in parlays if p.get("status") == "push")
    win_rate = wins / max(1, n)

    # By leg count
    by_lc: dict[int, dict] = {}
    for p in parlays:
        lc = int(p.get("leg_count") or len(p.get("legs") or []))
        if lc <= 0:
            continue
        row = by_lc.setdefault(lc, {"n": 0, "wins": 0, "losses": 0, "pushes": 0})
        row["n"] += 1
        st = p.get("status")
        if st == "won":  row["wins"] += 1
        elif st == "lost": row["losses"] += 1
        elif st == "push": row["pushes"] += 1
    for lc, row in by_lc.items():
        row["win_rate"] = round(row["wins"] / max(1, row["n"]), 3)

    # Combos + losing legs — per-leg (sport, family) aggregation.
    leg_agg: dict[tuple[str, str], dict] = {}
    combo_agg: dict[tuple[tuple[str, str], tuple[str, str]], dict] = {}

    for p in parlays:
        status = p.get("status")
        legs = p.get("legs") or []
        if not legs:
            continue
        keys = [_leg_key(L) for L in legs]
        for k in keys:
            row = leg_agg.setdefault(k, {"n": 0, "wins": 0, "losses": 0})
            row["n"] += 1
            if status == "won": row["wins"] += 1
            elif status == "lost": row["losses"] += 1
        # Combo pairs (sorted for dedupe)
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                ck = tuple(sorted((keys[i], keys[j])))
                row = combo_agg.setdefault(ck, {"n": 0, "wins": 0, "losses": 0})
                row["n"] += 1
                if status == "won": row["wins"] += 1
                elif status == "lost": row["losses"] += 1

    best_combos: list[dict] = []
    for ck, row in combo_agg.items():
        if row["n"] < MIN_SAMPLE_FOR_COMBO:
            continue
        wr = row["wins"] / max(1, row["n"])
        best_combos.append({
            "sport_a": ck[0][0], "family_a": ck[0][1],
            "sport_b": ck[1][0], "family_b": ck[1][1],
            "n": row["n"], "wins": row["wins"], "losses": row["losses"],
            "win_rate": round(wr, 3),
        })
    best_combos.sort(key=lambda r: (r["win_rate"], r["n"]), reverse=True)

    losing_legs: list[dict] = []
    for k, row in leg_agg.items():
        if row["n"] < MIN_SAMPLE_FOR_COMBO:
            continue
        wr = row["wins"] / max(1, row["n"])
        lose_rate = row["losses"] / max(1, row["n"])
        losing_legs.append({
            "sport": k[0], "family": k[1],
            "n": row["n"], "wins": row["wins"], "losses": row["losses"],
            "win_rate": round(wr, 3),
            "lose_rate": round(lose_rate, 3),
        })
    losing_legs.sort(key=lambda r: r["lose_rate"], reverse=True)

    # Confidence calibration: bin predicted survival_pct into deciles
    # and compare to actual win rate. Only counts parlays that recorded
    # `survival_pct` (added in Phase 2).
    bins: dict[int, dict] = {}
    for p in parlays:
        sv = p.get("survival_pct")
        if not isinstance(sv, (int, float)):
            continue
        bucket = min(9, max(0, int(sv // 10)))
        row = bins.setdefault(bucket, {"n": 0, "wins": 0, "pred_sum": 0.0})
        row["n"] += 1
        row["pred_sum"] += float(sv)
        if p.get("status") == "won":
            row["wins"] += 1
    conf_accuracy: list[dict] = []
    for b, row in sorted(bins.items()):
        if row["n"] == 0:
            continue
        conf_accuracy.append({
            "bucket_pct":    b * 10,
            "predicted_avg": round(row["pred_sum"] / row["n"], 1),
            "actual_pct":    round(100.0 * row["wins"] / row["n"], 1),
            "n":             row["n"],
        })

    report = {
        "n_parlays": n, "wins": wins, "losses": losses, "pushes": pushes,
        "win_rate": round(win_rate, 3),
        "by_leg_count": {str(k): v for k, v in sorted(by_lc.items())},
        "best_combos": best_combos[:15],
        "common_losing_legs": losing_legs[:15],
        "confidence_accuracy": conf_accuracy,
        "lookback_days": days,
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
    }

    if persist and db is not None:
        try:
            snap_id = f"pbs_{int(_dt.datetime.now(_dt.timezone.utc).timestamp())}"
            await db[SNAPSHOT_COLL].insert_one({**report, "id": snap_id})
        except Exception as e:
            logger.warning("backtester persist failed: %s", e)
    return report


def summarize_backtest(report: dict) -> list[str]:
    """One-line bullets summarising the backtest — for the parlay explainer."""
    if not report or report.get("n_parlays", 0) == 0:
        return ["No settled parlays yet — learning loop is warming up."]
    bullets = [
        f"{report['n_parlays']} settled parlays over last "
        f"{report.get('lookback_days', 60)}d — "
        f"{int(report['win_rate']*100)}% hit rate.",
    ]
    best = report.get("best_combos") or []
    if best:
        top = best[0]
        bullets.append(
            f"Top combo: {top['sport_a']}/{top['family_a']} × "
            f"{top['sport_b']}/{top['family_b']} "
            f"({int(top['win_rate']*100)}% over {top['n']} parlays)."
        )
    losers = report.get("common_losing_legs") or []
    if losers and losers[0]["lose_rate"] >= 0.55:
        w = losers[0]
        bullets.append(
            f"Frequent parlay-killer: {w['sport']}/{w['family']} "
            f"({int(w['lose_rate']*100)}% loss over {w['n']})."
        )
    return bullets
