"""SportMarketReachability — universal starvation-classifier
================================================================

PERKLOCKS-MAIN 34 · STEP 15 helper.

Consumers (health endpoint, admin dashboard, this session's contract
tests) can call `classify_starvation(sport, family, funnel_counts)`
to bucket a supported sport/market's disappearance into ONE of the
canonical reason codes the user's directive lists. This kills silent
starvation: every zero-published count must carry a reason.

Reason codes (must match the directive verbatim):
    NO_EVENTS
    NO_REAL_MARKETS
    NORMALIZATION_FAILURE
    IDENTITY_FAILURE
    MODEL_UNAVAILABLE
    INTEGRITY_REJECTED
    LEGITIMATELY_BELOW_85
    PUBLICATION_FAILURE
    API_FAILURE
    FRONTEND_FAILURE

Funnel input schema (all int; missing keys default to 0):
    provider_events
    provider_real_markets
    normalized_candidates
    identity_resolved
    model_evaluated
    integrity_passed
    below_85
    at_or_above_85
    published
    served_full_api
    served_lite_api

The classifier walks the funnel top-down and reports the FIRST layer
where the count collapses. Layer ordering matches the directive.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, Optional

from services.universal_market_contract import (
    get as get_market, ACTIVE, MODEL_UNAVAILABLE,
    PROVIDER_UNAVAILABLE, SETTLEMENT_UNAVAILABLE,
)


NO_EVENTS             = "NO_EVENTS"
NO_REAL_MARKETS       = "NO_REAL_MARKETS"
NORMALIZATION_FAILURE = "NORMALIZATION_FAILURE"
IDENTITY_FAILURE      = "IDENTITY_FAILURE"
MODEL_UNAVAILABLE_    = "MODEL_UNAVAILABLE"
INTEGRITY_REJECTED    = "INTEGRITY_REJECTED"
LEGITIMATELY_BELOW_85 = "LEGITIMATELY_BELOW_85"
PUBLICATION_FAILURE   = "PUBLICATION_FAILURE"
API_FAILURE           = "API_FAILURE"
FRONTEND_FAILURE      = "FRONTEND_FAILURE"

# Ordered list of (layer_name, upstream_count_key, downstream_count_key,
#                    reason_code_when_downstream_zero_and_upstream_nonzero)
_FUNNEL = [
    ("events",             None,                   "provider_events",        NO_EVENTS),
    ("markets",            "provider_events",      "provider_real_markets",  NO_REAL_MARKETS),
    ("normalize",          "provider_real_markets", "normalized_candidates", NORMALIZATION_FAILURE),
    ("identity",           "normalized_candidates","identity_resolved",      IDENTITY_FAILURE),
    ("model",              "identity_resolved",    "model_evaluated",        MODEL_UNAVAILABLE_),
    ("integrity",          "model_evaluated",      "integrity_passed",       INTEGRITY_REJECTED),
    # Reachability split — see below.
    ("publish",            "at_or_above_85",       "published",              PUBLICATION_FAILURE),
    ("api_full",           "published",            "served_full_api",        API_FAILURE),
    ("api_lite",           "served_full_api",      "served_lite_api",        API_FAILURE),
]


@dataclass(frozen=True)
class ReachabilityResult:
    sport: str
    family: str
    reason: str
    layer: str
    funnel: Dict[str, int]
    # If capability says ACTIVE but the funnel indicates the market
    # is genuinely unavailable, flag the contradiction.
    contradicts_market_contract: bool = False


def _z(counts: Dict[str, int], key: Optional[str]) -> int:
    if key is None:
        return 1  # sentinel: upstream is always "present" for the first layer
    return int(counts.get(key, 0) or 0)


def classify_starvation(
    sport: str, family: str, funnel_counts: Dict[str, int],
) -> ReachabilityResult:
    """Bucket a supported sport/market's zero-published into one of
    the canonical reason codes above.

    Layer ordering: any layer whose downstream count is 0 but whose
    upstream count is > 0 emits the corresponding reason. If EVERY
    integrity-passed layer is > 0 and yet `published == 0`, we split:
      • if `at_or_above_85 == 0` → LEGITIMATELY_BELOW_85 (expected)
      • else                       → PUBLICATION_FAILURE
    """
    entry = get_market(sport, family)

    # If UniversalMarketContract already says PROVIDER/MODEL unavailable,
    # honour that BEFORE looking at the funnel — it's an honest status,
    # not silent starvation.
    if entry and entry.capability_state == PROVIDER_UNAVAILABLE:
        return ReachabilityResult(sport, family, NO_EVENTS,
                                     "events", funnel_counts, False)
    if entry and entry.capability_state == MODEL_UNAVAILABLE:
        return ReachabilityResult(sport, family, MODEL_UNAVAILABLE_,
                                     "model", funnel_counts, False)

    # Walk the funnel top-down.
    for layer, up, down, reason in _FUNNEL:
        if layer == "publish":
            # Special-case the 85+ threshold branch.
            above = _z(funnel_counts, "at_or_above_85")
            published = _z(funnel_counts, "published")
            if above == 0 and _z(funnel_counts, "integrity_passed") > 0:
                # Model produced candidates but NONE reached 85.
                return ReachabilityResult(
                    sport, family, LEGITIMATELY_BELOW_85,
                    "publish", funnel_counts, False,
                )
            if above > 0 and published == 0:
                return ReachabilityResult(
                    sport, family, PUBLICATION_FAILURE,
                    "publish", funnel_counts,
                    contradicts_market_contract=bool(
                        entry and entry.capability_state == ACTIVE
                    ),
                )
            continue
        up_c = _z(funnel_counts, up)
        down_c = _z(funnel_counts, down)
        if up_c > 0 and down_c == 0:
            return ReachabilityResult(
                sport, family, reason, layer, funnel_counts,
                contradicts_market_contract=bool(
                    entry and entry.capability_state == ACTIVE
                    and reason in (MODEL_UNAVAILABLE_,)
                ),
            )

    # Everything flowed through and something is published. If the
    # frontend layer explicitly reports zero renders while served > 0,
    # THAT is a FRONTEND_FAILURE (visible-but-invisible class).
    if (
        "frontend_rendered" in funnel_counts
        and _z(funnel_counts, "frontend_rendered") == 0
        and _z(funnel_counts, "served_lite_api") > 0
    ):
        return ReachabilityResult(sport, family, FRONTEND_FAILURE,
                                     "frontend", funnel_counts, False)

    # Healthy path — nothing to classify.
    return ReachabilityResult(sport, family, "HEALTHY",
                                 "healthy", funnel_counts, False)
