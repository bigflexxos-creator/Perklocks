"""Elite Evidence Gate — Phase 2 (2026-08-11).

Runs LATE in the pick refresh pipeline, AFTER every evidence-producing
enrichment has landed (form, sim, fusion, bandit) and BEFORE
``tag_board_visibility`` snapshots off-board reasons.

Purpose
───────
`elite_players.apply_elite_boost` applies a reputation-anchored lock
boost (+15 clamped to [95, 99]) very early in the pipeline, before any
evidence enrichment has run.  That boost is now **provisional**: this
gate re-examines each elite-tagged pick using the enrichment signals
that have since arrived and, if multi-source evidence does NOT agree,
restores the pre-boost lock score.

Contract
────────
* Fame alone can never sustain a 95-99 lock.  Multi-source agreement
  is required (≥ 2 positive signals AND net positive score).
* PRESERVE the elite concept — a passing evidence gate keeps the
  full elite boost, so genuinely-elite matchup situations still land
  in Elite Lock tier.
* Demotion behaviour: on failure we restore ``pre_elite_lock_score``
  exactly (see `elite_players.apply_elite_boost` for where that is
  stamped).  Board eligibility then follows the normal ``>85``
  contract — a demoted pick with a pre-boost lock ≤ 85 falls off; a
  pick with a pre-boost lock > 85 remains on Locks at that value.
  We do NOT force demoted picks off-board.
* Never hardcodes player names.

Evidence signals consulted (all optional; missing → 0)
──────────────────────────────────────────────────────
* ``edge_percent``               — market disagreement / positive EV
* ``player_form.classification`` — hot / cold / neutral
* ``sim_result``                 — MC simulator consensus
* ``fusion``                     — Prediction Fusion agreement
* ``factors``                    — first-order model factors
* ``learning``                   — historical hit-rate adjustment

Each signal contributes +1 / 0 / -1.  Gate PASSES when
``sum(positive) >= 2 AND sum(positive) - sum(negative) >= 1``.

Trace metadata written on the pick
──────────────────────────────────
    elite_gate_passed    : bool
    elite_gate_demoted   : bool
    elite_gate_signals   : { signal_name: +1 / 0 / -1 }
    elite_gate_score     : int  # net = positives - negatives
    pre_elite_lock_score : float (stamped by apply_elite_boost)

DO NOT change Lock Score formulas from this module.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("lockscore.elite_evidence_gate")


# ── Signal thresholds ────────────────────────────────────────────────
_EDGE_POS = 2.0     # edge_percent ≥ +2 → positive
_EDGE_NEG = -3.0    # edge_percent ≤ -3 → negative
_FUSION_AGREE_POS = 0.03    # fusion vs model_prob delta ≥ +3pp → positive
_FUSION_DISAGREE_NEG = -0.05  # ≤ -5pp → negative


def _f(v, default=0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _classify_form(pick: dict[str, Any]) -> int:
    """`player_form` is attached by `player_form.apply_player_form`
    with keys `classification` in {hot, cold, neutral} + `delta_pct`.
    Missing/neutral is 0.
    """
    pf = pick.get("player_form")
    if not isinstance(pf, dict):
        return 0
    cls = str(pf.get("classification") or "").lower()
    if cls == "hot":
        return +1
    if cls == "cold":
        return -1
    return 0


def _classify_sim(pick: dict[str, Any]) -> int:
    """`sim_result` produced by `brain.sim_runner.apply_simulations`
    (MLB) and `sim_engine.simulate_board` (all sports).  Keys we
    care about: ``consensus`` / ``direction`` / ``model_delta``.
    """
    sim = pick.get("sim_result")
    if not isinstance(sim, dict):
        return 0
    consensus = str(sim.get("consensus") or "").lower()
    if consensus in ("stronger", "confirms", "over_agree", "agree_over"):
        return +1
    if consensus in ("weaker", "fades", "under_agree", "disagree"):
        return -1
    # Fall back to model_delta if present.
    md = sim.get("model_delta")
    if md is not None:
        d = _f(md)
        if d >= 3.0:
            return +1
        if d <= -5.0:
            return -1
    return 0


def _classify_fusion(pick: dict[str, Any]) -> int:
    """`fusion` is attached by `services/pick_fusion_decorator`.  We
    surface it here as bounded SUPPORTING evidence — never as a
    replacement for model probability.  A supported fusion whose
    ``final_probability`` agrees with the model win probability by ≥
    +3pp is positive; disagreement of ≥ 5pp is negative.
    """
    fus = pick.get("fusion")
    if not isinstance(fus, dict) or not fus.get("supported"):
        return 0
    fp = _f(fus.get("final_probability"))
    if fp <= 0:
        return 0
    # Normalise both sides to [0, 1] before comparing.
    if fp > 1.0:
        fp = fp / 100.0
    wp = _f(pick.get("win_probability"))
    if wp > 1.0:
        wp = wp / 100.0
    if wp <= 0:
        return 0
    delta = fp - wp
    if delta >= _FUSION_AGREE_POS:
        return +1
    if delta <= _FUSION_DISAGREE_NEG:
        return -1
    return 0


def _classify_edge(pick: dict[str, Any]) -> int:
    edge = _f(pick.get("edge_percent"), default=0.0)
    if edge >= _EDGE_POS:
        return +1
    if edge <= _EDGE_NEG:
        return -1
    return 0


def _classify_factors(pick: dict[str, Any]) -> int:
    """Aggregate the model's first-order factors dict.  A factor
    value ≥ 0.6 is positive; ≤ 0.35 is negative.  Only counts when
    factors dict actually contains numeric values."""
    f = pick.get("factors")
    if not isinstance(f, dict) or not f:
        return 0
    pos = neg = 0
    for v in f.values():
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if fv >= 0.6:
            pos += 1
        elif fv <= 0.35:
            neg += 1
    if pos >= 2 and pos > neg:
        return +1
    if neg >= 2 and neg > pos:
        return -1
    return 0


def _classify_learning(pick: dict[str, Any]) -> int:
    """`learning` block is attached by `learning_engine.apply_learning`.
    Positive lift ⇒ positive; negative lift ⇒ negative."""
    lg = pick.get("learning")
    if not isinstance(lg, dict):
        return 0
    adj = _f(lg.get("adjustment_bp") or lg.get("delta"))
    if adj >= 200:      # +2pp probability adjustment
        return +1
    if adj <= -300:     # -3pp
        return -1
    return 0


# ── Public API ───────────────────────────────────────────────────────
def evaluate_elite_gate(pick: dict[str, Any]) -> dict[str, Any]:
    """Return the gate verdict for one pick.

    Result:
        {
          "eligible": True/False,   # gate applies at all?
          "signals":  {name: +1/0/-1},
          "positives": int,
          "negatives": int,
          "score":    positives - negatives,
          "passed":   bool,
        }
    """
    if not pick.get("elite_player"):
        return {"eligible": False, "signals": {}, "positives": 0,
                "negatives": 0, "score": 0, "passed": True}

    signals = {
        "edge":     _classify_edge(pick),
        "form":     _classify_form(pick),
        "sim":      _classify_sim(pick),
        "fusion":   _classify_fusion(pick),
        "factors":  _classify_factors(pick),
        "learning": _classify_learning(pick),
    }
    positives = sum(1 for v in signals.values() if v > 0)
    negatives = sum(1 for v in signals.values() if v < 0)
    score = positives - negatives
    # Multi-source agreement: at LEAST two positive signals AND net
    # positive score.  A single positive signal (even a strong one) is
    # not sufficient to justify a 95-99 elite lock.
    passed = positives >= 2 and score >= 1
    return {
        "eligible": True,
        "signals":  signals,
        "positives": positives,
        "negatives": negatives,
        "score":    score,
        "passed":   passed,
    }


def apply_elite_evidence_gate(picks: list[dict[str, Any]]) -> dict[str, int]:
    """Apply the evidence gate to every elite-tagged pick in the batch.

    Demoted picks:
      * ``lock_score`` restored to ``pre_elite_lock_score`` (exact
        pre-boost value).
      * ``grade`` and ``confidence`` re-derived.
      * ``elite_gate_demoted = True`` written for observability.
      * ``elite_player`` stays True — the pick is still flagged as
        the star's line for downstream badging, but no longer rides a
        reputation boost.  This preserves the reputation prior while
        removing the artificial score.

    Returns a small stats dict for logging.
    """
    stats = {"total_elite": 0, "passed": 0, "demoted": 0, "skipped": 0}
    if not picks:
        return stats

    _grade_fn = _conf_fn = None
    try:
        from sports_engine import _grade, _confidence
        _grade_fn, _conf_fn = _grade, _confidence
    except Exception as e:
        logger.debug("sports_engine grade/confidence import failed: %s", e)

    for p in picks:
        if not p.get("elite_player"):
            continue
        pre = p.get("pre_elite_lock_score")
        if pre is None:
            # No boost was applied to this pick (e.g. synthetic AGS/FGS
            # picks born as elite).  We do NOT touch them here — they
            # keep their computed lock.  Future iterations may extend
            # the gate to synthetic picks with a separate policy.
            stats["skipped"] += 1
            continue
        stats["total_elite"] += 1
        verdict = evaluate_elite_gate(p)
        p["elite_gate_signals"] = verdict["signals"]
        p["elite_gate_score"] = verdict["score"]
        p["elite_gate_passed"] = verdict["passed"]
        p["elite_gate_demoted"] = not verdict["passed"]
        if verdict["passed"]:
            stats["passed"] += 1
            continue
        # ── DEMOTE — restore pre-boost Lock Score exactly ─────────
        try:
            restored = round(float(pre), 1)
        except (TypeError, ValueError):
            restored = 0.0
        p["lock_score"] = restored
        if _grade_fn and _conf_fn:
            try:
                p["grade"] = _grade_fn(restored)
                p["confidence"] = _conf_fn(restored)
            except Exception:
                pass
        # Tag reason on the pick so `why_missing` / debug UI can trace.
        p.setdefault("elite_gate_reason",
                     "insufficient_multi_source_evidence")
        stats["demoted"] += 1
    return stats


__all__ = [
    "evaluate_elite_gate",
    "apply_elite_evidence_gate",
]
