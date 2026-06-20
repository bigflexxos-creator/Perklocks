"""Learning System v2 — ROI-weighted self-tuning + calibration band gates + market filter weights.

Replaces the v1 calibration logic with the user's explicit spec:

    Learning weights:
        ROI         = 50%
        CLV         = 25%
        Calibration = 20%
        Volume      =  5%

    Re-learn only when:
        100+ settled picks total
        30+ picks per market

    Calibration band gates:
        Track expected vs actual hit-rate per band (99 / 95-98 / 90-94 / 85-89 / 80-84).
        If a band underperforms expected by >10 percentage points, RAISE the future entry
        requirement for that band (band_threshold += 5 each time).

    Market filter weights:
        Tennis           = 1.15
        Double Chance    = 1.10
        Player Points    = 0.85  (reduced priority)
        Auto-decay:      ROI < -10% over 50+ picks → multiplier *= 0.90 (capped at 0.50)

    Lock Score 99 gates (per spec):
        edge_percent ≥ 8%
        ≥ 4 independent signals agree (factor values > 0.55)
        no conflicting injuries (deep_dive risk_score < 60)
        historical bucket ROI ≥ 0
        prediction stable on recalc (no_bet flag false)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("lockscore.learning_v2")


# ───────────────────────── Spec constants ─────────────────────────

LEARNING_WEIGHTS = {"roi": 0.50, "clv": 0.25, "calibration": 0.20, "volume": 0.05}

MIN_TOTAL_PICKS = 100
MIN_PICKS_PER_MARKET = 30

CALIBRATION_BANDS = [
    {"name": "99",     "min": 99.0, "max": 99.0, "expected": 98.0},
    {"name": "95-98",  "min": 95.0, "max": 98.99, "expected": 80.0},
    {"name": "90-94",  "min": 90.0, "max": 94.99, "expected": 70.0},
    {"name": "85-89",  "min": 85.0, "max": 89.99, "expected": 62.0},
    {"name": "80-84",  "min": 80.0, "max": 84.99, "expected": 55.0},
]
CALIBRATION_DROP_THRESHOLD = 10.0     # band underperforms by >10pp → raise gate
GATE_RAISE_STEP = 5.0                  # raise lock_floor to enter band by +5 each time
GATE_MAX_RAISE = 15.0                  # cap total raise per band

MARKET_WEIGHTS = {
    "tennis":          1.15,
    "double chance":   1.10,
    "win or draw":     1.05,
    "moneyline":       1.00,
    "spread":          1.00,
    "run line":        1.00,
    "totals":          1.00,
    "player points":   0.85,   # reduced per spec
    "player rebounds": 0.90,
    "player assists":  0.90,
}
DECAY_MIN_PICKS = 50
DECAY_ROI_PCT = -10.0
DECAY_MULTIPLIER = 0.90
DECAY_FLOOR = 0.50

LOCK99_GATES = {
    "min_edge_pct":     8.0,
    "min_signals":      4,           # factors > 0.55
    "signal_threshold": 0.55,
    "max_risk_score":   60.0,        # deep_dive risk
    "min_bucket_roi":   0.0,         # historical bucket ROI ≥ 0%
}


# ─────────────────────────────────────────────────────────────
# League-aware learning + market blacklist
# ─────────────────────────────────────────────────────────────
# Sports where league granularity matters for learning. For these sports,
# the bucket key becomes (sport, league, market) instead of (sport, market).
# Adds smarter weight isolation for niche leagues whose playstyle differs
# from the sport mean (e.g. League of Ireland defensive 1-0 grind vs the
# attacking, draw-rare Brasileirão).
LEAGUE_AWARE_SPORTS = {"Soccer"}

# Hard blacklist — sport+league_substring+market_substring combos that have
# shown sustained -EV. Picks matching get auto-flagged `no_bet=True` and
# disappear from the home feed (filtered server-side already).
LEAGUE_MARKET_BLACKLIST: list[tuple[str, str, str, str]] = [
    ("Soccer", "league of ireland", "moneyline",
     "League of Ireland Moneyline is -27% ROI over last 14 days — auto-blocked"),
]


def _bucket_key(sport: str, league: str | None, market_norm: str) -> tuple:
    """Canonical learning bucket key.

    Returns a 3-tuple for league-aware sports, 2-tuple for others. The
    2/3-tuple union adds granularity for sports that need it without
    churning all existing buckets.
    """
    if sport in LEAGUE_AWARE_SPORTS and league:
        return (sport, league, market_norm)
    return (sport, market_norm)


def _is_blacklisted(sport: str, league: str | None, market: str) -> tuple[bool, str]:
    """Return (True, reason) if the pick matches a blacklist entry."""
    lg = (league or "").lower()
    mk = (market or "").lower()
    for bl_sport, bl_lg, bl_mk, reason in LEAGUE_MARKET_BLACKLIST:
        if sport == bl_sport and bl_lg in lg and bl_mk in mk:
            return True, reason
    return False, ""


# ───────────────────────── Helpers ─────────────────────────


def _market_key(market: str) -> str:
    """Normalize a market label to one of the canonical keys in MARKET_WEIGHTS."""
    m = (market or "").lower()
    if "double chance" in m:
        return "double chance"
    if "win or draw" in m:
        return "win or draw"
    if "moneyline" in m:
        return "moneyline"
    if "run line" in m or "runline" in m:
        return "run line"
    if "spread" in m:
        return "spread"
    if "total" in m:
        return "totals"
    if "points" in m:
        return "player points"
    if "rebounds" in m:
        return "player rebounds"
    if "assists" in m:
        return "player assists"
    return "moneyline"


# ───────────────────────── Market performance aggregator ─────────────────────────


async def compute_market_performance(db) -> dict[tuple[str, str], dict]:
    """Aggregate settled picks into per-(sport, market) performance rows.

    Returns: {(sport, market): {n, won, lost, units_risked, units_profit,
                                 roi, clv_avg, calibration_err}}.
    """
    pipeline = [
        {"$match": {"status": {"$in": ["won", "lost", "push"]}}},
        {"$project": {
            "_id": 0, "sport": 1, "league": 1,
            "market_key": {"$toLower": {"$ifNull": ["$market", ""]}},
            "status": 1, "units_risked": 1, "units_profit": 1,
            "clv_value": 1, "lock_score": 1, "win_probability": 1,
        }},
    ]
    rows: dict[tuple, dict] = {}
    async for p in db.picks.aggregate(pipeline):
        sport = p.get("sport") or "Unknown"
        league = p.get("league")
        market_norm = _market_key(p.get("market_key", ""))
        key = _bucket_key(sport, league, market_norm)
        r = rows.setdefault(key, {
            "sport": sport,
            "league": league if sport in LEAGUE_AWARE_SPORTS else None,
            "market": market_norm,
            "n": 0, "won": 0, "lost": 0, "push": 0,
            "units_risked": 0.0, "units_profit": 0.0,
            "clv_sum": 0.0, "clv_n": 0,
            "calib_err_sum": 0.0, "calib_n": 0,
        })
        r["n"] += 1
        r[p["status"]] += 1
        r["units_risked"] += float(p.get("units_risked") or 0)
        r["units_profit"] += float(p.get("units_profit") or 0)
        clv = p.get("clv_value")
        if clv is not None:
            r["clv_sum"] += float(clv)
            r["clv_n"] += 1
        # Calibration: expected hit-rate (win_prob/100) vs actual (1 if won else 0).
        wp = p.get("win_probability")
        if wp is not None and p["status"] != "push":
            r["calib_err_sum"] += abs((float(wp) / 100.0) - (1.0 if p["status"] == "won" else 0.0))
            r["calib_n"] += 1
    # Final derived metrics.
    for r in rows.values():
        r["roi"] = (r["units_profit"] / r["units_risked"] * 100.0) if r["units_risked"] else 0.0
        r["clv_avg"] = (r["clv_sum"] / r["clv_n"]) if r["clv_n"] else 0.0
        r["calibration_err"] = (r["calib_err_sum"] / r["calib_n"] * 100.0) if r["calib_n"] else 0.0
        r["hit_rate"] = (r["won"] / (r["won"] + r["lost"]) * 100.0) if (r["won"] + r["lost"]) else 0.0
    return rows


def composite_market_score(row: dict) -> float:
    """Apply the v2 learning weights to produce a composite weight 0..1 for
    a (sport, market) bucket. Anchored so 0 = avoid, 1 = strongly prefer."""
    # ROI: -20% → 0, 0% → 0.5, +20% → 1.0
    roi_norm  = max(0.0, min(1.0, (row.get("roi", 0) + 20) / 40))
    # CLV: -5 → 0, 0 → 0.5, +5 → 1.0
    clv_norm  = max(0.0, min(1.0, (row.get("clv_avg", 0) + 5) / 10))
    # Calibration: 0% err → 1.0, 30% err → 0.0
    cal_norm  = max(0.0, min(1.0, 1.0 - (row.get("calibration_err", 30) / 30.0)))
    # Volume: 30 picks → 0.5, 300 picks → 1.0 (logarithmic, capped)
    n = row.get("n", 0)
    vol_norm  = max(0.0, min(1.0, (n / 300.0) if n else 0))
    score = (LEARNING_WEIGHTS["roi"]         * roi_norm +
             LEARNING_WEIGHTS["clv"]         * clv_norm +
             LEARNING_WEIGHTS["calibration"] * cal_norm +
             LEARNING_WEIGHTS["volume"]      * vol_norm)
    return round(score, 3)


# ───────────────────────── Calibration band gates ─────────────────────────


async def compute_band_calibration(db) -> list[dict]:
    """Per-band hit-rate vs expected — used to decide whether to raise the
    future entry threshold for that band."""
    out = []
    for band in CALIBRATION_BANDS:
        cursor = db.picks.find({
            "status": {"$in": ["won", "lost"]},
            "lock_score": {"$gte": band["min"], "$lte": band["max"]},
        }, {"_id": 0, "status": 1})
        won = lost = 0
        async for p in cursor:
            if p["status"] == "won":
                won += 1
            elif p["status"] == "lost":
                lost += 1
        n = won + lost
        actual = (won / n * 100.0) if n else 0.0
        expected = band["expected"]
        gap = expected - actual         # positive = underperforming
        out.append({
            "band":     band["name"],
            "n":        n,
            "expected": expected,
            "actual":   round(actual, 2),
            "gap":      round(gap, 2),
            "needs_gate_raise": (n >= 20 and gap > CALIBRATION_DROP_THRESHOLD),
        })
    return out


async def recompute_and_persist(db) -> dict:
    """Run all v2 learning calculations and persist results to MongoDB.

    Returns a summary dict ready for the analytics dashboard."""
    # Volume gate: skip relearn if not enough settled picks.
    total_settled = await db.picks.count_documents({"status": {"$in": ["won", "lost"]}})
    if total_settled < MIN_TOTAL_PICKS:
        logger.info("v2 learning gated: total_settled=%d < %d — using neutral weights",
                    total_settled, MIN_TOTAL_PICKS)
        return {"gated": True, "total_settled": total_settled}

    # Per-(sport, market) performance.
    rows = await compute_market_performance(db)

    # Compute composite + auto-decay multiplier per row.
    learning_log: list[dict] = []
    market_weights_runtime: dict[str, float] = {}
    for key, row in rows.items():
        # key is (sport, market) or (sport, league, market) — extract sport+market
        if len(key) == 3:
            sport, _league, market = key
        else:
            sport, market = key
        n_market = sum(r["n"] for k, r in rows.items() if k[-1] == market)
        # Min picks per market gate
        if n_market < MIN_PICKS_PER_MARKET:
            row["composite"] = 0.5  # neutral
            continue
        row["composite"] = composite_market_score(row)
        # Base weight from MARKET_WEIGHTS, then decay if ROI < -10% with 50+ picks.
        base_w = MARKET_WEIGHTS.get(market, 1.0)
        if row["n"] >= DECAY_MIN_PICKS and row["roi"] < DECAY_ROI_PCT:
            new_w = max(DECAY_FLOOR, base_w * DECAY_MULTIPLIER)
            if abs(new_w - base_w) > 0.01:
                learning_log.append({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "type": "market_decay",
                    "sport": sport, "market": market,
                    "from": base_w, "to": round(new_w, 3),
                    "reason": f"ROI {row['roi']:.1f}% < {DECAY_ROI_PCT}% over {row['n']} picks",
                })
            market_weights_runtime[market] = max(market_weights_runtime.get(market, 0), new_w)
        else:
            market_weights_runtime[market] = max(market_weights_runtime.get(market, 0), base_w)

    # Band calibration → gate raises.
    bands = await compute_band_calibration(db)
    band_raises: dict[str, float] = {}
    for b in bands:
        if not b["needs_gate_raise"]:
            continue
        raise_amount = min(GATE_MAX_RAISE, GATE_RAISE_STEP)
        band_raises[b["band"]] = raise_amount
        learning_log.append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": "band_gate_raise",
            "band": b["band"], "raise_by": raise_amount,
            "reason": f"actual {b['actual']}% vs expected {b['expected']}% (gap {b['gap']}pp)",
        })

    # Persist results in a single document (overwrite previous).
    doc = {
        "_id": "learning_v2_state",
        "as_of": datetime.now(timezone.utc).isoformat(),
        "total_settled": total_settled,
        "market_rows": [{**v, "composite_weight": v.get("composite", 0.5)}
                         for k, v in rows.items()],
        "market_weights": market_weights_runtime,
        "band_calibration": bands,
        "band_raises": band_raises,
        "changes_log": learning_log,
    }
    await db.learning_state.replace_one({"_id": "learning_v2_state"}, doc, upsert=True)

    # Append to the changes log audit collection (append-only).
    if learning_log:
        await db.learning_log.insert_many(learning_log)

    logger.info("v2 learning persisted: %d rows, %d market weights, %d band raises, %d new log entries",
                len(rows), len(market_weights_runtime), len(band_raises), len(learning_log))
    return {
        "gated": False,
        "total_settled": total_settled,
        "rows": len(rows),
        "market_weights": market_weights_runtime,
        "band_raises": band_raises,
        "changes_log_count": len(learning_log),
    }


# ───────────────────────── Lock Score 99 gates ─────────────────────────


def apply_lock99_gates(pick: dict, factors: dict, lock_score: float,
                       bucket_row: Optional[dict] = None) -> tuple[float, str]:
    """Enforce the spec's 99-only requirements. Caps lock_score to a lower
    band when ANY gate fails. Returns (new_score, gate_failed_reason).

    Gates (all must pass for a 99):
        1. edge_percent ≥ 8%
        2. ≥ 4 factor signals > 0.55 (independent signal agreement)
        3. deep_dive.risk_score < 60 (no conflicting injuries/news)
        4. historical bucket ROI ≥ 0%
        5. no_bet flag is False (prediction stable on recalc)
    """
    if lock_score < 99:
        return (lock_score, "")
    reasons: list[str] = []

    # 1. Edge gate
    if (pick.get("edge_percent") or 0) < LOCK99_GATES["min_edge_pct"]:
        reasons.append("edge<8%")

    # 2. Signal agreement gate
    signals_agree = sum(1 for v in (factors or {}).values()
                        if v >= LOCK99_GATES["signal_threshold"])
    if signals_agree < LOCK99_GATES["min_signals"]:
        reasons.append(f"only {signals_agree}/{LOCK99_GATES['min_signals']} signals agree")

    # 3. Conflicting injuries / risk gate
    dd = pick.get("deep_dive_scores") or {}
    risk = dd.get("risk") if isinstance(dd, dict) else None
    if risk is not None and risk >= LOCK99_GATES["max_risk_score"]:
        reasons.append(f"risk {risk} ≥ {LOCK99_GATES['max_risk_score']}")

    # 4. Historical ROI gate
    if bucket_row and bucket_row.get("n", 0) >= 10:
        if bucket_row.get("roi", 0) < LOCK99_GATES["min_bucket_roi"]:
            reasons.append(f"bucket ROI {bucket_row.get('roi'):.1f}% < 0")

    # 5. Stability gate
    if pick.get("no_bet"):
        reasons.append("flagged no_bet")

    if not reasons:
        return (99.0, "")
    # Cap at 98 (Premium tier) by default; if multiple gates failed, drop further.
    cap = 98.0 - min(8.0, 3.0 * (len(reasons) - 1))   # 98 / 95 / 92 / 89 ...
    new_score = min(lock_score, cap)
    return (round(new_score, 1), ", ".join(reasons))


# ───────────────────────── Apply v2 to a fresh picks list ─────────────────────────


async def apply_v2_to_picks(picks: list[dict], db) -> list[dict]:
    """Re-weight picks using current learning state + apply 99 gates.

    Side effects on each pick:
      • lock_score may be lowered if 99-gates fail (`lock99_gate_failed_reason`)
      • lock_score multiplied by market_weight (clamped 80-99)
      • `learning_v2_weight` stored for analytics
    """
    # Pull latest learning state.
    state = await db.learning_state.find_one({"_id": "learning_v2_state"}) or {}
    market_weights = state.get("market_weights") or MARKET_WEIGHTS
    band_raises = state.get("band_raises") or {}
    market_rows_list = state.get("market_rows") or []
    # Build a dual lookup: league-aware key wins, falls back to (sport, market).
    market_rows_la: dict[tuple, dict] = {}
    market_rows_sw: dict[tuple, dict] = {}
    for r in market_rows_list:
        s, m, lg = r.get("sport"), r.get("market"), r.get("league")
        if lg:
            market_rows_la[(s, lg, m)] = r
        market_rows_sw[(s, m)] = r  # always keep sport-wide fallback

    blacklisted_count = 0

    # Per-event goalscorer cap: keep top 2 by lock_score, flag rest as no_bet.
    # Prevents flooding the feed with 5+ "Anytime Goal Scorer" picks for a
    # single match (e.g. Brazil vs Haiti spawning Vinicius + Rodrygo +
    # Neymar + ... when realistically only the top 1-2 are real edges).
    GS_PICKS_PER_EVENT = 2
    gs_by_event: dict[str, list[dict]] = {}
    for p in picks:
        if "goal scorer" in (p.get("market") or "").lower():
            gs_by_event.setdefault(p.get("event") or "", []).append(p)
    for event, gs in gs_by_event.items():
        if len(gs) <= GS_PICKS_PER_EVENT:
            continue
        gs.sort(key=lambda x: -float(x.get("lock_score") or 0))
        for extra in gs[GS_PICKS_PER_EVENT:]:
            extra["no_bet"] = True
            extra["no_bet_reason"] = (
                f"Max {GS_PICKS_PER_EVENT} goalscorer picks per match — "
                f"surfaced top {GS_PICKS_PER_EVENT} only"
            )
            extra["capped_by_learning"] = True

    for p in picks:
        sport = p.get("sport") or "Unknown"
        league = p.get("league")
        market_norm = _market_key(p.get("market") or "")

        # 0) Hard blacklist — sport+league+market combos with sustained -EV.
        # Flag as `no_bet=True` (API filter already hides these from the feed)
        # and record the reason for the user.
        bl, reason = _is_blacklisted(sport, league, p.get("market") or "")
        if bl:
            p["no_bet"] = True
            p["no_bet_reason"] = reason
            p["blacklisted_by_learning"] = True
            blacklisted_count += 1
            continue

        # 1) Apply 99 gates (don't touch elite anchors per spec — they stay 99)
        if not p.get("elite_player"):
            # League-aware bucket lookup with sport-wide fallback
            bucket = (
                market_rows_la.get((sport, league, market_norm))
                if sport in LEAGUE_AWARE_SPORTS and league else None
            )
            if bucket is None:
                bucket = market_rows_sw.get((sport, market_norm))
            new_score, gate_reason = apply_lock99_gates(
                p, p.get("factors") or {}, float(p.get("lock_score") or 0), bucket
            )
            if gate_reason:
                p["lock99_gate_failed"] = gate_reason
                p["lock_score"] = new_score
        # 2) Apply market weight multiplier (subtle nudge, not a full re-score)
        mw = market_weights.get(market_norm, 1.0)
        if abs(mw - 1.0) > 0.001:
            adjusted = float(p.get("lock_score", 0)) * mw
            p["lock_score"] = round(max(0.0, min(99.0, adjusted)), 1)
            p["learning_v2_weight"] = round(mw, 3)
        # 3) Apply calibration band raise — raise the minimum lock to enter a band
        band_name = _band_for_score(p.get("lock_score", 0))
        raise_amt = band_raises.get(band_name, 0)
        if raise_amt > 0:
            # If the score's just barely in the band, push it down one band.
            band = next((b for b in CALIBRATION_BANDS if b["name"] == band_name), None)
            if band and (p["lock_score"] - band["min"]) < raise_amt:
                p["lock_score"] = round(band["min"] - 0.1, 1)
                p["calibration_demotion"] = f"{band_name} band underperformed"
        # 4) Re-grade after any change
        try:
            from sports_engine import _grade, _confidence
            p["grade"] = _grade(p["lock_score"])
            p["confidence"] = _confidence(p["lock_score"])
        except Exception:
            pass
    return picks


def _band_for_score(score: float) -> str:
    for b in CALIBRATION_BANDS:
        if b["min"] <= score <= b["max"]:
            return b["name"]
    return "<80"
