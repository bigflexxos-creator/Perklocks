"""Block 2C (2026-08) — Explicit cache-state model + 422 isolation contract.

Vocabulary that lets every consumer distinguish:

    "empty because provider genuinely returned zero" vs
    "empty because provider errored" vs
    "empty because market is unsupported globally" vs
    "empty because budget blocked the request" vs
    "empty because we haven't retried yet after 422".

Spec §4, §5, §12, §18 vocabulary.  Reason codes are STRING enums so
they can flow through JSON caches, logs, and the pipeline_diagnostic
framework unchanged.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional


# ═══════════════════════════════════════════════════════════════════
# Canonical cache-state enum (spec §4)
# ═══════════════════════════════════════════════════════════════════
class CacheState(str, enum.Enum):
    VALID_DATA               = "VALID_DATA"
    VALID_EMPTY_PROVIDER_ZERO = "VALID_EMPTY_PROVIDER_ZERO"
    EVENT_NOT_READY          = "EVENT_NOT_READY"
    PROVIDER_ERROR           = "PROVIDER_ERROR"
    PROVIDER_CIRCUIT_OPEN    = "PROVIDER_CIRCUIT_OPEN"
    BUDGET_BLOCKED           = "BUDGET_BLOCKED"
    PRIORITY_SHED            = "PRIORITY_SHED"
    PARTIAL_MARKET_RESPONSE  = "PARTIAL_MARKET_RESPONSE"
    MARKET_UNSUPPORTED       = "MARKET_UNSUPPORTED"
    EVENT_MARKET_422         = "EVENT_MARKET_422"
    PARTIAL_422_UNRESOLVED   = "PARTIAL_422_UNRESOLVED"
    STALE_DATA               = "STALE_DATA"
    EXPIRED                  = "EXPIRED"


# ═══════════════════════════════════════════════════════════════════
# Differentiated TTL policy (spec §5)
# ═══════════════════════════════════════════════════════════════════
# Seconds.  A "None" TTL means "never treat as valid — always retry".
_TTL_POLICY: dict[CacheState, Optional[int]] = {
    CacheState.VALID_DATA:                600,  # 10 min baseline
    CacheState.VALID_EMPTY_PROVIDER_ZERO: 180,  # 3 min — provider may add later
    CacheState.EVENT_NOT_READY:           240,  # 4 min short retry
    CacheState.PROVIDER_ERROR:             60,  # 1 min backoff
    CacheState.PROVIDER_CIRCUIT_OPEN:     120,  # 2 min while breaker open
    CacheState.BUDGET_BLOCKED:             30,  # very short — budget resets
    CacheState.PRIORITY_SHED:              30,  # same as budget
    CacheState.PARTIAL_MARKET_RESPONSE:   300,  # 5 min then re-request missing
    CacheState.MARKET_UNSUPPORTED:      86400,  # 24 h — matches registry
    CacheState.EVENT_MARKET_422:         3600,  # 1 h event-scoped
    CacheState.PARTIAL_422_UNRESOLVED:   600,   # 10 min - retry allowed
    CacheState.STALE_DATA:                  0,  # never fresh
    CacheState.EXPIRED:                     0,
}


def ttl_seconds(state: CacheState) -> int:
    """Return TTL in seconds for the given state (per-policy)."""
    v = _TTL_POLICY.get(state)
    return int(v or 0)


def near_first_pitch_ttl(state: CacheState,
                          minutes_to_first_pitch: Optional[int]) -> int:
    """Shorten empty/error TTLs near first pitch (spec §6 sketch).

    NOTE: the actual scheduler-side wiring is spec §6 and DEFERRED
    to Block 2C-cont.  This helper is provided so downstream code
    can adopt the policy without a scheduler-touching change today.
    """
    base = ttl_seconds(state)
    if minutes_to_first_pitch is None:
        return base
    if minutes_to_first_pitch <= 120 and state in (
            CacheState.VALID_EMPTY_PROVIDER_ZERO,
            CacheState.EVENT_NOT_READY,
            CacheState.PARTIAL_MARKET_RESPONSE):
        return min(base, 120)   # 2 min close to first pitch
    return base


# ═══════════════════════════════════════════════════════════════════
# Envelope helpers — attach reason & state to every cache row
# ═══════════════════════════════════════════════════════════════════
@dataclass
class CacheEnvelope:
    state:         CacheState
    data:          Any
    reason:        Optional[str] = None
    provider:      Optional[str] = None
    fetched_at:    Optional[str] = None
    expires_at:    Optional[str] = None
    markets_present: list[str] = field(default_factory=list)
    markets_missing: list[str] = field(default_factory=list)
    error_detail:  Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "state": self.state.value,
            "data":  self.data,
            "reason": self.reason,
            "provider": self.provider,
            "fetched_at": self.fetched_at,
            "expires_at": self.expires_at,
            "markets_present": self.markets_present,
            "markets_missing": self.markets_missing,
            "error_detail": self.error_detail,
        }


# ═══════════════════════════════════════════════════════════════════
# 422 bundle-isolation contract (spec §2)
# ═══════════════════════════════════════════════════════════════════
# HARD LIMITS to prevent an unbounded one-at-a-time explosion when
# a large market bundle fails.  These constants are the ONLY place
# retry policy is defined; every retrying caller MUST honor them.
MAX_422_RETRY_REQUESTS = 8          # bundle 422 → ≤8 extra requests
MAX_422_RETRY_CREDITS  = 8          # spend at most 8 credits isolating
MAX_422_RETRY_DEPTH    = 3          # recursion depth cap for bisection


@dataclass
class BundleIsolationResult:
    """Result of a bundle-422 isolation pass."""
    supported_markets:   list[str]
    bad_markets:         list[str]
    retries_used:        int
    credits_used:        int
    state:               CacheState
    unresolved_markets:  list[str] = field(default_factory=list)


async def isolate_bad_markets(
    bundle_markets: list[str],
    single_request_fn,
    *,
    max_retries: int = MAX_422_RETRY_REQUESTS,
    max_credits: int = MAX_422_RETRY_CREDITS,
    max_depth:   int = MAX_422_RETRY_DEPTH,
) -> BundleIsolationResult:
    """Isolate 422-offending markets from a bundle via bounded
    bisection.

    ``single_request_fn`` — an async callable taking a list[str] of
    markets and returning EITHER:
        * a dict of {market: <payload>} on success (all supported),
        * ``None`` / raises on a 422 for the whole subset,
        * a partial dict when partially supported.

    The isolation is bisection with a hard depth cap.  For a bundle
    of N markets, worst-case fanout is O(log N) requests but we ALSO
    enforce ``max_retries``/``max_credits`` as absolute ceilings.

    If we can't finish the isolation within budget, we return
    ``PARTIAL_422_UNRESOLVED`` with the remaining ambiguous set.
    """
    supported:  list[str] = []
    bad:        list[str] = []
    unresolved: list[str] = []
    retries = 0
    credits = 0

    async def _probe(subset: list[str], depth: int) -> None:
        nonlocal retries, credits
        if not subset:
            return
        if retries >= max_retries or credits >= max_credits:
            unresolved.extend(subset)
            return
        try:
            retries += 1
            credits += 1
            result = await single_request_fn(subset)
        except Exception:
            result = None
        if result is None:
            # Whole subset 422'd.
            if len(subset) == 1:
                bad.append(subset[0])
                return
            if depth >= max_depth:
                unresolved.extend(subset)
                return
            mid = len(subset) // 2
            await _probe(subset[:mid], depth + 1)
            await _probe(subset[mid:], depth + 1)
        elif isinstance(result, dict):
            got_markets = list(result.keys())
            supported.extend(got_markets)
            missing = [m for m in subset if m not in result]
            if missing:
                if depth >= max_depth:
                    unresolved.extend(missing)
                else:
                    await _probe(missing, depth + 1)
        else:
            unresolved.extend(subset)

    await _probe(list(bundle_markets), 0)

    if unresolved and not bad:
        state = CacheState.PARTIAL_422_UNRESOLVED
    elif bad and not unresolved:
        state = CacheState.EVENT_MARKET_422
    elif bad and unresolved:
        state = CacheState.PARTIAL_422_UNRESOLVED
    else:
        state = CacheState.VALID_DATA
    return BundleIsolationResult(
        supported_markets=supported, bad_markets=bad,
        retries_used=retries, credits_used=credits,
        state=state, unresolved_markets=unresolved)


# ═══════════════════════════════════════════════════════════════════
# Pipeline diagnostic mapping (spec §12/§18)
# ═══════════════════════════════════════════════════════════════════
def cache_state_to_reason_code(state: CacheState) -> str:
    """Map a CacheState to a services.pipeline_diagnostic.ReasonCode.

    The diagnostic framework already carries all these codes.  This
    mapping guarantees every consumer emits the SAME reason string
    regardless of whether they route via cache or provider directly.
    """
    from services.pipeline_diagnostic import ReasonCode
    return {
        CacheState.VALID_DATA:                None,
        CacheState.VALID_EMPTY_PROVIDER_ZERO: ReasonCode.EMPTY_CACHE.value,
        CacheState.EVENT_NOT_READY:           ReasonCode.EVENT_TIME_FILTER.value,
        CacheState.PROVIDER_ERROR:            ReasonCode.PROVIDER_ERROR.value,
        CacheState.PROVIDER_CIRCUIT_OPEN:     ReasonCode.PROVIDER_ERROR.value,
        CacheState.BUDGET_BLOCKED:            ReasonCode.BUDGET_BLOCKED.value,
        CacheState.PRIORITY_SHED:             ReasonCode.BUDGET_BLOCKED.value,
        CacheState.PARTIAL_MARKET_RESPONSE:   ReasonCode.PARTIAL_MARKET_SNAPSHOT.value,
        CacheState.MARKET_UNSUPPORTED:        ReasonCode.MARKET_NOT_SUPPORTED.value,
        CacheState.EVENT_MARKET_422:          ReasonCode.PROVIDER_422.value,
        CacheState.PARTIAL_422_UNRESOLVED:    ReasonCode.PROVIDER_422.value,
        CacheState.STALE_DATA:                ReasonCode.STALE_CACHE_USED.value,
        CacheState.EXPIRED:                   ReasonCode.STALE_CACHE_USED.value,
    }.get(state)


__all__ = [
    "CacheState",
    "CacheEnvelope",
    "BundleIsolationResult",
    "MAX_422_RETRY_REQUESTS",
    "MAX_422_RETRY_CREDITS",
    "MAX_422_RETRY_DEPTH",
    "isolate_bad_markets",
    "ttl_seconds",
    "near_first_pitch_ttl",
    "cache_state_to_reason_code",
]
