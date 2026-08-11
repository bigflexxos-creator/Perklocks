"""Shared Production-Truth Vocabulary (§2, §3).

This module is the SINGLE source of truth for:

* The canonical set of production stages ``ProductionStage``.
* The four possible per-stage states ``StageStatus`` — including
  the explicit ``NOT_APPLICABLE`` state (§2 rule: an irrelevant
  stage must NEVER receive a fake PASS).
* The canonical drop-reason contract ``DropReason`` (§3).  These
  codes are the ONLY vocabulary allowed when a candidate drops
  between stages.  Existing ``services.pipeline_diagnostic.ReasonCode``
  values are re-exported / aliased so we do not create a duplicate
  taxonomy — the two enums are kept intentionally compatible.

Design notes
------------
* All members are ``str`` enums so JSON serialisation is trivial
  and stable.
* No stage/reason may silently collapse into another — every
  distinct condition gets its own code.
* ``NOT_APPLICABLE`` is not a synonym for PASS and never counts
  toward "supported" status.
"""
from __future__ import annotations

import enum
from typing import Optional

# Re-use the existing catalogue so the two vocabularies remain
# congruent — the Production-Truth Contract is a superset.
from services.pipeline_diagnostic import ReasonCode as _LegacyReasonCode


# ═══════════════════════════════════════════════════════════════════
# Canonical Production Stages (§2)
# ═══════════════════════════════════════════════════════════════════
class ProductionStage(str, enum.Enum):
    """Every stage a candidate must traverse from raw source to
    measurable settled result.  The order matches the physical
    production pipeline and must not be reordered lightly."""

    DATA_AVAILABLE          = "DATA_AVAILABLE"
    IDENTITY_RESOLVED       = "IDENTITY_RESOLVED"
    CURRENT_EVENT_VALID     = "CURRENT_EVENT_VALID"
    CURRENT_ROSTER_VALID    = "CURRENT_ROSTER_VALID"
    REAL_MARKET_AVAILABLE   = "REAL_MARKET_AVAILABLE"
    EVIDENCE_AVAILABLE      = "EVIDENCE_AVAILABLE"
    MODEL_CONSUMED          = "MODEL_CONSUMED"
    CANDIDATE_GENERATED     = "CANDIDATE_GENERATED"
    CANONICAL_PUBLISHED     = "CANONICAL_PUBLISHED"
    VISIBLE_TO_CONSUMER     = "VISIBLE_TO_CONSUMER"
    LOCKS_ELIGIBLE          = "LOCKS_ELIGIBLE"
    ROLLOVER_ELIGIBLE       = "ROLLOVER_ELIGIBLE"
    PARLAY_ELIGIBLE         = "PARLAY_ELIGIBLE"
    PREGAME_FROZEN          = "PREGAME_FROZEN"
    SETTLED                 = "SETTLED"
    MEASURABLE              = "MEASURABLE"


# ═══════════════════════════════════════════════════════════════════
# Per-stage state (§2 — NOT_APPLICABLE is explicit, never fake PASS)
# ═══════════════════════════════════════════════════════════════════
class StageStatus(str, enum.Enum):
    PASS            = "PASS"
    FAIL            = "FAIL"
    UNKNOWN         = "UNKNOWN"          # cannot be proven either way
    NOT_APPLICABLE  = "NOT_APPLICABLE"   # stage does not apply here


def stage_status_pass(evidence: Optional[str] = None) -> dict:
    """Structured PASS state.  ``evidence`` should be a short
    machine-readable trace (file:function or record id) — never a
    marketing string.  Missing evidence is allowed but discouraged.
    """
    return {"status": StageStatus.PASS.value, "evidence": evidence}


def stage_status_fail(reason: "DropReason | str",
                       detail: Optional[str] = None) -> dict:
    code = reason.value if isinstance(reason, DropReason) else str(reason)
    return {"status": StageStatus.FAIL.value,
            "reason": code, "detail": detail}


def stage_status_unknown(detail: Optional[str] = None) -> dict:
    return {"status": StageStatus.UNKNOWN.value, "detail": detail}


def stage_status_not_applicable(detail: Optional[str] = None) -> dict:
    """Explicit NOT_APPLICABLE.  §2: an irrelevant stage must NEVER
    receive a fake PASS.  Use this whenever a stage legitimately
    does not apply (e.g. CURRENT_ROSTER_VALID for a game-line
    market)."""
    return {"status": StageStatus.NOT_APPLICABLE.value, "detail": detail}


def is_terminal(status: str) -> bool:
    """A stage is *terminal* when the reachability chain cannot
    proceed past it.  Only FAIL and UNKNOWN are terminal — PASS and
    NOT_APPLICABLE both allow the chain to continue."""
    return status in (StageStatus.FAIL.value, StageStatus.UNKNOWN.value)


# ═══════════════════════════════════════════════════════════════════
# Drop-Reason Contract (§3)
# ═══════════════════════════════════════════════════════════════════
class DropReason(str, enum.Enum):
    """Explicit drop-reason contract.  Reuses existing codes from
    ``services.pipeline_diagnostic.ReasonCode`` wherever they already
    represent the same condition — the string value is kept
    identical so producers can emit either enum interchangeably.
    """

    # ── source / data ──────────────────────────────────────────
    SOURCE_UNAVAILABLE          = "SOURCE_UNAVAILABLE"
    # ── identity ───────────────────────────────────────────────
    IDENTITY_UNRESOLVED         = _LegacyReasonCode.PLAYER_IDENTITY_UNRESOLVED.value
    STALE_IDENTITY              = "STALE_IDENTITY"
    # ── event / roster ─────────────────────────────────────────
    CURRENT_EVENT_INVALID       = "CURRENT_EVENT_INVALID"
    CURRENT_ROSTER_INVALID      = "CURRENT_ROSTER_INVALID"
    # ── real market / line ─────────────────────────────────────
    MARKET_UNAVAILABLE          = _LegacyReasonCode.MARKET_NOT_SUPPORTED.value
    REAL_LINE_UNAVAILABLE       = _LegacyReasonCode.NO_REAL_LINE.value
    # ── evidence ───────────────────────────────────────────────
    EVIDENCE_UNAVAILABLE        = "EVIDENCE_UNAVAILABLE"
    INSUFFICIENT_EVIDENCE       = _LegacyReasonCode.ENGINE_INSUFFICIENT_DATA.value
    # ── model / engine ─────────────────────────────────────────
    MODEL_INPUT_INVALID         = "MODEL_INPUT_INVALID"
    MODEL_REJECTED              = _LegacyReasonCode.ENGINE_OUTPUT_IGNORED.value
    # ── candidate / publication ────────────────────────────────
    CANDIDATE_FILTERED          = _LegacyReasonCode.CANDIDATE_NOT_GENERATED.value
    PUBLICATION_REJECTED        = _LegacyReasonCode.PUBLICATION_BARRIER_REJECT.value
    NON_CANONICAL_WRITE         = _LegacyReasonCode.NON_CANONICAL_WRITE.value
    # ── consumer eligibility ───────────────────────────────────
    BOARD_INELIGIBLE            = "BOARD_INELIGIBLE"
    ROLLOVER_INELIGIBLE         = _LegacyReasonCode.ROLLOVER_MARKET_BLOCKED.value
    PARLAY_INELIGIBLE           = _LegacyReasonCode.PARLAY_MARKET_BLOCKED.value
    # ── freeze / settlement / analytics ────────────────────────
    FREEZE_FAILED               = "FREEZE_FAILED"
    SETTLEMENT_PENDING          = "SETTLEMENT_PENDING"
    SETTLEMENT_FAILED           = "SETTLEMENT_FAILED"
    ACTUALS_UNAVAILABLE         = "ACTUALS_UNAVAILABLE"
    ANALYTICS_WRITE_FAILED      = "ANALYTICS_WRITE_FAILED"
    # ── legacy compatibility ───────────────────────────────────
    LEGACY_MISSING_METADATA     = "LEGACY_MISSING_METADATA"


# Convenience mapping — every stage lists the drop-reasons that are
# semantically appropriate at that stage.  Consumers may still emit
# a different code (nothing enforces the mapping at runtime), but
# ``DropReason.for_stage`` is the recommended default lookup.
_STAGE_DEFAULT_REASONS: dict[ProductionStage, tuple[DropReason, ...]] = {
    ProductionStage.DATA_AVAILABLE:          (DropReason.SOURCE_UNAVAILABLE,),
    ProductionStage.IDENTITY_RESOLVED:       (DropReason.IDENTITY_UNRESOLVED,
                                              DropReason.STALE_IDENTITY),
    ProductionStage.CURRENT_EVENT_VALID:     (DropReason.CURRENT_EVENT_INVALID,),
    ProductionStage.CURRENT_ROSTER_VALID:    (DropReason.CURRENT_ROSTER_INVALID,),
    ProductionStage.REAL_MARKET_AVAILABLE:   (DropReason.MARKET_UNAVAILABLE,
                                              DropReason.REAL_LINE_UNAVAILABLE),
    ProductionStage.EVIDENCE_AVAILABLE:      (DropReason.EVIDENCE_UNAVAILABLE,
                                              DropReason.INSUFFICIENT_EVIDENCE),
    ProductionStage.MODEL_CONSUMED:          (DropReason.MODEL_INPUT_INVALID,
                                              DropReason.MODEL_REJECTED),
    ProductionStage.CANDIDATE_GENERATED:     (DropReason.CANDIDATE_FILTERED,),
    ProductionStage.CANONICAL_PUBLISHED:     (DropReason.PUBLICATION_REJECTED,
                                              DropReason.NON_CANONICAL_WRITE),
    ProductionStage.VISIBLE_TO_CONSUMER:     (DropReason.BOARD_INELIGIBLE,),
    ProductionStage.LOCKS_ELIGIBLE:          (DropReason.BOARD_INELIGIBLE,),
    ProductionStage.ROLLOVER_ELIGIBLE:       (DropReason.ROLLOVER_INELIGIBLE,),
    ProductionStage.PARLAY_ELIGIBLE:         (DropReason.PARLAY_INELIGIBLE,),
    ProductionStage.PREGAME_FROZEN:          (DropReason.FREEZE_FAILED,),
    ProductionStage.SETTLED:                 (DropReason.SETTLEMENT_PENDING,
                                              DropReason.SETTLEMENT_FAILED,
                                              DropReason.ACTUALS_UNAVAILABLE),
    ProductionStage.MEASURABLE:              (DropReason.ANALYTICS_WRITE_FAILED,),
}


def default_drop_reasons(stage: ProductionStage) -> tuple[DropReason, ...]:
    """Return the canonical drop-reasons for a given stage."""
    return _STAGE_DEFAULT_REASONS.get(stage, ())


__all__ = [
    "ProductionStage",
    "StageStatus",
    "DropReason",
    "stage_status_pass",
    "stage_status_fail",
    "stage_status_unknown",
    "stage_status_not_applicable",
    "is_terminal",
    "default_drop_reasons",
]
