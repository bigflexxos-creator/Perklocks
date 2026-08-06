"""cache_policy — Phase 2δ centralized cache freshness windows.

Defines the freshness and stale-while-revalidate windows for every
Odds API endpoint category.  Consumers of the persistent cache
(``services.odds_cache`` / ``services.odds_api_gateway``) consult
this module instead of hard-coding TTLs at each call site.

Semantics
─────────
- ``fresh_seconds``  → data ≤ this age is served as a HIT (no upstream call).
- ``stale_seconds``  → data > fresh AND ≤ stale is served as a STALE-hit
                       (returned immediately, upstream refresh queued in
                       the background via single-flight).
- ``max_seconds``    → data older than this is a MISS (blocking upstream
                       call required).  Beyond ``max_seconds`` we also
                       will not serve the row as an emergency fallback.
"""
from __future__ import annotations

from typing import TypedDict


class CachePolicy(TypedDict):
    fresh_seconds: int
    stale_seconds: int
    max_seconds:   int
    notes:         str


# Endpoint tag → policy.
POLICIES: dict[str, CachePolicy] = {
    # /sports catalog — very stable, rarely changes.  Snapshot-scoped
    # reuse via services.sports_catalog gives us at most one upstream
    # /sports request per coordinated run.
    "sports_list":      {"fresh_seconds": 60 * 60,       # 1 h
                          "stale_seconds": 6 * 3600,      # 6 h
                          "max_seconds":   24 * 3600,     # 24 h
                          "notes": "Provider list rarely changes."},

    # /sports/<key>/events — event schedule.  A schedule change is
    # infrequent within a session but can happen intra-day (weather
    # postponements, lineup announcements).
    "events_list":      {"fresh_seconds": 15 * 60,       # 15 min
                          "stale_seconds": 60 * 60,       # 1 h
                          "max_seconds":   6 * 3600,      # 6 h
                          "notes": "Schedule stable; intra-day changes rare."},

    # /sports/<key>/odds — bulk odds.  Lines move; fresh window kept
    # tight so the board reflects the current book.
    "bulk_odds":        {"fresh_seconds": 5 * 60,        # 5 min
                          "stale_seconds": 30 * 60,      # 30 min
                          "max_seconds":   2 * 3600,     # 2 h
                          "notes": "Lines can move meaningfully in 5 min."},

    # /sports/<key>/events/<event>/odds — per-event odds & alt lines.
    "event_odds":       {"fresh_seconds": 5 * 60,
                          "stale_seconds": 30 * 60,
                          "max_seconds":   2 * 3600,
                          "notes": "Same profile as bulk_odds."},

    "alt_lines":        {"fresh_seconds": 10 * 60,
                          "stale_seconds": 45 * 60,
                          "max_seconds":   4 * 3600,
                          "notes": "Alt lines less volatile than main markets."},

    # /sports/<key>/scores — settlement fallback.  Very tight fresh
    # window because a completed game needs to grade quickly.
    "scores":           {"fresh_seconds": 60,            # 1 min
                          "stale_seconds": 5 * 60,       # 5 min
                          "max_seconds":   60 * 60,      # 1 h
                          "notes": "Settlement latency budget."},

    # Fallback for anything unclassified.
    "generic":          {"fresh_seconds": 10 * 60,
                          "stale_seconds": 60 * 60,
                          "max_seconds":   4 * 3600,
                          "notes": "Default policy — should not be hit in prod."},
}


def get_policy(endpoint_type: str) -> CachePolicy:
    """Return the policy tuple for the given endpoint tag.  Falls
    back to ``generic`` when the tag is unknown."""
    return POLICIES.get(endpoint_type) or POLICIES["generic"]


def is_fresh(age_seconds: float, endpoint_type: str) -> bool:
    return age_seconds <= get_policy(endpoint_type)["fresh_seconds"]


def is_stale(age_seconds: float, endpoint_type: str) -> bool:
    p = get_policy(endpoint_type)
    return p["fresh_seconds"] < age_seconds <= p["stale_seconds"]


def is_max_stale(age_seconds: float, endpoint_type: str) -> bool:
    return age_seconds > get_policy(endpoint_type)["max_seconds"]


__all__ = [
    "CachePolicy", "POLICIES",
    "get_policy", "is_fresh", "is_stale", "is_max_stale",
]
