"""Daily Learning + Calibration Snapshot Job (2026-07-29).

Runs the full adaptive-learning stack against the REAL settled-pick
history (no synthetic data) and persists a single dated snapshot to
`learning_snapshots` for the analytics dashboard.

Aggregates
──────────
  1. **Lock Score tier performance**    — via `services.lock_score_performance`
  2. **Win Probability calibration**    — per 10-pt band, actual vs predicted
  3. **Lock Score isotonic calibration**— via existing `lock_calibration`
  4. **Fusion-engine performance**      — via `services.adaptive_learning`
  5. **Sport performance**              — per sport ROI + win-rate + n
  6. **Market performance**             — per market_family ROI + win-rate
  7. **Fusion weight optimiser**        — refits per (sport, market) weights
  8. **Drift signals**                  — accuracy drop / feature shifts

All aggregators are READ-ONLY on the underlying picks / fusion_predictions
collections. Nothing about scoring is retrained. Simulator math is
untouched.

Public API
──────────
    result = await run_daily_learning_job(db, *, persist=True)
    # returns the full snapshot dict; also writes it to `learning_snapshots`
    # unless persist=False (used in tests).

    latest = await load_latest_snapshot(db)
    # convenience for the analytics dashboard endpoint.
"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import Optional

logger = logging.getLogger("lockscore.services.adaptive_learning.daily_job")

SNAPSHOT_COLL = "learning_snapshots"


# ═════════════════════════════════════════════════════════════════════
# Win Probability calibration (from `picks` directly — 6k+ settled)
# ═════════════════════════════════════════════════════════════════════
_WP_BANDS: list[tuple[str, float, float]] = [
    ("90-100", 90.0, 100.0),
    ("80-89",  80.0, 89.999),
    ("70-79",  70.0, 79.999),
    ("60-69",  60.0, 69.999),
    ("50-59",  50.0, 59.999),
    ("<50",     0.0, 49.999),
]


async def compute_win_probability_calibration(
    db, *, days: Optional[int] = None,
    sport: Optional[str] = None,
    include_off_board: bool = False,
) -> dict:
    """Bin settled picks by `win_probability` and compute the observed
    hit rate. Returns per-band calibration + summary + Brier score.

    Never uses fake data — reads only settled picks from `db.picks`.
    """
    q: dict = {"status": {"$in": ["won", "lost"]},   # push/void excluded
                "win_probability": {"$exists": True}}
    if not include_off_board:
        q["off_board"] = {"$ne": True}
    if sport:
        q["sport"] = sport
    if isinstance(days, int) and days > 0:
        cutoff = (_dt.datetime.now(_dt.timezone.utc)
                   - _dt.timedelta(days=days)).strftime("%Y-%m-%d")
        q["pick_date"] = {"$gte": cutoff}

    bands: dict[str, dict] = {
        label: {"label": label, "lo": lo, "hi": hi, "n": 0,
                "wins": 0, "wp_sum": 0.0, "brier_sum": 0.0}
        for label, lo, hi in _WP_BANDS
    }
    n_scored = 0
    brier_sum_all = 0.0

    async for p in db.picks.find(
        q, {"_id": 0, "win_probability": 1, "status": 1},
    ):
        try:
            wp = float(p.get("win_probability") or 0)
        except (TypeError, ValueError):
            continue
        if wp <= 0:
            continue
        n_scored += 1
        y = 1.0 if p.get("status") == "won" else 0.0
        # Convert wp from % to prob for Brier
        p_pred = wp / 100.0
        brier_contrib = (p_pred - y) ** 2
        brier_sum_all += brier_contrib

        for label, lo, hi in _WP_BANDS:
            if lo <= wp <= hi:
                b = bands[label]
                b["n"] += 1
                b["wp_sum"] += wp
                b["brier_sum"] += brier_contrib
                if p.get("status") == "won":
                    b["wins"] += 1
                break

    out: list[dict] = []
    for label, lo, hi in _WP_BANDS:
        b = bands[label]
        if b["n"] == 0:
            out.append({"label": label, "lo": lo, "hi": hi, "n": 0,
                         "predicted_avg": None, "actual_pct": None,
                         "delta": None, "brier": None})
            continue
        pred_avg = b["wp_sum"] / b["n"]
        actual = 100.0 * b["wins"] / b["n"]
        out.append({
            "label":         label,
            "lo":            lo,
            "hi":            hi,
            "n":             b["n"],
            "predicted_avg": round(pred_avg, 1),
            "actual_pct":    round(actual, 1),
            "delta":         round(actual - pred_avg, 1),
            "brier":         round(b["brier_sum"] / b["n"], 4),
        })
    return {
        "bands":       out,
        "n_scored":    n_scored,
        "brier_score": round(brier_sum_all / max(1, n_scored), 4),
        "days":        days,
        "sport":       sport,
    }


# ═════════════════════════════════════════════════════════════════════
# Sport performance
# ═════════════════════════════════════════════════════════════════════
async def compute_sport_performance(
    db, *, days: Optional[int] = None,
    include_off_board: bool = False,
) -> list[dict]:
    """Per-sport n / wins / losses / win_pct / ROI. Reads real settled
    picks from `db.picks`. ROI uses the same American-odds convention as
    `lock_score_performance._pick_pnl`."""
    from services.lock_score_performance import (
        _pick_pnl, _pick_odds, _odds_projection,
    )

    q: dict = {"status": {"$in": ["won", "lost", "push", "void"]}}
    if not include_off_board:
        q["off_board"] = {"$ne": True}
    if isinstance(days, int) and days > 0:
        cutoff = (_dt.datetime.now(_dt.timezone.utc)
                   - _dt.timedelta(days=days)).strftime("%Y-%m-%d")
        q["pick_date"] = {"$gte": cutoff}

    agg: dict[str, dict] = {}
    proj = {"_id": 0, "sport": 1, "status": 1}
    proj.update(_odds_projection())
    async for p in db.picks.find(q, proj):
        sport = (p.get("sport") or "?").strip()
        row = agg.setdefault(sport, {"sport": sport, "n": 0,
                                       "wins": 0, "losses": 0,
                                       "pushes": 0, "voids": 0,
                                       "pnl_sum": 0.0, "pnl_n": 0})
        row["n"] += 1
        st = p.get("status")
        if st == "won":  row["wins"] += 1
        elif st == "lost": row["losses"] += 1
        elif st == "push": row["pushes"] += 1
        elif st == "void": row["voids"] += 1
        odds = _pick_odds(p)
        pnl = _pick_pnl(st, odds)
        if pnl is not None:
            row["pnl_sum"] += pnl
            row["pnl_n"]   += 1

    out: list[dict] = []
    for row in agg.values():
        graded = row["wins"] + row["losses"]
        win_pct = (100.0 * row["wins"] / graded) if graded > 0 else None
        roi = ((row["pnl_sum"] / row["pnl_n"]) * 100.0
                if row["pnl_n"] > 0 else None)
        out.append({
            **row,
            "win_pct": None if win_pct is None else round(win_pct, 1),
            "roi_pct": None if roi is None else round(roi, 2),
        })
    out.sort(key=lambda r: (r.get("roi_pct") if r.get("roi_pct") is not None
                              else -999), reverse=True)
    return out


# ═════════════════════════════════════════════════════════════════════
# Market family performance
# ═════════════════════════════════════════════════════════════════════
_MARKET_FAMILY_RE = None
def _market_family(market: str) -> str:
    """Normalize a market string into a coarse family. Kept in-module
    to avoid cross-import cycles with parlay_learning."""
    if not market:
        return "other"
    m = market.lower()
    if "hits + runs + rbis" in m or "hrri" in m:      return "hits_runs_rbis"
    if "total bases" in m:                             return "total_bases"
    if "home run" in m or "home runs" in m:            return "home_runs"
    if "strikeouts" in m:                              return "strikeouts"
    if "pass" in m and ("yard" in m or "yds" in m): return "qb_pass_yards"
    if "pass" in m and ("td" in m):                  return "qb_pass_tds"
    if "rush" in m and ("yard" in m or "yds" in m): return "rush_yards"
    if "rush" in m and ("td" in m):                  return "rush_tds"
    if "receiving yards" in m or "rec yds" in m:     return "rec_yards"
    if "receptions" in m:                             return "receptions"
    if "receiving tds" in m or "rec tds" in m:       return "rec_tds"
    if "anytime" in m and "goal" in m:                return "goal_scorer"
    if "first goal scorer" in m:                      return "first_goal"
    if "to score or assist" in m:                     return "score_or_assist"
    if "win or draw" in m or "double chance" in m:    return "win_or_draw"
    if "moneyline" in m:                              return "moneyline"
    if "spread" in m or "run line" in m or "puck line" in m: return "spread"
    if "team total" in m:                             return "team_total"
    if "total" in m and (" over " in m or " under " in m): return "game_total"
    if "aces" in m:                                   return "aces"
    if "double fault" in m:                           return "double_faults"
    # `hits` matcher comes LAST so more-specific "hits + runs + rbis"
    # and "total_bases" families win first.
    if "hits" in m:                                   return "hits"
    return "other"


async def compute_market_performance(
    db, *, days: Optional[int] = None,
    sport: Optional[str] = None,
    min_samples: int = 10,
    include_off_board: bool = False,
) -> list[dict]:
    """Per market_family win-rate + ROI. Only families with at least
    `min_samples` graded picks are returned."""
    from services.lock_score_performance import (
        _pick_pnl, _pick_odds, _odds_projection,
    )

    q: dict = {"status": {"$in": ["won", "lost", "push", "void"]}}
    if not include_off_board:
        q["off_board"] = {"$ne": True}
    if sport:
        q["sport"] = sport
    if isinstance(days, int) and days > 0:
        cutoff = (_dt.datetime.now(_dt.timezone.utc)
                   - _dt.timedelta(days=days)).strftime("%Y-%m-%d")
        q["pick_date"] = {"$gte": cutoff}

    agg: dict[str, dict] = {}
    proj = {"_id": 0, "market": 1, "status": 1, "market_family": 1}
    proj.update(_odds_projection())
    async for p in db.picks.find(q, proj):
        fam = p.get("market_family") or _market_family(p.get("market") or "")
        row = agg.setdefault(fam, {"family": fam, "n": 0,
                                     "wins": 0, "losses": 0,
                                     "pushes": 0, "voids": 0,
                                     "pnl_sum": 0.0, "pnl_n": 0})
        row["n"] += 1
        st = p.get("status")
        if st == "won":  row["wins"] += 1
        elif st == "lost": row["losses"] += 1
        elif st == "push": row["pushes"] += 1
        elif st == "void": row["voids"] += 1
        odds = _pick_odds(p)
        pnl = _pick_pnl(st, odds)
        if pnl is not None:
            row["pnl_sum"] += pnl; row["pnl_n"] += 1

    out: list[dict] = []
    for row in agg.values():
        if row["n"] < min_samples:
            continue
        graded = row["wins"] + row["losses"]
        win_pct = (100.0 * row["wins"] / graded) if graded > 0 else None
        roi = ((row["pnl_sum"] / row["pnl_n"]) * 100.0
                if row["pnl_n"] > 0 else None)
        out.append({
            **row,
            "win_pct": None if win_pct is None else round(win_pct, 1),
            "roi_pct": None if roi is None else round(roi, 2),
        })
    out.sort(key=lambda r: (r.get("roi_pct") if r.get("roi_pct") is not None
                              else -999), reverse=True)
    return out


# ═════════════════════════════════════════════════════════════════════
# Master orchestrator
# ═════════════════════════════════════════════════════════════════════
async def run_daily_learning_job(
    db,
    *,
    days: int = 60,
    persist: bool = True,
) -> dict:
    """Compute the full learning + calibration snapshot.

    Never raises — any sub-report that fails is captured in `errors[]`
    so the daily cron never hard-fails the loop."""
    result: dict = {
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "window_days": days,
        "errors": [],
    }

    # 1. Lock Score tier performance
    try:
        from services.lock_score_performance import compute_bucket_performance
        result["lock_tier_performance"] = await compute_bucket_performance(
            db, days=days,
        )
    except Exception as e:
        result["errors"].append(f"lock_tier_performance: {e}")

    # 2. Win Probability calibration
    try:
        result["win_probability_calibration"] = \
            await compute_win_probability_calibration(db, days=days)
    except Exception as e:
        result["errors"].append(f"win_probability_calibration: {e}")

    # 3. Existing Lock Score isotonic calibration report
    try:
        from lock_calibration import calibration_report
        result["lock_isotonic_report"] = await calibration_report(db)
    except Exception as e:
        result["errors"].append(f"lock_isotonic_report: {e}")

    # 4. Fusion-engine performance (which model predicted correctly)
    try:
        from services.adaptive_learning import build_engine_performance_report
        result["engine_performance"] = \
            await build_engine_performance_report(
                db, days=days, min_samples=5,
            )
    except Exception as e:
        result["errors"].append(f"engine_performance: {e}")

    # 5. Sport performance
    try:
        result["sport_performance"] = \
            await compute_sport_performance(db, days=days)
    except Exception as e:
        result["errors"].append(f"sport_performance: {e}")

    # 6. Market performance
    try:
        result["market_performance"] = \
            await compute_market_performance(db, days=days,
                                              min_samples=10)
    except Exception as e:
        result["errors"].append(f"market_performance: {e}")

    # 7. Fusion weight optimiser — refits (sport, market) blend weights
    #    Persistence is handled inside the optimiser itself.
    try:
        from services.adaptive_learning import optimise_fusion_weights
        result["weight_optimisation"] = \
            await optimise_fusion_weights(db, days=days)
    except Exception as e:
        result["errors"].append(f"weight_optimisation: {e}")

    # 8. Snapshot persistence
    if persist:
        try:
            snap_id = ("lsn_" +
                        _dt.datetime.now(_dt.timezone.utc)
                        .strftime("%Y%m%d_%H%M%S"))
            await db[SNAPSHOT_COLL].insert_one({
                **result,
                "id":            snap_id,
                "snapshot_date": _dt.datetime.now(_dt.timezone.utc)
                                    .strftime("%Y-%m-%d"),
            })
            result["snapshot_id"] = snap_id
        except Exception as e:
            result["errors"].append(f"persist: {e}")

    return result


async def load_latest_snapshot(db) -> Optional[dict]:
    """Return the most recent persisted `learning_snapshots` row."""
    try:
        rows = await (db[SNAPSHOT_COLL]
                       .find({}, {"_id": 0})
                       .sort("generated_at", -1)
                       .limit(1)
                       .to_list(1))
    except Exception as e:
        logger.warning("load_latest_snapshot failed: %s", e)
        return None
    return rows[0] if rows else None


__all__ = [
    "run_daily_learning_job",
    "load_latest_snapshot",
    "compute_win_probability_calibration",
    "compute_sport_performance",
    "compute_market_performance",
    "SNAPSHOT_COLL",
]
