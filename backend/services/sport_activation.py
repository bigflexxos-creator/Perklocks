"""Automatic sport activation / scheduler contract (B3 — items 43-47).

Replaces the ``season_active=True`` boolean gate with an invariant
that follows real provider events to the real refresh pipeline:

    supported real events exist
      → sport enters real refresh / acquisition invocation
      → ingestion runs
      → normalization runs
      → model capability gate runs

Also introduces:
  * ``SPORT_REFRESH_STARVED`` — surfaces the specific failure where
    supported real events exist AND the sport is season-active AND
    the scheduler never invokes acquisition (item 47).
  * ``classify_sport_activation(...)`` — decides whether a sport
    should enter acquisition this cycle based on real provider
    events (NOT a hard-coded season window).
  * ``register_scheduler_entry(...)`` — a small observability
    anchor so operators can see the exact scheduler entry point
    that chose each sport.

NBA (and any other sport with MODEL_UNAVAILABLE engines) still
enters acquisition when real events exist — ingestion wakes
automatically — but ``model_candidates`` stays zero because the
model layer is unavailable, NOT because the scheduler skipped it.
"""
from __future__ import annotations

import enum
import threading
from typing import Any, Iterable, Optional


class ActivationState(str, enum.Enum):
    #: Scheduler should invoke this sport for acquisition this cycle.
    ACTIVE                    = "ACTIVE"
    #: No supported real events exist upstream (nothing to acquire).
    NO_EVENTS                 = "NO_EVENTS"
    #: Events exist but the scheduler is NOT invoking the sport —
    #: operator must investigate (item 47).
    SPORT_REFRESH_STARVED     = "SPORT_REFRESH_STARVED"
    #: Sport is intentionally off (migration / production flag /
    #: operator pause).  Different from STARVED.
    SUSPENDED                 = "SUSPENDED"


_LOCK = threading.Lock()
_SCHEDULER_LOG: dict[str, dict[str, Any]] = {}


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────
def classify_sport_activation(sport: str,
                               *,
                               supported_events_exist: bool,
                               scheduler_invoked: bool,
                               suspended: bool = False,
                               ) -> ActivationState:
    """Return the authoritative activation state for ``sport``.

    * ``suspended`` wins over everything (operator intent).
    * Events exist + scheduler invoked               → ACTIVE
    * Events exist + scheduler not invoked           → SPORT_REFRESH_STARVED
    * No events                                       → NO_EVENTS

    The caller passes the REAL runtime signals — this function does
    NOT read any hard-coded season table.
    """
    if suspended:
        return ActivationState.SUSPENDED
    if not supported_events_exist:
        return ActivationState.NO_EVENTS
    if not scheduler_invoked:
        return ActivationState.SPORT_REFRESH_STARVED
    return ActivationState.ACTIVE


def register_scheduler_entry(sport: str,
                              *,
                              entry_point: str,
                              invoked: bool,
                              detail: Optional[dict] = None,
                              ) -> None:
    """Record the scheduler entry point that chose ``sport``.

    ``entry_point`` is the module.function string the operator can
    search for (e.g. ``pick_refresh_orchestrator.refresh_sport``).
    This is the one authoritative anchor for the "exact scheduler
    entry point" return field of the B3 report.
    """
    with _LOCK:
        _SCHEDULER_LOG[sport] = {
            "entry_point": entry_point,
            "invoked":     bool(invoked),
            "detail":      dict(detail or {}),
        }


def scheduler_report() -> dict[str, dict[str, Any]]:
    with _LOCK:
        return {k: dict(v) for k, v in _SCHEDULER_LOG.items()}


def reset() -> None:
    with _LOCK:
        _SCHEDULER_LOG.clear()


# ─────────────────────────────────────────────────────────────────────
# Dynamic Soccer / Tennis provider competition discovery (item 45)
# ─────────────────────────────────────────────────────────────────────
def classify_discovered_provider_key(provider_key: str,
                                      *,
                                      sport_family_classifier,
                                      supported_sports: Iterable[str],
                                      ) -> dict[str, Any]:
    """Map a provider competition key (e.g. ``soccer_uefa_europa_league``)
    to Perklocks sport-family classification.

    Returns ``{family, supported, action}`` where:
      * ``family``   — Perklocks sport label the provider key maps to.
      * ``supported`` — True iff Perklocks models the family.
      * ``action``   — ``acquire`` | ``fail_closed``.

    The classifier is dependency-injected so this module stays pure.
    """
    try:
        family = sport_family_classifier(provider_key)
    except Exception:
        family = None
    supported = family in set(supported_sports)
    return {
        "provider_key": provider_key,
        "family": family,
        "supported": supported,
        "action": "acquire" if supported else "fail_closed",
    }


# ─────────────────────────────────────────────────────────────────────
# Bad-market registry (item 46)
# ─────────────────────────────────────────────────────────────────────
# Known-invalid provider market keys that MUST be isolated: a 422 on
# one of these may NEVER suppress acquisition of valid markets for
# the same sport / event.  Keep this as a mutable registry so
# operators can add new bad keys at runtime without a redeploy.
_BAD_MARKETS: set[str] = {
    # NFL — the alt-reception misspelling that the Odds API 422'd.
    "player_reception_alternate",
    # NHL — the "alternate_puck_lines" misspelling.
    "alternate_puck_lines",
}


def register_bad_market(provider_key: str) -> None:
    _BAD_MARKETS.add(provider_key)


def is_bad_market(provider_key: str) -> bool:
    return provider_key in _BAD_MARKETS


def valid_markets(all_requested: Iterable[str]) -> list[str]:
    """Return the subset of ``all_requested`` that are NOT in the
    bad-market registry.  Preserves input order."""
    out: list[str] = []
    for mk in all_requested:
        if is_bad_market(mk):
            continue
        out.append(mk)
    return out


__all__ = [
    "ActivationState",
    "classify_sport_activation", "register_scheduler_entry",
    "scheduler_report", "reset",
    "classify_discovered_provider_key",
    "register_bad_market", "is_bad_market", "valid_markets",
    "_BAD_MARKETS",
]
