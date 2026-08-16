"""Universal Simulator Provenance Contract — PHASE 2 (2026-06).

Single source of truth for classifying every sport-simulator output.
Consumed by Magic / Bet Quality / Apex evaluators so a simulator's
"agreement" with the model can only be counted as INDEPENDENT
EVIDENCE when its provenance genuinely supports that claim.

CONTRACT
========
Every simulator that emits a probability/distribution must carry:

    provenance     : str
        CAUSAL_INDEPENDENT
            Simulator inputs are causal/mechanistic (Poisson from real
            rates, xG from shot geometry, serve/return from Elo, etc.)
            and are NOT back-solved from a book/model probability.

        EMPIRICAL_INDEPENDENT
            Inputs are recent-form / opponent-specific / matchup-
            specific empirical rows.  Independent of the book/model.

        MODEL_CONDITIONED
            The simulator ingests the CURRENT model probability (or a
            book de-vig probability) as a primary input.  Its output
            distribution is USEFUL for quantiles/tails but MUST NOT
            count as independent agreement with the model.

        PRIOR_ONLY
            Only league / sport prior means available (no player,
            no team, no opponent evidence).  Distribution is generic;
            cannot boost Lock Score / Magic / Apex.

        INVALID
            Simulator failed to produce a coherent output (no inputs,
            NaNs, contradictions).  Must NOT be used to reject the
            model either.

    input_quality  : str
        FULL       – all recommended inputs present (5+ real signals)
        STRONG     – most inputs present (4)
        PARTIAL    – some inputs present (2-3)
        PRIOR_ONLY – only priors / defaults (1)
        INVALID    – 0 real inputs

    decision_valid : bool
        Convenience: True iff simulator output can be treated as
        decision evidence (Provenance in CAUSAL/EMPIRICAL AND
        input_quality in FULL/STRONG/PARTIAL).

Magic / Apex Rules
------------------
An independent-agreement category may be counted ONLY when:

    provenance in {CAUSAL_INDEPENDENT, EMPIRICAL_INDEPENDENT}
    AND input_quality in {FULL, STRONG, PARTIAL}

A MODEL_CONDITIONED simulator may:
    * populate distribution / quantiles (Why This Pick),
    * flag SIM_MODEL_SEVERE_DISAGREEMENT when the sim tail diverges,
but may NOT act as a positive Magic category.

A PRIOR_ONLY or INVALID simulator may NEVER:
    * count as agreement (positive Magic),
    * punish the model (negative Magic / contradiction),
    * boost Lock Score or unlock Apex.

Severe Disagreement
-------------------
Emit ``SIM_MODEL_SEVERE_DISAGREEMENT`` when

    provenance in {CAUSAL_INDEPENDENT, EMPIRICAL_INDEPENDENT}
    AND input_quality in {FULL, STRONG}
    AND |sim_prob - model_prob| >= 0.20

Old Elite/95+ Floors MUST NOT suppress this signal.
"""
from __future__ import annotations

from typing import Any


PROV_CAUSAL      = "CAUSAL_INDEPENDENT"
PROV_EMPIRICAL   = "EMPIRICAL_INDEPENDENT"
PROV_CONDITIONED = "MODEL_CONDITIONED"
PROV_PRIOR       = "PRIOR_ONLY"
PROV_INVALID     = "INVALID"

QUAL_FULL    = "FULL"
QUAL_STRONG  = "STRONG"
QUAL_PARTIAL = "PARTIAL"
QUAL_PRIOR   = "PRIOR_ONLY"
QUAL_INVALID = "INVALID"


VALID_PROVENANCES: frozenset[str] = frozenset({
    PROV_CAUSAL, PROV_EMPIRICAL, PROV_CONDITIONED, PROV_PRIOR, PROV_INVALID,
})
VALID_QUALITIES: frozenset[str] = frozenset({
    QUAL_FULL, QUAL_STRONG, QUAL_PARTIAL, QUAL_PRIOR, QUAL_INVALID,
})

# Provenances that CAN act as independent evidence for Magic/Apex.
_INDEPENDENT_PROVENANCES: frozenset[str] = frozenset({
    PROV_CAUSAL, PROV_EMPIRICAL,
})
# Qualities that qualify for decision use.
_DECISION_QUALITIES: frozenset[str] = frozenset({
    QUAL_FULL, QUAL_STRONG, QUAL_PARTIAL,
})


def classify_input_quality(signals: int) -> str:
    """Standardised signal-count → quality mapping used by every sport."""
    if signals >= 5: return QUAL_FULL
    if signals >= 4: return QUAL_STRONG
    if signals >= 2: return QUAL_PARTIAL
    if signals >= 1: return QUAL_PRIOR
    return QUAL_INVALID


def is_decision_valid(provenance: str, input_quality: str) -> bool:
    """Return True iff the simulator output can be treated as decision
    evidence by Magic / Bet Quality / Apex.
    """
    return (
        provenance in _INDEPENDENT_PROVENANCES
        and input_quality in _DECISION_QUALITIES
    )


def is_independent_agreement(provenance: str, input_quality: str) -> bool:
    """Alias of :func:`is_decision_valid` for Magic-category counters.
    Named for the specific rule "may count as independent agreement"."""
    return is_decision_valid(provenance, input_quality)


def can_flag_severe_disagreement(provenance: str, input_quality: str) -> bool:
    """Return True iff a sim/model contradiction should raise
    ``SIM_MODEL_SEVERE_DISAGREEMENT``.  PRIOR_ONLY / MODEL_CONDITIONED
    / INVALID simulators must NEVER punish the model.
    """
    return (
        provenance in _INDEPENDENT_PROVENANCES
        and input_quality in (QUAL_FULL, QUAL_STRONG)
    )


def severe_disagreement(
    provenance: str,
    input_quality: str,
    sim_prob: float | None,
    model_prob: float | None,
    threshold: float = 0.20,
) -> bool:
    """Emit True when a FULL/STRONG independent simulator diverges from
    the model by ``threshold`` (default 20 percentage points)."""
    if sim_prob is None or model_prob is None:
        return False
    if not can_flag_severe_disagreement(provenance, input_quality):
        return False
    try:
        return abs(float(sim_prob) - float(model_prob)) >= float(threshold)
    except (TypeError, ValueError):
        return False


def stamp_sim_output(
    result: dict[str, Any],
    *,
    provenance: str,
    input_quality: str,
    sim_prob: float | None = None,
    model_prob: float | None = None,
) -> dict[str, Any]:
    """Attach the standard provenance envelope to a simulator output
    dict.  Idempotent — safe to call from anywhere in the pipeline.
    Rejects unknown labels with a defensive INVALID stamp so a mis-
    spelling can never silently claim independent evidence.
    """
    if provenance not in VALID_PROVENANCES:
        provenance = PROV_INVALID
    if input_quality not in VALID_QUALITIES:
        input_quality = QUAL_INVALID
    result["provenance"]     = provenance
    result["input_quality"]  = input_quality
    result["decision_valid"] = is_decision_valid(provenance, input_quality)
    if sim_prob is not None and model_prob is not None:
        result["sim_model_severe_disagreement"] = severe_disagreement(
            provenance, input_quality, sim_prob, model_prob,
        )
    return result


__all__ = [
    "PROV_CAUSAL", "PROV_EMPIRICAL", "PROV_CONDITIONED",
    "PROV_PRIOR", "PROV_INVALID",
    "QUAL_FULL", "QUAL_STRONG", "QUAL_PARTIAL", "QUAL_PRIOR", "QUAL_INVALID",
    "VALID_PROVENANCES", "VALID_QUALITIES",
    "classify_input_quality",
    "is_decision_valid",
    "is_independent_agreement",
    "can_flag_severe_disagreement",
    "severe_disagreement",
    "stamp_sim_output",
]
