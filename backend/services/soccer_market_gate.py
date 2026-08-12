"""Soccer Market Gate — Session B (2026-06).

Bridge between the Session-B Soccer Capability Registry and the
Session-A canonical publication boundary.

Every soccer producer should call ``classify_market(league,
market_key)`` before emitting a pick to decide whether a
``book_odds`` value can be attached.  When the registry says the
market is not REAL_VERIFIED for the league, the producer MUST emit
``book_odds=None + no_real_book_line=True + odds_source="MODEL_ONLY"``
— otherwise the Session-A canonical boundary rejects the pick with
reason ``SYNTHETIC_BOOK_ODDS`` (fail closed).

This module is INTENTIONALLY thin — it does not perform any provider
requests itself.  Its only job is to keep the decision "may I attach
sportsbook odds to this market for this league?" in ONE place.
"""
from __future__ import annotations

from typing import Any

from services.soccer_capability_registry import (
    Capability, MARKET_KEYS, market_status, is_real_market,
)


# Market-key aliases so producer-side strings match registry keys.
_ALIASES: dict[str, str] = {
    # Game markets
    "moneyline":              "h2h",
    "1x2":                    "h2h",
    "spread":                 "spreads",
    "handicap":               "spreads",
    "total":                  "totals",
    "over_under":             "totals",
    "both_teams_to_score":    "btts",
    "both-teams-to-score":    "btts",
    # Player markets — align with pipeline_diagnostic
    "player_goal_scorer_anytime": "anytime_goalscorer",
    "goal_scorer_anytime":        "anytime_goalscorer",
    "player_first_goal_scorer":   "first_goalscorer",
    "player_to_score_or_assist":  "score_or_assist",
    "score_or_assist":            "score_or_assist",
    "player_shots":               "shots",
    "player_shots_on_target":     "shots_on_target",
}


def normalize_market_key(key: str) -> str:
    """Return the canonical registry key for ``key``.  Unknown keys
    pass through unchanged (they will resolve to UNVERIFIED in the
    registry)."""
    if not isinstance(key, str):
        return ""
    k = key.strip().lower()
    return _ALIASES.get(k, k)


def classify_market(league: str, market_key: str) -> dict[str, Any]:
    """Return a decision block:

        {
          "league":        str,
          "market":        str  (normalized),
          "status":        str  (Capability enum value),
          "may_attach_book_odds": bool,
          "must_be_model_only":  bool,
        }
    """
    m = normalize_market_key(market_key)
    status = market_status(league, m)
    may_attach = (status == Capability.REAL_VERIFIED.value)
    return {
        "league":                league,
        "market":                m,
        "status":                status,
        "may_attach_book_odds":  may_attach,
        "must_be_model_only":    not may_attach,
    }


__all__ = [
    "normalize_market_key",
    "classify_market",
    "MARKET_KEYS",
]
