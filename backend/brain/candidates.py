"""Candidate Generation & Ranker.

The existing engine already generates a slate of picks; this module
formalises the user spec by:

  • Treating every freshly-built pick as a CANDIDATE bet.
  • Computing a composite RANK SCORE per candidate from 5 factors:
      0.30 * normalized_edge
      0.25 * confidence_calibrated         (from brain.calibration)
      0.20 * historical_roi_normalized     (per-market bucket)
      0.15 * data_completeness             (factors + insights + sportsdb)
      0.10 * consistency                   (low factor variance)
  • Sorting (descending) and tagging the top-K as `brain.candidate_rank`.

Note: we do NOT cull non-top candidates — feature parity is preserved.
The Decision Filter (below) handles culling via PASS verdicts.
"""
from __future__ import annotations

import logging
import statistics

from .memory import BrainMemory

logger = logging.getLogger("lockscore.brain.candidates")

W = {
    "edge":         0.30,
    "confidence":   0.25,
    "roi":          0.20,
    "data":         0.15,
    "consistency": 0.10,
}

TOP_K_RANK = 50   # top candidates also flagged for simulator + tight gating


def _normalize_edge(edge_pct: float) -> float:
    # -5% → 0, 0% → 0.5, +5% → 1.0, +10%+ → cap 1.0
    if edge_pct <= -5:
        return 0.0
    if edge_pct >= 5:
        return 1.0
    return (edge_pct + 5) / 10.0


def _normalize_roi(roi_pct: float) -> float:
    # -20% → 0, 0% → 0.5, +20%+ → 1.0
    if roi_pct <= -20:
        return 0.0
    if roi_pct >= 20:
        return 1.0
    return (roi_pct + 20) / 40.0


def _data_completeness(pick: dict) -> float:
    """How rich is the input data behind this pick? 0..1.

    Counts: factor count, insight count, deep-dive presence, sportdb enrich,
    selection_v2 presence.
    """
    score = 0.0
    factors = pick.get("factors") or {}
    if len(factors) >= 5:
        score += 0.30
    elif len(factors) >= 3:
        score += 0.20
    insights = pick.get("key_insights") or []
    if len(insights) >= 4:
        score += 0.20
    elif len(insights) >= 2:
        score += 0.10
    if pick.get("deep_dive_scores") or pick.get("edge_score"):
        score += 0.20
    if pick.get("enriched_by") == "sportdb":
        score += 0.15
    if pick.get("selection_v2"):
        score += 0.15
    return min(1.0, score)


def _consistency(pick: dict) -> float:
    """Higher = factors agree more. 0..1 from stdev of factor values."""
    f = pick.get("factors") or {}
    vals = [float(v) / 100.0 if v > 1 else float(v) for v in f.values()] if f else []
    if len(vals) < 2:
        return 0.5
    sd = statistics.pstdev(vals)
    # sd 0 → 1.0 ; sd 0.30+ → 0
    return max(0.0, min(1.0, 1.0 - sd * 3.3))


def rank_candidates(picks: list[dict], memory: BrainMemory) -> dict:
    """Compute composite rank scores. Mutates `brain` sub-dict on each pick."""
    for p in picks:
        brain = p.setdefault("brain", {})
        edge_n = _normalize_edge(p.get("edge_percent") or 0.0)
        conf = float(brain.get("confidence_calibrated") or (p.get("lock_score", 0) / 100.0))
        # Bucket ROI lookup (selection_v2.market.family preferred)
        sv2 = p.get("selection_v2") or {}
        family = (sv2.get("market") or {}).get("family") or "other"
        stats = memory.market(p.get("sport") or "", family)
        roi_n = _normalize_roi(stats.roi_pct) if stats and stats.n >= 8 else 0.5
        data = _data_completeness(p)
        cons = _consistency(p)
        score = (
            W["edge"]        * edge_n +
            W["confidence"]  * conf +
            W["roi"]         * roi_n +
            W["data"]        * data +
            W["consistency"] * cons
        )
        brain["candidate_score"] = round(score, 4)
        brain["candidate_components"] = {
            "edge":         round(edge_n, 3),
            "confidence":   round(conf, 3),
            "roi":          round(roi_n, 3),
            "data":         round(data, 3),
            "consistency":  round(cons, 3),
        }

    # Sort and tag rank.
    ranked = sorted(picks, key=lambda p: -p["brain"]["candidate_score"])
    for i, p in enumerate(ranked):
        p["brain"]["candidate_rank"] = i + 1
        p["brain"]["top_k"] = i < TOP_K_RANK
    return {"ranked": len(picks), "top_k": min(len(picks), TOP_K_RANK)}
