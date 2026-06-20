"""Parlay Learning Loop.

Tracks every parlay shown to the user, settles them when their legs finish,
and feeds the WIN RATE per (sport, market_family) **in parlay context** back
into the optimizer so future builds favour combinations that historically
cash and avoid ones that historically lose.

Collections:
  • parlay_history   — every distinct parlay signature shown to the user
  • parlay_synergy   — aggregated (sport, family) win rates in parlay context

Flow:
  1. `record_parlay_shown(db, parlay_card)` is called from the API endpoint
     whenever the optimizer returns a card. Deduplicates by signature.
  2. `settle_parlays(db)` runs in the nightly settlement loop. For every
     parlay whose legs are all settled, mark it WON (all legs won), LOST
     (any lost), or PUSH (any push and no losses).
  3. `compute_synergy_map(db)` aggregates settled parlays into a
     `(sport, market_family) → {hits, total, hit_rate}` map. Returned by
     `get_synergy_map()` to be merged into the optimizer's scoring.

Used by parlay_optimizer.score_leg() as an additional bonus/penalty term
keyed off the candidate leg's (sport, family). Picks that look strong on
paper but historically tank our parlays get penalised.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import logging
from typing import Any

from parlay_optimizer import _market_family

logger = logging.getLogger("lockscore.parlay_learning")

HISTORY_COLL = "parlay_history"
SYNERGY_COLL = "parlay_synergy"
LOOKBACK_DAYS = 60
MIN_PARLAY_SAMPLE = 3  # need ≥3 settled parlays before trusting a (sport,family) signal


def _signature(legs: list[dict]) -> str:
    """Deterministic signature for a parlay (order-independent leg-ID tuple)."""
    ids = sorted([str(L.get("id") or L.get("pick_id") or "") for L in legs])
    return hashlib.sha1("|".join(ids).encode()).hexdigest()[:24]


async def record_parlay_shown(db, parlay_card: dict, *,
                              mode: str, sport_mode: str) -> None:
    """Persist (or no-op-if-exists) a parlay that the optimizer just emitted.

    Cheap: ~1 ms per call. We dedupe by signature so refreshing the same
    parlay 50 times = 1 DB row, not 50.
    """
    try:
        legs = parlay_card.get("legs") or []
        if len(legs) < 2:
            return
        sig = _signature(legs)
        now = _dt.datetime.now(_dt.timezone.utc).isoformat()
        leg_summary = [
            {
                "pick_id":       L.get("id") or L.get("pick_id"),
                "sport":         L.get("sport"),
                "market":        L.get("market"),
                "market_family": _market_family(L.get("market") or ""),
                "event":         L.get("event"),
                "lock_score":    L.get("lock_score"),
                "win_probability": L.get("win_probability"),
            }
            for L in legs
        ]
        await db[HISTORY_COLL].update_one(
            {"signature": sig},
            {
                "$setOnInsert": {
                    "signature":  sig,
                    "legs":       leg_summary,
                    "leg_count":  len(legs),
                    "mode":       mode,
                    "sport_mode": sport_mode,
                    "status":     "pending",
                    "shown_at":   now,
                    "survival_pct": parlay_card.get("survival_pct"),
                    "combined_american_odds": parlay_card.get("combined_american_odds"),
                },
                "$inc": {"shown_count": 1},
                "$set": {"last_shown_at": now},
            },
            upsert=True,
        )
    except Exception as e:
        logger.warning("record_parlay_shown failed: %s", e)


async def settle_parlays(db) -> dict:
    """Finalise any pending parlay whose legs are all settled.

    Returns a summary dict. Safe to call repeatedly.
    """
    summary = {"checked": 0, "settled": 0, "won": 0, "lost": 0, "push": 0}
    cutoff = (_dt.datetime.now(_dt.timezone.utc)
              - _dt.timedelta(days=LOOKBACK_DAYS)).isoformat()
    async for parlay in db[HISTORY_COLL].find(
        {"status": "pending", "shown_at": {"$gte": cutoff}}
    ):
        summary["checked"] += 1
        leg_ids = [L.get("pick_id") for L in (parlay.get("legs") or []) if L.get("pick_id")]
        if not leg_ids:
            continue
        # Get current status of every leg.
        rows = await db.picks.find(
            {"id": {"$in": leg_ids}},
            {"_id": 0, "id": 1, "status": 1},
        ).to_list(length=len(leg_ids))
        statuses = {r["id"]: (r.get("status") or "pending") for r in rows}
        # If any leg still pending → skip
        if any(statuses.get(lid, "pending") == "pending" for lid in leg_ids):
            continue
        if any(statuses.get(lid) == "lost" for lid in leg_ids):
            outcome = "lost"
            summary["lost"] += 1
        elif all(statuses.get(lid) == "won" for lid in leg_ids):
            outcome = "won"
            summary["won"] += 1
        else:
            outcome = "push"
            summary["push"] += 1
        await db[HISTORY_COLL].update_one(
            {"signature": parlay["signature"]},
            {"$set": {
                "status": outcome,
                "settled_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                "leg_statuses": statuses,
            }},
        )
        summary["settled"] += 1
    return summary


async def compute_synergy_map(db) -> dict:
    """Aggregate per-(sport, market_family) parlay performance.

    Returns shape: {(sport_lower, family): {hit_rate: 0..1, n: int}}.

    A leg counts as a "parlay hit" when its parlay cashed. If a leg appears
    in 5 settled parlays and 4 of those won, hit_rate = 4/5 = 0.80.

    We multiply by leg-count fraction (e.g. weight a 10-leg parlay leg less
    than a 3-leg parlay leg) so single-bet-equivalent picks dominate.
    """
    cutoff = (_dt.datetime.now(_dt.timezone.utc)
              - _dt.timedelta(days=LOOKBACK_DAYS)).isoformat()
    agg: dict[tuple[str, str], dict] = {}
    async for p in db[HISTORY_COLL].find(
        {"status": {"$in": ["won", "lost", "push"]},
         "shown_at": {"$gte": cutoff}},
        {"_id": 0, "legs": 1, "status": 1, "leg_count": 1},
    ):
        legs = p.get("legs") or []
        if not legs:
            continue
        weight = 1.0 / max(1, len(legs))  # 5-leg parlay = 0.2 weight per leg
        for L in legs:
            sport = (L.get("sport") or "").lower()
            family = L.get("market_family") or _market_family(L.get("market") or "")
            if not family or family == "other":
                continue
            key = (sport, family)
            row = agg.setdefault(key, {"hit_weight": 0.0, "total_weight": 0.0, "hits": 0, "n": 0})
            row["total_weight"] += weight
            row["n"] += 1
            if p["status"] == "won":
                row["hit_weight"] += weight
                row["hits"] += 1
    # Persist + return
    out: dict[tuple[str, str], dict] = {}
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    for key, row in agg.items():
        hit_rate = row["hit_weight"] / row["total_weight"] if row["total_weight"] > 0 else 0.0
        result = {
            "sport":     key[0],
            "family":    key[1],
            "n":         row["n"],
            "hits":      row["hits"],
            "hit_rate":  round(hit_rate, 3),
            "updated_at": now,
        }
        out[key] = {"hit_rate": hit_rate, "n": row["n"]}
        await db[SYNERGY_COLL].update_one(
            {"sport": key[0], "family": key[1]},
            {"$set": result},
            upsert=True,
        )
    return out


async def load_synergy_map(db) -> dict:
    """Load the cached synergy map (without recomputing). Used by the parlay
    endpoint to avoid re-aggregating on every request."""
    out: dict[tuple[str, str], dict] = {}
    async for row in db[SYNERGY_COLL].find({}, {"_id": 0}):
        sport = (row.get("sport") or "").lower()
        family = row.get("family") or "other"
        out[(sport, family)] = {
            "hit_rate": float(row.get("hit_rate") or 0),
            "n":        int(row.get("n") or 0),
        }
    return out


def synergy_bonus(synergy_map: dict, sport: str, family: str) -> float:
    """Return a -15…+15 bonus to be added to the leg's composite score.

    Above 60 % parlay hit rate → positive bonus (well-correlated winner).
    Below 35 % → negative penalty (consistent parlay-killer).
    Centred at 50 % so neutral families don't move.
    Only applies when n ≥ MIN_PARLAY_SAMPLE — avoid noise on cold rows.
    """
    if not synergy_map:
        return 0.0
    row = synergy_map.get((sport.lower(), family))
    if not row or row["n"] < MIN_PARLAY_SAMPLE:
        return 0.0
    hr = row["hit_rate"]
    # Map [0, 1] hit rate → [-15, +15] centred at 0.5.
    bonus = (hr - 0.5) * 30.0
    return max(-15.0, min(15.0, bonus))
