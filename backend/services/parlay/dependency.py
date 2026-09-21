"""Parlay Dependency Authority — universal same-event / correlation classifier
================================================================================

Parlay 3.0 Universal Closure (2026-06-21).

Prior to this module, dependent-sport blocking was scattered:
  * `parlay_optimizer.diversification_ok` hard-coded a tuple
    ``("mlb","ufc","tennis","nba","nfl","nhl")`` for same-event blocks.
  * Soccer had a soft "up to 2 same-event" carve-out that could combine
    strongly-dependent Total Goals + Anytime Goal Scorer wagers.
  * CFB had NO explicit dependency rule at all.
  * Alternate selection did not consult dependency safety.

``classify_pair(a, b, policy)`` is now the ONE authority.  Returns:

  INDEPENDENT_ENOUGH
  KNOWN_POSITIVE_CORRELATION
  KNOWN_NEGATIVE_CORRELATION
  UNKNOWN_DEPENDENCY
  SAME_EVENT_UNSUPPORTED

Callers decide policy: the optimizer / route / alternate ranker treat
UNKNOWN_DEPENDENCY and SAME_EVENT_UNSUPPORTED as FAIL-CLOSED unless a
calibrated joint-probability model exists (none currently does).

Sport-specific rules are declarative data, NOT hard-coded branches:
  * Sports that fail-closed on same event: MLB, NFL, NBA, NHL, UFC,
    Tennis, CFB.
  * Soccer: same-event fails closed too — 1X2 / Total Goals / BTTS /
    scorer markets are strongly dependent within one match, and we do
    NOT have calibrated joint probability.  (Was previously the ONLY
    sport with a same-event carve-out; universal closure removes it.)
  * Same-player: hard block regardless of sport.
"""
from __future__ import annotations

from typing import Optional


# Classification labels
INDEPENDENT_ENOUGH           = "INDEPENDENT_ENOUGH"
KNOWN_POSITIVE_CORRELATION   = "KNOWN_POSITIVE_CORRELATION"
KNOWN_NEGATIVE_CORRELATION   = "KNOWN_NEGATIVE_CORRELATION"
UNKNOWN_DEPENDENCY           = "UNKNOWN_DEPENDENCY"
SAME_EVENT_UNSUPPORTED       = "SAME_EVENT_UNSUPPORTED"


# Sports where two wagers on the SAME event have material joint
# dependence AND we do NOT have a calibrated joint model.  These are
# fail-closed by policy.  Extending the set to Soccer closes the
# previous "up to 2 same-match" carve-out.
_SAME_EVENT_UNSUPPORTED_SPORTS = frozenset({
    "mlb", "nfl", "nba", "nhl", "ufc", "tennis", "cfb", "soccer",
})


def _event_key(p: dict) -> str:
    return (p.get("canonical_event_id") or p.get("event_id")
            or p.get("event") or "").strip().lower()


def _sport_key(p: dict) -> str:
    return (p.get("sport") or "").strip().lower()


def _player_key(p: dict) -> Optional[str]:
    """Return normalized player identity if the wager is a player prop."""
    v = (p.get("canonical_player_id") or p.get("elite_player_name")
         or p.get("player_name") or p.get("player") or "")
    v = str(v).strip().lower()
    return v or None


def classify_pair(a: dict, b: dict) -> str:
    """Classify the dependency between two candidate wagers.

    * Same wager id / duplicate → SAME_EVENT_UNSUPPORTED (should never
      combine).
    * Same player, any market → SAME_EVENT_UNSUPPORTED (100 % correlated).
    * Different events → INDEPENDENT_ENOUGH (baseline for cross-event
      parlays; a light diversification haircut is applied elsewhere).
    * Same event within a fail-closed sport → SAME_EVENT_UNSUPPORTED.
    * Same event within an unlisted sport → UNKNOWN_DEPENDENCY (also
      fail-closed by policy until we ship a joint model).
    """
    if not isinstance(a, dict) or not isinstance(b, dict):
        return UNKNOWN_DEPENDENCY

    if a.get("id") and a.get("id") == b.get("id"):
        return SAME_EVENT_UNSUPPORTED

    pa, pb = _player_key(a), _player_key(b)
    if pa and pb and pa == pb:
        return SAME_EVENT_UNSUPPORTED

    ea, eb = _event_key(a), _event_key(b)
    if ea and eb and ea == eb:
        # Same event: check if the sport is in the fail-closed set.
        sa = _sport_key(a)
        sb = _sport_key(b)
        if sa in _SAME_EVENT_UNSUPPORTED_SPORTS or sb in _SAME_EVENT_UNSUPPORTED_SPORTS:
            return SAME_EVENT_UNSUPPORTED
        return UNKNOWN_DEPENDENCY

    # Different events — treat as independent enough (small
    # diversification haircut still applies in parlay_survival).
    return INDEPENDENT_ENOUGH


def is_safe_to_combine(a: dict, b: dict) -> bool:
    """Convenience: True only when classify_pair returns INDEPENDENT_ENOUGH."""
    return classify_pair(a, b) == INDEPENDENT_ENOUGH


def classify_against_ticket(candidate: dict, current_legs: list) -> str:
    """Return the worst classification across every current leg pair.

    Used by the optimizer BEFORE admitting a candidate: if ANY existing
    leg makes the pair SAME_EVENT_UNSUPPORTED or UNKNOWN_DEPENDENCY the
    candidate is rejected fail-closed.
    """
    if not current_legs:
        return INDEPENDENT_ENOUGH
    worst = INDEPENDENT_ENOUGH
    order = {
        INDEPENDENT_ENOUGH: 0,
        KNOWN_NEGATIVE_CORRELATION: 1,
        KNOWN_POSITIVE_CORRELATION: 2,
        UNKNOWN_DEPENDENCY: 3,
        SAME_EVENT_UNSUPPORTED: 4,
    }
    for L in current_legs:
        c = classify_pair(candidate, L)
        if order.get(c, 3) > order.get(worst, 0):
            worst = c
    return worst


__all__ = [
    "INDEPENDENT_ENOUGH", "KNOWN_POSITIVE_CORRELATION",
    "KNOWN_NEGATIVE_CORRELATION", "UNKNOWN_DEPENDENCY",
    "SAME_EVENT_UNSUPPORTED",
    "classify_pair", "is_safe_to_combine", "classify_against_ticket",
]
