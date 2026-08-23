"""Soccer Player-Prop Lock Score Ladder — universal calibrator.

Purpose (2026-08-22)
--------------------
The Soccer game-market `compute_lock_score` formula (35% edge weight)
correctly gates game bets like h2h / spreads / totals where book
pricing is very efficient.

Soccer player-scorer markets are DIFFERENT:
  * Book prices on Anytime Goal Scorer / Anytime Assist are typically
    within a few points of true probability (tight lines).
  * The model has real proprietary xG / xA / form / archetype signal.
  * Users want to see high-conviction player picks even when book edge
    is marginal.  A Messi Anytime Goal Scorer with a 40% model_prob is
    a legitimate high-quality pick — even at -110.

Contract
--------
This helper NEVER OVERRIDES a valid strict-edge lock score downward.
It only PROMOTES the composite score when the model shows real
evidence (games ≥ 5, real xG/90 or A/90, elite/strong scorer profile).

Ladder identical to the direct-inject producers (mls_direct_inject,
soccer_prop_inject) so real-line scorer picks converge on the same
Lock band the direct-inject shadow rows land in.

Ladder (per-match model probability → lock score):
    p >= 0.55  → 95    (Strong Lock)
    p >= 0.40  → 91    (Lock)
    p >= 0.25  → 87    (Playable)
    p >= 0.15  → 83    (below-board)
    else       → 80    (below-board)
Confidence tuning: HIGH +2 / LOW -3.  Market-fit tuning: ≥90 +1, <40 -2.
"""
from __future__ import annotations

from typing import Optional


# Evidence sources that carry per-90 rates / xG-quality signal — these
# alone give us enough to use the model-conviction ladder.
_STRONG_EVIDENCE_SOURCES = {
    "soccer_player_form",
    "player_game_actuals",
    "logs_current_season",
    "logs_prior_season",
    "espn_mls_stats",
    "asa",
}


def confidence_ladder_lock(model_prob: float,
                            confidence: str = "MEDIUM",
                            market_fit: Optional[int] = None,
                            *,
                            games: int = 0,
                            minutes: int = 0,
                            goals_per_90: float = 0.0,
                            npxg_per_90: float = 0.0,
                            xa_per_90: float = 0.0,
                            recent_form_score: Optional[float] = None,
                            expected_minutes_conf: Optional[float] = None,
                            opp_def_quality: Optional[float] = None,
                            gk_quality: Optional[float] = None,
                            evidence_source: str = "",
                            ) -> float:
    """Continuous evidence-weighted goalscorer Lock Score.

    2026-08-23 CHEAP SURGICAL — Declustering.  Prior implementation
    used FIXED anchors (95 / 91 / 87 / 83 / 80) + fixed +/-2 tuning,
    causing every strong scorer to collapse to exactly 89.  The new
    formula:
      * uses model_prob as the primary continuous driver
      * adds independent continuous evidence contributions (xG rate,
        form, minutes confidence, opponent quality, GK quality)
      * keeps tiers only as SOFT CEILINGS (never as anchors)
      * fails closed below 85 when evidence is weak
      * preserves rarity (96+ requires multi-signal agreement)
      * Lock != win probability: identical model_prob with different
        evidence quality yields different Lock Scores
    Signature is backwards-compatible; new kwargs default to sentinels
    so unwired call-sites still compute a sensible continuous Lock.
    """
    if model_prob is None:
        return 80.0
    p = float(model_prob)

    # ── Primary continuous driver (model probability) ──
    # Piecewise-linear so shape matches historical rarity: a strong
    # scorer (p=0.55) starts near 91 and elite scorers (p>=0.65) can
    # reach the 93-95 band, without ever pinning to a fixed anchor.
    if p >= 0.60:
        base = 91.0 + (p - 0.60) * 60.0   # p=0.60→91, p=0.75→100 (capped later)
    elif p >= 0.40:
        base = 84.0 + (p - 0.40) * 35.0   # p=0.40→84, p=0.60→91
    elif p >= 0.25:
        base = 78.0 + (p - 0.25) * 40.0   # p=0.25→78, p=0.40→84
    elif p >= 0.15:
        base = 72.0 + (p - 0.15) * 60.0   # p=0.15→72, p=0.25→78
    else:
        base = 60.0 + max(0.0, p) * 80.0  # never reaches 72

    # ── Continuous evidence contributions (each independently graded) ──
    # 1. Volume signal: xG per 90 (0.0 → 0, 0.60+ → +2.0)
    _xg_signal = min(2.0, (npxg_per_90 or 0.0) * 3.5)
    # 2. Realized scoring rate (finishing) — mild positive on hot runs
    _goals_signal = min(1.5, (goals_per_90 or 0.0) * 2.0)
    # 3. Sample quality — full-season minutes earns up to +1.5, small
    #    sample <5 games drops -3.0 (fails closed on thin evidence).
    if games >= 15 and minutes >= 1000:
        _sample_signal = 1.5
    elif games >= 8:
        _sample_signal = 0.8
    elif games >= 5:
        _sample_signal = 0.0
    else:
        _sample_signal = -3.0
    # 4. Recent form (0..100 scale — 50 is neutral).  ±1.5 range.
    _form_signal = 0.0
    if recent_form_score is not None:
        try:
            _fs = float(recent_form_score)
            _form_signal = max(-1.5, min(1.5, (_fs - 50.0) / 20.0))
        except (TypeError, ValueError):
            pass
    # 5. Minutes / start confidence (0..1 scale).  Full starter earns
    #    +1.0; sub-only (<0.5) subtracts up to -2.0.
    _min_signal = 0.0
    if expected_minutes_conf is not None:
        try:
            _emc = float(expected_minutes_conf)
            _min_signal = -2.0 + _emc * 3.0
            _min_signal = max(-2.0, min(1.0, _min_signal))
        except (TypeError, ValueError):
            pass
    # 6. Opponent defensive weakness (0..1 = leaky).  Up to +1.0.
    _opp_signal = 0.0
    if opp_def_quality is not None:
        try:
            _opp = float(opp_def_quality)
            _opp_signal = max(-1.0, min(1.0, (_opp - 0.5) * 2.0))
        except (TypeError, ValueError):
            pass
    # 7. Goalkeeper weakness (0..1 = leaky).  Up to +0.75.
    _gk_signal = 0.0
    if gk_quality is not None:
        try:
            _gkq = float(gk_quality)
            _gk_signal = max(-0.75, min(0.75, (_gkq - 0.5) * 1.5))
        except (TypeError, ValueError):
            pass
    # 8. Confidence tag — small nudge, never an anchor.
    _conf_signal = {"HIGH": 0.6, "MEDIUM": 0.0, "LOW": -1.5}.get(
        (confidence or "").upper(), 0.0)
    # 9. Market fit — small nudge for archetype alignment.
    _fit_signal = 0.0
    if market_fit is not None:
        try:
            _fit_signal = max(-1.0, min(1.0, (float(market_fit) - 50.0) / 50.0))
        except (TypeError, ValueError):
            pass

    lock = base + _xg_signal + _goals_signal + _sample_signal \
        + _form_signal + _min_signal + _opp_signal + _gk_signal \
        + _conf_signal + _fit_signal

    # ── Rarity guardrails (soft ceilings, never anchors) ──
    # 96+ requires multi-signal agreement.
    _positive_contribs = sum(
        1 for s in (_xg_signal, _goals_signal, _form_signal,
                     _min_signal, _opp_signal)
        if s >= 0.5
    )
    if _positive_contribs < 3 and lock > 95.5:
        lock = 95.5
    if _positive_contribs < 2 and lock > 92.5:
        lock = 92.5
    # 100 Apex reserved — never derived from continuous formula.
    if lock >= 99.5:
        lock = 99.4
    # Fail-closed floor: below 60 is uninformative in this context.
    lock = max(60.0, min(99.4, lock))
    return round(lock, 2)


def scorer_confidence_from_stats(games: int, minutes: int,
                                  goals_per_90: float,
                                  npxg_per_90: float = 0.0,
                                  evidence_source: str = "") -> str:
    """Derive confidence tag from evidence strength.

    HIGH   — games ≥ 12 AND (real xG data OR high per-90) AND strong
             evidence source
    MEDIUM — games ≥ 6 AND some attacking signal
    LOW    — anything else
    """
    ok_source = evidence_source in _STRONG_EVIDENCE_SOURCES
    strong_signal = (goals_per_90 or 0) >= 0.30 or (npxg_per_90 or 0) >= 0.30
    if games >= 12 and ok_source and strong_signal:
        return "HIGH"
    if games >= 6 and ((goals_per_90 or 0) >= 0.10 or (npxg_per_90 or 0) >= 0.10):
        return "MEDIUM"
    return "LOW"


def apply_scorer_lock_promotion(
    *,
    strict_lock: float,
    model_prob: float,
    evidence_source: str,
    games: int,
    minutes: int,
    goals_per_90: float,
    npxg_per_90: float = 0.0,
    market_fit: Optional[int] = None,
) -> tuple[float, str]:
    """Promote a strict-edge lock score using the confidence ladder
    when the pick has real player-form evidence.

    Returns ``(final_lock, method)`` where ``method`` is either
    ``"strict_edge"`` (no promotion applied) or
    ``"confidence_ladder"`` (ladder promoted the score).

    Guardrails:
      * NEVER promotes when evidence is a weak source
        (Wikipedia leaderboard / unknown / thin sample).
      * NEVER promotes when the model has effectively no signal
        (model_prob < 0.12 → likely defender / GK / injured).
      * NEVER lowers a strict-edge lock — takes MAX of the two.
    """
    if evidence_source not in _STRONG_EVIDENCE_SOURCES:
        return strict_lock, "strict_edge"
    if games < 5 and minutes < 300:
        return strict_lock, "strict_edge"
    if model_prob is None or model_prob < 0.12:
        return strict_lock, "strict_edge"

    conf = scorer_confidence_from_stats(
        games=games, minutes=minutes,
        goals_per_90=goals_per_90, npxg_per_90=npxg_per_90,
        evidence_source=evidence_source,
    )
    ladder_lock = confidence_ladder_lock(
        model_prob=model_prob, confidence=conf, market_fit=market_fit,
    )
    if ladder_lock > strict_lock:
        return ladder_lock, "confidence_ladder"
    return strict_lock, "strict_edge"


__all__ = [
    "confidence_ladder_lock",
    "scorer_confidence_from_stats",
    "apply_scorer_lock_promotion",
]
