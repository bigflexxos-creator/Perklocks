"""Player Intelligence Profile — canonical schema.

All resolver outputs MUST match this shape (extra fields allowed; missing ones
filled with sensible defaults). Stored in Mongo `player_profiles_v2`.
"""
from __future__ import annotations

from typing import Any

ARCHETYPES = {
    "Soccer":  ["finisher", "playmaker", "creator",
                "high-xG attacker", "defensive anchor"],
    "NBA":     ["scorer", "facilitator", "two-way wing",
                "rim protector", "volume shooter"],
    "NFL":     ["deep threat", "possession receiver", "red zone target",
                "dual-threat QB", "power back",
                "pocket QB", "workhorse RB", "shutdown CB", "edge rusher"],
    "Tennis":  ["baseline grinder", "aggressive server",
                "counterpuncher", "all-court player"],
    "MLB":     ["contact hitter", "power slugger", "speed threat",
                "ace pitcher", "control pitcher"],
}

USAGE_BUCKETS = ("low", "medium", "high")


def empty_profile(name: str, sport: str) -> dict[str, Any]:
    """Skeleton profile — used when no enrichment data yet."""
    return {
        "canonical_name":   name,
        "aliases":          [],
        "sport":            sport,
        "team":              None,
        "position":          None,
        "archetype":         None,           # MANDATORY but populated lazily
        "archetype_source":  None,           # "seed" | "learned" | "llm" | "apisports"
        "last_n_results":    [],             # [{event_time, won, pick_market}]
        "last_n_window":     0,              # how many games in last_n_results
        "usage_intensity":   "medium",       # low | medium | high
        "volatility":        50,             # 0-100 (100 = max consistent)
        "sample_size":       0,              # # of settled picks behind score
        "injury_status":     None,           # "healthy" | "questionable" | "out"
        "updated_at":        None,
        "source":            "empty",
    }
