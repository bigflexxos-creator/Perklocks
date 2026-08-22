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
                            market_fit: Optional[int] = None) -> float:
    """Return the model-conviction lock score for a player-scorer pick.

    ``confidence`` — "HIGH" / "MEDIUM" / "LOW".  Defaults to MEDIUM.
    ``market_fit`` — optional 0-100 archetype/market fit score.
    """
    if model_prob is None:
        return 80.0
    p = float(model_prob)
    if p >= 0.55:
        lock = 95.0
    elif p >= 0.40:
        lock = 91.0
    elif p >= 0.25:
        lock = 87.0
    elif p >= 0.15:
        lock = 83.0
    else:
        lock = 80.0

    if confidence == "HIGH":
        lock = min(99.0, lock + 2.0)
    elif confidence == "LOW":
        lock = max(75.0, lock - 3.0)

    if market_fit is not None:
        if market_fit >= 90:
            lock = min(99.0, lock + 1.0)
        elif market_fit < 40:
            lock = max(75.0, lock - 2.0)

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
