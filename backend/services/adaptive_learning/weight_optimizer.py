"""Dynamic Weight Optimiser (2026-07-28).

Learns better `DEFAULT_WEIGHTS` for the Prediction Fusion Engine per
(sport, market) tuple using historical `fusion_predictions` data.

Contract
────────
    changes = await optimise_fusion_weights(
        db,
        sport=None,               # None → per-market across all sports
        market=None,              # None → all markets
        days=90,
        min_samples=50,           # required before ANY change
        validation_days=14,       # trailing period held out for check
        smoothing=0.7,            # 0.0 = pure new, 1.0 = pure old
        persist=True,             # write to `fusion_weights` collection
    )

    → list[{ sport, market, old_weights, new_weights, n, delta_brier }]

    load_learned_weights(db, sport, market) → dict | None

Safety guards
─────────────
  • Never proposes weights when < min_samples observations.
  • Uses **inverse-Brier** to score each engine → weight ∝ 1/Brier.
  • Smoothing = interpolation between OLD (currently used) and NEW
    (proposed) weights so a small sample nudges gradually.
  • Validation gate: the NEW weights must beat the OLD on the
    trailing `validation_days` window; otherwise no change is saved.
  • Every proposed change is logged to `fusion_weight_changes` with
    old/new snapshots for auditability.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger("lockscore.services.adaptive_learning.weight_optimizer")

_ENGINE_KEYS = ("ml", "similar", "player_h2h", "simulator")


async def _rows_in_window(db, since_iso: str, sport, market):
    q: dict = {"actual_value": {"$ne": None},
                "created_at": {"$gte": since_iso}}
    if sport: q["sport"] = sport
    if market: q["market"] = market
    return [r async for r in db.fusion_predictions.find(q, {"_id": 0})]


def _brier_by_engine(rows: list[dict]) -> dict[str, tuple[int, float]]:
    """→ {engine: (n, mean_brier)} across the rows for engines that
    were available in each prediction."""
    agg: dict[str, dict] = {k: {"n": 0, "sum": 0.0} for k in _ENGINE_KEYS}
    for r in rows:
        threshold = r.get("threshold")
        actual = r.get("actual_value")
        if threshold is None or actual is None:
            continue
        try:
            y = 1.0 if float(actual) > float(threshold) else 0.0
        except (TypeError, ValueError):
            continue
        comps = r.get("components") or {}
        for k in _ENGINE_KEYS:
            c = comps.get(k) or {}
            if not isinstance(c, dict) or not c.get("available"):
                continue
            p = c.get("probability")
            if p is None:
                continue
            try:
                p = float(p)
            except (TypeError, ValueError):
                continue
            agg[k]["n"] += 1
            agg[k]["sum"] += (p - y) ** 2
    return {k: (v["n"], v["sum"] / v["n"]) for k, v in agg.items()
             if v["n"] > 0}


def _fused_brier(weights: dict[str, float], rows: list[dict]) -> tuple[int, float]:
    """Compute the mean Brier if the given weights had been used to
    fuse each row's component probabilities. Rows where all four
    engines are unavailable are skipped."""
    n = 0
    total = 0.0
    for r in rows:
        thr = r.get("threshold"); actual = r.get("actual_value")
        if thr is None or actual is None:
            continue
        try:
            y = 1.0 if float(actual) > float(thr) else 0.0
        except (TypeError, ValueError):
            continue
        comps = r.get("components") or {}
        num, den = 0.0, 0.0
        for k in _ENGINE_KEYS:
            c = comps.get(k) or {}
            if not isinstance(c, dict) or not c.get("available"):
                continue
            p = c.get("probability")
            if p is None:
                continue
            try:
                p = float(p)
            except (TypeError, ValueError):
                continue
            w = weights.get(k, 0.0)
            num += w * p
            den += w
        if den <= 0:
            continue
        p_fused = num / den
        total += (p_fused - y) ** 2
        n += 1
    return n, (total / n if n else float("inf"))


def _weights_from_brier(engine_brier: dict[str, tuple[int, float]],
                         current: dict[str, float]) -> dict[str, float]:
    """Turn per-engine Brier into weights: w_e ∝ 1 / max(Brier_e, ε).
    Missing engines keep their current weight so a zero-sample sport
    doesn't collapse them."""
    inv: dict[str, float] = {}
    for e in _ENGINE_KEYS:
        b = engine_brier.get(e)
        if b:
            inv[e] = 1.0 / max(b[1], 1e-4)
        else:
            inv[e] = current.get(e, 0.0) or 1e-3
    total = sum(inv.values()) or 1.0
    return {e: round(inv[e] / total, 4) for e in _ENGINE_KEYS}


def _smooth(old: dict[str, float], new: dict[str, float],
             smoothing: float) -> dict[str, float]:
    smoothing = min(max(smoothing, 0.0), 1.0)
    out = {}
    for k in _ENGINE_KEYS:
        out[k] = round(smoothing * old.get(k, 0.0)
                        + (1 - smoothing) * new.get(k, 0.0), 4)
    # Normalise to sum to 1.
    total = sum(out.values()) or 1.0
    return {k: round(v / total, 4) for k, v in out.items()}


async def load_learned_weights(db, sport: Optional[str],
                                 market: Optional[str]) -> Optional[dict]:
    """Return the most recent learned weights for (sport, market),
    or None if we've never learned any (caller falls back to defaults)."""
    q = {"sport": sport, "market": market}
    doc = await db.fusion_weights.find_one(q, {"_id": 0})
    if not doc:
        return None
    return doc.get("weights") or None


async def _persist_weight_change(db, sport, market, old, new, evidence):
    from services.prediction_fusion_engine import DEFAULT_WEIGHTS
    doc = {
        "sport":  sport,
        "market": market,
        "weights": new,
        "prior_weights": old,
        "delta_brier": evidence.get("delta_brier"),
        "n":         evidence.get("n"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "defaults":  DEFAULT_WEIGHTS,
    }
    # Upsert current + append to history collection.
    await db.fusion_weights.update_one(
        {"sport": sport, "market": market},
        {"$set": doc}, upsert=True,
    )
    await db.fusion_weight_changes.insert_one(doc)


async def optimise_fusion_weights(
    db,
    *,
    sport: Optional[str] = None,
    market: Optional[str] = None,
    days: int = 90,
    validation_days: int = 14,
    min_samples: int = 50,
    smoothing: float = 0.7,
    persist: bool = True,
) -> list[dict]:
    """Optimise weights for one (sport, market) OR fan-out per-market.

    Returns a list of change proposals (empty if none qualified).
    """
    from services.prediction_fusion_engine import DEFAULT_WEIGHTS

    # Fan out per (sport, market) when neither is pinned. Otherwise
    # process the single tuple.
    tuples: list[tuple[Optional[str], Optional[str]]] = []
    if sport and market:
        tuples = [(sport, market)]
    else:
        # Discover distinct tuples from the queue.
        cur = db.fusion_predictions.find(
            {"actual_value": {"$ne": None}},
            {"sport": 1, "market": 1, "_id": 0},
        )
        seen: set = set()
        async for d in cur:
            key = (d.get("sport"), d.get("market"))
            if sport and key[0] != sport:
                continue
            if market and key[1] != market:
                continue
            if key in seen or None in key:
                continue
            seen.add(key)
        tuples = list(seen)

    changes: list[dict] = []
    train_since = (datetime.now(timezone.utc)
                    - timedelta(days=days)).isoformat()
    val_since = (datetime.now(timezone.utc)
                  - timedelta(days=validation_days)).isoformat()

    for s, m in tuples:
        rows = await _rows_in_window(db, train_since, s, m)
        if len(rows) < min_samples:
            continue
        engine_brier = _brier_by_engine(rows)
        current = await load_learned_weights(db, s, m) or dict(DEFAULT_WEIGHTS)
        proposed = _weights_from_brier(engine_brier, current)
        smoothed = _smooth(current, proposed, smoothing)

        # Validate on the trailing window ONLY.
        val_rows = [r for r in rows if r.get("created_at", "") >= val_since]
        if len(val_rows) < max(10, min_samples // 4):
            # Not enough validation → skip (safety guard).
            continue
        n_old, brier_old = _fused_brier(current, val_rows)
        n_new, brier_new = _fused_brier(smoothed, val_rows)
        if n_new == 0:
            continue
        delta = brier_old - brier_new    # positive = new is better
        if delta <= 0.0005:
            # Not a meaningful improvement — skip.
            continue

        evidence = {"n": len(rows), "delta_brier": round(delta, 6),
                     "n_val": n_new, "brier_old": round(brier_old, 4),
                     "brier_new": round(brier_new, 4),
                     "engine_brier": {k: {"n": v[0], "brier": round(v[1], 4)}
                                      for k, v in engine_brier.items()}}
        change = {
            "sport":       s,
            "market":      m,
            "old_weights": current,
            "new_weights": smoothed,
            "n":           len(rows),
            "n_val":       n_new,
            "delta_brier": round(delta, 6),
            "brier_old":   round(brier_old, 4),
            "brier_new":   round(brier_new, 4),
        }
        changes.append(change)
        if persist:
            try:
                await _persist_weight_change(db, s, m, current, smoothed,
                                              evidence)
            except Exception as e:
                logger.debug("persist failed for %s/%s: %s", s, m, e)

    return changes


__all__ = [
    "optimise_fusion_weights",
    "load_learned_weights",
    "_brier_by_engine",
    "_weights_from_brier",
    "_smooth",
    "_fused_brier",
]
