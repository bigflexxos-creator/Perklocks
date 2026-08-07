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
import math
import statistics
from typing import Optional

from .memory import BrainMemory

logger = logging.getLogger("lockscore.brain.candidates")


def _as_float(v) -> Optional[float]:
    """Safely coerce ``v`` to a finite float, or ``None`` if it is not
    numerically meaningful.

    Accepts:  int, float, numeric strings ("1.5", "3", " 2 ").
    Rejects (returns None):  None, NaN, +/-inf, empty strings, non-numeric
    strings (e.g. narrative factor descriptions like "Wikipedia top
    scorer table"), and any object that cannot be safely converted.

    Rejecting rather than defaulting to 0.0 lets the ranker skip
    non-numeric factors entirely — they contribute neither signal nor
    noise to consistency / variance calculations, which preserves the
    exact ranking behaviour for picks whose factors are all-numeric
    (this is our regression-safety guarantee).
    """
    if v is None or isinstance(v, bool):
        # bool is a subclass of int, but we don't want True/False here.
        return None
    if isinstance(v, (int, float)):
        fv = float(v)
    else:
        try:
            fv = float(v)
        except (TypeError, ValueError):
            return None
    if math.isnan(fv) or math.isinf(fv):
        return None
    return fv

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
    selection_v2 presence.  Fault-tolerant: non-container values for
    ``factors`` / ``key_insights`` (e.g. a corrupt int) contribute 0
    rather than raising ``TypeError`` on ``len(...)``.
    """
    score = 0.0
    factors = pick.get("factors") or {}
    n_factors = len(factors) if hasattr(factors, "__len__") else 0
    if n_factors >= 5:
        score += 0.30
    elif n_factors >= 3:
        score += 0.20
    insights = pick.get("key_insights") or []
    n_insights = len(insights) if hasattr(insights, "__len__") else 0
    if n_insights >= 4:
        score += 0.20
    elif n_insights >= 2:
        score += 0.10
    if pick.get("deep_dive_scores") or pick.get("edge_score"):
        score += 0.20
    if pick.get("enriched_by") == "sportdb":
        score += 0.15
    if pick.get("selection_v2"):
        score += 0.15
    return min(1.0, score)


def _consistency(pick: dict) -> float:
    """Higher = factors agree more. 0..1 from stdev of factor values.

    Fault-tolerant: any factor whose value is not numerically usable
    (None, empty string, narrative text, NaN, inf, wrong type) is
    silently skipped.  When every factor is a valid number the result
    is *identical* to the previous implementation, which guarantees
    regression safety on normal picks.
    """
    f = pick.get("factors") or {}
    if not isinstance(f, dict):
        return 0.5
    vals: list[float] = []
    for v in f.values():
        fv = _as_float(v)
        if fv is None:
            continue
        vals.append(fv / 100.0 if fv > 1 else fv)
    if len(vals) < 2:
        return 0.5
    sd = statistics.pstdev(vals)
    # sd 0 → 1.0 ; sd 0.30+ → 0
    return max(0.0, min(1.0, 1.0 - sd * 3.3))


def _first_bad_factor(pick: dict) -> tuple[Optional[str], object]:
    """Return the (name, value) of the first non-numeric factor for
    diagnostic logging.  Returns (None, None) when everything looks
    numeric.  Never raises.
    """
    try:
        f = pick.get("factors") or {}
        if not isinstance(f, dict):
            return ("<factors>", type(f).__name__)
        for k, v in f.items():
            if _as_float(v) is None and v is not None:
                # Trim large repr strings so logs stay clean.
                r = v if isinstance(v, (int, float, bool)) else repr(v)[:80]
                return (str(k), r)
    except Exception:
        return ("<unknown>", "<unreadable>")
    return (None, None)


def rank_candidates(picks: list[dict], memory: BrainMemory) -> dict:
    """Compute composite rank scores. Mutates `brain` sub-dict on each pick.

    Per-pick exception isolation: a single malformed pick (bad factors,
    non-numeric edge, corrupt selection_v2, etc.) is logged at WARNING
    and skipped — the rest of the batch is scored and ranked normally.
    Skipped picks receive ``candidate_score = 0.0`` and
    ``brain.candidate_error`` so they sort to the bottom without
    crashing the sort key.
    """
    failed = 0
    for p in picks:
        try:
            brain = p.setdefault("brain", {})
            edge_pct = _as_float(p.get("edge_percent"))
            edge_n = _normalize_edge(edge_pct if edge_pct is not None else 0.0)
            # Confidence input MUST come from a real probability, not from
            # `lock_score`. Per product spec (2026-07-01): lock_score is a
            # tier label (Elite / Strong / Standard), NOT a probability, so
            # we cannot divide it by 100 and pretend it's a P(win). Preference
            # order: brain.confidence_calibrated → pick.win_probability →
            # pick.raw_win_probability → neutral 0.5.
            conf_raw = (
                brain.get("confidence_calibrated")
                if brain.get("confidence_calibrated") is not None
                else (p.get("win_probability") or p.get("raw_win_probability"))
            )
            conf_f = _as_float(conf_raw)
            conf = conf_f if conf_f is not None else 0.5
            # win_probability is 0..100 in some pick shapes and 0..1 in others.
            # Normalise once here so the ranker always sees 0..1.
            if conf > 1.0:
                conf = conf / 100.0
            conf = max(0.0, min(1.0, conf))
            # Bucket ROI lookup (selection_v2.market.family preferred)
            sv2 = p.get("selection_v2") or {}
            if not isinstance(sv2, dict):
                sv2 = {}
            market_obj = sv2.get("market") or {}
            if not isinstance(market_obj, dict):
                market_obj = {}
            family = market_obj.get("family") or "other"
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
        except Exception as e:
            # Do not silently swallow — log WARNING with enough context to
            # trace the offending pick, but never dump the full pick dict
            # (per product directive: keep logs clean).
            failed += 1
            try:
                pick_id = (
                    p.get("pick_id")
                    or p.get("id")
                    or p.get("_id")
                    or "<no-id>"
                )
                sport = p.get("sport") or "<no-sport>"
                sv2_raw = p.get("selection_v2")
                sv2_safe = sv2_raw if isinstance(sv2_raw, dict) else {}
                mkt_obj = sv2_safe.get("market")
                mkt_obj = mkt_obj if isinstance(mkt_obj, dict) else {}
                market = (
                    mkt_obj.get("family")
                    or p.get("market")
                    or p.get("market_key")
                    or "<no-market>"
                )
                bad_factor, bad_value = _first_bad_factor(p)
            except Exception:
                pick_id, sport, market = "<unreadable>", "<unreadable>", "<unreadable>"
                bad_factor, bad_value = None, None
            logger.warning(
                "rank_candidates: skipped pick due to %s: %s "
                "(pick_id=%s sport=%s market=%s bad_factor=%s bad_value=%r)",
                type(e).__name__, e, pick_id, sport, market, bad_factor, bad_value,
            )
            # Best-effort mark the pick so sorting stays safe.
            try:
                brain = p.setdefault("brain", {})
                brain["candidate_score"] = 0.0
                brain["candidate_components"] = {
                    "edge": 0.0, "confidence": 0.0, "roi": 0.0,
                    "data": 0.0, "consistency": 0.0,
                }
                brain["candidate_error"] = {
                    "type": type(e).__name__,
                    "message": str(e)[:200],
                    "bad_factor": bad_factor,
                }
            except Exception:
                # Pick is so malformed we can't even attach brain — drop it
                # by not touching picks (sort key below will still work
                # because we filter such picks out).
                pass

    # Sort and tag rank.  Every pick that survived either succeeded (score
    # set) or was marked with score=0.0 in the except branch.  Guard the
    # sort key just in case an unrecoverable pick lacks a brain dict.
    def _score_key(p: dict) -> float:
        try:
            return -float(p["brain"]["candidate_score"])
        except Exception:
            return 0.0
    ranked = sorted(picks, key=_score_key)
    for i, p in enumerate(ranked):
        try:
            brain = p.setdefault("brain", {})
            brain["candidate_rank"] = i + 1
            brain["top_k"] = i < TOP_K_RANK
        except Exception:
            # Pick is unrecoverable; leave it alone (already logged).
            continue
    return {
        "ranked": len(picks),
        "top_k": min(len(picks), TOP_K_RANK),
        "failed": failed,
    }
