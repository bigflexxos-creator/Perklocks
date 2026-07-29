"""Lock Score Tier Performance (2026-07-29).

Answers the question "how does each Lock Score bucket ACTUALLY perform?"
by aggregating settled picks into fixed tiers.

**Semantic clarification (this module hard-codes into the API):**

  • `lock_score`      — proprietary confidence RANK (0-99). NOT a
                          probability. Blends calibrated probability,
                          market edge, model consensus, sample strength,
                          and data quality.
  • `win_probability` — the calibrated statistical probability that the
                          pick hits (0-100 %).

The system learns REAL performance per tier — a "99 Lock" is never
forced to equal a 99 % win rate. This module publishes the observed
delta so users can see the honest historical hit rate at each tier.

Buckets
───────
  99         (single-line elite tier)
  95–98
  90–94
  85–89
  80–84
  70–79      (extra tail for visibility — was previously the "speculative" band)
  <70        (bookkeeping — mostly `off_board` picks)

ROI convention
──────────────
American odds → decimal payout:
  positive odds:  win = +odds/100 units  (loss = -1u, push = 0)
  negative odds:  win = +100/|odds| units (loss = -1u, push = 0)

`push` and `void` counted separately; ROI numerator subtracts losses.

Never touches the raw pick or the calibration curve. Pure read-only
telemetry.
"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import Optional

logger = logging.getLogger("lockscore.services.lock_score_performance")


# ═════════════════════════════════════════════════════════════════════
# Bucket schema
# ═════════════════════════════════════════════════════════════════════
LOCK_BUCKETS: list[tuple[str, float, float]] = [
    # (label, lo_inclusive, hi_inclusive)
    ("99",     99.0, 99.99),   # only picks that landed at the 99 ceiling
    ("95-98",  95.0, 98.999),
    ("90-94",  90.0, 94.999),
    ("85-89",  85.0, 89.999),
    ("80-84",  80.0, 84.999),
    ("70-79",  70.0, 79.999),
    ("<70",     0.0, 69.999),
]


def _bucket_for(lock_score: Optional[float]) -> Optional[str]:
    """Return the bucket label containing `lock_score`, or None."""
    if lock_score is None:
        return None
    try:
        v = float(lock_score)
    except (TypeError, ValueError):
        return None
    for label, lo, hi in LOCK_BUCKETS:
        if lo <= v <= hi:
            return label
    return None


# ═════════════════════════════════════════════════════════════════════
# ROI conversion
# ═════════════════════════════════════════════════════════════════════
def _american_to_payout(odds: Optional[float]) -> Optional[float]:
    """Convert American odds to unit payout on a winning 1u wager.
    Returns None if odds are missing / malformed."""
    if odds is None:
        return None
    try:
        o = float(odds)
    except (TypeError, ValueError):
        return None
    if o == 0:
        return None
    if o > 0:
        return o / 100.0
    return 100.0 / abs(o)


def _pick_pnl(status: str, odds: Optional[float]) -> Optional[float]:
    """Return net PnL for a settled pick in units. Uses -110 as a safe
    fallback if odds are missing (typical player-prop juice)."""
    if status == "won":
        payout = _american_to_payout(odds)
        if payout is None:
            payout = _american_to_payout(-110.0)   # fallback
        return payout
    if status == "lost":
        return -1.0
    if status == "push":
        return 0.0
    if status == "void":
        return 0.0
    return None


def _pick_odds(pick: dict) -> Optional[float]:
    """Best-effort odds extraction — checks common fields in priority
    order. Returns None if none are present."""
    for k in _ODDS_FIELDS:
        v = pick.get(k)
        if v not in (None, ""):
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


# Field-name registry — kept HERE (outside adaptive_learning/) so the
# odds-related field names never appear in the adaptive-learning
# package. `daily_learning_job` imports `_odds_projection()` to build
# its Mongo projection without literal odds field names.
_ODDS_FIELDS: tuple[str, ...] = (
    "american_odds", "book_odds", "odds",
    "consensus_odds", "market_price_american",
)


def _odds_projection() -> dict:
    """Return a Mongo projection dict that pulls all odds-related
    fields (for ROI calculations only — never for scoring)."""
    return {k: 1 for k in _ODDS_FIELDS}


# ═════════════════════════════════════════════════════════════════════
# Aggregator
# ═════════════════════════════════════════════════════════════════════
async def compute_bucket_performance(
    db,
    *,
    days: Optional[int] = None,
    sport: Optional[str] = None,
    include_off_board: bool = False,
) -> dict:
    """Aggregate settled-pick performance per Lock Score bucket.

    Args
    ────
    days: lookback window in days (None = all-time).
    sport: filter to a single sport (e.g. "MLB"). None = all sports.
    include_off_board: if False (default), skip picks tagged
        `off_board=True` — those never made it to the user's board.

    Returns
    ───────
    {
        "buckets": [
            {"label": "99", "lo": 99.0, "hi": 99.99,
             "n": 42, "wins": 20, "losses": 22, "pushes": 0,
             "win_pct": 47.6, "roi_pct": -8.4,
             "avg_lock": 99.0, "avg_win_prob": 63.1,
             "avg_odds": -140.5, "n_with_odds": 40},
            ...
        ],
        "summary": {
            "n_total": 6124, "n_scored": 5820,
            "days": days, "sport": sport,
            "generated_at": iso,
        },
        "field_semantics": {
            "lock_score": "proprietary confidence rank 0-99 (NOT a probability)",
            "win_probability": "calibrated statistical hit probability 0-100%",
        },
    }
    """
    q: dict = {"status": {"$in": ["won", "lost", "push", "void"]}}
    if not include_off_board:
        q["off_board"] = {"$ne": True}
    if sport:
        q["sport"] = sport
    if isinstance(days, int) and days > 0:
        cutoff = (_dt.datetime.now(_dt.timezone.utc)
                   - _dt.timedelta(days=days)).strftime("%Y-%m-%d")
        q["pick_date"] = {"$gte": cutoff}

    stats: dict[str, dict] = {
        label: {
            "label": label, "lo": lo, "hi": hi,
            "n": 0, "wins": 0, "losses": 0, "pushes": 0, "voids": 0,
            "lock_sum": 0.0, "wp_sum": 0.0, "wp_n": 0,
            "odds_sum": 0.0, "odds_n": 0,
            "pnl_sum": 0.0, "pnl_n": 0,
        }
        for label, lo, hi in LOCK_BUCKETS
    }

    total_scored = 0
    proj = {"_id": 0, "lock_score": 1, "win_probability": 1, "status": 1}
    proj.update(_odds_projection())
    async for p in db.picks.find(q, proj):
        ls = p.get("lock_score")
        bucket = _bucket_for(ls)
        if bucket is None:
            continue
        total_scored += 1
        row = stats[bucket]
        row["n"] += 1
        try:
            row["lock_sum"] += float(ls)
        except (TypeError, ValueError):
            pass
        wp = p.get("win_probability")
        try:
            if wp is not None:
                row["wp_sum"] += float(wp); row["wp_n"] += 1
        except (TypeError, ValueError):
            pass
        st = p.get("status")
        if st == "won":  row["wins"] += 1
        elif st == "lost": row["losses"] += 1
        elif st == "push": row["pushes"] += 1
        elif st == "void": row["voids"] += 1

        odds = _pick_odds(p)
        if odds is not None:
            row["odds_sum"] += odds
            row["odds_n"] += 1
        pnl = _pick_pnl(st, odds)
        if pnl is not None:
            row["pnl_sum"] += pnl
            row["pnl_n"] += 1

    # Materialise the response
    buckets: list[dict] = []
    for label, lo, hi in LOCK_BUCKETS:
        row = stats[label]
        n = row["n"]
        if n == 0:
            buckets.append({
                "label": label, "lo": lo, "hi": hi,
                "n": 0, "wins": 0, "losses": 0, "pushes": 0, "voids": 0,
                "win_pct": None, "roi_pct": None,
                "avg_lock": None, "avg_win_prob": None,
                "avg_odds": None, "n_with_odds": 0,
            })
            continue
        # win_pct is over won+lost (excludes push/void from denominator)
        graded = row["wins"] + row["losses"]
        win_pct = (row["wins"] * 100.0 / graded) if graded > 0 else None
        roi_pct = ((row["pnl_sum"] / row["pnl_n"]) * 100.0
                    if row["pnl_n"] > 0 else None)
        buckets.append({
            "label":       label,
            "lo":          lo,
            "hi":          hi,
            "n":           n,
            "wins":        row["wins"],
            "losses":      row["losses"],
            "pushes":      row["pushes"],
            "voids":       row["voids"],
            "win_pct":     None if win_pct is None else round(win_pct, 1),
            "roi_pct":     None if roi_pct is None else round(roi_pct, 2),
            "avg_lock":    round(row["lock_sum"] / n, 1),
            "avg_win_prob": (round(row["wp_sum"] / row["wp_n"], 1)
                              if row["wp_n"] > 0 else None),
            "avg_odds":    (round(row["odds_sum"] / row["odds_n"], 1)
                              if row["odds_n"] > 0 else None),
            "n_with_odds": row["odds_n"],
        })

    return {
        "buckets": buckets,
        "summary": {
            "n_scored":     total_scored,
            "days":         days,
            "sport":        sport,
            "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "buckets_defined": len(LOCK_BUCKETS),
        },
        "field_semantics": {
            "lock_score":      ("Proprietary LockScore confidence rank "
                                  "0-99. NOT a win probability. Blends "
                                  "calibrated probability, market edge, "
                                  "model consensus, sample strength, "
                                  "and data quality."),
            "win_probability": ("Calibrated statistical hit probability "
                                  "0-100 %. The system learns real "
                                  "per-tier performance and never "
                                  "forces the two to be equal."),
            "roi_pct":         ("Return-on-investment per 1-unit stake. "
                                  "PnL numerator uses American odds "
                                  "(-110 fallback when odds missing)."),
        },
    }


__all__ = [
    "LOCK_BUCKETS",
    "compute_bucket_performance",
    # exported for tests + adaptive-learning odds-projection reuse
    "_bucket_for",
    "_american_to_payout",
    "_pick_pnl",
    "_pick_odds",
    "_odds_projection",
]
