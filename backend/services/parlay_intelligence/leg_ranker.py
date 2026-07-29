"""Smart Leg Ranker (Phase 5, 2026-06-30).

Replaces the naive "sort by lock_score" pick-ordering with a
multi-signal parlay leg score.

Inputs on the pick document (all optional; safe fallbacks used)
──────────────────────────────────────────────────────────────
  • fusion.final_probability     — Prediction Fusion Engine output
  • fusion.agreement_score       — model consensus 0..1
  • fusion.components            — {ml, similar, player_h2h, simulator}
  • win_probability              — legacy % (0..100)
  • lock_score / lock_score_v2   — legacy lock score 0..99
  • edge_percent                 — model edge %
  • matchup_intel.grade          — "A+" .. "F"
  • matchup_intel.score          — 0..100 numeric grade
  • sample_size / bucket_n       — number of historical observations
  • player_recent                — list of recent stat values
  • simulator.probability        — Monte Carlo p (0..1)
  • roi_bucket_pct               — historical bucket ROI %

Outputs (LegRanking dataclass, also serialisable to dict)
─────────────────────────────────────────────────────────
  • parlay_score        (0..100)
  • confidence_grade    ("A+".."F")
  • risk_level          ("safe" / "balanced" / "risky")
  • components          per-signal contribution
  • notes               list of human bullets ("Strong matchup edge", …)

NO sportsbook odds are consumed. `edge_percent` is a model-side quantity
in this codebase (win_probability - fair_probability), not a book price.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional

try:
    from services.discovery.confidence_system import (
        confidence_label,
        consistency_score,
    )
except Exception:  # pragma: no cover
    def confidence_label(n: int) -> str:
        if n >= 30: return "high"
        if n >= 15: return "medium"
        if n >= 5:  return "low"
        return "insufficient"

    def consistency_score(values: list) -> float:
        if not values or len(values) < 2:
            return 0.0
        m = sum(values) / len(values)
        if m <= 0:
            return 0.0
        import math
        v = sum((x - m) ** 2 for x in values) / len(values)
        cv = math.sqrt(v) / m
        return max(0.0, min(1.0, 1.0 - cv))


# ═════════════════════════════════════════════════════════════════════
# Weights
# ═════════════════════════════════════════════════════════════════════
# Rebalanced to reward multi-model agreement — a fusion-backed leg with
# strong matchup edge should out-rank a lock-alone leg.
W_FUSED_PROB      = 0.25    # Fused final probability (or fallback win_p)
W_MODEL_AGREEMENT = 0.15    # Model consensus from fusion agreement
W_MATCHUP         = 0.15    # Matchup Intelligence grade
W_SAMPLE          = 0.10    # Sample confidence (n observations)
W_SIMULATOR       = 0.10    # Monte Carlo simulator confidence
W_HIST_PERF       = 0.15    # Bucket ROI + lock_score signal
W_CONSISTENCY     = 0.10    # Player recent value consistency

# Grade cut-offs (parlay_score 0..100)
GRADE_TABLE = [
    ("A+", 88),
    ("A",  78),
    ("B",  68),
    ("C",  55),
    ("D",  42),
    ("F",   0),
]

# Risk level cut-offs
RISK_SAFE_MIN     = 72
RISK_BALANCED_MIN = 55


@dataclass
class LegRanking:
    pick_id: str
    parlay_score: float
    confidence_grade: str
    risk_level: str
    components: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ═════════════════════════════════════════════════════════════════════
# Signal extractors — all defensive; any missing input returns a neutral
# component (50) so the pick is never unfairly penalised for absent data
# ═════════════════════════════════════════════════════════════════════
def _fused_probability(pick: dict) -> tuple[float, str]:
    """Return (0..100 signal, source_tag)."""
    fusion = (pick.get("fusion") or {}) if isinstance(pick, dict) else {}
    fp = fusion.get("final_probability")
    if isinstance(fp, (int, float)):
        return max(0.0, min(100.0, float(fp) * 100.0)), "fusion"
    win_p = pick.get("win_probability")
    if isinstance(win_p, (int, float)) and win_p > 0:
        return max(0.0, min(100.0, float(win_p))), "win_probability"
    lock = pick.get("lock_score") or pick.get("lock_score_v2")
    if isinstance(lock, (int, float)) and lock > 0:
        # Lock is 0..99 already; use as a weak fallback signal.
        return max(0.0, min(100.0, float(lock))), "lock_fallback"
    return 50.0, "neutral"


def _model_agreement(pick: dict) -> float:
    """0..100 — fusion agreement_score if present, else derive from
    win_prob vs lock_score coherence."""
    fusion = pick.get("fusion") or {}
    ag = fusion.get("agreement_score")
    if isinstance(ag, (int, float)):
        return max(0.0, min(100.0, float(ag) * 100.0))
    # Heuristic: how well do lock_score (%) and win_prob (%) agree?
    win_p = pick.get("win_probability")
    lock  = pick.get("lock_score") or pick.get("lock_score_v2")
    if isinstance(win_p, (int, float)) and isinstance(lock, (int, float)) \
            and win_p > 0 and lock > 0:
        gap = abs(float(win_p) - float(lock))
        # gap 0 → 100, gap 25 → 50, gap 50 → 0
        return max(0.0, min(100.0, 100.0 - gap * 2.0))
    return 50.0


def _matchup_grade(pick: dict) -> float:
    """0..100. Prefer numeric score; fall back to letter grade mapping."""
    mi = pick.get("matchup_intel") or pick.get("matchup") or {}
    score = mi.get("score") if isinstance(mi, dict) else None
    if isinstance(score, (int, float)):
        return max(0.0, min(100.0, float(score)))
    grade = mi.get("grade") if isinstance(mi, dict) else None
    mapping = {
        "A+": 95, "A": 88, "A-": 82, "B+": 76, "B": 70, "B-": 64,
        "C+": 58, "C": 52, "C-": 46, "D+": 40, "D": 34, "F": 20,
    }
    if isinstance(grade, str) and grade.strip() in mapping:
        return float(mapping[grade.strip()])
    return 50.0


def _sample_confidence(pick: dict) -> float:
    """0..100 based on sample size."""
    n_raw = (
        pick.get("sample_size")
        or pick.get("bucket_n")
        or (pick.get("matchup_intel") or {}).get("n")
        or 0
    )
    try:
        n = int(n_raw or 0)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return 40.0    # neutral-low — no evidence
    lbl = confidence_label(n)
    return {"insufficient": 35.0, "low": 55.0,
            "medium": 75.0, "high": 92.0}[lbl]


def _simulator_confidence(pick: dict) -> float:
    """0..100 from simulator.probability if available, else neutral."""
    sim = pick.get("simulator") or {}
    p = sim.get("probability") if isinstance(sim, dict) else None
    if isinstance(p, (int, float)):
        return max(0.0, min(100.0, float(p) * 100.0))
    # Alternate path: fusion.components.simulator
    fusion = pick.get("fusion") or {}
    comps = fusion.get("components") or {}
    sim_p = comps.get("simulator") if isinstance(comps, dict) else None
    if isinstance(sim_p, (int, float)):
        return max(0.0, min(100.0, float(sim_p) * 100.0))
    return 50.0


def _historical_performance(pick: dict) -> float:
    """0..100. Combines bucket ROI% and lock_score for a compact signal."""
    roi = pick.get("roi_bucket_pct")
    if not isinstance(roi, (int, float)):
        roi = 0.0
    # -10 → 0, 0 → 50, +20 → 100
    roi_component = max(0.0, min(100.0, (float(roi) + 10.0) * (100.0 / 30.0)))
    lock = pick.get("lock_score") or pick.get("lock_score_v2") or 0
    lock_component = max(0.0, min(100.0, float(lock)))
    # Weighted 60% lock + 40% roi.
    return 0.6 * lock_component + 0.4 * roi_component


def _consistency(pick: dict) -> float:
    """0..100 from stddev of `player_recent` values."""
    recents = pick.get("player_recent") or pick.get("recent_stats")
    if not isinstance(recents, (list, tuple)) or len(recents) < 3:
        return 55.0    # slight positive default — no reason to punish
    try:
        vals = [float(x) for x in recents if x is not None]
    except (TypeError, ValueError):
        return 55.0
    s = consistency_score(vals)
    return max(0.0, min(100.0, s * 100.0))


# ═════════════════════════════════════════════════════════════════════
# Grade + risk mapping
# ═════════════════════════════════════════════════════════════════════
def grade_from_score(score: float) -> str:
    for g, thr in GRADE_TABLE:
        if score >= thr:
            return g
    return "F"


def risk_level_from_score(score: float) -> str:
    if score >= RISK_SAFE_MIN:
        return "safe"
    if score >= RISK_BALANCED_MIN:
        return "balanced"
    return "risky"


# ═════════════════════════════════════════════════════════════════════
# Public API
# ═════════════════════════════════════════════════════════════════════
def rank_leg(pick: dict) -> LegRanking:
    """Compute parlay_score + confidence grade + risk level for a pick."""
    if not isinstance(pick, dict):
        return LegRanking(
            pick_id="", parlay_score=0.0, confidence_grade="F",
            risk_level="risky", components={}, notes=["Invalid input"],
        )

    fused_prob, fused_src = _fused_probability(pick)
    agreement            = _model_agreement(pick)
    matchup              = _matchup_grade(pick)
    sample               = _sample_confidence(pick)
    simulator            = _simulator_confidence(pick)
    hist_perf            = _historical_performance(pick)
    consistency          = _consistency(pick)

    components = {
        "fused_probability":  round(fused_prob, 1),
        "model_agreement":    round(agreement, 1),
        "matchup":            round(matchup, 1),
        "sample_confidence":  round(sample, 1),
        "simulator":          round(simulator, 1),
        "historical":         round(hist_perf, 1),
        "consistency":        round(consistency, 1),
        "_fused_source":      fused_src,
    }

    score = (
        W_FUSED_PROB      * fused_prob
        + W_MODEL_AGREEMENT * agreement
        + W_MATCHUP         * matchup
        + W_SAMPLE          * sample
        + W_SIMULATOR       * simulator
        + W_HIST_PERF       * hist_perf
        + W_CONSISTENCY     * consistency
    )
    score = max(0.0, min(100.0, score))

    notes: list = []
    if fused_src == "fusion":
        notes.append(f"Fusion probability {fused_prob:.0f}%")
    if agreement >= 75:
        notes.append("Strong model consensus")
    elif agreement < 45:
        notes.append("Model disagreement — caution")
    if matchup >= 80:
        notes.append("Elite matchup edge")
    elif matchup < 40:
        notes.append("Tough matchup")
    if sample >= 75:
        notes.append("Robust sample size")
    elif sample < 45:
        notes.append("Thin historical sample")
    if consistency >= 75:
        notes.append("Consistent recent form")
    elif consistency < 40:
        notes.append("Volatile recent form")

    return LegRanking(
        pick_id=str(pick.get("id") or pick.get("pick_id") or ""),
        parlay_score=round(score, 2),
        confidence_grade=grade_from_score(score),
        risk_level=risk_level_from_score(score),
        components=components,
        notes=notes,
    )


def rank_legs(picks: list) -> list[LegRanking]:
    """Rank a batch — returns sorted list (highest score first)."""
    if not picks:
        return []
    rankings = [rank_leg(p) for p in picks if isinstance(p, dict)]
    rankings.sort(key=lambda r: r.parlay_score, reverse=True)
    return rankings
