"""Phase 4C — MLB Rejection Counters + Lineup / Starter Gates + Bookmaker Metadata.

Structured, thread-safe (asyncio-safe under sequential access) counters
that record WHY MLB candidate picks failed to publish.  Exposed via an
admin-safe diagnostics helper — never leaks provider secrets.

Also ships the lineup-gate helper (:func:`classify_lineup_status`) and
the confidence-cap helper (:func:`data_quality_cap_for_status`) used at
the emission tier.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock
from typing import Optional


# ── Rejection reason enum ───────────────────────────────────────────
REJECTION_REASONS: tuple[str, ...] = (
    "provider_market_missing",       # provider didn't return the market for the event
    "provider_line_missing",         # provider returned market but not the specific line
    "invalid_player_identity",       # cannot resolve player to a stable id
    "missing_feature_data",          # has_enough_real_data gate failed
    "lineup_not_confirmed",          # projected but not confirmed starter (with cap)
    "lineup_scratched",              # player scratched from lineup
    "lineup_bench",                  # player on the bench
    "lineup_unknown",                # lineup status entirely unknown
    "data_quality_block",            # data-quality score below the hard floor
    "implied_probability_gate",      # implied probability below the market gate
    "edge_gate",                     # edge below the required threshold
    "ev_gate",                       # expected value below the required threshold
    "duplicate_contract",            # same (player, market_family, line, side, book)
    "correlation_conflict",          # over/under contradiction, or cross-market conflict
    "stale_odds",                    # odds timestamp beyond staleness limit
    "publication_error",             # emit failed at the last mile
    "sim_invalid",                   # simulator returned invalid result
    "sim_uncertainty_cap",           # posterior uncertainty capped confidence to reject
)


@dataclass
class _CounterState:
    counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    counts_by_market: dict[str, dict[str, int]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(int)))
    # ── 2026-06 μ-closure — five-market funnel telemetry ─────────
    # Per-family step counters: provider_received → candidate_created
    # → model_evaluated → passed_model → published → board_visible.
    # Reset()able and exposed via snapshot().
    funnel_by_market: dict[str, dict[str, int]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(int)))
    since: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


_state = _CounterState()
_lock = Lock()


# ── Funnel step-counter API ─────────────────────────────────────────
FUNNEL_STEPS: tuple[str, ...] = (
    "provider_received",
    "candidate_created",
    "model_evaluated",
    "passed_model",
    "published",
    "board_visible",
)


def record_funnel_step(step: str, *, market_key: str, amount: int = 1) -> None:
    """Increment a step counter for ``market_key`` in the production
    funnel.  Unknown steps are silently dropped."""
    if step not in FUNNEL_STEPS or not market_key:
        return
    with _lock:
        _state.funnel_by_market[market_key][step] += amount


def record_rejection(reason: str, *,
                       market_key: Optional[str] = None,
                       amount: int = 1) -> None:
    """Record ``amount`` rejections of ``reason``.  Unknown reasons
    are silently dropped (no crash — the enum is authoritative)."""
    if reason not in REJECTION_REASONS:
        return
    with _lock:
        _state.counts[reason] += amount
        if market_key:
            _state.counts_by_market[market_key][reason] += amount


def snapshot() -> dict:
    """Admin-safe snapshot of the current counters."""
    with _lock:
        return {
            "since": _state.since,
            "totals": dict(_state.counts),
            "by_market": {mk: dict(v) for mk, v in _state.counts_by_market.items()},
            "funnel_by_market": {
                mk: dict(v) for mk, v in _state.funnel_by_market.items()
            },
            "funnel_steps": list(FUNNEL_STEPS),
            "reasons": list(REJECTION_REASONS),
        }


def reset() -> None:
    with _lock:
        _state.counts.clear()
        _state.counts_by_market.clear()
        _state.funnel_by_market.clear()
        _state.since = datetime.now(timezone.utc).isoformat()


# ── Lineup / starter gates ──────────────────────────────────────────
LINEUP_STATES = ("confirmed_starter", "projected_starter",
                  "bench", "scratched", "unknown")


def classify_lineup_status(
    *,
    lineup_confirmed: Optional[bool] = None,
    is_starter: Optional[bool] = None,
    scratched: Optional[bool] = None,
    on_bench: Optional[bool] = None,
    lineup_slot: Optional[int] = None,
) -> str:
    """Reduce raw ingest flags to one of ``LINEUP_STATES``.

    Priority: scratched > bench > confirmed_starter > projected_starter >
    unknown.
    """
    if scratched is True:
        return "scratched"
    if on_bench is True:
        return "bench"
    if lineup_confirmed is True:
        return "confirmed_starter"
    if is_starter is True and lineup_confirmed is False:
        return "projected_starter"
    if lineup_slot is not None and 1 <= int(lineup_slot) <= 9 and \
            lineup_confirmed is not True:
        return "projected_starter"
    return "unknown"


def data_quality_cap_for_status(status: str) -> Optional[float]:
    """Return the maximum lock-score cap for a given lineup status.

    Returns ``None`` for statuses that must NOT publish (bench / scratched)
    to signal the caller to drop the pick entirely.  Returns a float cap
    otherwise.

    ── MLB Early-Availability μ-fix (2026-06) ────────────────────
    Prior ``unknown`` cap of 79.0 was BELOW the canonical 85 Board
    floor, meaning ANY hitter prop reaching the pipeline before a
    projected lineup landed was silently invisible on the Board even
    if the sportsbook line was real and the pick otherwise qualified.
    The intended contract is: preserve uncertainty safeguards for
    UNKNOWN, but allow Board reachability when the pick legitimately
    qualifies ≥ canonical floor.  New cap ``88`` sits ABOVE the
    canonical 85 floor and BELOW the ``projected_starter`` cap
    (92) — a truly elite pick can still surface, but its lock
    ceiling remains materially below a confirmed-lineup pick, which
    is the appropriate uncertainty safeguard.
    """
    if status in ("bench", "scratched"):
        return None            # do not publish
    if status == "unknown":
        return 88.0            # early-availability: above 85 Board floor
    if status == "projected_starter":
        return 92.0            # cap below Lock tier without confirmation
    # confirmed_starter — no cap
    return 99.0


def should_publish(status: str) -> bool:
    """Convenience: True iff the lineup status permits publication."""
    return status not in ("bench", "scratched")


# ── Bookmaker metadata retention ────────────────────────────────────
def build_bookmaker_metadata(
    *,
    provider: str,
    provider_event_id: Optional[str],
    provider_market_key: Optional[str],
    bookmakers_contributed: list[dict],
    consensus_method: str = "median_across_books",
    consensus_odds: Optional[int] = None,
    consensus_line: Optional[float] = None,
    odds_format: str = "american",
    odds_timestamp: Optional[str] = None,
    main_or_alt: Optional[str] = None,
    market_contract_id: Optional[str] = None,
) -> dict:
    """Build a stable internal metadata dict for a candidate pick.

    ``bookmakers_contributed`` must be a list of
    ``{book, odds, line, ts}`` dicts.  We do NOT leak any provider
    secret — the payload is safe to render in the pick document.

    ``consensus_method`` is documented so consumers cannot treat the
    median as a directly bettable single-book price.
    """
    return {
        "provider":            provider,
        "provider_event_id":   provider_event_id,
        "provider_market_key": provider_market_key,
        "bookmakers_contributed": list(bookmakers_contributed or []),
        "consensus_method":    consensus_method,
        "consensus_odds":      consensus_odds,
        "consensus_line":      consensus_line,
        "odds_format":         odds_format,
        "odds_timestamp":      odds_timestamp
                                or datetime.now(timezone.utc).isoformat(),
        "main_or_alt":         main_or_alt,
        "market_contract_id":  market_contract_id,
        "notice":              (
            "Consensus is NOT a directly bettable single-book price. "
            "Use bookmakers_contributed to select an actual sportsbook contract."
        ),
    }


__all__ = [
    "REJECTION_REASONS",
    "record_rejection",
    "FUNNEL_STEPS",
    "record_funnel_step",
    "snapshot",
    "reset",
    "LINEUP_STATES",
    "classify_lineup_status",
    "data_quality_cap_for_status",
    "should_publish",
    "build_bookmaker_metadata",
]
