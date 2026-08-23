"""Lock-Score Calibration Engine.

Purpose
-------
The raw `lock_score` produced by the existing scoring engine is an
*uncalibrated* model probability proxy. Empirically the high bands
over-promise: Elite (95+) shows lock-score ≈ 98 but settles ~64% real.

This module fits an **isotonic regression** curve mapping
  raw_lock_score (0-99)  →  calibrated_probability (0-1)
using historical settled picks (won=1, lost=0, push excluded), and
then composes a `display_lock_score` via the user-specified blend
formula:

  display = 0.40·calibrated  + 0.25·market_edge   + 0.15·consensus
          + 0.10·sample_strength + 0.10·data_quality

Hard constraints from the spec
------------------------------
1. Keep the 0-99 scale and existing badge thresholds.
2. The number 99 must remain reachable for the truly elite ~top 1-2%
   of historical picks. Implementation: 99 is only allowed when the
   raw score is at or above the 98th-percentile of the historical
   distribution AND the blended display lands at 99.
3. Do NOT downgrade ALREADY-SETTLED picks (would rewrite history /
   confuse the analytics page). Calibration is applied only to
   *pending* picks at serialisation time.
4. Auto-recalibrate every 100 newly-settled picks so the curve stays
   current as the model evolves.

Isotonic regression — Pool Adjacent Violators
---------------------------------------------
Implemented inline (no sklearn dependency). Given (x_i, y_i) sorted
by x, PAV merges adjacent points until y is non-decreasing in x. The
resulting step function is the unique monotone non-decreasing curve
minimising squared error against the y's. We then linearly
interpolate between knots for smooth lookup.

Persistence
-----------
The fitted curve and metadata (sample size, last-fit timestamp,
percentiles, band stats) are stored in `db.lock_calibration_curve`
(single document, _id="curve"). Reloaded on every backend boot.
"""

from __future__ import annotations

import bisect
import logging
import math
import datetime as _dt
from typing import Optional

logger = logging.getLogger("lockscore.calibration")

# ──────────────────────────────────────────────────────────────────────────
# Tunables
# ──────────────────────────────────────────────────────────────────────────
MIN_FIT_SAMPLES = 50            # below this we use a static identity-like fallback
RECALIBRATE_EVERY = 100         # settled picks
TOP_PERCENTILE_FOR_99 = 0.98    # only raw scores at/above this percentile can reach display 99
COLLECTION = "lock_calibration_curve"
CURVE_DOC_ID = "curve"


# ──────────────────────────────────────────────────────────────────────────
# In-memory curve singleton (re-hydrated on boot via load_curve())
# ──────────────────────────────────────────────────────────────────────────

class _Curve:
    """Mutable in-memory state shared across requests."""
    def __init__(self) -> None:
        self.knots_x: list[float] = []       # raw scores (sorted, asc)
        self.knots_y: list[float] = []       # calibrated prob 0..1, monotone non-decreasing
        self.percentiles: list[float] = []   # sorted historical raw scores for percentile rank lookup
        self.fit_sample_size: int = 0
        self.last_fit_at: Optional[str] = None
        self.band_stats: list[dict] = []     # for /api/analytics/calibration

    def has_curve(self) -> bool:
        return len(self.knots_x) >= 2

    def transform(self, raw_score: float) -> float:
        """Map raw 0-99 → calibrated probability 0-1.

        With <50 historical samples, falls back to raw/100 (identity)
        so the system behaves like today until enough data accrues.

        Sample-size shrinkage (added 2026-06-23 — user bug "Why would
        this pick be considered a pass when everything looks good"):
        with only 434 settled picks and 52 Elite-band samples, the raw
        isotonic curve is over-confident and crushes legitimate strong
        picks (e.g. Bieber Over 2.5 K's at -650, win_prob 82%, edge
        -4.5%) from raw lock 90+ down to display 58. We shrink the
        isotonic estimate toward the raw probability based on sample
        size — at 500 samples we trust ~10% calibration, at 5000 we
        trust it fully. This way the calibration overlay sharpens
        gradually as data accumulates instead of slamming the dial.
        """
        if not self.has_curve():
            return max(0.0, min(1.0, float(raw_score) / 100.0))
        x = float(raw_score)
        if x <= self.knots_x[0]:
            iso = self.knots_y[0]
        elif x >= self.knots_x[-1]:
            iso = self.knots_y[-1]
        else:
            i = bisect.bisect_left(self.knots_x, x)
            x0, x1 = self.knots_x[i - 1], self.knots_x[i]
            y0, y1 = self.knots_y[i - 1], self.knots_y[i]
            iso = y0 if x1 == x0 else y0 + (y1 - y0) * (x - x0) / (x1 - x0)
        # Shrinkage: blend isotonic toward raw_prob proportional to confidence
        raw_prob = max(0.0, min(1.0, x / 100.0))
        w_iso = min(1.0, self.fit_sample_size / 5000.0)  # 0..1 weight on calibration
        return raw_prob * (1.0 - w_iso) + iso * w_iso

    def percentile_of(self, raw_score: float) -> float:
        """Return the historical CDF value (0..1) of raw_score.
        Used to gate the display=99 ceiling to top 1-2%."""
        if not self.percentiles:
            return float(raw_score) / 100.0
        idx = bisect.bisect_right(self.percentiles, float(raw_score))
        return idx / len(self.percentiles)


_curve = _Curve()


def get_curve() -> _Curve:
    return _curve


# ──────────────────────────────────────────────────────────────────────────
# Isotonic regression — Pool Adjacent Violators
# ──────────────────────────────────────────────────────────────────────────

def _pool_adjacent_violators(xs: list[float], ys: list[float]) -> tuple[list[float], list[float]]:
    """Returns (knots_x, knots_y) — monotone non-decreasing step function.

    xs MUST be sorted ascending. Equal xs get pre-averaged (else PAV
    can produce duplicate knots that confuse the interpolator).
    """
    if not xs:
        return [], []
    # Pre-average ties
    merged_x: list[float] = []
    merged_y: list[float] = []
    merged_w: list[float] = []   # weight = count of original points
    i = 0
    n = len(xs)
    while i < n:
        j = i
        s_y = 0.0
        c = 0
        while j < n and xs[j] == xs[i]:
            s_y += ys[j]
            c += 1
            j += 1
        merged_x.append(xs[i])
        merged_y.append(s_y / c)
        merged_w.append(float(c))
        i = j

    # Now apply PAV on (merged_x, merged_y, merged_w)
    out_x: list[float] = []
    out_y: list[float] = []
    out_w: list[float] = []
    for k in range(len(merged_x)):
        out_x.append(merged_x[k])
        out_y.append(merged_y[k])
        out_w.append(merged_w[k])
        # While the last block violates monotonicity with the previous, merge
        while len(out_y) >= 2 and out_y[-2] > out_y[-1]:
            # weighted average merge
            w1, w2 = out_w[-2], out_w[-1]
            y_merged = (out_y[-2] * w1 + out_y[-1] * w2) / (w1 + w2)
            # Keep the right-edge x as the block's x (so the step "covers" up to this score)
            x_merged_right = out_x[-1]
            out_y.pop(); out_w.pop(); out_x.pop()
            out_y[-1] = y_merged
            out_w[-1] = w1 + w2
            out_x[-1] = x_merged_right
    return out_x, out_y


# ──────────────────────────────────────────────────────────────────────────
# Fit / load / save
# ──────────────────────────────────────────────────────────────────────────

async def fit_from_db(db) -> dict:
    """Refits the curve from all settled, decisive (won/lost) picks.

    Returns a summary dict. Safe to call repeatedly — overwrites the
    persisted curve atomically.
    """
    cursor = db.picks.find(
        {"status": {"$in": ["won", "lost"]}, "lock_score": {"$gt": 0}},
        {"_id": 0, "lock_score": 1, "status": 1},
    )
    pairs: list[tuple[float, float]] = []
    async for p in cursor:
        try:
            x = float(p.get("lock_score") or 0)
            y = 1.0 if p.get("status") == "won" else 0.0
            if x > 0:
                pairs.append((x, y))
        except (TypeError, ValueError):
            continue
    n = len(pairs)
    if n < MIN_FIT_SAMPLES:
        logger.info(
            "Calibration: only %d settled picks (<%d) — keeping identity fallback",
            n, MIN_FIT_SAMPLES,
        )
        return {"fit": False, "samples": n}

    pairs.sort(key=lambda t: t[0])
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    # ── 2026-08-23 FINAL SURGICAL — event-loop offload ──
    # ``_pool_adjacent_violators`` (isotonic regression PAVA) and
    # ``_compute_band_stats`` are pure-CPU numeric passes; on large
    # settled-picks samples they can hog the single asyncio thread
    # long enough to freeze inbound HTTP.  Push those two sync
    # calculations onto a worker thread (asyncio.to_thread) while
    # keeping the async DB read/write boundaries unchanged.  No math
    # change, no cadence change.
    import asyncio as _asyncio
    knots_x, knots_y = await _asyncio.to_thread(
        _pool_adjacent_violators, xs, ys,
    )

    # Light smoothing: clamp y in [0.02, 0.98] so display can never collapse
    # to extreme 0/1 — keeps tiny samples (e.g. 1 win out of 1) from
    # producing a 100% calibrated probability that's clearly over-trusting.
    knots_y = [max(0.02, min(0.98, y)) for y in knots_y]

    _curve.knots_x = knots_x
    _curve.knots_y = knots_y
    _curve.percentiles = xs[:]   # already sorted
    _curve.fit_sample_size = n
    _curve.last_fit_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
    _curve.band_stats = await _asyncio.to_thread(_compute_band_stats, pairs)

    # Persist
    try:
        await db[COLLECTION].update_one(
            {"_id": CURVE_DOC_ID},
            {"$set": {
                "knots_x": knots_x,
                "knots_y": knots_y,
                "percentiles": xs,
                "fit_sample_size": n,
                "last_fit_at": _curve.last_fit_at,
                "band_stats": _curve.band_stats,
            }},
            upsert=True,
        )
    except Exception as e:
        logger.warning("Calibration persist failed: %s", e)
    logger.info(
        "Calibration refit: %d samples, %d knots, fit_at=%s",
        n, len(knots_x), _curve.last_fit_at,
    )
    return {
        "fit": True,
        "samples": n,
        "knots": len(knots_x),
        "fit_at": _curve.last_fit_at,
        "band_stats": _curve.band_stats,
    }


async def load_curve(db) -> None:
    """Re-hydrate the in-memory curve from Mongo on backend boot."""
    try:
        doc = await db[COLLECTION].find_one({"_id": CURVE_DOC_ID})
    except Exception as e:
        logger.warning("Calibration load failed: %s — using identity fallback", e)
        return
    if not doc:
        # First boot — fit from scratch
        try:
            await fit_from_db(db)
        except Exception as e:
            logger.warning("Initial calibration fit failed: %s", e)
        return
    _curve.knots_x = list(doc.get("knots_x") or [])
    _curve.knots_y = list(doc.get("knots_y") or [])
    _curve.percentiles = list(doc.get("percentiles") or [])
    _curve.fit_sample_size = int(doc.get("fit_sample_size") or 0)
    _curve.last_fit_at = doc.get("last_fit_at")
    _curve.band_stats = list(doc.get("band_stats") or [])
    logger.info(
        "Calibration loaded: %d samples, %d knots, fit_at=%s",
        _curve.fit_sample_size, len(_curve.knots_x), _curve.last_fit_at,
    )


# ──────────────────────────────────────────────────────────────────────────
# Auto-recalibration trigger (every RECALIBRATE_EVERY settled picks)
# ──────────────────────────────────────────────────────────────────────────

_last_recalibrate_count = 0  # module-level baseline; recalibrate when (current - baseline) >= RECALIBRATE_EVERY


async def maybe_recalibrate(db) -> Optional[dict]:
    """Called periodically (e.g. from the settlement loop). Refits the
    curve once we've accumulated `RECALIBRATE_EVERY` more settled picks
    since the last fit. Returns the fit summary if refit happened, else None."""
    global _last_recalibrate_count
    try:
        n_settled = await db.picks.count_documents({"status": {"$in": ["won", "lost"]}})
    except Exception:
        return None
    if not _curve.has_curve():
        # Curve hasn't been built yet — try to build whenever we have enough data.
        if n_settled >= MIN_FIT_SAMPLES:
            summary = await fit_from_db(db)
            _last_recalibrate_count = n_settled
            return summary
        return None
    if n_settled - _curve.fit_sample_size >= RECALIBRATE_EVERY:
        summary = await fit_from_db(db)
        _last_recalibrate_count = n_settled
        return summary
    return None


# ──────────────────────────────────────────────────────────────────────────
# Display lock score blend
# ──────────────────────────────────────────────────────────────────────────

def compute_display_lock_score(pick: dict) -> Optional[float]:
    """Compute the 5-component blended display lock score.

    Returns a float in [0, 99] or None if we should leave the pick's
    existing lock_score untouched (e.g. malformed pick).

    Components (all rescaled to 0..100 before weighting):
      * 40% calibrated_probability   — isotonic-regressed raw lock_score
      * 25% market_edge              — model edge vs the book
      * 15% model_consensus          — agreement across numeric factors
      * 10% sample_strength          — historical bucket sample / bandit
      * 10% data_quality             — completeness of evidence inputs

    99 ceiling: only reachable when the raw lock_score is at the
    `TOP_PERCENTILE_FOR_99` percentile of historical raw scores. All
    other picks are capped at 98 (so 99 remains a meaningful badge).
    """
    try:
        raw = float(pick.get("lock_score") or 0)
    except (TypeError, ValueError):
        return None
    if raw <= 0:
        return None

    # 1. Calibrated probability (40%)
    calib_prob = _curve.transform(raw)        # 0..1
    calib_component = calib_prob * 100.0      # 0..100

    # 2. Market edge (25%)
    try:
        edge = float(pick.get("edge_percent") or 0.0)
    except (TypeError, ValueError):
        edge = 0.0
    # Soft slope so a -4.5% edge on a chalk pick (e.g. Bieber Over 2.5
    # K's at -650) doesn't catastrophically erase the score. With the
    # old +/- 4.5 slope a -4.5% edge produced a 30/100 component; the
    # new +/- 2.0 slope produces a 41/100 floor (and 0% edge stays at
    # 50). Win probability is captured via the calibration component
    # so we don't need edge to do double duty.
    edge_component = max(20.0, min(100.0, 50.0 + edge * 2.0))

    # 3. Model consensus (15%) — low variance across numeric factors = high consensus
    consensus_component = 60.0   # default neutral when no numeric factors available
    factors = pick.get("factors") or {}
    nums: list[float] = []
    if isinstance(factors, dict):
        for v in factors.values():
            try:
                nums.append(float(v))
            except (TypeError, ValueError):
                continue
    if len(nums) >= 2:
        mean = sum(nums) / len(nums)
        var = sum((v - mean) ** 2 for v in nums) / len(nums)
        std = math.sqrt(var)
        # 0 std → 100, 25 std → 50, 50+ std → 0
        consensus_component = max(0.0, min(100.0, 100.0 - std * 2.0))

    # 4. Sample strength (10%)
    bucket_n = float(pick.get("bucket_sample_size")
                     or pick.get("bucket_n")
                     or (pick.get("bucket_meta") or {}).get("n")
                     or 0)
    # 0 → 0, 50 → 100, capped
    sample_component = max(0.0, min(100.0, bucket_n * 2.0))
    if sample_component < 30:
        # Tiny bonus for bandit-validated arms even when bucket_n itself is small
        try:
            blift = float(pick.get("bandit_lift") or 0)
            if blift > 0:
                sample_component = max(sample_component, 40.0 + min(20.0, blift))
        except (TypeError, ValueError):
            pass

    # 5. Data quality (10%) — presence of evidence layers
    quality = 40.0
    if isinstance(factors, dict) and len(factors) >= 3:
        quality += 20.0
    if isinstance(factors, dict) and len(factors) >= 5:
        quality += 10.0
    if pick.get("player_intel") or pick.get("learning_v2_active") or pick.get("v2_promoted_at_read"):
        quality += 15.0
    if pick.get("closing_odds") is not None or pick.get("clv_observed") is not None:
        quality += 10.0
    if pick.get("simulation"):
        quality += 5.0
    quality_component = max(0.0, min(100.0, quality))

    # Weighted blend
    display = (
        0.40 * calib_component
        + 0.25 * edge_component
        + 0.15 * consensus_component
        + 0.10 * sample_component
        + 0.10 * quality_component
    )

    # 99 ceiling rule: reserve 99 for the top 1-2% of raw historical scores.
    # Any pick below that percentile is hard-capped at 98 to keep 99 meaningful.
    pctile = _curve.percentile_of(raw)
    if pctile < TOP_PERCENTILE_FOR_99 and display > 98:
        display = 98.0

    # Final clamp to [0, 99].
    return round(max(0.0, min(99.0, display)), 1)


def apply_calibration(pick: dict) -> dict:
    """Mutate `pick` in place — replace `lock_score` with the calibrated
    blended value, retaining the original under `raw_lock_score`.

    Settled picks (won/lost/void/push) are NOT recalibrated — that would
    rewrite history on the analytics page. Only pending picks get the
    new display number.
    """
    status = pick.get("status")
    if status in ("won", "lost", "void", "push"):
        return pick
    new_score = compute_display_lock_score(pick)
    if new_score is None:
        return pick
    original = pick.get("lock_score")
    pick["raw_lock_score"] = original
    pick["lock_score"] = new_score
    # Stamp the calibrator state so the front-end can reason about freshness
    # (or so we can debug a stale curve in production).
    pick["calibration_fit_sample_size"] = _curve.fit_sample_size
    return pick


# ──────────────────────────────────────────────────────────────────────────
# Analytics — Expected vs Actual vs Delta per band
# ──────────────────────────────────────────────────────────────────────────

_BANDS = [
    ("Elite (95+)",       95.0, 100.0),
    ("Premium (90-94)",   90.0, 94.999),
    ("Strong (85-89)",    85.0, 89.999),
    ("Standard (80-84)",  80.0, 84.999),
    ("Speculative (70-79)", 70.0, 79.999),
    ("Pass (<70)",        0.0,  69.999),
]


def _compute_band_stats(pairs: list[tuple[float, float]]) -> list[dict]:
    """Per-band rollup of historical hit rates, plus expected (avg lock
    score) and the delta. Same payload shape as analytics._lock_calibration
    but built directly off the (raw_lock_score, outcome) pairs."""
    rows: list[dict] = []
    for label, lo, hi in _BANDS:
        n = 0
        wins = 0
        lock_sum = 0.0
        for x, y in pairs:
            if lo <= x <= hi:
                n += 1
                lock_sum += x
                if y >= 0.5:
                    wins += 1
        if n == 0:
            continue
        actual = round(wins * 100.0 / n, 1)
        expected = round(lock_sum / n, 1)
        rows.append({
            "band": label,
            "n": n,
            "expected_win_pct": expected,
            "actual_win_pct": actual,
            "calibration_delta": round(actual - expected, 1),
        })
    return rows


async def calibration_report(db) -> dict:
    """For the /api/analytics/calibration endpoint. Reads the persisted
    band_stats if available, otherwise rebuilds on the fly."""
    if _curve.band_stats:
        return {
            "fit_sample_size": _curve.fit_sample_size,
            "last_fit_at": _curve.last_fit_at,
            "rows": _curve.band_stats,
            "knot_count": len(_curve.knots_x),
        }
    # Fallback: compute on demand
    cursor = db.picks.find(
        {"status": {"$in": ["won", "lost"]}, "lock_score": {"$gt": 0}},
        {"_id": 0, "lock_score": 1, "status": 1},
    )
    pairs: list[tuple[float, float]] = []
    async for p in cursor:
        try:
            x = float(p.get("lock_score") or 0)
            y = 1.0 if p.get("status") == "won" else 0.0
            if x > 0:
                pairs.append((x, y))
        except (TypeError, ValueError):
            continue
    return {
        "fit_sample_size": len(pairs),
        "last_fit_at": None,
        "rows": _compute_band_stats(pairs),
        "knot_count": 0,
    }
