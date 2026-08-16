"""Block 8 — Controlled Magic → Lock Score Integration.

Single, controlled entry point that translates a certified ``MagicOutput``
into a bounded adjustment on top of the existing Lock Score, and separately
gates APEX 100 assignment.

Contract (per user-approved Phase 2 proposal, 2026-06 Block 8):

1. Magic REFINES — never replaces — the finalised base Lock Score.
2. Positive Magic uplift is HARD-CAPPED and asymmetric relative to negative
   downside:

       base < 80  →  positive cap +0.5
       80 – 89    →  positive cap +1.0
       90 – 94    →  positive cap +1.5
       95 – 98    →  positive cap +1.0
       99         →  positive cap  0.0
       negative cap  −4.0  (all base buckets)

3. Contradictions can only REDUCE / BLOCK — never inflate.
4. Non-APEX HARD CAP = **99.0**.  Only the explicit APEX gate assigns 100.
5. APEX 100 ≠ 100 % win probability.  It is a badge for exceptionally
   supported, contradiction-free multi-category evidence stacks.
6. Correlated evidence (MODEL_PROBABILITY / SIMULATOR_PROBABILITY /
   CALIBRATED_PROBABILITY) collapses to ONE independent vote.
7. Same-source HISTORICAL_EXACT_THRESHOLD + RECENT_FORM collapse to one.
8. Sport / market not on the APEX whitelist → APEX unavailable.
9. Defensive downgrade — see ``defensive_downgrade_if_needed`` — a
   ``lock_score`` of 100 without ``apex_lock=True`` is forced back to 99.

The module writes only to the pick dictionary; no DB access.  Persistence
happens later in the refresh pipeline via the normal pick-writer flow.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from services.magic.contract import (
    Availability, EvidenceItem, EvidenceType, MagicOutput, MagicTier,
)
from services.magic.apex_gate import (
    APEX_ELIGIBLE_SPORTS,
    ApexDecision,
    evaluate_apex,
)

logger = logging.getLogger("lockscore.magic.block8")


BLOCK8_INTEGRATOR_VERSION = "block8_magic.v1.0"
NON_APEX_HARD_CAP = 99.0
APEX_SCORE = 100.0
NEG_CAP = 4.0
MIN_CATEGORY_CONFIDENCE = 0.6


# ─────────────────────────────────────────────────────────────────────────
# Evidence category grouping
# ─────────────────────────────────────────────────────────────────────────

# Independent categories used by the APEX gate + the bounded-delta engine.
CATEGORY_HISTORY = "history_exact"
CATEGORY_FORM    = "recent_form"
CATEGORY_ROLE    = "role_opportunity"
CATEGORY_MATCHUP = "matchup"
CATEGORY_MODEL   = "model_family"
CATEGORY_MARKET  = "market_intel"

ALL_CATEGORIES = (
    CATEGORY_HISTORY, CATEGORY_FORM, CATEGORY_ROLE,
    CATEGORY_MATCHUP, CATEGORY_MODEL, CATEGORY_MARKET,
)

_CATEGORY_MAP: dict[EvidenceType, str] = {
    EvidenceType.HISTORICAL_EXACT_THRESHOLD: CATEGORY_HISTORY,
    EvidenceType.RECENT_FORM:                CATEGORY_FORM,
    EvidenceType.ROLE_OPPORTUNITY:           CATEGORY_ROLE,
    EvidenceType.LINEUP_INJURY:              CATEGORY_ROLE,
    EvidenceType.MATCHUP:                    CATEGORY_MATCHUP,
    EvidenceType.OPPONENT_STRENGTH:          CATEGORY_MATCHUP,
    EvidenceType.SURFACE_CONTEXT:            CATEGORY_MATCHUP,
    EvidenceType.MODEL_PROBABILITY:          CATEGORY_MODEL,
    EvidenceType.SIMULATOR_PROBABILITY:      CATEGORY_MODEL,
    EvidenceType.CALIBRATED_PROBABILITY:     CATEGORY_MODEL,
    EvidenceType.SPORTSBOOK_CONSENSUS:       CATEGORY_MARKET,
    EvidenceType.LINE_MOVEMENT:              CATEGORY_MARKET,
    EvidenceType.CLV:                        CATEGORY_MARKET,
}


@dataclass
class CategoryVote:
    """Aggregated per-category vote used by the delta engine + APEX gate."""

    category:     str
    available:    bool = False
    positive:     bool = False
    contradictory: bool = False
    max_confidence: float = 0.0
    n_items:       int = 0
    sources:       set[str] = field(default_factory=set)

    def sources_tuple(self) -> tuple[str, ...]:
        return tuple(sorted(self.sources))


def _pos(direction: Optional[str]) -> bool:
    return (direction or "").strip().lower() == "positive"


def _neg(direction: Optional[str]) -> bool:
    return (direction or "").strip().lower() == "negative"


def _source_key(ev: EvidenceItem) -> str:
    """Identifier for source-independence checks (A + B collapse)."""
    src = (ev.source or "").strip().lower()
    cls = (ev.source_class or "").strip().lower()
    return f"{cls}::{src}"


def categorize_evidence(mo: MagicOutput) -> dict[str, CategoryVote]:
    """Group ``MagicOutput.evidence`` into the 6 independent categories.

    Rules enforced here:
      • Same-source A (HISTORICAL_EXACT_THRESHOLD) + B (RECENT_FORM)
        collapse to ONE positive vote (via ``collapse_history_form``).
      • Category E (Model / Sim / Calibration) is ALWAYS one vote —
        this is enforced structurally by the category map.
      • Within a category, multiple items count as one vote regardless
        of how many sub-signals fire (this naturally handles
        soccer shots + SOT + xG — they all sit inside one category and
        cannot inflate to multiple votes).
    """
    votes: dict[str, CategoryVote] = {c: CategoryVote(c) for c in ALL_CATEGORIES}

    for ev in mo.evidence:
        cat = _CATEGORY_MAP.get(ev.evidence_type)
        if cat is None:
            continue
        v = votes[cat]
        v.n_items += 1
        v.sources.add(_source_key(ev))
        if ev.availability == Availability.CONTRADICTORY:
            v.contradictory = True
        # An item contributes to "available" if it's AVAILABLE or PARTIAL
        # (both are usable evidence — STALE/UNAVAILABLE/CONTRADICTORY are not).
        if ev.availability in (Availability.AVAILABLE, Availability.PARTIAL):
            v.available = True
            if _pos(ev.direction):
                conf = float(ev.confidence or 0.0)
                if conf >= MIN_CATEGORY_CONFIDENCE:
                    v.positive = True
                    v.max_confidence = max(v.max_confidence, conf)
    return votes


def collapse_history_form(votes: dict[str, CategoryVote]) -> None:
    """A + B same-source collapse.

    If A (history exact) and B (recent form) are BOTH positive AND every
    contributing source_key overlaps, they represent the same underlying
    game log and count as ONE independent category — we knock out B.

    Mutates ``votes`` in place.
    """
    a = votes.get(CATEGORY_HISTORY)
    b = votes.get(CATEGORY_FORM)
    if not (a and b and a.positive and b.positive):
        return
    if a.sources and b.sources and a.sources.issubset(b.sources | a.sources):
        # If the source sets fully overlap (identical), collapse B.
        if a.sources == b.sources:
            b.positive = False
            b.max_confidence = 0.0


def count_positive_categories(votes: dict[str, CategoryVote]) -> list[str]:
    """Return the ordered list of category names that count as positive."""
    return [c for c in ALL_CATEGORIES if votes[c].positive]


def count_contradictory_categories(votes: dict[str, CategoryVote]) -> list[str]:
    return [c for c in ALL_CATEGORIES if votes[c].contradictory]


def count_available_categories(votes: dict[str, CategoryVote]) -> list[str]:
    return [c for c in ALL_CATEGORIES if votes[c].available]


# ─────────────────────────────────────────────────────────────────────────
# Positive-uplift cap by base score bucket
# ─────────────────────────────────────────────────────────────────────────

def positive_cap_for_base(base_score: float) -> float:
    """Bucketed positive-uplift cap.

    <80   →  0.5
    80-89 →  1.0
    90-94 →  1.5
    95-98 →  1.0
    99    →  0.0
    """
    if base_score >= 99.0:
        return 0.0
    if base_score >= 95.0:
        return 1.0
    if base_score >= 90.0:
        return 1.5
    if base_score >= 80.0:
        return 1.0
    return 0.5


# ─────────────────────────────────────────────────────────────────────────
# Delta computation
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class MagicDeltaResult:
    delta:                  float
    positive_cap_applied:   float
    negative_cap_applied:   float
    reasons:                list[str] = field(default_factory=list)
    categories_positive:    list[str] = field(default_factory=list)
    categories_contradictory: list[str] = field(default_factory=list)
    categories_available:   list[str] = field(default_factory=list)
    risk_capped:            bool = False
    contradiction_capped:   bool = False
    insufficient_evidence:  bool = False


def _score_from_magic(mo: MagicOutput) -> float:
    """Map ``MagicOutput.magic_score`` (0..100 already) to a bounded signed
    delta candidate BEFORE applying caps.

    A magic_score is a positive-evidence composite:
      * 50 = neutral
      * >50 = supportive lean
      * <50 = risk lean

    We linearise it into a ±5 signed candidate.  Caps then squeeze into
    the allowed range.
    """
    if not mo.magic_score_available or mo.magic_score is None:
        return 0.0
    ms = float(mo.magic_score)
    ms = max(0.0, min(100.0, ms))
    # 50 → 0.0 ; 100 → +5.0 ; 0 → -5.0
    return (ms - 50.0) / 10.0


def compute_magic_delta(base_score: float, mo: MagicOutput,
                          votes: Optional[dict[str, CategoryVote]] = None
                          ) -> MagicDeltaResult:
    """Compute the bounded Magic delta for a pick.

    ``base_score`` MUST be the *finalised* base lock score (after all
    prior writers — sim anchor, bandit, BvP, matchup bump, rank_boost).
    """
    if votes is None:
        votes = categorize_evidence(mo)
        collapse_history_form(votes)

    positive_cats = count_positive_categories(votes)
    contradictory_cats = count_contradictory_categories(votes)
    available_cats = count_available_categories(votes)

    pos_cap = positive_cap_for_base(base_score)
    neg_cap = NEG_CAP
    reasons: list[str] = []

    contradiction_capped = False
    risk_capped = False
    insufficient = False

    # Tier-driven caps
    if mo.magic_tier == MagicTier.INSUFFICIENT_EVIDENCE:
        # Both directions locked to 0.  Magic contributes nothing.
        insufficient = True
        reasons.append("insufficient_evidence:zero_delta")
        return MagicDeltaResult(
            delta=0.0, positive_cap_applied=0.0, negative_cap_applied=0.0,
            reasons=reasons,
            categories_positive=positive_cats,
            categories_contradictory=contradictory_cats,
            categories_available=available_cats,
            insufficient_evidence=True,
        )

    if mo.magic_tier in (MagicTier.CONFLICTED, MagicTier.RISK_ELEVATED):
        pos_cap = 0.0
        reasons.append(f"tier={mo.magic_tier.value}:positive_delta_zeroed")
        contradiction_capped = mo.magic_tier == MagicTier.CONFLICTED
        risk_capped = mo.magic_tier == MagicTier.RISK_ELEVATED

    # Contradiction caps
    n_contra = len(contradictory_cats)
    if n_contra >= 2:
        pos_cap = min(pos_cap, 0.0)
        contradiction_capped = True
        reasons.append(f"contradictions>=2:{n_contra}_categories:positive_delta_zeroed")
    elif n_contra == 1:
        pos_cap = min(pos_cap, 0.5)
        contradiction_capped = True
        reasons.append(f"contradictions=1:{contradictory_cats[0]}:positive_delta_capped_+0.5")

    # RISK_ELEVATED risk flags -- override positive cap regardless of tier
    if mo.risk_flags:
        pos_cap = min(pos_cap, 0.5)
        risk_capped = True
        reasons.append(f"risk_flags:{','.join(mo.risk_flags)}:positive_delta_capped")

    # Candidate delta from the magic_score
    candidate = _score_from_magic(mo)

    # Clamp: negative cap always -NEG_CAP..0 available; positive up to pos_cap.
    if candidate >= 0:
        delta = min(candidate, pos_cap)
    else:
        delta = max(candidate, -neg_cap)

    delta = round(delta, 2)

    if positive_cats:
        reasons.append(f"positive_categories:{','.join(positive_cats)}")
    if contradictory_cats:
        reasons.append(f"contradictory_categories:{','.join(contradictory_cats)}")

    return MagicDeltaResult(
        delta=delta,
        positive_cap_applied=pos_cap,
        negative_cap_applied=neg_cap,
        reasons=reasons,
        categories_positive=positive_cats,
        categories_contradictory=contradictory_cats,
        categories_available=available_cats,
        risk_capped=risk_capped,
        contradiction_capped=contradiction_capped,
        insufficient_evidence=insufficient,
    )


# ─────────────────────────────────────────────────────────────────────────
# Grade helpers
# ─────────────────────────────────────────────────────────────────────────

def block8_grade(score: float, apex_lock: bool) -> str:
    """Grade labels including the new APEX_LOCK tier."""
    if apex_lock and score >= APEX_SCORE:
        return "APEX Lock"
    if score >= 98:
        return "Elite Lock"
    if score >= 95:
        return "Strong Lock"
    if score >= 90:
        return "Lock"
    if score >= 85:
        return "Playable"
    return "Pass"


def block8_tier(score: float, apex_lock: bool) -> str:
    if apex_lock and score >= APEX_SCORE:
        return "APEX_LOCK"
    if score >= 99.0:
        return "PEAK_NON_APEX"
    if score >= 98:
        return "ELITE_LOCK"
    if score >= 95:
        return "STRONG_LOCK"
    if score >= 90:
        return "LOCK"
    if score >= 85:
        return "PLAYABLE"
    return "PASS"


def defensive_downgrade_if_needed(pick: dict) -> None:
    """If ``lock_score`` == 100 but ``apex_lock`` isn't True, force to 99.

    This is the safety net that guarantees non-APEX picks can NEVER
    display as 100 regardless of any upstream bug.
    """
    try:
        ls = float(pick.get("lock_score") or 0.0)
    except (TypeError, ValueError):
        return
    if ls >= APEX_SCORE and not pick.get("apex_lock"):
        pick["lock_score"] = NON_APEX_HARD_CAP
        pick["apex_defensive_downgrade"] = True


# ─────────────────────────────────────────────────────────────────────────
# Immutable pregame score snapshot
# ─────────────────────────────────────────────────────────────────────────

def snapshot_pregame_score(pick: dict) -> None:
    """Freezes an immutable pregame snapshot of the score triple.

    Written once per pick — subsequent calls are no-ops.  This gives
    the settlement layer / analytics an unambiguous "what was the score
    at publication time" number, independent of any future recomputes.
    """
    if pick.get("pregame_score_snapshot") is not None:
        return
    snap = {
        "lock_score":     pick.get("lock_score"),
        "lock_score_v3_base":  pick.get("lock_score_v3_base"),
        "lock_score_v3_delta": pick.get("lock_score_v3_delta"),
        "apex_lock":      bool(pick.get("apex_lock", False)),
        "grade":          pick.get("grade"),
        "tier":           pick.get("tier"),
        "block8_version": BLOCK8_INTEGRATOR_VERSION,
    }
    pick["pregame_score_snapshot"] = snap


# ─────────────────────────────────────────────────────────────────────────
# Main entry: apply Magic + APEX to a single pick
# ─────────────────────────────────────────────────────────────────────────

def apply_magic_and_apex(pick: dict, mo: MagicOutput) -> dict[str, Any]:
    """Mutate ``pick`` — apply bounded Magic delta, then evaluate APEX.

    Returns a small audit dict with the decisions taken (also mirrored
    onto ``pick`` as ``magic_delta_reasons`` / ``apex_reasons`` / …).

    Settled picks (won / lost / void / push) are NOT re-scored — Magic
    would only rewrite history.  Immutable snapshot is left intact.
    """
    status = pick.get("status")
    if status in ("won", "lost", "void", "push"):
        return {"skipped": "settled", "status": status}

    try:
        base = float(pick.get("lock_score") or 0.0)
    except (TypeError, ValueError):
        return {"skipped": "no_base_lock_score"}

    votes = categorize_evidence(mo)
    collapse_history_form(votes)

    delta_res = compute_magic_delta(base, mo, votes=votes)
    delta = delta_res.delta

    # Refined score (non-APEX ceiling = 99).
    refined = max(0.0, min(NON_APEX_HARD_CAP, base + delta))
    refined = round(refined, 1)

    # Stamp Magic delta provenance (regardless of APEX outcome).
    pick["lock_score_v3_base"]         = round(base, 1)
    pick["lock_score_v3_delta"]        = delta
    pick["lock_score_v3_positive_cap"] = delta_res.positive_cap_applied
    pick["lock_score_v3_negative_cap"] = delta_res.negative_cap_applied
    pick["magic_categories_available"] = list(delta_res.categories_available)
    pick["magic_categories_positive"]  = list(delta_res.categories_positive)
    pick["magic_categories_contradictory"] = list(delta_res.categories_contradictory)
    pick["magic_delta_reasons"]        = list(delta_res.reasons)
    pick["magic_tier_at_integration"]  = mo.magic_tier.value
    pick["block8_integrator_version"]  = BLOCK8_INTEGRATOR_VERSION

    # APEX gate — evaluated against the BASE score (pre-Magic).
    apex_dec: ApexDecision = evaluate_apex(
        base_score=base,
        mo=mo,
        pick=pick,
        categories_positive=delta_res.categories_positive,
        categories_contradictory=delta_res.categories_contradictory,
        categories_available=delta_res.categories_available,
    )
    pick["apex_gate_version"] = apex_dec.gate_version

    if apex_dec.eligible:
        # Explicit APEX — bypass the 99 cap.
        pick["lock_score"]    = APEX_SCORE
        pick["apex_lock"]     = True
        pick["apex_reasons"]  = list(apex_dec.requirements_met)
        pick["apex_block_reason"] = None
        pick["grade"]         = block8_grade(APEX_SCORE, True)
        pick["tier"]          = block8_tier(APEX_SCORE, True)
        pick["confidence"]    = "Very High"
    else:
        # Non-APEX — cap at 99.
        pick["lock_score"]    = refined
        pick["apex_lock"]     = False
        pick["apex_reasons"]  = []
        pick["apex_block_reason"] = apex_dec.block_reason
        pick["grade"]         = block8_grade(refined, False)
        pick["tier"]          = block8_tier(refined, False)
        # Confidence is left to the existing writer; we only re-stamp
        # the grade because the tier band may have moved.

    # Defensive downgrade — invariant safeguard.
    defensive_downgrade_if_needed(pick)
    # Immutable snapshot for settlement/analytics.
    snapshot_pregame_score(pick)

    # PHASE 1B (2026-06) — Magic/APEX Final-State Freeze.
    # Stamp ``magic_final=True`` **only for APEX picks** so the
    # downstream evidence governor cannot silently demote a
    # legitimately-gated APEX 100 to 99 (the audit-confirmed bug).
    # Non-APEX picks stay eligible for the evidence-governor
    # multiplier — the Magic delta is additive on top of a base
    # lock that still benefits from evidence-quality haircuts
    # (matches pre-Phase-1B production behaviour for non-APEX).
    if pick.get("apex_lock") is True:
        pick["magic_final"] = True

    return {
        "base": round(base, 1),
        "delta": delta,
        "final": pick["lock_score"],
        "apex_lock": pick["apex_lock"],
        "apex_block_reason": pick.get("apex_block_reason"),
        "positive_categories": list(delta_res.categories_positive),
        "contradictory_categories": list(delta_res.categories_contradictory),
    }


# ─────────────────────────────────────────────────────────────────────────
# Batch entry — for use by the refresh orchestrator.
# ─────────────────────────────────────────────────────────────────────────

async def apply_block8_magic_to_picks(db, picks: list[dict]) -> dict[str, Any]:
    """Run Magic evidence build + Block 8 integration for every pick.

    Failure-tolerant: any per-pick exception is logged but the batch
    continues so a single bad pick can't poison the slate.  Returns
    a summary suitable for logging + tests.
    """
    from services.magic.adapters import build_evidence

    counts = {
        "considered": 0,
        "delta_applied": 0,
        "positive_delta": 0,
        "negative_delta": 0,
        "zero_delta":     0,
        "apex_assigned":  0,
        "apex_blocked":   0,
        "insufficient_evidence": 0,
        "errors":         0,
    }

    for pick in picks:
        counts["considered"] += 1
        try:
            mo = await build_evidence(db, pick)
        except Exception as e:                      # pragma: no cover
            logger.warning("Magic evidence build failed for pick %s: %s",
                            (pick.get("id") or "?")[:8], e)
            counts["errors"] += 1
            continue
        if mo is None:                              # pragma: no cover
            counts["errors"] += 1
            continue
        try:
            audit = apply_magic_and_apex(pick, mo)
        except Exception as e:                      # pragma: no cover
            logger.warning("Magic integration failed for pick %s: %s",
                            (pick.get("id") or "?")[:8], e)
            counts["errors"] += 1
            continue

        counts["delta_applied"] += 1
        d = audit.get("delta") or 0.0
        if d > 0:
            counts["positive_delta"] += 1
        elif d < 0:
            counts["negative_delta"] += 1
        else:
            counts["zero_delta"] += 1

        if audit.get("apex_lock"):
            counts["apex_assigned"] += 1
        else:
            counts["apex_blocked"] += 1

        if mo.magic_tier == MagicTier.INSUFFICIENT_EVIDENCE:
            counts["insufficient_evidence"] += 1

    return counts


__all__ = [
    "BLOCK8_INTEGRATOR_VERSION",
    "NON_APEX_HARD_CAP",
    "APEX_SCORE",
    "APEX_ELIGIBLE_SPORTS",
    "CATEGORY_HISTORY", "CATEGORY_FORM", "CATEGORY_ROLE",
    "CATEGORY_MATCHUP", "CATEGORY_MODEL", "CATEGORY_MARKET",
    "ALL_CATEGORIES",
    "CategoryVote", "MagicDeltaResult",
    "categorize_evidence", "collapse_history_form",
    "count_positive_categories",
    "count_contradictory_categories",
    "count_available_categories",
    "positive_cap_for_base",
    "compute_magic_delta",
    "block8_grade", "block8_tier",
    "defensive_downgrade_if_needed",
    "snapshot_pregame_score",
    "apply_magic_and_apex",
    "apply_block8_magic_to_picks",
]
