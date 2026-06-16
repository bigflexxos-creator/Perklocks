"""Model performance analytics — simulates 1u flat-betting on every pick the
engine has ever produced, no manual bet-tracking required.

All metrics are derived from the existing `picks` collection. The first time
we run, we backfill three fields on each settled pick:

    odds_at_pick     — copy of `book_odds` when the pick was first generated.
                       (We also start preserving this across daily refreshes;
                        see server.py::_refresh_picks.)
    closing_odds     — best snapshot of the line at game start. Today this is
                       the same as `book_odds`; once a closing-line snapshotter
                       runs, it will be updated by that job.
    units_profit     — flat 1u stake → won at American odds → profit in units.
                       lost = -1u, push = 0.
    clv_value        — closing_odds − odds_at_pick (American). 0 today.

The endpoint `/api/analytics/model-performance` returns the full dashboard.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Any

logger = logging.getLogger("lockscore.analytics")


def american_to_decimal_payout(american: int | float) -> float:
    """1u stake → returned payout (incl. stake) in decimal. -110 → 1.909, +220 → 3.20."""
    if not american:
        return 1.0
    a = float(american)
    if a > 0:
        return 1.0 + (a / 100.0)
    return 1.0 + (100.0 / abs(a))


def american_profit_per_unit(american: int | float, status: str) -> float:
    """Profit on a 1u stake. status ∈ {won, lost, push}."""
    if status == "push":
        return 0.0
    if status == "lost":
        return -1.0
    if status == "won":
        payout = american_to_decimal_payout(american)
        return round(payout - 1.0, 4)
    return 0.0  # pending / unknown


def american_to_implied_pct(american: int | float) -> float:
    if not american:
        return 0.0
    a = float(american)
    if a > 0:
        return 100.0 / (a + 100.0) * 100
    return abs(a) / (abs(a) + 100.0) * 100


def clv_units(odds_at_pick: float | None, closing_odds: float | None) -> float:
    """CLV expressed in "implied probability points": positive means you got a
    better-than-closing price (line moved against you AFTER you took it).

    We use Δ implied-probability rather than raw American because raw American
    is non-linear (e.g. -150 → -200 is a smaller real CLV than +100 → +150).
    Implied-prob deltas are directly comparable across price levels.
    """
    if odds_at_pick is None or closing_odds is None:
        return 0.0
    if not odds_at_pick or not closing_odds:
        return 0.0
    return round(american_to_implied_pct(closing_odds) - american_to_implied_pct(odds_at_pick), 3)


def confidence_bucket(lock_score: float) -> str:
    if lock_score is None:
        return "Unknown"
    if lock_score >= 95:
        return "Elite (95+)"
    if lock_score >= 90:
        return "Premium (90-94)"
    if lock_score >= 85:
        return "Strong (85-89)"
    if lock_score >= 80:
        return "Standard (80-84)"
    if lock_score >= 70:
        return "Speculative (70-79)"
    return "Pass (<70)"


async def backfill_metrics(db) -> int:
    """One-time backfill (idempotent): populate odds_at_pick / closing_odds /
    units_profit / clv_value on any settled pick that doesn't have them yet."""
    cursor = db.picks.find(
        {"status": {"$in": ["won", "lost", "push"]},
         "$or": [{"units_profit": {"$exists": False}}, {"odds_at_pick": {"$exists": False}}]},
        {"_id": 0, "id": 1, "book_odds": 1, "status": 1, "lock_score": 1, "edge_percent": 1},
    )
    updated = 0
    async for p in cursor:
        book = p.get("book_odds")
        status = p.get("status")
        profit = american_profit_per_unit(book or 0, status or "")
        await db.picks.update_one(
            {"id": p["id"]},
            {"$set": {
                "odds_at_pick": book,
                "closing_odds": book,
                "units_risked": 1.0,
                "units_profit": profit,
                "clv_value": 0.0,
                "confidence_bucket": confidence_bucket(p.get("lock_score")),
            }},
        )
        updated += 1
    if updated:
        logger.info("analytics backfill updated %d picks", updated)
    return updated


def _safe_div(n: float, d: float, default: float = 0.0) -> float:
    return round(n / d, 4) if d else default


async def compute_model_performance(db, days: int = 30) -> dict[str, Any]:
    """Build the analytics dashboard payload."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    week_cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    iso_cutoff = cutoff.isoformat()
    iso_week = week_cutoff.isoformat()

    # Pull all settled picks once.
    cursor = db.picks.find(
        {"status": {"$in": ["won", "lost", "push"]}},
        {"_id": 0, "sport": 1, "market": 1, "status": 1, "lock_score": 1,
         "win_probability": 1, "edge_percent": 1, "book_odds": 1,
         "odds_at_pick": 1, "closing_odds": 1, "units_profit": 1,
         "settled_at": 1, "commence_time": 1, "event_time": 1,
         "confidence_bucket": 1},
    )
    picks = await cursor.to_list(length=10_000)

    if not picks:
        return _empty_payload()

    total = len(picks)
    wins = sum(1 for p in picks if p["status"] == "won")
    losses = sum(1 for p in picks if p["status"] == "lost")
    pushes = sum(1 for p in picks if p["status"] == "push")
    decisive = wins + losses  # excludes pushes for hit rate
    units_risked = sum(p.get("units_risked", 1.0) for p in picks if p["status"] != "push")
    units_profit = sum(p.get("units_profit") or 0.0 for p in picks)

    # Time-bounded slices.
    def _is_within(p, iso) -> bool:
        t = p.get("settled_at") or p.get("event_time") or p.get("commence_time") or ""
        return t >= iso if t else False

    p7 = [p for p in picks if _is_within(p, iso_week)]
    p30 = [p for p in picks if _is_within(p, iso_cutoff)]
    units_profit_7 = sum((p.get("units_profit") or 0.0) for p in p7)
    units_profit_30 = sum((p.get("units_profit") or 0.0) for p in p30)

    # Edge / CLV averages over all decisive picks.
    edges = [p.get("edge_percent") for p in picks if isinstance(p.get("edge_percent"), (int, float))]
    avg_edge = round(sum(edges) / len(edges), 2) if edges else 0.0
    clvs = [p.get("clv_value") for p in picks if isinstance(p.get("clv_value"), (int, float))]
    avg_clv = round(sum(clvs) / len(clvs), 3) if clvs else 0.0
    positive_clv_pct = round(100 * sum(1 for c in clvs if c > 0) / len(clvs), 1) if clvs else 0.0

    # Breakdowns ──────────────────────────────────────────────────────────
    by_sport = _group(picks, lambda p: p.get("sport") or "Unknown")
    by_market = _group(picks, lambda p: _market_label(p.get("market")))
    by_conf = _group(picks, lambda p: p.get("confidence_bucket") or confidence_bucket(p.get("lock_score")))

    # Lock-score calibration audit: how accurate are the bands really?
    calibration = _lock_calibration(picks)

    # Best / worst markets by ROI (min 5 picks for stability).
    market_rows = [r for r in by_market if r["count"] >= 5]
    best_market = max(market_rows, key=lambda r: r["roi"]) if market_rows else None
    worst_market = min(market_rows, key=lambda r: r["roi"]) if market_rows else None
    sport_rows = [r for r in by_sport if r["count"] >= 5]
    best_sport = max(sport_rows, key=lambda r: r["roi"]) if sport_rows else None

    # Tennis-specific breakdown: ROI by tournament + by surface.
    # Used by /api/analytics/tennis and the Analytics dashboard.
    tennis_picks = [p for p in picks if (p.get("sport") or "").lower() == "tennis"]
    by_tennis_tournament = _group(tennis_picks, lambda p: p.get("league") or "Unknown")
    def _surface_of(p: dict) -> str:
        tc = p.get("tennis_components") or {}
        if tc.get("surface_name"):
            return tc["surface_name"]
        # Fallback: import map lazily to avoid circular imports.
        try:
            from tennis_engine import SURFACE_BY_LEAGUE
            return SURFACE_BY_LEAGUE.get(p.get("league") or "", "Hard")
        except Exception:
            return "Hard"
    by_tennis_surface = _group(tennis_picks, _surface_of)

    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "totals": {
            "picks": total,
            "wins": wins,
            "losses": losses,
            "pushes": pushes,
            "decisive": decisive,
            "hit_rate": _safe_div(wins * 100, decisive),
            "units_risked": round(units_risked, 2),
            "units_won": round(units_profit, 2),         # net profit in units
            "roi_pct": round(_safe_div(units_profit * 100, units_risked), 2),
            "units_profit_7d": round(units_profit_7, 2),
            "units_profit_30d": round(units_profit_30, 2),
            "avg_edge_pct": avg_edge,
            "avg_clv": avg_clv,
            "positive_clv_pct": positive_clv_pct,
        },
        "by_sport": by_sport,
        "by_market": by_market,
        "by_confidence": by_conf,
        "by_tennis_tournament": by_tennis_tournament,
        "by_tennis_surface": by_tennis_surface,
        "calibration": calibration,
        "highlights": {
            "best_sport": best_sport,
            "best_market": best_market,
            "worst_market": worst_market,
        },
    }


def _market_label(market: str | None) -> str:
    """Group player-prop variants under their stat name."""
    if not market:
        return "Other"
    m = market.lower()
    if " hits" in m or m.endswith(" hits"):
        return "MLB Hits"
    if "total bases" in m:
        return "MLB Total Bases"
    if "home runs" in m:
        return "MLB HRs"
    if "strikeouts" in m:
        return "MLB Strikeouts"
    if "points" in m and "total" not in m:
        return "Player Points"
    if "rebounds" in m:
        return "Player Rebounds"
    if "assists" in m:
        return "Player Assists"
    if "anytime goal scorer" in m:
        return "Soccer Anytime Goal Scorer"
    if "moneyline" in m:
        return "Moneyline"
    if "win or draw" in m or "double chance" in m:
        return "Double Chance"
    if "spread" in m or "run line" in m or "puck line" in m:
        return "Spread"
    if "total" in m or "over/under" in m:
        return "Game Total O/U"
    return market[:40]


def _group(picks: list[dict], key_fn) -> list[dict]:
    """Aggregate stats per key. Returns sorted by ROI descending."""
    buckets: dict[str, dict] = {}
    for p in picks:
        k = key_fn(p)
        if k not in buckets:
            buckets[k] = {"key": k, "count": 0, "wins": 0, "losses": 0, "pushes": 0,
                          "units_risked": 0.0, "units_profit": 0.0, "edges": [], "clvs": []}
        b = buckets[k]
        b["count"] += 1
        if p["status"] == "won":
            b["wins"] += 1
        elif p["status"] == "lost":
            b["losses"] += 1
        elif p["status"] == "push":
            b["pushes"] += 1
        if p["status"] != "push":
            b["units_risked"] += p.get("units_risked", 1.0)
        b["units_profit"] += (p.get("units_profit") or 0.0)
        if isinstance(p.get("edge_percent"), (int, float)):
            b["edges"].append(p["edge_percent"])
        if isinstance(p.get("clv_value"), (int, float)):
            b["clvs"].append(p["clv_value"])
    rows = []
    for b in buckets.values():
        decisive = b["wins"] + b["losses"]
        rows.append({
            "key": b["key"],
            "count": b["count"],
            "wins": b["wins"],
            "losses": b["losses"],
            "pushes": b["pushes"],
            "hit_rate": _safe_div(b["wins"] * 100, decisive),
            "units": round(b["units_profit"], 2),
            "roi": round(_safe_div(b["units_profit"] * 100, b["units_risked"]), 2),
            "avg_edge": round(sum(b["edges"]) / len(b["edges"]), 2) if b["edges"] else 0.0,
            "avg_clv": round(sum(b["clvs"]) / len(b["clvs"]), 3) if b["clvs"] else 0.0,
        })
    rows.sort(key=lambda r: r["roi"], reverse=True)
    return rows


def _lock_calibration(picks: list[dict]) -> list[dict]:
    """How accurate are our lock-score bands really?
    Returns rows aligned with confidence buckets, ordered from highest band down."""
    order = ["Elite (95+)", "Premium (90-94)", "Strong (85-89)",
             "Standard (80-84)", "Speculative (70-79)", "Pass (<70)"]
    buckets: dict[str, dict] = {k: {"count": 0, "wins": 0, "losses": 0, "lock_sum": 0.0} for k in order}
    for p in picks:
        bk = p.get("confidence_bucket") or confidence_bucket(p.get("lock_score"))
        if bk not in buckets:
            continue
        if p["status"] == "push":
            continue
        b = buckets[bk]
        b["count"] += 1
        b["lock_sum"] += p.get("lock_score") or 0.0
        if p["status"] == "won":
            b["wins"] += 1
        elif p["status"] == "lost":
            b["losses"] += 1
    rows = []
    for k in order:
        b = buckets[k]
        if b["count"] == 0:
            continue
        decisive = b["wins"] + b["losses"]
        actual = _safe_div(b["wins"] * 100, decisive)
        expected = round(b["lock_sum"] / b["count"], 1) if b["count"] else 0
        rows.append({
            "band": k,
            "count": b["count"],
            "avg_lock_score": expected,
            "actual_hit_rate": actual,
            "delta": round(actual - expected, 2),  # negative = lock score over-promised
        })
    return rows


def _empty_payload() -> dict:
    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "totals": {"picks": 0, "wins": 0, "losses": 0, "pushes": 0, "decisive": 0,
                   "hit_rate": 0, "units_risked": 0, "units_won": 0, "roi_pct": 0,
                   "units_profit_7d": 0, "units_profit_30d": 0,
                   "avg_edge_pct": 0, "avg_clv": 0, "positive_clv_pct": 0},
        "by_sport": [], "by_market": [], "by_confidence": [],
        "calibration": [],
        "highlights": {"best_sport": None, "best_market": None, "worst_market": None},
    }
