"""Isolated learning buckets — analytics-only (no prediction impact).

Each settled pick is classified into a unique bucket keyed by
``(sport, market_type, prop_type)``. Learning is NEVER shared across:

* sports (NBA Points stats never influence NFL pass-yard stats)
* market types (Moneyline ROI never mixes with Spread ROI)
* prop types (NBA Points ≠ NBA Rebounds ≠ NBA Assists)

Per-spec bucket structure:

============  ============================================================
SPORT         BUCKETS (market_type | prop_type)
------------  ------------------------------------------------------------
NBA           ml, spread, totals, props_points, props_rebounds,
              props_assists, props_3pt
NFL           ml, spread, totals, props_pass_yds, props_rush_yds,
              props_recs, props_tds
MLB           ml, spread, totals, props_hits  # BATTERS ONLY
Soccer        ml, draw, btts, totals, props_goalscorer, props_shots
Tennis        match_winner, sets, games
KBO           ml, spread, totals, props_hits  # BATTERS ONLY
UFC           ml, method, rounds
============  ============================================================

Per-bucket stored metrics
-------------------------
* ``n``                — completed predictions count
* ``wins / losses / pushes``
* ``accuracy``         — wins / (wins + losses)
* ``roi_pct``          — units-profit / units-staked × 100
* ``avg_book_odds``    — American odds average
* ``avg_confidence``   — model win-prob average (raw, untouched)
* ``avg_clv``          — closing-line value (where stored)
* ``peak_roi``         — historical max ROI seen
* ``frozen``           — True if current ROI < peak_roi - 10%
* ``last_adjustment``  — computed delta (±5% max, NOT yet applied to model)
* ``ready_to_adjust``  — True only when n >= MIN_SAMPLES (100) and not frozen
* ``updated_at``       — ISO timestamp

Rollback
--------
Every recompute writes a versioned snapshot. Keeps last 5 snapshots in
``learning_bucket_snapshots`` collection. Rollback by promoting an older
snapshot to current.

This module is ANALYTICS-ONLY in this release. The ``last_adjustment`` field
is computed but never read by the prediction pipeline. The infrastructure
is ready-to-activate when the user flips the switch.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("lockscore.buckets")

# ──────────────────────────────────────────────────────────────────────────
# CONFIG (per spec)
# ──────────────────────────────────────────────────────────────────────────

MIN_SAMPLES_FOR_ADJUSTMENT = 100   # Per spec: "Minimum 100 completed predictions"
MAX_ADJUSTMENT_PCT = 0.05          # Per spec: "Maximum adjustment 5% per cycle"
FREEZE_ROI_DROP_PCT = 0.10         # Freeze when current ROI < peak - 10pp
SNAPSHOT_RETENTION = 5             # Keep last N snapshots for rollback


# ──────────────────────────────────────────────────────────────────────────
# CLASSIFIER — pick → (sport, market_type, prop_type)
# ──────────────────────────────────────────────────────────────────────────

def classify_pick(pick: dict) -> tuple[str, str, str | None]:
    """Return (sport, market_type, prop_type) for a pick. ``prop_type`` is
    None for non-prop markets. Picks that don't fit any spec bucket return
    ('other', 'other', None) so callers can ignore them gracefully.
    """
    sport = (pick.get("sport") or "").strip()
    market = (pick.get("market") or "").lower()

    # ── NBA ──────────────────────────────────────────────────────────────
    if sport == "NBA":
        if "moneyline" in market: return ("NBA", "ml", None)
        if "spread" in market: return ("NBA", "spread", None)
        if "total" in market and ("over" in market or "under" in market) and "player" not in market:
            return ("NBA", "totals", None)
        if "rebound" in market: return ("NBA", "props", "rebounds")
        if "assist" in market: return ("NBA", "props", "assists")
        if "3-point" in market or "3pt" in market or "three point" in market or "threes" in market:
            return ("NBA", "props", "3pt")
        if "point" in market: return ("NBA", "props", "points")

    # ── NFL ──────────────────────────────────────────────────────────────
    if sport == "NFL":
        if "moneyline" in market: return ("NFL", "ml", None)
        if "spread" in market: return ("NFL", "spread", None)
        if "total" in market and "player" not in market: return ("NFL", "totals", None)
        if "pass" in market and ("yard" in market or "yds" in market): return ("NFL", "props", "pass_yds")
        if "rush" in market and ("yard" in market or "yds" in market): return ("NFL", "props", "rush_yds")
        if "reception" in market or "recs" in market: return ("NFL", "props", "receptions")
        if "td" in market or "touchdown" in market: return ("NFL", "props", "tds")

    # ── MLB ──────────────────────────────────────────────────────────────
    # MLB: BATTERS ONLY for props. Pitcher props excluded per spec.
    if sport == "MLB":
        if "moneyline" in market: return ("MLB", "ml", None)
        if "spread" in market or "run line" in market: return ("MLB", "spread", None)
        # Game totals (Total Runs Over/Under N.N) — distinct from "total bases" prop
        if "total runs" in market or (
            "total" in market and ("over" in market or "under" in market)
            and "bases" not in market and "hits" not in market and "player" not in market
        ):
            return ("MLB", "totals", None)
        # Pitcher props: explicit exclusion (strikeouts, outs recorded, earned runs)
        pitcher_kw = ("strikeout", "outs recorded", "earned run", "pitch", "k's")
        if any(kw in market for kw in pitcher_kw):
            return ("MLB", "other_pitcher", None)  # tagged but bucketed separately
        # Batter hits
        if "hit" in market or "total bases" in market:
            return ("MLB", "props", "hits")

    # ── Soccer ───────────────────────────────────────────────────────────
    if sport == "Soccer":
        if "moneyline" in market and "draw" not in market: return ("Soccer", "ml", None)
        if "win or draw" in market or "double chance" in market or "draw no bet" in market:
            return ("Soccer", "draw", None)
        if "both teams to score" in market or "btts" in market:
            return ("Soccer", "btts", None)
        if ("total goals" in market or ("total" in market and ("over" in market or "under" in market))) \
                and "player" not in market:
            return ("Soccer", "totals", None)
        if "anytime goal scorer" in market or "first goal scorer" in market \
                or "to score or assist" in market or "to score" in market:
            return ("Soccer", "props", "goalscorer")
        if "shot" in market: return ("Soccer", "props", "shots")

    # ── Tennis ───────────────────────────────────────────────────────────
    if sport == "Tennis":
        if "moneyline" in market or "match winner" in market or "to win" in market:
            return ("Tennis", "match_winner", None)
        if "spread" in market:
            # Tennis spreads are game-margin bets (e.g. "Player -3.5 Spread")
            # Belongs in the "games" bucket per spec.
            return ("Tennis", "games", None)
        if "set" in market and "game" not in market:
            return ("Tennis", "sets", None)
        if "game" in market:
            return ("Tennis", "games", None)

    # ── KBO ──────────────────────────────────────────────────────────────
    if sport == "KBO":
        if "moneyline" in market: return ("KBO", "ml", None)
        if "spread" in market or "run line" in market: return ("KBO", "spread", None)
        # Game totals (Total Runs Over/Under N.N)
        if "total runs" in market or (
            "total" in market and ("over" in market or "under" in market)
            and "bases" not in market and "hits" not in market and "player" not in market
        ):
            return ("KBO", "totals", None)
        # KBO pitcher exclusion (same as MLB)
        pitcher_kw = ("strikeout", "pitch", "earned run")
        if any(kw in market for kw in pitcher_kw):
            return ("KBO", "other_pitcher", None)
        if "hit" in market or "total bases" in market:
            return ("KBO", "props", "hits")

    # ── UFC ──────────────────────────────────────────────────────────────
    if sport == "UFC":
        if "moneyline" in market or "to win" in market:
            return ("UFC", "ml", None)
        if "method of victory" in market or "by ko" in market or "by submission" in market or "by decision" in market:
            return ("UFC", "method", None)
        if "round" in market and ("over" in market or "under" in market):
            return ("UFC", "rounds", None)

    return ("other", "other", None)


def _bucket_key(sport: str, mtype: str, ptype: str | None) -> str:
    """Stable key for storage. e.g. 'NBA|props|points', 'MLB|ml|na'."""
    return f"{sport}|{mtype}|{ptype or 'na'}"


# ──────────────────────────────────────────────────────────────────────────
# RECOMPUTE — settled picks → per-bucket metrics
# ──────────────────────────────────────────────────────────────────────────

async def recompute_buckets(db) -> dict:
    """Scan all settled picks, classify into isolated buckets, compute
    metrics, persist to ``learning_buckets`` collection, snapshot for
    rollback. Returns a summary dict.

    SAFETY: never writes to or reads from collections that influence
    prediction generation. Pure analytics.
    """
    # ── Load existing buckets to preserve peak_roi across runs ────────────
    existing_docs = await db.learning_buckets.find().to_list(length=10_000)
    existing: dict[str, dict] = {d["key"]: d for d in existing_docs}

    # ── Scan all settled picks (in past 365 days) ─────────────────────────
    cursor = db.picks.find(
        {"status": {"$in": ["won", "lost", "push"]}},
        {"_id": 0, "sport": 1, "market": 1, "status": 1, "book_odds": 1,
         "win_probability": 1, "closing_odds": 1, "units_profit": 1},
    )
    raw_buckets: dict[str, dict] = {}
    skipped = 0
    async for p in cursor:
        sport, mtype, ptype = classify_pick(p)
        if sport == "other":
            skipped += 1
            continue
        key = _bucket_key(sport, mtype, ptype)
        b = raw_buckets.setdefault(key, {
            "sport": sport, "market_type": mtype, "prop_type": ptype,
            "n": 0, "wins": 0, "losses": 0, "pushes": 0,
            "units_staked": 0.0, "units_profit": 0.0,
            "book_odds_sum": 0.0, "book_odds_n": 0,
            "conf_sum": 0.0, "conf_n": 0,
            "clv_sum": 0.0, "clv_n": 0,
        })
        outcome = p.get("status")  # 'won'/'lost'/'push' lives in the `status` field
        odds = p.get("book_odds")
        wp = p.get("win_probability")
        closing = p.get("closing_odds")
        units_p = float(p.get("units_profit") or 0)

        b["n"] += 1
        if outcome == "won":
            b["wins"] += 1
        elif outcome == "lost":
            b["losses"] += 1
        elif outcome == "push":
            b["pushes"] += 1

        # ROI: 1 unit staked per pick (or honour stored units_profit)
        b["units_staked"] += 1.0
        b["units_profit"] += units_p

        if isinstance(odds, (int, float)):
            b["book_odds_sum"] += float(odds)
            b["book_odds_n"] += 1
        if isinstance(wp, (int, float)):
            b["conf_sum"] += float(wp)
            b["conf_n"] += 1
        if isinstance(closing, (int, float)) and isinstance(odds, (int, float)):
            # CLV: difference between our taken odds and closing odds.
            # Positive = we beat the close. Approximate in American points.
            b["clv_sum"] += float(odds) - float(closing)
            b["clv_n"] += 1

    # ── Finalise: compute derived metrics + freeze/adjustment rules ──────
    now_iso = datetime.now(timezone.utc).isoformat()
    finals: list[dict] = []
    for key, b in raw_buckets.items():
        decided = b["wins"] + b["losses"]
        accuracy = (b["wins"] / decided) if decided > 0 else 0.0
        roi_pct = (b["units_profit"] / b["units_staked"] * 100.0) if b["units_staked"] > 0 else 0.0
        avg_odds = (b["book_odds_sum"] / b["book_odds_n"]) if b["book_odds_n"] else None
        avg_conf = (b["conf_sum"] / b["conf_n"]) if b["conf_n"] else None
        avg_clv = (b["clv_sum"] / b["clv_n"]) if b["clv_n"] else None

        prev = existing.get(key, {})
        peak_roi = max(roi_pct, float(prev.get("peak_roi") or roi_pct))

        # Freeze rule: current ROI < peak ROI - 10 pp → freeze adjustments.
        frozen = (peak_roi - roi_pct) >= (FREEZE_ROI_DROP_PCT * 100.0)
        ready = (b["n"] >= MIN_SAMPLES_FOR_ADJUSTMENT) and (not frozen)

        # Last adjustment: clamped ±5%. Computed but NOT applied to predictions.
        # Direction: positive if bucket beating break-even (52.4% accuracy),
        # negative if losing. Magnitude: capped at MAX_ADJUSTMENT_PCT.
        edge_vs_break_even = accuracy - 0.524 if accuracy else 0.0
        last_adjustment = max(-MAX_ADJUSTMENT_PCT,
                             min(MAX_ADJUSTMENT_PCT, edge_vs_break_even * 0.5))

        finals.append({
            "key": key,
            "sport": b["sport"],
            "market_type": b["market_type"],
            "prop_type": b["prop_type"],
            "n": b["n"],
            "wins": b["wins"],
            "losses": b["losses"],
            "pushes": b["pushes"],
            "accuracy": round(accuracy * 100, 2),
            "roi_pct": round(roi_pct, 2),
            "units_profit": round(b["units_profit"], 2),
            "avg_book_odds": round(avg_odds, 1) if avg_odds is not None else None,
            "avg_confidence": round(avg_conf, 1) if avg_conf is not None else None,
            "avg_clv": round(avg_clv, 1) if avg_clv is not None else None,
            "peak_roi": round(peak_roi, 2),
            "frozen": frozen,
            "ready_to_adjust": ready,
            "last_adjustment": round(last_adjustment, 4),
            "updated_at": now_iso,
        })

    # Sort for stable output: by sport then n desc
    finals.sort(key=lambda r: (r["sport"], -r["n"]))

    # ── Snapshot current state BEFORE overwriting (for rollback) ─────────
    if existing_docs:
        snapshot = {
            "ts": now_iso,
            "buckets": existing_docs,
        }
        await db.learning_bucket_snapshots.insert_one(snapshot)
        # Keep only last SNAPSHOT_RETENTION snapshots
        old_ids = await db.learning_bucket_snapshots.find(
            {}, {"_id": 1}
        ).sort("ts", -1).skip(SNAPSHOT_RETENTION).to_list(length=100)
        if old_ids:
            await db.learning_bucket_snapshots.delete_many({"_id": {"$in": [d["_id"] for d in old_ids]}})

    # ── Replace current state with fresh recompute ───────────────────────
    await db.learning_buckets.delete_many({})
    if finals:
        await db.learning_buckets.insert_many(finals, ordered=False)

    summary = {
        "buckets": len(finals),
        "settled_total": sum(b["n"] for b in finals),
        "ready_to_adjust": sum(1 for b in finals if b["ready_to_adjust"]),
        "frozen": sum(1 for b in finals if b["frozen"]),
        "skipped": skipped,
        "updated_at": now_iso,
    }
    logger.info("learning_buckets recomputed: %s", summary)
    return summary


# ──────────────────────────────────────────────────────────────────────────
# ROLLBACK — promote an older snapshot back to current
# ──────────────────────────────────────────────────────────────────────────

async def rollback_buckets(db, snapshot_index: int = 1) -> dict:
    """Restore the Nth-most-recent snapshot as the current buckets state.

    ``snapshot_index=1`` = previous version, =2 = two versions ago, etc.
    Returns the summary of restored buckets.
    """
    snaps = await db.learning_bucket_snapshots.find().sort("ts", -1) \
        .skip(snapshot_index - 1).limit(1).to_list(length=1)
    if not snaps:
        return {"rolled_back": False, "reason": f"No snapshot at index {snapshot_index}"}
    snap = snaps[0]
    await db.learning_buckets.delete_many({})
    if snap.get("buckets"):
        # Strip _id from snapshot docs so MongoDB regenerates them
        clean = [{k: v for k, v in d.items() if k != "_id"} for d in snap["buckets"]]
        if clean:
            await db.learning_buckets.insert_many(clean, ordered=False)
    return {
        "rolled_back": True,
        "snapshot_ts": snap.get("ts"),
        "restored_buckets": len(snap.get("buckets") or []),
    }


# ──────────────────────────────────────────────────────────────────────────
# PUBLIC: fetch current bucket state for the analytics endpoint
# ──────────────────────────────────────────────────────────────────────────

async def get_buckets(db) -> dict:
    """Return current bucket state grouped by sport for analytics consumers."""
    docs = await db.learning_buckets.find({}, {"_id": 0}).to_list(length=1000)
    by_sport: dict[str, list[dict]] = {}
    for d in docs:
        by_sport.setdefault(d["sport"], []).append(d)
    # Get latest snapshot ts so caller can show "last updated"
    last = await db.learning_bucket_snapshots.find().sort("ts", -1).limit(1).to_list(length=1)
    return {
        "total_buckets": len(docs),
        "by_sport": by_sport,
        "snapshot_count": await db.learning_bucket_snapshots.count_documents({}),
        "last_snapshot_ts": last[0]["ts"] if last else None,
    }
