"""Prediction Calibration System (2026-07-28).

Consumes graded rows from `fusion_predictions` and produces reliability
diagnostics answering: **"When the fused prediction says X% over, does
that bucket actually win X% of the time?"**

Outputs
───────
    build_calibration_report(db, sport=None, market=None,
                              confidence=None, days=90)

    → {
        "n_graded":              int,
        "brier_score":           float,        # overall Brier
        "accuracy":              float,        # fused accuracy at 0.50
        "log_loss":              float,
        "calibration_curve":     [ { bucket, n, expected, observed, delta } ...],
        "by_sport":              { sport: {n, accuracy, brier} },
        "by_market":             { market: {n, accuracy, brier} },
        "by_confidence_tier":    { high|medium|low: {n, accuracy, brier} },
        "by_engine":             { ml|similar|player_h2h|simulator|fused:
                                   {n, accuracy, brier, mean_absolute_error} },
        "reliability_flag":      "ok|over_confident|under_confident|insufficient",
        "generated_at":          str ISO,
      }

Zero writes. Never raises. Returns an empty-but-well-formed payload
when the queue has no graded rows in the window.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone, timedelta
from typing import Optional


def _safe_log(x: float) -> float:
    return math.log(max(min(x, 1 - 1e-12), 1e-12))


def _brier(p: float, y: float) -> float:
    return (p - y) ** 2


def _log_loss(p: float, y: float) -> float:
    return -(y * _safe_log(p) + (1 - y) * _safe_log(1 - p))


def _bucket_edges(n: int = 10) -> list[float]:
    return [i / n for i in range(n + 1)]


def _bucket_of(p: float, edges: list[float]) -> int:
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        if lo <= p < hi:
            return i
    return len(edges) - 2


def _reliability_flag(curve: list[dict]) -> str:
    """Coarse global reliability label based on average signed delta.

    Positive delta = predicted > observed → over_confident.
    Negative delta = predicted < observed → under_confident.
    """
    if not curve:
        return "insufficient"
    total_n = sum(b["n"] for b in curve)
    if total_n < 30:
        return "insufficient"
    signed_delta = sum((b["expected"] - b["observed"]) * b["n"]
                        for b in curve) / total_n
    if abs(signed_delta) < 3.0:
        return "ok"
    return "over_confident" if signed_delta > 0 else "under_confident"


async def build_calibration_report(
    db,
    *,
    sport: Optional[str] = None,
    market: Optional[str] = None,
    confidence: Optional[str] = None,
    days: int = 90,
    n_buckets: int = 10,
) -> dict:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    q: dict = {"actual_value": {"$ne": None},
                "created_at": {"$gte": since}}
    if sport:      q["sport"] = sport
    if market:     q["market"] = market
    if confidence: q["confidence"] = confidence

    edges = _bucket_edges(n_buckets)
    buckets: list[dict] = [
        {"bucket_lo": edges[i], "bucket_hi": edges[i + 1],
          "n": 0, "sum_p": 0.0, "sum_y": 0.0}
        for i in range(n_buckets)
    ]
    per_sport: dict[str, dict] = {}
    per_market: dict[str, dict] = {}
    per_conf: dict[str, dict] = {}
    per_engine: dict[str, dict] = {
        n: {"n": 0, "correct": 0, "brier_sum": 0.0, "err_sum": 0.0}
        for n in ("ml", "similar", "player_h2h", "simulator", "fused")
    }

    total = 0
    correct = 0
    brier_sum = 0.0
    log_loss_sum = 0.0

    async for d in db.fusion_predictions.find(q, {"_id": 0}):
        threshold = d.get("threshold")
        actual = d.get("actual_value")
        p_fused = d.get("final_probability")
        if threshold is None or actual is None or p_fused is None:
            continue
        try:
            actual = float(actual)
            p = float(p_fused)
            thr = float(threshold)
        except (TypeError, ValueError):
            continue
        y = 1.0 if actual > thr else 0.0
        total += 1
        pred = 1.0 if p >= 0.50 else 0.0
        if pred == y:
            correct += 1
        brier_sum += _brier(p, y)
        log_loss_sum += _log_loss(p, y)

        b = _bucket_of(p, edges)
        buckets[b]["n"] += 1
        buckets[b]["sum_p"] += p
        buckets[b]["sum_y"] += y

        s = d.get("sport") or "?"
        m = d.get("market") or "?"
        c = d.get("confidence") or "?"
        for group, key in ((per_sport, s), (per_market, m), (per_conf, c)):
            row = group.setdefault(key, {"n": 0, "correct": 0,
                                          "brier_sum": 0.0})
            row["n"] += 1
            row["brier_sum"] += _brier(p, y)
            if pred == y:
                row["correct"] += 1

        # Per-engine accuracy + error.
        comps = d.get("components") or {}
        for name in ("ml", "similar", "player_h2h", "simulator"):
            c = comps.get(name) or {}
            if not isinstance(c, dict) or not c.get("available"):
                continue
            p_e = c.get("probability")
            if p_e is None:
                continue
            try:
                p_e = float(p_e)
            except (TypeError, ValueError):
                continue
            per_engine[name]["n"] += 1
            per_engine[name]["brier_sum"] += _brier(p_e, y)
            per_engine[name]["err_sum"] += abs(p_e - y)
            if (p_e >= 0.5) == (y >= 0.5):
                per_engine[name]["correct"] += 1
        # Fused's own row.
        per_engine["fused"]["n"] += 1
        per_engine["fused"]["brier_sum"] += _brier(p, y)
        per_engine["fused"]["err_sum"] += abs(p - y)
        if pred == y:
            per_engine["fused"]["correct"] += 1

    # Assemble calibration curve.
    curve: list[dict] = []
    for i, b in enumerate(buckets):
        if b["n"] == 0:
            continue
        exp = round(b["sum_p"] / b["n"] * 100, 2)
        obs = round(b["sum_y"] / b["n"] * 100, 2)
        curve.append({
            "bucket": f"{b['bucket_lo']:.1f}-{b['bucket_hi']:.1f}",
            "n": b["n"],
            "expected": exp,
            "observed": obs,
            "delta": round(exp - obs, 2),
        })

    def _finish(group: dict) -> dict:
        out = {}
        for k, v in group.items():
            n = v["n"]
            out[k] = {
                "n": n,
                "accuracy": round(v["correct"] / n, 4) if n else 0.0,
                "brier": round(v["brier_sum"] / n, 4) if n else 0.0,
            }
        return out

    engine_out: dict[str, dict] = {}
    for name, v in per_engine.items():
        n = v["n"]
        engine_out[name] = {
            "n": n,
            "accuracy": round(v["correct"] / n, 4) if n else 0.0,
            "brier": round(v["brier_sum"] / n, 4) if n else 0.0,
            "mean_absolute_error": round(v["err_sum"] / n, 4) if n else 0.0,
        }

    return {
        "n_graded":            total,
        "accuracy":            round(correct / total, 4) if total else 0.0,
        "brier_score":         round(brier_sum / total, 4) if total else 0.0,
        "log_loss":            round(log_loss_sum / total, 4) if total else 0.0,
        "calibration_curve":   curve,
        "by_sport":            _finish(per_sport),
        "by_market":           _finish(per_market),
        "by_confidence_tier":  _finish(per_conf),
        "by_engine":           engine_out,
        "reliability_flag":    _reliability_flag(curve),
        "window_days":         days,
        "generated_at":        datetime.now(timezone.utc).isoformat(),
    }


__all__ = ["build_calibration_report"]
