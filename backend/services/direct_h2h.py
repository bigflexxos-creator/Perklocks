"""Universal direct VS OPP H2H helper (B2 — items 22-29).

Replaces the anti-pattern:

    fetch recent 80 / 120 / 200 → then filter opponent

with a DIRECT opponent query against the right history collection.
Identity priority is strict (item 22):

    1. canonical opponent ID exact match
    2. verified provider alias (opponent_aliases list)
    3. exact normalized canonical name
    4. fail closed  — no fuzzy / substring identity

The helper is shared infrastructure — each sport's feature engine
calls one function instead of each engine inventing its own loop.
Supported sports: NFL, MLB, NBA, NHL, Soccer, Tennis, CFB.
"""
from __future__ import annotations

import unicodedata
from typing import Any, Optional


def _norm(name: str) -> str:
    if not name:
        return ""
    nk = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in nk if not unicodedata.combining(c))
    return s.lower().strip()


# Collection-name map per sport (writeable canonical history +
# optional legacy fallback).  The resolver walks canonical first.
_COLL_CANONICAL = "player_game_actuals"
_COLL_TEAM_CANONICAL = "team_game_actuals"

_SPORT_LEGACY: dict[str, tuple[str, ...]] = {
    "NFL":    ("player_game_logs",),
    "MLB":    ("player_game_logs",),
    "NBA":    ("player_game_logs",),
    "NHL":    ("player_game_logs",),
    "Soccer": ("soccer_player_game_logs",),
    "Tennis": ("tennis_matches_history",),
    "CFB":    ("player_game_logs",),
}


# ─────────────────────────────────────────────────────────────────────
# Identity resolution
# ─────────────────────────────────────────────────────────────────────
def _opponent_match_clauses(canonical_opponent_id: Optional[str],
                             opponent_aliases: Optional[list[str]],
                             opponent_name: Optional[str],
                             ) -> list[dict]:
    """Return the OR clauses for a direct opponent query.

    Preserves strict identity priority — identity-ID first, verified
    aliases second, normalized exact name third.  No regex.  No
    substring.  Returns an empty list iff NOTHING is provided (caller
    MUST fail closed).
    """
    clauses: list[dict] = []
    if canonical_opponent_id:
        clauses.append({"canonical_opponent_id": canonical_opponent_id})
        clauses.append({"opponent_canonical_id": canonical_opponent_id})
    if opponent_aliases:
        clauses.append({"opponent_canonical_id": {"$in": opponent_aliases}})
        clauses.append({"canonical_opponent_id": {"$in": opponent_aliases}})
        clauses.append({"opponent": {"$in": opponent_aliases}})
    if opponent_name:
        n = _norm(opponent_name)
        clauses.append({"opponent": opponent_name})
        clauses.append({"opponent_name": opponent_name})
        clauses.append({"opponent_norm": n})
    return clauses


def _player_match_clauses(canonical_player_id: Optional[str],
                           player_aliases: Optional[list[str]],
                           player_name: Optional[str],
                           ) -> list[dict]:
    clauses: list[dict] = []
    if canonical_player_id:
        clauses.append({"canonical_player_id": canonical_player_id})
        clauses.append({"player_id":           canonical_player_id})
    if player_aliases:
        clauses.append({"canonical_player_id": {"$in": player_aliases}})
        clauses.append({"player_name":         {"$in": player_aliases}})
    if player_name:
        n = _norm(player_name)
        clauses.append({"player_name":      player_name})
        clauses.append({"player_name_norm": n})
    return clauses


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────
async def direct_player_vs_opp(db,
                                *,
                                sport: str,
                                canonical_player_id: Optional[str] = None,
                                player_aliases: Optional[list[str]] = None,
                                player_name: Optional[str] = None,
                                canonical_opponent_id: Optional[str] = None,
                                opponent_aliases: Optional[list[str]] = None,
                                opponent_name: Optional[str] = None,
                                as_of_iso: Optional[str] = None,
                                limit: int = 400,
                                ) -> list[dict]:
    """Return player's completed games DIRECTLY against the opponent.

    Reads canonical (``player_game_actuals``) first; walks the
    sport-specific legacy fallbacks only when canonical returns
    nothing.  Identity priority is strict (no substring).

    Fails closed (returns ``[]``) when NEITHER a player identity nor
    an opponent identity is supplied.
    """
    p_clauses = _player_match_clauses(canonical_player_id,
                                       player_aliases, player_name)
    o_clauses = _opponent_match_clauses(canonical_opponent_id,
                                         opponent_aliases, opponent_name)
    if not p_clauses or not o_clauses:
        return []

    event_time_or = []
    if as_of_iso:
        for k in ("event_time", "game_date", "match_date",
                  "event_date", "date"):
            event_time_or.append({k: {"$lt": as_of_iso}})

    q: dict[str, Any] = {"$and": [
        {"$or": p_clauses},
        {"$or": o_clauses},
    ]}
    q["$and"].append({"sport": sport})
    if event_time_or:
        q["$and"].append({"$or": event_time_or})

    out: list[dict] = []
    # 1. Canonical collection first.
    try:
        cur = db[_COLL_CANONICAL].find(q).limit(limit)
        async for r in cur:
            out.append(r)
    except Exception:
        pass
    if out:
        return out

    # 2. Legacy fallbacks per sport.
    for coll_name in _SPORT_LEGACY.get(sport, ()):
        try:
            cur = db[coll_name].find(q).limit(limit)
            async for r in cur:
                out.append(r)
            if out:
                return out
        except Exception:
            continue

    return out


async def direct_team_vs_opp(db,
                              *,
                              sport: str,
                              canonical_team_id: Optional[str] = None,
                              team_aliases: Optional[list[str]] = None,
                              team_name: Optional[str] = None,
                              canonical_opponent_id: Optional[str] = None,
                              opponent_aliases: Optional[list[str]] = None,
                              opponent_name: Optional[str] = None,
                              as_of_iso: Optional[str] = None,
                              limit: int = 400,
                              ) -> list[dict]:
    """Direct TEAM-vs-OPP query (same identity-priority contract)."""
    team_clauses: list[dict] = []
    if canonical_team_id:
        team_clauses.append({"canonical_team_id": canonical_team_id})
        team_clauses.append({"team_canonical_id": canonical_team_id})
    if team_aliases:
        team_clauses.append({"canonical_team_id": {"$in": team_aliases}})
        team_clauses.append({"team":              {"$in": team_aliases}})
    if team_name:
        team_clauses.append({"team":      team_name})
        team_clauses.append({"team_name": team_name})

    opp_clauses = _opponent_match_clauses(canonical_opponent_id,
                                           opponent_aliases, opponent_name)
    if not team_clauses or not opp_clauses:
        return []

    q: dict[str, Any] = {"$and": [
        {"$or": team_clauses},
        {"$or": opp_clauses},
        {"sport": sport},
    ]}
    if as_of_iso:
        q["$and"].append({"$or": [
            {k: {"$lt": as_of_iso}}
            for k in ("event_time", "game_date", "match_date",
                      "event_date", "date")
        ]})

    out: list[dict] = []
    try:
        cur = db[_COLL_TEAM_CANONICAL].find(q).limit(limit)
        async for r in cur:
            out.append(r)
    except Exception:
        pass
    if out:
        return out
    for coll_name in ("team_game_logs",) + _SPORT_LEGACY.get(sport, ()):
        try:
            cur = db[coll_name].find(q).limit(limit)
            async for r in cur:
                out.append(r)
            if out:
                return out
        except Exception:
            continue
    return out


__all__ = [
    "direct_player_vs_opp",
    "direct_team_vs_opp",
]
