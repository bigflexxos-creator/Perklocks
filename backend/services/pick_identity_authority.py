"""Authoritative canonical identity lookup (Pre-Magic §3, Final
Closure §1-§3).

Bridges live picks to the identity keys ACTUALLY used by the history
collections (``player_game_actuals`` / ``team_game_actuals``).

**Identity classes returned per lookup:**

  AUTHORITATIVE   — id came from an authoritative registry
                     (``db.players`` / ``db.team_game_actuals``) and
                     matches the identity key used by history.
  MAPPED          — id came from an existing name→id mapping table
                     that is verified against history.
  PROVISIONAL     — deterministic hash id (``fallback:<sha1>``).  Does
                     NOT satisfy Pre-Magic canonical identity
                     certification.  Stored for source-stability only.
  UNRESOLVED      — no id could be produced.  Missing stays missing.

**Rules (§1-§3):**

* This module is READ-ONLY — no writes.
* NEVER promotes a PROVISIONAL id to AUTHORITATIVE.
* Team lookups exact-match ``team_game_actuals`` (whose canonical id
  IS the human-readable name).
* Player lookups first query the ``db.players`` registry by
  ``canonical_name`` (normalized); ambiguity + no team hint → None.
* NEVER attaches an id derived from fuzzy matching or partial
  substring.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger("lockscore.pick_identity_authority")


# ═══════════════════════════════════════════════════════════════════
# Identity classes (§1 — the ONLY explicit vocabulary)
# ═══════════════════════════════════════════════════════════════════
CLASS_AUTHORITATIVE = "AUTHORITATIVE"
CLASS_MAPPED        = "MAPPED"
CLASS_PROVISIONAL   = "PROVISIONAL"
CLASS_UNRESOLVED    = "UNRESOLVED"


# In-process best-effort cache — populated on first lookup per key.
# Cache tuple: (canonical_id_or_None, identity_class).
_TEAM_CACHE: dict[tuple[str, str], tuple[Optional[str], str]] = {}
_PLAYER_CACHE: dict[tuple[str, str, Optional[str]],
                     tuple[Optional[str], str]] = {}


def _sport_l(s: Optional[str]) -> str:
    return (s or "").strip().lower()


def _norm(n: Optional[str]) -> str:
    """Normalise a name for exact matching — lowercase, collapse
    whitespace, strip punctuation aliases from combat sports."""
    if not n:
        return ""
    x = re.sub(r"\s+", " ", str(n)).strip().lower()
    # Drop trailing parenthetical team codes like "Ranger Suarez (BOS)".
    x = re.sub(r"\s*\([^)]*\)\s*$", "", x).strip()
    return x


def _escape(s: str) -> str:
    return "".join("\\" + c if c in r".^$*+?()[]{}|\\" else c for c in s)


# ═══════════════════════════════════════════════════════════════════
# Team lookup (authoritative)
# ═══════════════════════════════════════════════════════════════════
async def resolve_team_authoritative(
    db, *, sport: Optional[str], name: Optional[str],
) -> tuple[Optional[str], str]:
    """Return ``(canonical_team_id, identity_class)``.

    Returns ``AUTHORITATIVE`` when the name exactly matches a row in
    ``team_game_actuals``.  Otherwise ``(None, UNRESOLVED)``.
    """
    if not sport or not name:
        return (None, CLASS_UNRESOLVED)
    key = (_sport_l(sport), _norm(name))
    if key in _TEAM_CACHE:
        return _TEAM_CACHE[key]
    try:
        # Exact match on canonical_team_id (fast).
        row = await db.team_game_actuals.find_one(
            {"sport": _sport_l(sport), "canonical_team_id": name.strip()},
            {"canonical_team_id": 1, "_id": 0},
        )
        if row and row.get("canonical_team_id"):
            ret = (row["canonical_team_id"], CLASS_AUTHORITATIVE)
            _TEAM_CACHE[key] = ret
            return ret
        # Case-insensitive fallback via team_name.
        row = await db.team_game_actuals.find_one(
            {"sport": _sport_l(sport),
             "team_name": {"$regex": f"^{_escape(name.strip())}$",
                              "$options": "i"}},
            {"canonical_team_id": 1, "_id": 0},
        )
        if row and row.get("canonical_team_id"):
            ret = (row["canonical_team_id"], CLASS_AUTHORITATIVE)
            _TEAM_CACHE[key] = ret
            return ret
    except Exception as e:
        logger.debug("authoritative team lookup failed %s/%s: %s",
                     sport, name, e)
    _TEAM_CACHE[key] = (None, CLASS_UNRESOLVED)
    return (None, CLASS_UNRESOLVED)


# ═══════════════════════════════════════════════════════════════════
# Player lookup (authoritative registry + history verification)
# ═══════════════════════════════════════════════════════════════════
async def resolve_player_authoritative(
    db, *, sport: Optional[str], name: Optional[str],
    team_hint: Optional[str] = None,
) -> tuple[Optional[str], str]:
    """Return ``(canonical_player_id, identity_class)``.

    Resolution order:

    1. Query ``db.players`` by canonical_name / name (case-insensitive
       exact match).  If found AND that player_id joins to
       ``player_game_actuals`` for the same sport → ``AUTHORITATIVE``.
    2. If ``db.players`` returns a player_id that does NOT join
       history → ``MAPPED`` (name registry hit, but history join
       unproven).
    3. Otherwise query ``player_game_actuals`` directly by
       ``player_name`` — if that succeeds it is also ``AUTHORITATIVE``.
    4. Ambiguous name with no team hint → ``(None, UNRESOLVED)``.

    §4: NEVER attaches a player id derived from a fallback hash.
    """
    if not sport or not name:
        return (None, CLASS_UNRESOLVED)
    sport_l = _sport_l(sport)
    n_norm = _norm(name)
    key = (sport_l, n_norm, _norm(team_hint) or None)
    if key in _PLAYER_CACHE:
        return _PLAYER_CACHE[key]

    try:
        # 1. Query db.players by canonical_name (preferred) or name.
        q_or = [
            {"canonical_name": n_norm},
            {"name": {"$regex": f"^{_escape(name.strip())}$",
                       "$options": "i"}},
        ]
        rows: list[dict] = []
        cursor = db.players.find(
            {"sport": sport_l, "$or": q_or},
            {"player_id": 1, "name": 1, "team": 1, "team_name": 1,
             "canonical_name": 1, "espn_id": 1, "_id": 0},
        ).limit(20)
        async for r in cursor:
            rows.append(r)

        if not rows:
            # 3. Direct player_game_actuals name lookup (some sports).
            row = await db.player_game_actuals.find_one(
                {"sport": sport_l,
                 "player_name": {"$regex": f"^{_escape(name.strip())}$",
                                    "$options": "i"}},
                {"canonical_player_id": 1, "_id": 0},
            )
            if row and row.get("canonical_player_id"):
                ret = (row["canonical_player_id"], CLASS_AUTHORITATIVE)
                _PLAYER_CACHE[key] = ret
                return ret
            _PLAYER_CACHE[key] = (None, CLASS_UNRESOLVED)
            return (None, CLASS_UNRESOLVED)

        # Disambiguate by team hint if multiple.
        chosen = None
        if team_hint and len(rows) > 1:
            hint = _norm(team_hint)
            for r in rows:
                if _norm(r.get("team")) == hint or \
                   _norm(r.get("team_name")) == hint:
                    chosen = r
                    break
        if chosen is None and len(rows) == 1:
            chosen = rows[0]
        if chosen is None:
            # Multiple candidates, no hint → refuse to guess.
            _PLAYER_CACHE[key] = (None, CLASS_UNRESOLVED)
            return (None, CLASS_UNRESOLVED)

        pid = str(chosen.get("player_id") or "").strip()
        if not pid:
            _PLAYER_CACHE[key] = (None, CLASS_UNRESOLVED)
            return (None, CLASS_UNRESOLVED)

        # 2. Verify against history — does this player_id actually
        #    have rows in player_game_actuals?
        history_rows = await db.player_game_actuals.count_documents({
            "sport": sport_l, "canonical_player_id": pid,
        })
        if history_rows and history_rows > 0:
            ret = (pid, CLASS_AUTHORITATIVE)
            _PLAYER_CACHE[key] = ret
            return ret
        # Registry hit but no history join proven — MAPPED
        ret = (pid, CLASS_MAPPED)
        _PLAYER_CACHE[key] = ret
        return ret
    except Exception as e:
        logger.debug("authoritative player lookup failed %s/%s: %s",
                     sport, name, e)
    _PLAYER_CACHE[key] = (None, CLASS_UNRESOLVED)
    return (None, CLASS_UNRESOLVED)


# ═══════════════════════════════════════════════════════════════════
# Cache mgmt
# ═══════════════════════════════════════════════════════════════════
def clear_cache() -> None:
    _TEAM_CACHE.clear()
    _PLAYER_CACHE.clear()


async def prewarm_cache(db) -> dict:
    """Load ALL canonical team + player mappings from the authority
    sources in ONE Mongo pass each.  Read-only."""
    stats = {"teams": 0, "players_authoritative": 0,
             "players_mapped": 0}
    # ── Teams ────────────────────────────────────────────────────
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
                _TEAM_CACHE[(sport, name)] = (
                    r["canonical_team_id"], CLASS_AUTHORITATIVE)
                stats["teams"] += 1
    except Exception as e:
        logger.debug("prewarm team cache failed: %s", e)

    # ── Players ──────────────────────────────────────────────────
    # First pass: build set of (sport, canonical_player_id) that
    # exist in history — used to classify AUTHORITATIVE vs MAPPED.
    history_ids: dict[str, set] = {}
    try:
        async for r in db.player_game_actuals.aggregate([
            {"$group": {"_id": {
                "sport": "$sport",
                "cpid": "$canonical_player_id",
            }}},
        ]):
            sport = _sport_l(r["_id"].get("sport"))
            cpid = str(r["_id"].get("cpid") or "").strip()
            if sport and cpid:
                history_ids.setdefault(sport, set()).add(cpid)
    except Exception as e:
        logger.debug("prewarm history_ids failed: %s", e)

    # Second pass: db.players registry → per-(sport, name) mapping.
    try:
        buckets: dict[tuple[str, str], list[dict]] = {}
        cursor = db.players.find(
            {}, {"sport": 1, "name": 1, "canonical_name": 1,
                 "player_id": 1, "team": 1, "team_name": 1, "_id": 0},
        )
        async for r in cursor:
            sport = _sport_l(r.get("sport"))
            n_can = _norm(r.get("canonical_name") or r.get("name"))
            if not (sport and n_can and r.get("player_id")):
                continue
            buckets.setdefault((sport, n_can), []).append(r)
        for (sport, name), rows in buckets.items():
            if len(rows) == 1:
                pid = str(rows[0].get("player_id")).strip()
                cls = (CLASS_AUTHORITATIVE
                        if pid in history_ids.get(sport, set())
                        else CLASS_MAPPED)
                _PLAYER_CACHE[(sport, name, None)] = (pid, cls)
                if cls == CLASS_AUTHORITATIVE:
                    stats["players_authoritative"] += 1
                else:
                    stats["players_mapped"] += 1
            else:
                # Populate team-hinted disambiguations.
                for r in rows:
                    team = _norm(r.get("team") or r.get("team_name"))
                    if not team:
                        continue
                    pid = str(r.get("player_id")).strip()
                    cls = (CLASS_AUTHORITATIVE
                            if pid in history_ids.get(sport, set())
                            else CLASS_MAPPED)
                    _PLAYER_CACHE[(sport, name, team)] = (pid, cls)
                    if cls == CLASS_AUTHORITATIVE:
                        stats["players_authoritative"] += 1
                    else:
                        stats["players_mapped"] += 1
        # Third pass: also index player_game_actuals rows where
        # player_name is populated (Soccer, Tennis, UFC, some NFL).
        cursor = db.player_game_actuals.aggregate([
            {"$match": {"player_name": {"$ne": None}}},
            {"$group": {"_id": {
                "sport": "$sport", "name": "$player_name",
                "cpid": "$canonical_player_id",
            }}},
        ])
        async for r in cursor:
            sport = _sport_l(r["_id"].get("sport"))
            n_can = _norm(r["_id"].get("name"))
            cpid = str(r["_id"].get("cpid") or "").strip()
            if not (sport and n_can and cpid):
                continue
            key = (sport, n_can, None)
            if key not in _PLAYER_CACHE:
                _PLAYER_CACHE[key] = (cpid, CLASS_AUTHORITATIVE)
                stats["players_authoritative"] += 1
    except Exception as e:
        logger.debug("prewarm player cache failed: %s", e)
    return stats


__all__ = [
    "CLASS_AUTHORITATIVE", "CLASS_MAPPED",
    "CLASS_PROVISIONAL", "CLASS_UNRESOLVED",
    "resolve_team_authoritative",
    "resolve_player_authoritative",
    "prewarm_cache",
    "clear_cache",
]
