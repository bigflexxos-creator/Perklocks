"""Universal pipeline reachability + observability contract (2026-06).

Every candidate that enters the production pipeline for ANY sport MUST
terminate in one of the allowed states — never in a silent
``UNKNOWN_DISAPPEARED`` state.

Producers stamp `ReachabilityCounters` per sport per refresh cycle so
we can:
  * detect whole-sport disappearance,
  * detect supported-market drop paths,
  * reconcile ``supported_markets_seen == candidate_generated +
    legitimate_rejections`` at the batch level,
  * flag first-N truncations and UTC-boundary losses.

The counter is intentionally aggregate (no per-row logging spam).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ── Terminal-state contract ──────────────────────────────────────────
# Every candidate MUST end in one of these six states.  New states may
# only be added with an accompanying regression test.
ALLOWED_TERMINAL_STATES: tuple[str, ...] = (
    "GENERATED",
    "UNSUPPORTED_MARKET",
    "IDENTITY_REJECTED",
    "MISSING_REQUIRED_EVIDENCE",
    "PROVIDER_UNAVAILABLE",
    "CANONICAL_PUBLICATION_REJECTED",
)


# ── Supported-market classification (audit contract) ────────────────
CLASSIFICATION_UNAVAILABLE_PROVIDER = "PROVIDER_UNAVAILABLE"
CLASSIFICATION_UNSUPPORTED_MARKET   = "UNSUPPORTED_MARKET"
CLASSIFICATION_SUPPORTED_BLOCKED    = "SUPPORTED_MARKET_BLOCKED"   # ← BUG
CLASSIFICATION_NOT_LOCKS_QUALIFIED  = "GENERATED_NOT_LOCKS_QUALIFIED"


@dataclass
class ReachabilityCounters:
    """Aggregate counters for one sport for one refresh cycle."""

    sport: str
    counts: dict[str, int] = field(default_factory=dict)
    reasons: dict[str, dict[str, int]] = field(default_factory=dict)

    def record(self, terminal_state: str, reason: Optional[str] = None) -> None:
        if terminal_state not in ALLOWED_TERMINAL_STATES:
            raise ValueError(
                f"UNKNOWN_DISAPPEARED not allowed: {terminal_state!r}. "
                f"Must be one of {ALLOWED_TERMINAL_STATES}")
        self.counts[terminal_state] = self.counts.get(terminal_state, 0) + 1
        if reason is not None:
            bucket = self.reasons.setdefault(terminal_state, {})
            bucket[reason] = bucket.get(reason, 0) + 1

    def reconcile(self, *, supported_markets_seen: int) -> tuple[bool, str]:
        """Verify: supported_markets_seen ==
        GENERATED + IDENTITY_REJECTED + MISSING_REQUIRED_EVIDENCE +
        CANONICAL_PUBLICATION_REJECTED.

        Returns (ok, detail).
        """
        legitimate_rejections = (
            self.counts.get("IDENTITY_REJECTED", 0)
            + self.counts.get("MISSING_REQUIRED_EVIDENCE", 0)
            + self.counts.get("CANONICAL_PUBLICATION_REJECTED", 0)
        )
        accounted = self.counts.get("GENERATED", 0) + legitimate_rejections
        if accounted == supported_markets_seen:
            return True, "reconciled"
        return False, (
            f"unaccounted: supported_markets_seen={supported_markets_seen} "
            f"but accounted={accounted} "
            f"(diff={supported_markets_seen - accounted}). "
            f"counts={self.counts}"
        )

    def as_dict(self) -> dict:
        return {**self.counts, "_reasons": self.reasons, "_sport": self.sport}


__all__ = [
    "ALLOWED_TERMINAL_STATES",
    "CLASSIFICATION_UNAVAILABLE_PROVIDER",
    "CLASSIFICATION_UNSUPPORTED_MARKET",
    "CLASSIFICATION_SUPPORTED_BLOCKED",
    "CLASSIFICATION_NOT_LOCKS_QUALIFIED",
    "ReachabilityCounters",
]
