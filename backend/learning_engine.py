"""Self-tuning learning engine.

Uses the model performance dataset (every settled pick + simulated 1u ROI)
to bias future picks toward markets the model has actually been profitable in.

Two complementary signals:

  1. **Bucket weight** — per-(sport, market_label). Combines ROI and
     calibration error into a single win-probability delta in [-0.08, +0.08].
     Computed only when ≥ MIN_SAMPLES picks exist in the bucket so a hot or
     cold streak doesn't whipsaw the model.

  2. **Calibration correction** — per Lock-score band. Pulls future
     win-probabilities toward the actual historical hit-rate of that band
     (so if Lock-90 picks really hit 87%, fresh 90-band picks lose ~3%).

Both are persisted to `db.learned_weights` (singleton doc, _id="current")
and refreshed at the end of every settlement run — no extra API calls.

`apply_learning(db, pick)` is the integration point — call it inside the
pick-refresh loop AFTER all other enrichment so the learned bias sits on
top of model + SportDB + Odds-API edge.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from analytics import _market_label, confidence_bucket  # type: ignore

logger = logging.getLogger("lockscore.learning")

# Safeguards
MIN_SAMPLES = 3            # ≥ N picks needed before a bucket gets ANY weight
                           # (was 10 binary gate — now combined with shrinkage)
SHRINKAGE_K = 10           # Bayesian prior strength: at n=K, weight is 50% applied
MAX_WP_DELTA = 0.08        # cap learned win_prob adjustment at ±8 percentage points
MAX_CAL_DELTA = 0.06       # cap calibration correction at ±6 pp
ROI_GAIN = 0.4             # how much of bucket ROI/100 maps to win_prob delta
CAL_GAIN = 0.6             # how much of (actual − expected)/100 maps to delta
HALF_LIFE_DAYS = 30        # time-decay half-life: a 30-day-old result counts ~50%
                           # of a fresh one. Lets the model forget stale form fast.


async def recompute_learned_weights(db) -> dict[str, Any]:
    """Recompute and persist all learning signals from settled picks.

    Phase-1+2 upgrade (2026-06-22): adds Bayesian shrinkage + time-decay.
      - Shrinkage: weight scales with n/(n+K) so small-sample buckets
        (n<10) get a proportionally smaller learned bias instead of
        being dropped entirely by a binary MIN_SAMPLES gate.
      - Time-decay: each pick's contribution is multiplied by
        exp(-age_days / HALF_LIFE_DAYS). A 30-day-old win now counts
        ~50% of a fresh one, so the model adapts to recent form.

    Returns the same payload that `/api/analytics/learned-weights` serves so
    the caller can log / inspect.
    """
    import math
    cursor = db.picks.find(
        {"status": {"$in": ["won", "lost", "push"]}},
        {"_id": 0, "sport": 1, "market": 1, "status": 1, "lock_score": 1,
         "win_probability": 1, "edge_percent": 1, "units_profit": 1,
         "units_risked": 1, "confidence_bucket": 1,
         "event_time": 1, "settled_at": 1, "clv_value": 1},
    )
    picks = await cursor.to_list(length=20_000)
    now_iso = datetime.now(timezone.utc).isoformat()
    now_utc = datetime.now(timezone.utc)

    if not picks:
        empty = {"_id": "current", "buckets": [], "calibration": [],
                 "updated_at": now_iso, "sample_size": 0}
        await db.learned_weights.replace_one({"_id": "current"}, empty, upsert=True)
        return empty

    def _age_weight(p: dict) -> float:
        """Exponential time-decay: w = exp(-age_days / HALF_LIFE_DAYS).
        Falls back to 1.0 if event_time/settled_at can't be parsed."""
        ts = p.get("settled_at") or p.get("event_time") or ""
        if not ts:
            return 1.0
        try:
            iso = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age_days = max(0.0, (now_utc - dt).total_seconds() / 86400.0)
            return math.exp(-age_days / HALF_LIFE_DAYS)
        except Exception:
            return 1.0

    # ── 1) Per-(sport, market_label) bucket weight ──────────────────────
    buckets: dict[tuple[str, str], dict] = {}
    for p in picks:
        key = (p.get("sport") or "Unknown", _market_label(p.get("market")))
        b = buckets.setdefault(key, {
            "sport": key[0], "market_label": key[1],
            "n": 0, "wins": 0, "losses": 0, "pushes": 0,
            "units_risked": 0.0, "units_profit": 0.0,
            "model_wp_sum": 0.0,
            "decayed_n": 0.0, "decayed_wins": 0.0, "decayed_risked": 0.0,
            "decayed_profit": 0.0, "decayed_wp_sum": 0.0,
        })
        w = _age_weight(p)
        b["n"] += 1
        b["decayed_n"] += w
        if p["status"] == "won":
            b["wins"] += 1
            b["decayed_wins"] += w
        elif p["status"] == "lost":
            b["losses"] += 1
        else:
            b["pushes"] += 1
        if p["status"] != "push":
            risked = p.get("units_risked", 1.0)
            b["units_risked"] += risked
            b["decayed_risked"] += risked * w
        profit = p.get("units_profit") or 0.0
        b["units_profit"] += profit
        b["decayed_profit"] += profit * w
        wp = p.get("win_probability") or 0.0
        b["model_wp_sum"] += wp
        b["decayed_wp_sum"] += wp * w

    rows: list[dict] = []
    for b in buckets.values():
        decisive = b["wins"] + b["losses"]
        hit_rate = (b["wins"] * 100 / decisive) if decisive else 0.0
        expected = (b["model_wp_sum"] / b["n"]) if b["n"] else 0.0
        roi = (b["units_profit"] * 100 / b["units_risked"]) if b["units_risked"] else 0.0
        # Time-decayed metrics for the learnable weight
        d_roi = (b["decayed_profit"] * 100 / b["decayed_risked"]) if b["decayed_risked"] else 0.0
        d_expected = (b["decayed_wp_sum"] / b["decayed_n"]) if b["decayed_n"] else 0.0
        d_hit_rate = (b["decayed_wins"] * 100 / b["decayed_n"]) if b["decayed_n"] else 0.0
        # ── Bayesian shrinkage ──────────────────────────────────────
        # At n=K (=10), shrinkage = 0.5; at n=30, shrinkage = 0.75; at
        # n=100, shrinkage ≈ 0.91. Replaces the old binary gate so we
        # extract SOME signal from low-sample buckets while still
        # capping over-confidence on tiny samples.
        if b["n"] >= MIN_SAMPLES:
            shrinkage = b["n"] / (b["n"] + SHRINKAGE_K)
            roi_signal = (d_roi / 100.0) * ROI_GAIN
            cal_signal = ((d_hit_rate - d_expected) / 100.0) * CAL_GAIN
            raw = (roi_signal + cal_signal) * shrinkage
            weight = max(-MAX_WP_DELTA, min(MAX_WP_DELTA, raw))
        else:
            weight = 0.0
        rows.append({
            "sport": b["sport"],
            "market_label": b["market_label"],
            "n": b["n"],
            "wins": b["wins"],
            "losses": b["losses"],
            "hit_rate": round(hit_rate, 1),
            "expected_wp": round(expected, 1),
            "roi": round(roi, 2),
            "decayed_roi": round(d_roi, 2),
            "decayed_hit_rate": round(d_hit_rate, 1),
            "shrinkage": round(b["n"] / (b["n"] + SHRINKAGE_K), 3) if b["n"] else 0.0,
            "weight": round(weight, 4),
            "active": b["n"] >= MIN_SAMPLES,
        })
    rows.sort(key=lambda r: r["decayed_roi"], reverse=True)

    # ── 2) Win-Probability calibration (NOT lock-score bands) ───────────
    # Per spec v3: Lock Score is bet-quality, NOT win-probability. So
    # calibration must compare the model's Expected Win % to Actual Win %,
    # binned by WP range — not by lock-score band.
    wp_bins = [(50, 60), (60, 70), (70, 80), (80, 90), (90, 100)]
    bin_labels = ["WP 50-60%", "WP 60-70%", "WP 70-80%", "WP 80-90%", "WP 90-100%"]
    bands: dict[str, dict] = {}
    for label in bin_labels:
        bands[label] = {"band": label, "n": 0, "wins": 0, "losses": 0, "wp_sum": 0.0}
    for p in picks:
        if p["status"] == "push":
            continue
        if (p.get("formula_v") or 1) < 2:
            continue
        wp = p.get("win_probability") or 0
        idx = None
        for i, (lo, hi) in enumerate(wp_bins):
            if lo <= wp < hi or (i == len(wp_bins) - 1 and wp >= hi - 10):
                idx = i
                break
        if idx is None:
            continue
        bk = bin_labels[idx]
        bd = bands[bk]
        bd["n"] += 1
        bd["wp_sum"] += wp
        if p["status"] == "won":
            bd["wins"] += 1
        elif p["status"] == "lost":
            bd["losses"] += 1

    calibration: list[dict] = []
    for label in bin_labels:
        bd = bands[label]
        decisive = bd["wins"] + bd["losses"]
        actual = (bd["wins"] * 100 / decisive) if decisive else 0.0
        expected = bd["wp_sum"] / bd["n"] if bd["n"] else 0.0
        delta_pp = actual - expected
        if bd["n"] >= MIN_SAMPLES:
            adj = max(-MAX_CAL_DELTA, min(MAX_CAL_DELTA, delta_pp / 100.0))
        else:
            adj = 0.0
        calibration.append({
            "band": label,
            "n": bd["n"],
            "actual": round(actual, 1),
            "expected": round(expected, 1),
            "delta": round(delta_pp, 2),
            "adjustment": round(adj, 4),
            "active": bd["n"] >= MIN_SAMPLES,
        })

    payload = {
        "_id": "current",
        "buckets": rows,
        "calibration": calibration,
        "updated_at": now_iso,
        "sample_size": len(picks),
        "settings": {
            "min_samples": MIN_SAMPLES,
            "max_wp_delta": MAX_WP_DELTA,
            "max_cal_delta": MAX_CAL_DELTA,
        },
    }
    await db.learned_weights.replace_one({"_id": "current"}, payload, upsert=True)
    active = sum(1 for r in rows if r["active"])
    logger.info("Learning engine recomputed: %d buckets (%d active) over %d picks",
                len(rows), active, len(picks))
    return payload


async def _load_weights(db) -> Optional[dict]:
    return await db.learned_weights.find_one({"_id": "current"}, {"_id": 0})


async def apply_learning(db, pick: dict) -> dict:
    """Adjust `win_probability` / `lock_score` of a fresh pick using learned
    weights. Mutates and returns the pick. Best-effort — if weights aren't
    available yet we silently no-op."""
    weights = await _load_weights(db)
    if not weights:
        return pick

    sport = pick.get("sport") or ""
    label = _market_label(pick.get("market"))

    # Bucket weight
    bucket_w = 0.0
    bucket_row = None
    for r in weights.get("buckets", []):
        if r.get("active") and r.get("sport") == sport and r.get("market_label") == label:
            bucket_w = r.get("weight") or 0.0
            bucket_row = r
            break

    # Calibration adjustment (depends on current pick's lock band)
    cal_adj = 0.0
    band = confidence_bucket(pick.get("lock_score"))
    for c in weights.get("calibration", []):
        if c.get("active") and c.get("band") == band:
            cal_adj = c.get("adjustment") or 0.0
            break

    # IDEMPOTENT — store the original model WP the first time, then always
    # recompute FROM the original. Without this the learning delta stacks
    # every time the function runs (refresh → weekly tune → manual relearn).
    if "model_win_probability" not in pick:
        pick["model_win_probability"] = pick.get("win_probability") or 0
    baseline = pick["model_win_probability"]

    # ── Hit-rate flooring for high-sample buckets ────────────────────────
    # If a market historically hits at rate R% over a meaningful sample, and
    # the book is pricing the current pick BELOW R%, the model should claim
    # at least R% — otherwise we're leaving real edge on the table.
    # Concrete: MLB Hits bucket hits 73.7% over 57 picks. A -150 player priced
    # at 60% implied should produce a +13.7pp edge, not +5pp.
    hit_rate_floor = 0.0
    if bucket_row and bucket_row.get("n", 0) >= 20:
        hr = bucket_row.get("hit_rate") or 0.0
        # Use HR as a soft floor — cap the lift at ±10pp from baseline so a
        # single hot/cold bucket can't crater the prediction.
        hit_rate_floor = max(baseline, min(baseline + 10, hr))

    total_delta = bucket_w + cal_adj
    new_wp = max(1.0, min(99.0, baseline + total_delta * 100))
    # Apply the hit-rate floor LAST so it can lift but never lowers WP.
    if hit_rate_floor > new_wp:
        new_wp = hit_rate_floor
    new_wp = round(new_wp, 1)

    if abs(new_wp - (pick.get("win_probability") or 0)) < 0.5 and abs(total_delta) < 0.002 and hit_rate_floor <= baseline:
        return pick

    pick["win_probability"] = new_wp

    # Recompute edge + implied to stay consistent.
    from analytics import american_to_implied_pct
    book = pick.get("book_odds")
    if book:
        implied = american_to_implied_pct(book)
        pick["implied_probability"] = round(implied, 1)
        pick["edge_percent"] = round(new_wp - implied, 2)

    # Recompute lock_score with the corrected win_prob, and KEEP the
    # grade + confidence fields in lock-step so any consumer that
    # forgets to call the validator afterwards still sees a coherent
    # pick (iter17/18 found: weekly tuning loop omitted grade sync →
    # 68 stale "Pass" badges per cycle).
    try:
        from sports_engine import compute_lock_score, _grade, _confidence
        factors_pct = pick.get("factors") or {}
        factors = {k: v / 100.0 for k, v in factors_pct.items()}
        lock, _ = compute_lock_score(factors, win_prob=new_wp, pick=pick)
        pick["lock_score"] = lock
        pick["grade"] = _grade(lock)
        pick["confidence"] = _confidence(lock)
    except Exception:
        pass

    pick["learning"] = {
        "bucket_weight": round(bucket_w, 4),
        "calibration_adj": round(cal_adj, 4),
        "hit_rate_floor": round(hit_rate_floor, 2) if hit_rate_floor > baseline else None,
        "total_delta_pp": round(new_wp - baseline, 2),
        "matched_bucket": f"{sport} · {label}" if bucket_row else None,
        "matched_band": band if cal_adj else None,
    }
    return pick
