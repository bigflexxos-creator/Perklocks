"""Magic Layer 2.0 — Magic Score aggregation.

Compact, transparent aggregation across evidence items.  Produces a
score in [0, 100] AND a tier.  The scoring is intentionally
minimal — Magic 2.0's job at this stage is to EXPOSE evidence, not
to replace Lock Score.

Rules
─────
* Never emits a score when there is INSUFFICIENT_EVIDENCE.
* PROVISIONAL / UNRESOLVED identity ⇒ tier =
  INSUFFICIENT_EVIDENCE regardless of the mix of AVAILABLE items.
* Risk flags subtract from the score but do NOT flip a positive
  signal to a negative one — the tier logic exposes the conflict.
"""
from __future__ import annotations

from typing import Any

from services.magic.contract import (
    Availability, EvidenceItem, EvidenceType, MagicOutput, MagicTier,
)


# Positive / negative contribution per evidence type when available.
_POSITIVE_WEIGHT: dict[EvidenceType, float] = {
    EvidenceType.HISTORICAL_EXACT_THRESHOLD: 25.0,
    EvidenceType.RECENT_FORM:                12.0,
    EvidenceType.ROLE_OPPORTUNITY:           15.0,
    EvidenceType.MATCHUP:                    10.0,
    EvidenceType.MODEL_PROBABILITY:          15.0,
    EvidenceType.SIMULATOR_PROBABILITY:      8.0,
    EvidenceType.CALIBRATED_PROBABILITY:     5.0,
    EvidenceType.SPORTSBOOK_CONSENSUS:       10.0,
    EvidenceType.SURFACE_CONTEXT:            5.0,
    EvidenceType.OPPONENT_STRENGTH:          5.0,
}


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def compute_magic_score(
    output: MagicOutput,
    *,
    identity_class: str | None = None,
) -> None:
    """Populate ``output.magic_score`` / ``magic_tier`` /
    ``strongest_positive`` / ``strongest_negative`` in-place."""
    # Identity gate — Magic score never emitted for provisional id
    # when the pick is a player market.  Team markets pass.
    ic = (identity_class or output.identity_class or "").upper()
    is_player_market = bool(output.canonical_player_id)
    if is_player_market and ic not in ("AUTHORITATIVE", "MAPPED"):
        output.magic_score = None
        output.magic_tier  = MagicTier.INSUFFICIENT_EVIDENCE
        output.magic_score_available = False
        return

    # Count evidence availability distribution.
    n_available = sum(1 for e in output.evidence
                        if e.availability == Availability.AVAILABLE)
    n_partial   = sum(1 for e in output.evidence
                        if e.availability == Availability.PARTIAL)
    if n_available + n_partial < 2:
        output.magic_score = None
        output.magic_tier  = MagicTier.INSUFFICIENT_EVIDENCE
        output.magic_score_available = False
        return

    # Score = sum over evidence: weight × direction_signal.
    raw = 0.0
    max_positive: tuple[float, EvidenceItem] | None = None
    max_negative: tuple[float, EvidenceItem] | None = None
    for ev in output.evidence:
        if ev.availability not in (Availability.AVAILABLE,
                                     Availability.PARTIAL):
            continue
        w = _POSITIVE_WEIGHT.get(ev.evidence_type, 0.0)
        if w == 0.0:
            continue
        # Direction-multiplier: positive=+1, neutral=+0.3, negative=-1.
        d = (ev.direction or "").lower()
        mult = (1.0 if d == "positive"
                else -1.0 if d == "negative" else 0.3)
        # Availability discount.
        av_mult = 1.0 if ev.availability == Availability.AVAILABLE else 0.6
        contrib = w * mult * av_mult
        raw += contrib
        if contrib > 0 and (max_positive is None or contrib > max_positive[0]):
            max_positive = (contrib, ev)
        if contrib < 0 and (max_negative is None or contrib < max_negative[0]):
            max_negative = (contrib, ev)

    # Risk-flag penalty — each risk flag reduces the score by 3 pts
    # (but a risk flag can never TURN a positive into negative — we
    # cap the penalty at 60% of the raw value).
    risk_penalty = min(len(output.risk_flags) * 3.0, max(0.0, raw) * 0.6)
    scaled = 50.0 + raw - risk_penalty
    score  = _clamp(scaled)

    output.magic_score = round(score, 2)
    output.magic_score_available = True
    output.strongest_positive = (
        f"{max_positive[1].evidence_type.value}"
        f" ({max_positive[1].label or 'strong positive'})"
        if max_positive else None
    )
    output.strongest_negative = (
        f"{max_negative[1].evidence_type.value}"
        f" ({max_negative[1].label or 'strong negative'})"
        if max_negative else None
    )

    # Tier assignment.
    if len(output.risk_flags) >= 3 and score < 55:
        output.magic_tier = MagicTier.RISK_ELEVATED
    elif max_positive and max_negative and score >= 40 and score <= 60:
        output.magic_tier = MagicTier.CONFLICTED
    elif score >= 80:
        output.magic_tier = MagicTier.ALIGNED_STRONG
    elif score >= 65:
        output.magic_tier = MagicTier.ALIGNED
    elif score >= 45:
        output.magic_tier = MagicTier.NEUTRAL
    else:
        output.magic_tier = (MagicTier.RISK_ELEVATED
                              if output.risk_flags
                              else MagicTier.NEUTRAL)


__all__ = ["compute_magic_score"]
