"""Authoritative canonical identity lookup (Pre-Magic §3).

Bridges live picks to the identity keys ACTUALLY used by the history
collections (``player_game_actuals`` / ``team_game_actuals``).  The
identity contract discovered in production:

    team_game_actuals.canonical_team_id      = human-readable team name
                                                 (e.g. "Miami Marlins")
    player_game_actuals.canonical_player_id  = provider ID as string
                                                 (e.g. "405395",
                                                 "00-0016919", "102254")

**Rules (§3, §4):**

* This module is READ-ONLY — it never writes to any collection.
* Team lookups are exact-name match against the history collection.
  A miss returns ``None`` (never a guess).
* Player lookups require sport + name match, and prefer additional
  team context when available.  A miss returns ``None``.
* Cache is best-effort in-process — safe to invalidate at any time.
* NEVER attaches a canonical id derived from fuzzy matching or
  partial substring.  Exact case-insensitive match only.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("lockscore.pick_identity_authority")


# In-process best-effort cache — populated on first lookup per key.
_TEAM_CACHE: dict[tuple[str, str], Optional[str]] = {}
_PLAYER_CACHE: dict[tuple[str, str, Optional[str]], Optional[str]] = {}


def _sport_l(s: Optional[str]) -> str:
    return (s or "").strip().lower()


def _norm(n: Optional[str]) -> str:
    return (n or "").strip().lower()


async def resolve_team_authoritative(
    db, *, sport: Optional[str], name: Optional[str],
) -> Optional[str]:
    """Return the ``canonical_team_id`` used by ``team_game_actuals``
    for this (sport, name) pair, or ``None`` if no exact match.

    The history collection uses human-readable team names as the
    canonical id — trivially reversible from the pick's ``event`` /
    ``selection`` fields.
    """
    if not sport or not name:
        return None
    key = (_sport_l(sport), _norm(name))
    if key in _TEAM_CACHE:
        return _TEAM_CACHE[key]
    try:
        # Exact match first (fast).
        row = await db.team_game_actuals.find_one(
            {"sport": _sport_l(sport), "canonical_team_id": name.strip()},
            {"canonical_team_id": 1, "_id": 0},
        )
        if row and row.get("canonical_team_id"):
            _TEAM_CACHE[key] = row["canonical_team_id"]
            return row["canonical_team_id"]
        # Case-insensitive fallback via `team_name` (secondary field).
        row = await db.team_game_actuals.find_one(
            {"sport": _sport_l(sport),
             "team_name": {"$regex": f"^{_escape(name)}$", "$options": "i"}},
            {"canonical_team_id": 1, "_id": 0},
        )
        if row and row.get("canonical_team_id"):
            _TEAM_CACHE[key] = row["canonical_team_id"]
            return row["canonical_team_id"]
    except Exception as e:
        logger.debug("authoritative team lookup failed %s/%s: %s",
                     sport, name, e)
    _TEAM_CACHE[key] = None
    return None


async def resolve_player_authoritative(
    db, *, sport: Optional[str], name: Optional[str],
    team_hint: Optional[str] = None,
) -> Optional[str]:
    """Return the ``canonical_player_id`` used by
    ``player_game_actuals`` for this (sport, name) pair, or ``None``
    if no exact match.

    * We do NOT do fuzzy matching — exact case-insensitive equality
      on ``player_name`` only.
    * When ``team_hint`` is provided AND multiple players match by
      name, we prefer the row whose historical team matches the hint.
    * If NO match found → return None.  UNKNOWN stays UNKNOWN (§4).
    """
    if not sport or not name:
        return None
    key = (_sport_l(sport), _norm(name), _norm(team_hint) or None)
    if key in _PLAYER_CACHE:
        return _PLAYER_CACHE[key]
    try:
        # Case-insensitive exact match on player_name.
        q = {"sport": _sport_l(sport),
             "player_name": {"$regex": f"^{_escape(name)}$",
                              "$options": "i"}}
        rows = []
        cursor = db.player_game_actuals.find(
            q, {"canonical_player_id": 1, "team": 1, "_id": 0}
        ).limit(20)
        async for r in cursor:
            rows.append(r)
        if not rows:
            _PLAYER_CACHE[key] = None
            return None
        # If unique, done.
        canon_ids = list({r.get("canonical_player_id") for r in rows
                          if r.get("canonical_player_id")})
        if len(canon_ids) == 1:
            _PLAYER_CACHE[key] = canon_ids[0]
            return canon_ids[0]
        # Multiple canonicals — collision.  Use team hint if we have one.
        if team_hint:
            hint = _norm(team_hint)
            preferred = [r for r in rows
                         if _norm(r.get("team")) == hint]
            if preferred:
                cid = preferred[0].get("canonical_player_id")
                _PLAYER_CACHE[key] = cid
                return cid
        # Ambiguous — refuse to guess (§4).
        _PLAYER_CACHE[key] = None
        return None
    except Exception as e:
        logger.debug("authoritative player lookup failed %s/%s: %s",
                     sport, name, e)
    _PLAYER_CACHE[key] = None
    return None


def _escape(s: str) -> str:
    """Escape special regex characters for exact-match matching."""
    return "".join("\\" + c if c in r".^$*+?()[]{}|\\" else c for c in s)


def clear_cache() -> None:
    """Drop the in-process cache.  Called by test setup."""
    _TEAM_CACHE.clear()
    _PLAYER_CACHE.clear()


async def prewarm_cache(db) -> dict:
    """Load ALL canonical team + player mappings from the history
    collections into the in-process cache in one pass.

    Dramatically speeds up bulk backfills (single Mongo round trip
    per collection instead of one per pick).  Read-only — never
    writes.
    """
    stats = {"teams": 0, "players": 0}
    try:
        async for r in db.team_game_actuals.aggregate([
            {"$group": {
                "_id": {"sport": "$sport",
                          "name": "$canonical_team_id"},
                "canonical_team_id": {"$first": "$canonical_team_id"},
            }},
        ]):
            sport = _sport_l(r["_id"].get("sport"))
            name = _norm(r["_id"].get("name"))
            if sport and name:
                _TEAM_CACHE[(sport, name)] = r["canonical_team_id"]
                stats["teams"] += 1
    except Exception as e:
        logger.debug("prewarm team cache failed: %s", e)
    try:
        # Collect (sport, name) → set of canonical_player_id.
        buckets: dict[tuple[str, str], set] = {}
        cursor = db.player_game_actuals.find(
            {"player_name": {"$exists": True, "$ne": None}},
            {"sport": 1, "player_name": 1,
             "canonical_player_id": 1, "team": 1, "_id": 0},
        )
        team_hints: dict[tuple[str, str, str], str] = {}
        async for r in cursor:
            sport = _sport_l(r.get("sport"))
            name = _norm(r.get("player_name"))
            cid = r.get("canonical_player_id")
            team = _norm(r.get("team"))
            if not (sport and name and cid):
                continue
            buckets.setdefault((sport, name), set()).add(cid)
            if team:
                team_hints[(sport, name, team)] = cid
        for (sport, name), cids in buckets.items():
            if len(cids) == 1:
                _PLAYER_CACHE[(sport, name, None)] = next(iter(cids))
                stats["players"] += 1
        # And record team-hinted disambiguations.
        for (sport, name, team), cid in team_hints.items():
            _PLAYER_CACHE[(sport, name, team)] = cid
            stats["players"] += 1
    except Exception as e:
        logger.debug("prewarm player cache failed: %s", e)
    return stats


__all__ = [
    "resolve_team_authoritative",
    "resolve_player_authoritative",
    "prewarm_cache",
    "clear_cache",
]
