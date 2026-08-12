"""MAGIC 3D.3 — MLB Producer-Side Canonical Identity Stamp.

Runs at the central publication choke point
(`publication_helpers.publish_upserted_picks`) so every newly-produced
MLB player-market pick receives a ``canonical_player_id`` that joins
authoritatively to the MLB Statcast / Stuff+ collections BEFORE Magic
Gold evidence is consumed.

Rules (per user directive):

* AUTHORITATIVE existing ID **always wins** — never overwrite a
  non-fallback ``canonical_player_id`` already stamped by
  ``pick_identity_enricher.enrich_pick_identity_async``.
* When the existing id is missing, empty, ``fallback:*``, or
  ``unresolved:*`` — attempt a deterministic normalized-name lookup
  against the union of ``mlb_statcast_players.player_id`` and
  ``mlb_stuff_plus_players.player_id``.
* AMBIGUOUS (>1 candidate for the same normalized name) → leave the
  pick unresolved.  No fuzzy matching, no team-heuristic guess.
* Idempotent — running twice on the same pick makes no additional
  changes.
* Stamps two additional trace fields so audit can distinguish sources:
    ``canonical_player_id_source`` = ``"mlb_source_producer_stamp"``
    ``canonical_player_id_class``  = ``"AUTHORITATIVE"``

**Read-only** with respect to the source collections.  The only write
is a single ``$set`` on the pick doc itself, executed by the caller
via the same update path that persists the enricher output.
"""
from __future__ import annotations

import logging
from typing import Optional

from services.magic.identity_join import normalize_name

logger = logging.getLogger("lockscore.mlb_producer_identity_stamp")


# Module-level cached name index — populated lazily on first use per
# process to avoid a full scan of the source collections on every
# publish.  The cache is small (~thousands of entries) and stable
# during a publish cycle.
_INDEX_CACHE: dict[str, set[str]] = {}
_INDEX_LOADED: bool = False


async def _build_index(db) -> dict[str, set[str]]:
    """Return ``{normalized_name: {player_ids}}`` union from statcast +
    stuff+ collections.  Multi-id keys are AMBIGUOUS and will be
    refused by :func:`resolve_mlb_source_id`."""
    idx: dict[str, set[str]] = {}
    for coll in ("mlb_statcast_players", "mlb_stuff_plus_players"):
        try:
            async for r in db[coll].find({},
                    {"player_id": 1, "name": 1, "_id": 0}):
                n = normalize_name(r.get("name") or "")
                pid = str(r.get("player_id") or "").strip()
                if n and pid:
                    idx.setdefault(n, set()).add(pid)
        except Exception as e:
            logger.debug("index load failed for %s: %s", coll, e)
    return idx


async def _ensure_index(db) -> dict[str, set[str]]:
    """Lazy-load the module-global name index."""
    global _INDEX_LOADED, _INDEX_CACHE
    if not _INDEX_LOADED:
        _INDEX_CACHE = await _build_index(db)
        _INDEX_LOADED = True
    return _INDEX_CACHE


def clear_cache() -> None:
    """Test hook — reset the cached name index."""
    global _INDEX_LOADED, _INDEX_CACHE
    _INDEX_CACHE = {}
    _INDEX_LOADED = False


def _existing_id_is_authoritative(cpid) -> bool:
    """Return True when the pick already carries an existing
    authoritative canonical_player_id that must NOT be overwritten."""
    if cpid in (None, "", 0):
        return False
    s = str(cpid).strip()
    if not s:
        return False
    if s.startswith("fallback:") or s.startswith("unresolved:"):
        return False
    return True


async def resolve_mlb_source_id(
    db, *, player_name: Optional[str],
) -> tuple[Optional[str], str]:
    """Resolve a player display-name to the MLB Stats API ID used by
    the Statcast / Stuff+ source collections.

    Returns ``(player_id, class)`` where class ∈
    ``{"AUTHORITATIVE", "AMBIGUOUS", "UNRESOLVED"}``.

    ``AUTHORITATIVE``  — exactly one match after normalization.
    ``AMBIGUOUS``      — multiple matches (do not stamp).
    ``UNRESOLVED``     — no match.

    Deterministic: normalized exact-match only.  No fuzzy, no
    substring, no team hint.
    """
    if not player_name:
        return (None, "UNRESOLVED")
    idx = await _ensure_index(db)
    n = normalize_name(player_name)
    if not n:
        return (None, "UNRESOLVED")
    cands = idx.get(n) or set()
    if len(cands) == 1:
        return (next(iter(cands)), "AUTHORITATIVE")
    if len(cands) > 1:
        return (None, "AMBIGUOUS")
    return (None, "UNRESOLVED")


async def stamp_mlb_producer_identity(db, pick: dict) -> dict:
    """Return a dict of fields to ``$set`` on the pick to give it a
    Gold-ready ``canonical_player_id``.  Empty dict when no change is
    warranted (either already authoritative, ambiguous, or unresolved).

    Contract:
    * Never overwrite an existing authoritative canonical_player_id.
    * Only applies to sport == "MLB" and picks with a player name /
      market signalling a player prop.
    * Adds ``canonical_player_id_source`` and ``canonical_player_id_class``
      for traceability.
    """
    sport = (pick.get("sport") or "").strip().lower()
    if sport != "mlb":
        return {}
    # Existing authoritative id wins (never overwrite).
    if _existing_id_is_authoritative(pick.get("canonical_player_id")):
        return {}
    # Deterministic resolve.
    pname = (pick.get("player_name") or pick.get("selection") or "")
    if not pname:
        return {}
    pid, cls = await resolve_mlb_source_id(db, player_name=pname)
    if cls != "AUTHORITATIVE" or not pid:
        return {}
    return {
        "canonical_player_id":         str(pid),
        "canonical_player_id_source":  "mlb_source_producer_stamp",
        "canonical_player_id_class":   "AUTHORITATIVE",
    }


__all__ = [
    "resolve_mlb_source_id",
    "stamp_mlb_producer_identity",
    "clear_cache",
]
