"""HTTP routes for the `/api/analytics/*` endpoint family.

Covers (15 endpoints):
  • Model performance      (auto-tracked ROI / CLV / edge / calibration)
  • Sim backtest           (Monte Carlo backtest with per-sport breakdown)
  • Learned weights        (self-tuning engine state)
  • Bandit state           (Thompson sampling arms)
  • Backtest               (Phase-3 strategy arm replay)
  • Backtest custom        (ad-hoc filtered backtest)
  • V2 dashboard           (market perf, calibration, weights, audit log)
  • V2 recompute           (force re-aggregation)
  • Isolated learning buckets (read / recompute / rollback)
  • Calibration            (read / refit)
  • xG Form shadow A/B     (HOT vs COLD vs NEUTRAL hit-rate report)
  • Learn now              (force learning loop + apply to today's picks)

Extracted from server.py during the 2026-06-24 Phase-2 monolith
decomposition. Zero behavior change — only relocation. Mounted by
`server.py` via `app.include_router(analytics_routes.router)`.

Most handlers are thin wrappers over functions defined in sibling
modules (`analytics.py`, `backtest.py`, `learning_*`,
`lock_calibration.py`, `brain.sim_backtest`, `learning_system_v2.py`).
The two non-trivial bodies — `/analytics/v2` and `/analytics/xg-form-shadow`
— are inline because they contain endpoint-specific aggregation logic.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends

from auth import UserPublic
from deps import current_user, db, today_str
from rate_limit import rate_limit

router = APIRouter(prefix="/api")

# ── SEC-002 (2026-06-26) ─────────────────────────────────────────────
# Per-user throttle for the 4 expensive recompute / refit / learn
# endpoints below. 30 req/min, burst 10 — generous for legit admin
# dashboard use but blocks scripted abuse that would re-run the
# isotonic calibration fit, Thompson bandit reseed, or full-pick
# learning sweep on every keystroke. Mirrors the `_compute_throttle`
# scope=user policy applied elsewhere in server.py.
_analytics_throttle = rate_limit(rate_per_min=30, burst=10, scope="user")


@router.get("/analytics/model-performance")
async def model_performance(
    user: Annotated[UserPublic, Depends(current_user)],
    days: int = 30,
    backfill: bool = True,
):
    """Auto-tracked model performance: ROI, CLV, Edge, calibration. Does
    NOT require the user to log any bets — every generated pick is
    simulated as a 1u flat stake."""
    from analytics import backfill_metrics, compute_model_performance
    if backfill:
        await backfill_metrics(db)
    return await compute_model_performance(db, days=days)


@router.get("/analytics/sim-backtest")
async def sim_backtest_endpoint(
    user: Annotated[UserPublic, Depends(current_user)],
    days: int = 30,
    sport: str | None = None,
):
    """Simulator backtest with optional per-sport breakdown. Returns
    calibration + strategy ROI against settled picks. When `sport` is
    None, returns an aggregate plus per-sport sections."""
    from brain.sim_backtest import run_sim_backtest
    return await run_sim_backtest(db, days=days, sport=sport)


@router.get("/analytics/learned-weights")
async def learned_weights(user: Annotated[UserPublic, Depends(current_user)]):
    """What the self-tuning engine has learned from past picks."""
    doc = await db.learned_weights.find_one({"_id": "current"}, {"_id": 0})
    if not doc:
        return {"buckets": [], "calibration": [], "updated_at": None, "sample_size": 0}
    return doc


@router.get("/analytics/bandit")
async def bandit_state(user: Annotated[UserPublic, Depends(current_user)]):
    """Phase-3 learning: Multi-Armed Bandit (Thompson sampling) arm
    states. Returns every strategy arm with its Beta(α, β) posterior,
    n samples, wins/losses, units P&L, ROI, posterior mean, and last
    Thompson sample. Sorted by posterior mean descending."""
    arms = await db.bandit_arms.find({}, {"_id": 0}).sort(
        "posterior_mean", -1
    ).to_list(length=100)
    return {"arms": arms, "n_arms": len(arms)}


@router.get("/analytics/backtest")
async def backtest_endpoint(
    user: Annotated[UserPublic, Depends(current_user)],
    days: int = 30,
):
    """Phase-3 back-test: replay every strategy arm against the last N
    days of settled picks. Returns ROI, hit rate, max drawdown, Sharpe
    per arm."""
    from backtest import backtest_arms
    return await backtest_arms(db, days=days)


@router.get("/analytics/backtest-custom")
async def backtest_custom_endpoint(
    user: Annotated[UserPublic, Depends(current_user)],
    days: int = 30,
    lock_floor: float = 0,
    edge_floor: float = -100,
    odds_min: int = -10000,
    odds_max: int = 10000,
    sport: Optional[str] = None,
    market_keyword: Optional[str] = None,
):
    """Ad-hoc backtest with explicit filters — A/B-test new strategies
    before promoting them to permanent bandit arms."""
    from backtest import backtest_custom
    return await backtest_custom(
        db, days=days, lock_floor=lock_floor, edge_floor=edge_floor,
        odds_min=odds_min, odds_max=odds_max, sport=sport,
        market_keyword=market_keyword,
    )


@router.get("/analytics/v2")
async def analytics_v2(user: Annotated[UserPublic, Depends(current_user)]):
    """Learning System v2 dashboard payload.

    Returns: market performance rows, band calibration, market weights,
    learning changes log (last 30), and high-level totals. Used by the
    new Analytics dashboard sections."""
    state = await db.learning_state.find_one(
        {"_id": "learning_v2_state"}, {"_id": 0},
    ) or {}
    # Last 30 audit log entries (most recent first).
    log = await db.learning_log.find({}, {"_id": 0}).sort(
        "ts", -1,
    ).to_list(length=30)
    state["changes_log"] = log
    # Sport-level profit summary.
    sport_rows: dict[str, dict] = {}
    async for p in db.picks.aggregate([
        {"$match": {"status": {"$in": ["won", "lost", "push"]}}},
        {"$group": {
            "_id": "$sport",
            "n": {"$sum": 1},
            "won": {"$sum": {"$cond": [{"$eq": ["$status", "won"]}, 1, 0]}},
            "lost": {"$sum": {"$cond": [{"$eq": ["$status", "lost"]}, 1, 0]}},
            "units_risked": {"$sum": {"$ifNull": ["$units_risked", 0]}},
            "units_profit": {"$sum": {"$ifNull": ["$units_profit", 0]}},
            "clv_avg": {"$avg": "$clv_value"},
        }},
    ]):
        s = p["_id"]
        risked = p.get("units_risked") or 0
        profit = p.get("units_profit") or 0
        sport_rows[s] = {
            "sport": s,
            "n": p["n"], "won": p["won"], "lost": p["lost"],
            "units_risked": round(risked, 2),
            "units_profit": round(profit, 2),
            "roi_pct": round((profit / risked * 100.0) if risked else 0, 2),
            "hit_rate_pct": round((p["won"] / (p["won"] + p["lost"]) * 100.0)
                                  if (p["won"] + p["lost"]) else 0, 2),
            "clv_avg": round(p.get("clv_avg") or 0, 2),
        }
    state["profit_by_sport"] = list(sport_rows.values())

    # Bet-Type breakdown — STRAIGHT (1.0u) / REDUCED (0.5u) / PARLAY
    # (0.25u). ROI is weighted per spec so heavy chalk doesn't distort
    # the metric.
    bt_rows: dict[str, dict] = {}
    async for p in db.picks.aggregate([
        {"$match": {"status": {"$in": ["won", "lost", "push"]}}},
        {"$group": {
            "_id": {"$ifNull": ["$bet_type", "STRAIGHT"]},
            "n": {"$sum": 1},
            "won": {"$sum": {"$cond": [{"$eq": ["$status", "won"]}, 1, 0]}},
            "lost": {"$sum": {"$cond": [{"$eq": ["$status", "lost"]}, 1, 0]}},
            "units_risked": {"$sum": {"$ifNull": ["$units_risked", 0]}},
            "units_profit": {"$sum": {"$ifNull": ["$units_profit", 0]}},
        }},
    ]):
        bt = p["_id"]
        risked = p.get("units_risked") or 0
        profit = p.get("units_profit") or 0
        bt_rows[bt] = {
            "bet_type": bt,
            "n": p["n"], "won": p["won"], "lost": p["lost"],
            "units_risked": round(risked, 2),
            "units_profit": round(profit, 2),
            "roi_pct": round((profit / risked * 100.0) if risked else 0, 2),
            "hit_rate_pct": round((p["won"] / (p["won"] + p["lost"]) * 100.0)
                                  if (p["won"] + p["lost"]) else 0, 2),
        }
    state["profit_by_bet_type"] = list(bt_rows.values())
    return state


@router.post("/analytics/v2/recompute")
async def analytics_v2_recompute(user: Annotated[UserPublic, Depends(current_user)]):
    """Force re-run of the v2 learning aggregation (market perf,
    calibration, band gates, market weights, audit log). Returns the
    new state summary."""
    from learning_system_v2 import recompute_and_persist
    return await recompute_and_persist(db)


# ──────────────────────────────────────────────────────────────────────
# Isolated learning buckets (sport × market_type × prop_type)
# ANALYTICS-ONLY: see /app/backend/learning_buckets.py — does NOT
# influence predictions, lock scores, or confidence outputs. Pure
# dashboard.
# ──────────────────────────────────────────────────────────────────────


@router.get("/analytics/buckets")
async def analytics_buckets(user: Annotated[UserPublic, Depends(current_user)]):
    """Return per-sport, per-market-type, per-prop-type bucket
    performance. NEVER influences live predictions. Pure analytics."""
    from learning_buckets import get_buckets
    return await get_buckets(db)


@router.get("/analytics/calibration")
async def analytics_calibration(user: Annotated[UserPublic, Depends(current_user)]):
    """Lock-score calibration report — Expected vs Actual vs Delta per
    band. Driven by the isotonic-regression curve fit nightly (or
    every 100 newly-settled picks via the settlement loop)."""
    from lock_calibration import calibration_report
    return await calibration_report(db)


@router.post("/analytics/calibration/refit")
async def analytics_calibration_refit(user: Annotated[UserPublic, Depends(current_user)]):
    """Manual trigger to refit the calibration curve right now (admin
    safety valve — does the same thing the auto-loop does on a
    100-pick cadence)."""
    from lock_calibration import fit_from_db
    return await fit_from_db(db)


@router.get("/analytics/xg-form-shadow")
async def analytics_xg_form_shadow(
    user: Annotated[UserPublic, Depends(current_user)],
):
    """xG Form A/B shadow report — does HOT form actually correlate
    with higher hit rate?

    Aggregates every settled soccer goalscorer pick that carries an
    `understat_form` snapshot. Groups by `understat_form.label` and
    reports n / won / lost / hit_rate / avg_lock / avg_shadow /
    brier_live / brier_shadow / delta_hit_pp.

    The decision rule for promoting the lift from shadow → live is
    SIMPLE: HOT.delta_hit >= +5pp AND COLD.delta_hit <= -5pp AND
    n_HOT >= 30 AND n_COLD >= 30. Anything less is too thin a sample
    to risk live deployment.
    """
    cur = db.picks.find(
        {
            "sport":           "Soccer",
            "understat_form":  {"$exists": True},
            "status":          {"$in": ["won", "lost"]},
        },
        {
            "_id": 0,
            "lock_score": 1,
            "win_probability": 1,
            "status": 1,
            "understat_form.label": 1,
            "understat_form.shadow_lock_score": 1,
            "understat_form.shadow_win_probability": 1,
        },
    )

    buckets: dict[str, dict] = {
        "HOT":     {"n": 0, "won": 0, "lock_sum": 0.0, "shadow_sum": 0.0,
                    "brier_live": 0.0, "brier_shadow": 0.0},
        "COLD":    {"n": 0, "won": 0, "lock_sum": 0.0, "shadow_sum": 0.0,
                    "brier_live": 0.0, "brier_shadow": 0.0},
        "NEUTRAL": {"n": 0, "won": 0, "lock_sum": 0.0, "shadow_sum": 0.0,
                    "brier_live": 0.0, "brier_shadow": 0.0},
    }

    async for pick in cur:
        form = pick.get("understat_form") or {}
        label = form.get("label") or "NEUTRAL"
        if label not in buckets:
            continue
        b = buckets[label]
        won = 1 if pick.get("status") == "won" else 0
        b["n"]   += 1
        b["won"] += won
        lock_score = pick.get("lock_score") or 0
        shadow_lock = form.get("shadow_lock_score") or lock_score
        b["lock_sum"]   += float(lock_score)
        b["shadow_sum"] += float(shadow_lock)
        # Brier scores — quadratic loss between predicted prob and actual.
        wp_live   = float(pick.get("win_probability") or 0) / 100.0
        wp_shadow = float(form.get("shadow_win_probability") or pick.get("win_probability") or 0) / 100.0
        b["brier_live"]   += (wp_live   - won) ** 2
        b["brier_shadow"] += (wp_shadow - won) ** 2

    # Final aggregation + delta_hit baseline against NEUTRAL.
    out: dict[str, Any] = {}
    base_hit = 0.0
    if buckets["NEUTRAL"]["n"] > 0:
        base_hit = buckets["NEUTRAL"]["won"] / buckets["NEUTRAL"]["n"]
    for label, b in buckets.items():
        n = b["n"]
        if n > 0:
            hit_rate = b["won"] / n
            out[label] = {
                "n":             n,
                "won":           b["won"],
                "lost":          n - b["won"],
                "hit_rate":      round(hit_rate * 100, 2),
                "avg_lock":      round(b["lock_sum"]   / n, 2),
                "avg_shadow":    round(b["shadow_sum"] / n, 2),
                "brier_live":    round(b["brier_live"]   / n, 4),
                "brier_shadow":  round(b["brier_shadow"] / n, 4),
                "delta_hit_pp":  round((hit_rate - base_hit) * 100, 2),
            }
        else:
            out[label] = {
                "n":             0,
                "won":           0,
                "lost":          0,
                "hit_rate":      None,
                "avg_lock":      None,
                "avg_shadow":    None,
                "brier_live":    None,
                "brier_shadow":  None,
                "delta_hit_pp":  None,
            }

    # Decision flag — should the lift be promoted from shadow → live?
    hot   = out["HOT"]
    cold  = out["COLD"]
    promote_ready = bool(
        hot.get("n", 0) >= 30 and cold.get("n", 0) >= 30
        and (hot.get("delta_hit_pp") or 0)  >= 5.0
        and (cold.get("delta_hit_pp") or 0) <= -5.0
    )

    return {
        "buckets":       out,
        "promote_ready": promote_ready,
        "promotion_rule": (
            "HOT.delta ≥ +5pp AND COLD.delta ≤ −5pp AND n ≥ 30 in both"
        ),
        "shadow_mode":   True,
        "generated_at":  datetime.now(timezone.utc).isoformat(),
    }


@router.post("/analytics/buckets/recompute")
async def analytics_buckets_recompute(user: Annotated[UserPublic, Depends(current_user)]):
    """Force re-scan settled picks and rebuild all isolated learning
    buckets. Snapshots the previous state for rollback (keeps last 5).
    Analytics-only."""
    from learning_buckets import recompute_buckets
    return await recompute_buckets(db)


@router.post("/analytics/buckets/rollback")
async def analytics_buckets_rollback(
    user: Annotated[UserPublic, Depends(current_user)],
    snapshot_index: int = 1,
):
    """Restore the Nth-most-recent bucket snapshot. snapshot_index=1 =
    the previous version, =2 = two versions ago. Analytics-only."""
    from learning_buckets import rollback_buckets
    return await rollback_buckets(
        db, snapshot_index=max(1, min(5, int(snapshot_index or 1))),
    )


@router.post("/analytics/learn")
async def learn_now(user: Annotated[UserPublic, Depends(current_user)]):
    """Force a recompute of learned weights and re-apply to today's
    pending picks."""
    from learning_engine import recompute_learned_weights, apply_learning
    weights = await recompute_learned_weights(db)
    # Apply to all picks generated for today that haven't been settled.
    cursor = db.picks.find(
        {"pick_date": today_str(), "status": {"$in": [None, "pending"]}},
        {"_id": 0},
    )
    adjusted = 0
    async for p in cursor:
        before = p.get("win_probability")
        await apply_learning(db, p)
        if p.get("learning") and p.get("win_probability") != before:
            adjusted += 1
            await db.picks.update_one(
                {"id": p["id"]},
                {"$set": {
                    "win_probability": p["win_probability"],
                    "lock_score": p.get("lock_score"),
                    "edge_percent": p.get("edge_percent"),
                    "implied_probability": p.get("implied_probability"),
                    "learning": p.get("learning"),
                }},
            )
    return {
        "active_buckets": sum(1 for b in weights.get("buckets", []) if b.get("active")),
        "picks_adjusted": adjusted,
        "sample_size": weights.get("sample_size", 0),
    }



# ── Phase 0.3 — CLV dashboard ────────────────────────────────────────
@router.get("/analytics/clv")
async def clv_report(
    user: Annotated[UserPublic, Depends(current_user)],
    days: int = 30,
):
    """CLV (Closing Line Value) breakdown for the user's picks over the
    last `days`. Buckets picks by odds band and returns per-band win %,
    flat-stake ROI, average CLV (in implied-probability points), and
    what % of picks BEAT the closing line.

    Positive CLV = you got a price better than the market closed at →
    sharp behaviour. The gold standard is Beat-Close %: sharp bettors
    consistently sit above 55%, retail sits at ~50%, losing bettors
    sit at ~45%.

    Response shape:
        {
          "since": "2026-06-14T00:00:00+00:00",
          "days": 30,
          "overall": {
            "n": 1234, "won": 780, "win_pct": 63.2,
            "roi_per_100u": -3.1, "avg_clv_pp": -0.8, "beat_close_pct": 47.4
          },
          "bands": [
            {"label": "Heavy fav (<-200)", "n": ..., "win_pct": ...,
             "roi_per_100u": ..., "avg_clv_pp": ..., "beat_close_pct": ...},
            ...
          ]
        }
    """
    from datetime import timedelta
    from analytics import (
        american_profit_per_unit,
        american_to_implied_pct,
        clv_units,
    )
    since = datetime.now(timezone.utc) - timedelta(days=max(days, 1))
    since_iso = since.isoformat()

    q = {
        "status": {"$in": ["won", "lost", "push"]},
        "event_time": {"$gte": since_iso},
        "book_odds": {"$exists": True, "$ne": None},
    }

    bands: list[tuple[str, dict]] = [
        ("Heavy fav (<-200)",        {"book_odds": {"$lt": -200}}),
        ("Fav (-200 to -110)",       {"book_odds": {"$gte": -200, "$lte": -110}}),
        ("Coin flip (-110 to +110)", {"book_odds": {"$gt": -110, "$lt": 110}}),
        ("Plus (+110 to +200)",      {"book_odds": {"$gte": 110, "$lte": 200}}),
        ("Big dog (+200 to +500)",   {"book_odds": {"$gt": 200, "$lte": 500}}),
        ("Long shot (+500+)",        {"book_odds": {"$gt": 500}}),
    ]

    def _bucket() -> dict:
        return {"n": 0, "won": 0, "profit_units": 0.0,
                "clv_sum": 0.0, "clv_count": 0, "beat_close": 0}

    def _finalize(b: dict, label: str | None = None) -> dict:
        n = b["n"]
        won = b["won"]
        clv_n = b["clv_count"]
        return {
            "label": label,
            "n": n,
            "won": won,
            "win_pct": round(won * 100 / n, 2) if n else 0.0,
            "roi_per_100u": round(b["profit_units"] * 100 / n, 2) if n else 0.0,
            "avg_clv_pp": round(b["clv_sum"] / clv_n, 3) if clv_n else None,
            "beat_close_pct": round(b["beat_close"] * 100 / clv_n, 2) if clv_n else None,
        }

    overall = _bucket()
    band_buckets: list[tuple[str, dict, dict]] = [(lbl, cond, _bucket())
                                                    for lbl, cond in bands]

    cursor = db.picks.find(q, {
        "book_odds": 1, "closing_odds": 1, "odds_at_pick": 1, "status": 1,
        "closing_odds_source": 1, "sharp_closing_odds": 1,
    })
    real_snap_n = 0
    sharp_snap_n = 0
    async for p in cursor:
        odds = p.get("book_odds")
        if not odds:
            continue
        status = p.get("status") or ""
        profit = american_profit_per_unit(odds, status)
        # Only count Beat-Close % against REAL closing snapshots. Fallback
        # snapshots (source=fallback_book_odds) have closing_odds ==
        # book_odds by construction → clv == 0 → they'd dilute the
        # Beat-Close % toward zero and hide the real signal.
        close_source = p.get("closing_odds_source") or ""
        real_snap = close_source == "odds_api_live"
        if real_snap:
            real_snap_n += 1
        if p.get("sharp_closing_odds"):
            sharp_snap_n += 1
        clv = None
        odds_at_pick = p.get("odds_at_pick") or odds
        closing = p.get("closing_odds")
        if real_snap and closing and odds_at_pick:
            clv = clv_units(odds_at_pick, closing)
        # Overall
        overall["n"] += 1
        overall["won"] += 1 if status == "won" else 0
        overall["profit_units"] += profit
        if clv is not None:
            overall["clv_sum"] += clv
            overall["clv_count"] += 1
            overall["beat_close"] += 1 if clv > 0 else 0
        # Bands
        o = float(odds)
        for lbl, cond, bucket in band_buckets:
            match = True
            for _, op_dict in cond.items():
                for op, val in op_dict.items():
                    if op == "$lt" and not (o < val):
                        match = False; break
                    if op == "$lte" and not (o <= val):
                        match = False; break
                    if op == "$gt" and not (o > val):
                        match = False; break
                    if op == "$gte" and not (o >= val):
                        match = False; break
                if not match:
                    break
            if match:
                bucket["n"] += 1
                bucket["won"] += 1 if status == "won" else 0
                bucket["profit_units"] += profit
                if clv is not None:
                    bucket["clv_sum"] += clv
                    bucket["clv_count"] += 1
                    bucket["beat_close"] += 1 if clv > 0 else 0
                break

    return {
        "since": since_iso,
        "days": days,
        "overall": _finalize(overall, "Overall"),
        "bands": [_finalize(b, lbl) for lbl, _, b in band_buckets],
        "snapshot_coverage": {
            "real_close_snapshots": real_snap_n,
            "sharp_book_snapshots": sharp_snap_n,
            "note": (
                "Only picks with a REAL closing snapshot "
                "(closing_odds_source='odds_api_live') are used for CLV "
                "and Beat-Close %. Player-prop markets aren't currently "
                "snapshotted via the Odds API events endpoint — that's "
                "why coverage is low. See closing_line_snapshotter."
            ),
        },
        "notes": (
            "Beat-Close % = the share of picks graded against a real "
            "closing snapshot where the closing line moved AWAY from "
            "your pick (i.e. you got a better price than the market "
            "closed at). Sharp bettors consistently sit >55%."
        ),
    }
