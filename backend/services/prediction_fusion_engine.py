"""Prediction Fusion Engine (2026-07-28).

Combines four independent player-projection signals into ONE final
projection with agreement scoring, weighted fusion, and backtesting
telemetry.

Signals (each produces a P(actual > threshold) in [0, 1])
────────────────────────────────────────────────────────
  1. Trained ML     — `services.trained_prediction_engine`.
                      Uses only pregame features. NO sportsbook odds.
  2. Similar Match  — `services.similar_matchup_engine`.
                      Player's hit rate vs defenses LIKE the target.
  3. Player Matchup — `services.player_matchup_intelligence` (or NFL).
                      Direct player-vs-opponent history hit rate.
  4. Monte Carlo    — `brain.sim_runner.simulate_pick` (optional).
                      Physics-style simulator; consumes a synthetic
                      pick dict we build on the fly.

Default weights (configurable per call)
    ml:          0.40
    similar:     0.25
    player_h2h:  0.20
    simulator:   0.15

**No sportsbook odds** — the market line is used ONLY as the target
threshold at inference. No book prices, spreads, consensus, or steam
signals ever enter the fusion math.

Public API
──────────
    result = await fuse_prediction(
        db,
        sport="NFL",
        player="Joe Burrow",
        stat="passing_yards",
        opponent="KC",
        threshold=249.5,
        weights=None,           # None → defaults
        include_simulator=False,# opt-in; sim requires a pick
        persist=True,           # log to fusion_predictions collection
    )

    → FusionResult{
        prediction_id:      "b02f...",
        final_probability:  0.55,
        projected_stat:     262.3,
        confidence:         "medium",
        model_agreement:    "moderate_convergence",
        agreement_score:    0.72,
        components:         { ml: 0.60, similar: 0.55, player_h2h: 0.50, ...},
        factors_for:        [...],
        factors_against:    [...],
        explanation:        "Multi-model consensus: ...",
        weights_used:       { ml: 0.44, similar: 0.28, ...},
        data_sources_used:  [...],
        notes:              [...],
      }

Backtesting hooks
─────────────────
    await record_prediction_actual(db, prediction_id, actual_value)
    # populates the `actual`, `outcome`, `winning_component` fields
    # on the persisted prediction, so aggregators can compute per-
    # component + per-sport accuracy.

    await get_backtest_summary(db, sport=None, stat=None,
                                confidence=None, days=90)
    # returns per-component + overall accuracy over the trailing window.

**Never wired into pick generation.** Read-only fusion + telemetry only.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
import statistics
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

logger = logging.getLogger("lockscore.services.prediction_fusion_engine")


# ─────────────────────────────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────────────────────────────
DEFAULT_WEIGHTS: dict[str, float] = {
    "ml":         0.40,
    "similar":    0.25,
    "player_h2h": 0.20,
    "simulator":  0.15,
}

COMPONENT_NAMES = tuple(DEFAULT_WEIGHTS.keys())


@dataclass
class ComponentPrediction:
    """One source's contribution to the fused prediction."""
    name:          str
    available:     bool                    = False
    probability:   Optional[float]         = None    # P(actual > threshold)
    projected:     Optional[float]         = None    # expected raw stat value
    sample_size:   Optional[int]           = None
    confidence:    Optional[str]           = None    # source's own conf label
    notes:         list[str]               = field(default_factory=list)
    meta:          dict[str, Any]          = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class FusionResult:
    prediction_id:      str
    sport:              str
    player:             str
    stat:               str
    opponent:           str
    threshold:          Optional[float]

    final_probability:  float
    projected_stat:     Optional[float]
    confidence:         str                     # low|medium|high
    model_agreement:    str                     # strong_convergence|moderate_convergence|weak_convergence|disagreement
    agreement_score:    float                   # 1.0 = identical, 0.0 = maximum disagreement

    components:         dict[str, ComponentPrediction] = field(default_factory=dict)
    weights_used:       dict[str, float]              = field(default_factory=dict)

    factors_for:        list[str] = field(default_factory=list)
    factors_against:    list[str] = field(default_factory=list)
    explanation:        str = ""

    data_sources_used:  list[str] = field(default_factory=list)
    notes:              list[str] = field(default_factory=list)
    created_at:         str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["components"] = {k: v.to_dict() if not isinstance(v, dict) else v
                           for k, v in self.components.items()}
        return d


# ─────────────────────────────────────────────────────────────────────
# Weight normalisation
# ─────────────────────────────────────────────────────────────────────
def _normalise_weights(weights: dict[str, float],
                       available: dict[str, bool]) -> dict[str, float]:
    """Zero-out weights for missing components then renormalise the
    remainder to sum to 1.0. If nothing is available, return all zeros."""
    kept = {k: max(0.0, float(weights.get(k, 0.0)))
             for k in COMPONENT_NAMES if available.get(k)}
    total = sum(kept.values())
    if total <= 0:
        return {k: 0.0 for k in COMPONENT_NAMES}
    return {k: round(v / total, 4) if k in kept else 0.0
             for k, v in {**{n: 0.0 for n in COMPONENT_NAMES}, **kept}.items()}


def _guard_weights(weights: Optional[dict[str, float]]) -> dict[str, float]:
    """Validate + sanitise user-supplied weights."""
    if not weights:
        return dict(DEFAULT_WEIGHTS)
    guarded: dict[str, float] = {}
    for k in COMPONENT_NAMES:
        v = weights.get(k, DEFAULT_WEIGHTS[k])
        try:
            v = float(v)
        except (TypeError, ValueError):
            v = DEFAULT_WEIGHTS[k]
        if not math.isfinite(v) or v < 0:
            v = 0.0
        guarded[k] = v
    return guarded


# ─────────────────────────────────────────────────────────────────────
# Agreement scoring
# ─────────────────────────────────────────────────────────────────────
def _agreement(probs: list[float]) -> tuple[str, float]:
    """Score agreement across [0, 1] probabilities.

    Returns (label, score) where score is 1 - (max - min) so identical
    probabilities score 1.0 and 0/100 disagreement scores 0.0.

    Labels:
      strong_convergence   → all probs in same half AND spread ≤ 0.10
      moderate_convergence → all probs in same half AND spread ≤ 0.20
      weak_convergence     → all probs on same side but spread ≤ 0.30
      disagreement         → spread > 0.30 OR components on opposite sides
    """
    if not probs:
        return "insufficient_signals", 0.0
    if len(probs) == 1:
        return "single_signal", 1.0
    lo, hi = min(probs), max(probs)
    spread = hi - lo
    score = max(0.0, min(1.0, 1.0 - spread))
    # Components on opposite sides of 0.5 → always disagreement
    opposite = any(p >= 0.5 for p in probs) and any(p < 0.5 for p in probs)
    if opposite and spread > 0.15:
        return "disagreement", round(score, 4)
    if spread <= 0.10:
        return "strong_convergence", round(score, 4)
    if spread <= 0.20:
        return "moderate_convergence", round(score, 4)
    if spread <= 0.30:
        return "weak_convergence", round(score, 4)
    return "disagreement", round(score, 4)


def _confidence_label(n_signals: int, agreement_score: float,
                       max_prob: float) -> str:
    """Confidence combines sample of signals + agreement + how far
    the fused prob is from 0.5."""
    conviction = abs(max_prob - 0.5) * 2   # 0..1
    if n_signals >= 3 and agreement_score >= 0.85 and conviction >= 0.30:
        return "high"
    if n_signals >= 2 and agreement_score >= 0.70:
        return "medium"
    if n_signals >= 1:
        return "low"
    return "none"


# ─────────────────────────────────────────────────────────────────────
# Component runners
# ─────────────────────────────────────────────────────────────────────
async def _lookup_player_position(db, sport: str,
                                    player_name: str) -> Optional[str]:
    """Best-effort position lookup — used by the ML component to
    disambiguate market→model routing (e.g. MLB pitcher Ks vs batter Ks).

    Never raises. Returns None if no cache row or the player isn't in
    `players`. Cached in-process to keep the fusion hot-path cheap."""
    if not sport or not player_name:
        return None
    key = ((sport or "").lower(), (player_name or "").strip().lower())
    cached = _POSITION_CACHE.get(key)
    if cached is not None:
        return cached[1]        # (fetched_at, position)
    try:
        row = await db.players.find_one(
            {"sport": key[0], "name": {"$regex": f"^{re.escape(player_name)}$",
                                        "$options": "i"}},
            {"_id": 0, "position": 1},
        )
    except Exception:
        row = None
    pos = (row or {}).get("position") if isinstance(row, dict) else None
    _POSITION_CACHE[key] = (0, pos)
    return pos


_POSITION_CACHE: dict[tuple[str, str], tuple[float, Optional[str]]] = {}


async def _run_ml_component(db, sport, player, stat, opponent,
                             threshold) -> ComponentPrediction:
    cp = ComponentPrediction(name="ml")
    try:
        from services.trained_prediction_engine import predict_player_prop
        # Best-effort position hint — feeds the market→model router so
        # MLB "strikeouts" props resolve to the pitcher-Ks model when
        # the player is a pitcher (and batter Ks safe-fail cleanly).
        # NEVER raises; NEVER blocks the ML component if lookup fails.
        player_position = await _lookup_player_position(db, sport, player)
        r = await predict_player_prop(
            db, sport=sport, player=player, stat=stat,
            opponent=opponent, line=threshold,
            player_position=player_position,
        )
    except Exception as e:
        cp.notes.append(f"error: {e}")
        return cp
    if not r or not r.get("supported"):
        cp.notes.append(r.get("reason") if r else "no result")
        return cp
    cp.available = True
    p = r.get("prediction_probability")
    cp.probability = round(float(p), 4) if p is not None else None
    cp.projected = r.get("expected_value")
    cp.confidence = r.get("confidence")
    cp.meta = {
        "model":       r.get("model"),
        "residual_std": r.get("residual_std"),
        "top_factors": r.get("top_factors") or [],
        "auc_p50":     (r.get("model_meta") or {}).get("auc_p50"),
    }
    return cp


async def _run_similar_component(db, sport, player, stat, opponent,
                                  threshold) -> ComponentPrediction:
    cp = ComponentPrediction(name="similar")
    try:
        from services.similar_matchup_engine import (
            get_similar_matchup_intelligence,
        )
        r = await get_similar_matchup_intelligence(
            db, sport=sport, player_name=player, stat=stat,
            opponent_team=opponent, threshold=threshold,
        )
    except Exception as e:
        cp.notes.append(f"error: {e}")
        return cp
    if not r or r.n_similar_games == 0:
        cp.notes.append("no analog games found")
        return cp
    cp.available = True
    cp.probability = round(float(r.hit_rate), 4)
    cp.projected = float(r.avg_stat_output) if r.avg_stat_output else None
    cp.sample_size = r.n_similar_games
    cp.confidence = r.sample_confidence
    cp.meta = {
        "grade": r.grade, "similarity_floor": r.similarity_floor,
        "similar_teams": [so.team for so in r.similar_opponents[:5]],
    }
    cp.notes.append(r.note)
    return cp


async def _run_player_h2h_component(db, sport, player, stat, opponent,
                                     threshold) -> ComponentPrediction:
    cp = ComponentPrediction(name="player_h2h")
    try:
        if sport.upper() == "NFL":
            from services.nfl_matchup_intelligence import (
                get_nfl_matchup_intelligence,
            )
            r = await get_nfl_matchup_intelligence(
                db, player_name=player, opponent_team=opponent,
            )
            if r.games_played == 0:
                cp.notes.append("no direct H2H rows")
                return cp
            cp.available = True
            cp.sample_size = r.games_played
            cp.confidence = r.sample_confidence
            # Snap to closest threshold in the position's stat_lines
            sl = r.stat_lines.get(stat)
            if sl and sl.thresholds and threshold is not None:
                closest = min(sl.thresholds.keys(),
                              key=lambda t: abs(float(t) - float(threshold)))
                cp.probability = round(float(sl.thresholds[closest].hit_rate), 4)
                cp.projected = float(sl.avg) if sl.avg else None
            elif sl:
                cp.probability = 0.5    # neutral if no threshold
                cp.projected = float(sl.avg) if sl.avg else None
            cp.meta = {"position": r.position,
                        "last_meeting": r.last_meeting}
        else:
            from services.player_matchup_intelligence import (
                get_matchup_intelligence,
            )
            r = await get_matchup_intelligence(
                db, sport=sport, player_name=player, stat=stat,
                opponent_team=opponent, threshold=threshold,
            )
            if r.sample_size == 0:
                cp.notes.append("no direct H2H rows")
                return cp
            cp.available = True
            cp.probability = round(float(r.threshold_hit_rate), 4) \
                              if threshold is not None else 0.5
            cp.projected = float(r.avg_stat_output) if r.avg_stat_output else None
            cp.sample_size = r.sample_size
            cp.confidence = r.sample_confidence
            cp.meta = {"grade": r.matchup_grade}
    except Exception as e:
        cp.notes.append(f"error: {e}")
    return cp


async def _run_simulator_component(db, sport, player, stat, opponent,
                                     threshold, pick: Optional[dict] = None
                                     ) -> ComponentPrediction:
    cp = ComponentPrediction(name="simulator")
    try:
        from brain.sim_runner import simulate_pick   # lazy
    except Exception as e:
        cp.notes.append(f"simulator import failed: {e}")
        return cp
    if pick is None:
        # Build a minimal synthetic pick dict for the sim runner.
        pick = {
            "sport": sport,
            "market": f"{player} Over {threshold} {stat.replace('_', ' ').title()}",
            "selection": player,
            "event": f"{player} @ {opponent}",
            "line": threshold,
            "line_over_under": threshold,
            "over_under": "over",
            "player_name": player,
            "player": player,
            "opponent_team": opponent,
        }
    try:
        sim = simulate_pick(pick)
    except Exception as e:
        cp.notes.append(f"sim error: {e}")
        return cp
    if not sim:
        cp.notes.append("sim runner returned no result")
        return cp
    p = sim.get("sim_win_probability") or sim.get("win_probability") \
         or sim.get("sim_wp")
    if p is None:
        cp.notes.append("sim result missing win_probability")
        return cp
    # Sim reports as percent 0-100 sometimes; normalise.
    p = float(p)
    if p > 1.5:
        p = p / 100.0
    cp.available = True
    cp.probability = round(p, 4)
    cp.projected = sim.get("expected_value") or sim.get("expected_stat")
    cp.meta = {"raw": {k: v for k, v in sim.items() if isinstance(v, (int, float, str))}}
    return cp


# ─────────────────────────────────────────────────────────────────────
# Explanation builder
# ─────────────────────────────────────────────────────────────────────
def _build_factors(components: dict[str, ComponentPrediction],
                    threshold: Optional[float]) -> tuple[list[str], list[str]]:
    """Pull directional factors from each available component."""
    fors, againsts = [], []
    for name, cp in components.items():
        if not cp.available or cp.probability is None:
            continue
        pos = cp.probability >= 0.55
        neg = cp.probability <= 0.45
        if name == "ml":
            top_feats = cp.meta.get("top_factors") or []
            for f in top_feats[:3]:
                fname = f.get("feature", "?")
                val = f.get("value", "?")
                (fors if pos else againsts if neg else fors).append(
                    f"model_feature[{fname}]={val}"
                )
        elif name == "similar" and cp.sample_size:
            msg = (f"similar-defense analog: {cp.sample_size} games, "
                   f"hit rate {int(round(cp.probability * 100))}%")
            (fors if pos else againsts if neg else fors).append(msg)
        elif name == "player_h2h" and cp.sample_size:
            msg = (f"direct H2H: {cp.sample_size} games vs opponent, "
                   f"hit rate {int(round(cp.probability * 100))}%")
            (fors if pos else againsts if neg else fors).append(msg)
        elif name == "simulator":
            (fors if pos else againsts if neg else fors).append(
                f"simulator: {int(round(cp.probability * 100))}% P(over)"
            )
    return fors[:6], againsts[:6]


def _build_explanation(final_p: float, agreement: str,
                        n_signals: int) -> str:
    if n_signals == 0:
        return "No prediction sources available for this player/opponent."
    lean = "OVER" if final_p >= 0.55 else "UNDER" if final_p <= 0.45 else "leaning neutral"
    agr_pretty = agreement.replace("_", " ")
    return (
        f"Fused {n_signals} independent signals → {agr_pretty}. "
        f"Final probability of exceeding threshold: {int(round(final_p * 100))}% ({lean})."
    )


# ─────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────
async def fuse_prediction(
    db,
    *,
    sport: str,
    player: str,
    stat: str,
    opponent: str,
    threshold: Optional[float] = None,
    weights: Optional[dict[str, float]] = None,
    include_simulator: bool = False,
    persist: bool = False,
) -> FusionResult:
    """Fuse the four signal sources into a single projection.

    Never raises — errors are folded into the `notes` list.
    """
    prediction_id = str(uuid.uuid4())
    result = FusionResult(
        prediction_id=prediction_id,
        sport=sport, player=player, stat=stat,
        opponent=opponent, threshold=threshold,
        final_probability=0.0, projected_stat=None,
        confidence="none", model_agreement="insufficient_signals",
        agreement_score=0.0,
        weights_used={},
        created_at=datetime.now(timezone.utc).isoformat(),
    )

    weights_guarded = _guard_weights(weights)
    result.notes.append(f"weights_input={weights_guarded}")

    # 1. Fan out — run each component concurrently.
    tasks = [
        _run_ml_component(db, sport, player, stat, opponent, threshold),
        _run_similar_component(db, sport, player, stat, opponent, threshold),
        _run_player_h2h_component(db, sport, player, stat, opponent, threshold),
    ]
    if include_simulator:
        tasks.append(_run_simulator_component(db, sport, player, stat, opponent, threshold))
    comps: list[ComponentPrediction] = await asyncio.gather(*tasks, return_exceptions=False)
    for cp in comps:
        result.components[cp.name] = cp

    # Ensure all four keys exist in the dict even if simulator disabled.
    for name in COMPONENT_NAMES:
        result.components.setdefault(name, ComponentPrediction(name=name))

    # 2. Renormalise weights to available components.
    avail = {n: cp.available and cp.probability is not None
              for n, cp in result.components.items()}
    result.weights_used = _normalise_weights(weights_guarded, avail)

    # 3. Fused probability.
    probs = [(w, result.components[n].probability)
              for n, w in result.weights_used.items() if w > 0]
    n_signals = len(probs)
    if n_signals > 0:
        total_w = sum(w for w, _ in probs) or 1.0
        result.final_probability = round(
            sum(w * p for w, p in probs) / total_w, 4,
        )
    # Projected stat = weighted avg of available projected values.
    proj_pairs = [(w, result.components[n].projected)
                   for n, w in result.weights_used.items()
                   if w > 0 and result.components[n].projected is not None]
    if proj_pairs:
        total_w = sum(w for w, _ in proj_pairs) or 1.0
        result.projected_stat = round(
            sum(w * v for w, v in proj_pairs) / total_w, 3,
        )

    # 4. Agreement.
    prob_list = [p for _, p in probs]
    agr_label, agr_score = _agreement(prob_list)
    result.model_agreement = agr_label
    result.agreement_score = agr_score

    # 5. Confidence.
    result.confidence = _confidence_label(
        n_signals, agr_score, result.final_probability,
    )

    # 6. Factors + explanation.
    fors, againsts = _build_factors(result.components, threshold)
    result.factors_for = fors
    result.factors_against = againsts
    result.explanation = _build_explanation(
        result.final_probability, result.model_agreement, n_signals,
    )

    # 7. Data sources rollup.
    for name, cp in result.components.items():
        if cp.available:
            result.data_sources_used.append(name)

    # 8. Persist for backtesting (opt-in).
    if persist:
        try:
            await _persist_prediction(db, result)
        except Exception as e:
            result.notes.append(f"persist failed: {e}")

    return result


# ─────────────────────────────────────────────────────────────────────
# Backtesting hooks
# ─────────────────────────────────────────────────────────────────────
async def _persist_prediction(db, result: FusionResult) -> None:
    doc = result.to_dict()
    doc["actual_value"] = None
    doc["outcome"]      = None      # "over" | "under" | "push" (set later)
    doc["correct"]      = None      # True/False
    doc["winning_component"] = None
    await db.fusion_predictions.insert_one(doc)


async def record_prediction_actual(db, prediction_id: str,
                                    actual_value: float) -> dict:
    """Close the loop: record the actual result of a predicted prop.

    Sets `actual_value`, `outcome`, `correct` (based on final_probability
    and threshold), and identifies the `winning_component` — the one
    whose probability was closest to reality (1 if actual > threshold
    else 0).
    """
    doc = await db.fusion_predictions.find_one({"prediction_id": prediction_id})
    if not doc:
        return {"ok": False, "error": "prediction not found"}
    threshold = doc.get("threshold")
    if threshold is None:
        return {"ok": False, "error": "prediction has no threshold to grade"}
    actual = float(actual_value)
    truth = 1.0 if actual > float(threshold) else 0.0
    outcome = ("push" if actual == float(threshold)
                else ("over" if actual > float(threshold) else "under"))
    fused_call = 1.0 if float(doc.get("final_probability") or 0.0) >= 0.5 else 0.0
    correct = bool(fused_call == truth)

    # Winning component = whichever component had the smallest error vs truth.
    winning = None
    best_err = float("inf")
    for name, cp in (doc.get("components") or {}).items():
        p = cp.get("probability") if isinstance(cp, dict) else None
        if p is None:
            continue
        err = abs(float(p) - truth)
        if err < best_err:
            best_err = err
            winning = name

    await db.fusion_predictions.update_one(
        {"prediction_id": prediction_id},
        {"$set": {
            "actual_value": actual,
            "outcome": outcome,
            "correct": correct,
            "winning_component": winning,
            "graded_at": datetime.now(timezone.utc).isoformat(),
        }},
    )
    return {"ok": True, "outcome": outcome, "correct": correct,
             "winning_component": winning}


async def get_backtest_summary(db, sport: Optional[str] = None,
                                stat: Optional[str] = None,
                                confidence: Optional[str] = None,
                                days: int = 90) -> dict:
    """Aggregate accuracy over the trailing window."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    match: dict[str, Any] = {"created_at": {"$gte": since},
                              "actual_value": {"$ne": None}}
    if sport:      match["sport"] = sport
    if stat:       match["stat"] = stat
    if confidence: match["confidence"] = confidence

    total = 0
    correct_ct = 0
    per_comp: dict[str, dict[str, int]] = {
        name: {"wins": 0, "total": 0} for name in COMPONENT_NAMES
    }
    by_stat: dict[str, dict[str, int]] = {}
    async for d in db.fusion_predictions.find(match, {"_id": 0}):
        total += 1
        if d.get("correct"):
            correct_ct += 1
        w = d.get("winning_component")
        if w and w in per_comp:
            per_comp[w]["wins"] += 1
        for n in COMPONENT_NAMES:
            comp = (d.get("components") or {}).get(n) or {}
            if isinstance(comp, dict) and comp.get("available"):
                per_comp[n]["total"] += 1
        s = d.get("stat") or "unknown"
        stat_row = by_stat.setdefault(s, {"n": 0, "correct": 0})
        stat_row["n"] += 1
        if d.get("correct"):
            stat_row["correct"] += 1

    return {
        "window_days":    days,
        "sport":          sport,
        "stat_filter":    stat,
        "confidence":     confidence,
        "n":              total,
        "fused_accuracy": round(correct_ct / total, 4) if total else 0.0,
        "component_wins": per_comp,
        "by_stat":        by_stat,
    }


__all__ = [
    "fuse_prediction",
    "record_prediction_actual",
    "get_backtest_summary",
    "FusionResult",
    "ComponentPrediction",
    "DEFAULT_WEIGHTS",
    "_normalise_weights",
    "_agreement",
    "_confidence_label",
    "_guard_weights",
]
