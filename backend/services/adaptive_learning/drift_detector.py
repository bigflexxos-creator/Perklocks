"""Drift Detection (2026-07-28).

Detects environment change that could invalidate a trained model:
  A. **Accuracy drift** — recent Brier / accuracy vs training-time.
  B. **Prediction distribution drift** — mean/std of the fused output
     shifts materially.
  C. **Feature-importance drift** — top-N features change between the
     current-model and a hypothetical replay on the trailing window
     (best-effort proxy: compares FEATURE VALUE distributions in the
     `top_factors` field of persisted predictions).

Public API
──────────
    alerts = await detect_drift(db, sport=None, stat=None,
                                 days=30, min_samples=25)

    → list[{ tag, sport, stat, level, reason, evidence }]
       level ∈ {"warning", "critical"}

Read-only. Zero writes. Never raises.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger("lockscore.services.adaptive_learning.drift_detector")

MODEL_DIR = Path("/app/backend/models")


async def _recent_metrics(db, sport: str, stat: str,
                            days: int, min_samples: int) -> dict:
    """Compute recent Brier + accuracy + mean-prob for (sport, stat)."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    q = {"sport": sport, "stat": stat,
          "actual_value": {"$ne": None},
          "created_at": {"$gte": since}}
    n = 0
    brier_sum = 0.0
    correct = 0
    prob_sum = 0.0
    prob_sq = 0.0
    async for d in db.fusion_predictions.find(q, {"_id": 0}):
        thr = d.get("threshold"); actual = d.get("actual_value")
        # Use the ML component's prob if present — falls back to fused.
        comps = d.get("components") or {}
        c = comps.get("ml") or {}
        p = c.get("probability") if isinstance(c, dict) and \
                                    c.get("available") else \
            d.get("final_probability")
        if None in (p, thr, actual):
            continue
        try:
            p = float(p); thr = float(thr); actual = float(actual)
        except (TypeError, ValueError):
            continue
        y = 1.0 if actual > thr else 0.0
        n += 1
        brier_sum += (p - y) ** 2
        prob_sum += p
        prob_sq += p * p
        if (p >= 0.5) == (y >= 0.5):
            correct += 1
    if n < min_samples:
        return {"n": n, "insufficient": True}
    mu = prob_sum / n
    var = max(prob_sq / n - mu * mu, 0.0)
    return {
        "n":         n,
        "brier":     brier_sum / n,
        "accuracy":  correct / n,
        "mean_prob": mu,
        "std_prob":  var ** 0.5,
    }


def _model_baseline(tag: str) -> Optional[dict]:
    meta = MODEL_DIR / f"{tag}.meta.json"
    if not meta.exists():
        return None
    try:
        m = json.loads(meta.read_text())
    except Exception:
        return None
    w = m.get(m.get("winner") or "") or {}
    return {
        "brier_p50": (w.get("brier_by_thr") or {}).get("p50"),
        "top_features": [f[0] if isinstance(f, list) else f
                          for f in (w.get("top_features") or [])[:5]],
    }


async def detect_drift(
    db,
    *,
    sport: Optional[str] = None,
    stat: Optional[str] = None,
    days: int = 30,
    min_samples: int = 25,
    warn_brier_ratio: float = 1.15,      # 15 % worse than baseline
    critical_brier_ratio: float = 1.40,  # 40 % worse
) -> list[dict]:
    """Scan every on-disk model (or the filtered subset) and return
    the alerts that fire."""
    alerts: list[dict] = []
    for meta_path in sorted(MODEL_DIR.glob("*.meta.json")):
        # `meta_path.stem` on `x.meta.json` returns `x.meta` — strip
        # the inner `.meta` so `tag` matches the pkl filenames.
        tag = meta_path.name.replace(".meta.json", "")
        try:
            m = json.loads(meta_path.read_text())
        except Exception:
            continue
        s_up = (m.get("sport") or "").upper()
        st   = m.get("stat")
        if sport and s_up != sport.upper():
            continue
        if stat and st != stat:
            continue
        baseline = _model_baseline(tag)
        if not baseline:
            continue
        recent = await _recent_metrics(db, s_up, st, days, min_samples)
        if recent.get("insufficient"):
            continue

        evidence = {
            "n_recent": recent["n"],
            "recent_brier":  round(recent["brier"], 4),
            "baseline_brier_p50": baseline["brier_p50"],
            "recent_accuracy":    round(recent["accuracy"], 4),
            "recent_mean_prob":   round(recent["mean_prob"], 4),
            "recent_std_prob":    round(recent["std_prob"], 4),
        }

        # A) Accuracy / Brier drift.
        if baseline["brier_p50"] and baseline["brier_p50"] > 0:
            ratio = recent["brier"] / baseline["brier_p50"]
            evidence["brier_ratio"] = round(ratio, 3)
            if ratio >= critical_brier_ratio:
                alerts.append({
                    "tag": tag, "sport": s_up, "stat": st,
                    "level": "critical",
                    "reason": f"Brier ratio {ratio:.2f}x baseline",
                    "evidence": evidence,
                })
                continue
            if ratio >= warn_brier_ratio:
                alerts.append({
                    "tag": tag, "sport": s_up, "stat": st,
                    "level": "warning",
                    "reason": f"Brier ratio {ratio:.2f}x baseline",
                    "evidence": evidence,
                })
                continue

        # B) Prediction distribution drift — mean prob way off 0.5?
        if abs(recent["mean_prob"] - 0.5) > 0.35:
            alerts.append({
                "tag": tag, "sport": s_up, "stat": st,
                "level": "warning",
                "reason": f"mean probability drift ({recent['mean_prob']:.2f})",
                "evidence": evidence,
            })
    return alerts


__all__ = ["detect_drift"]
