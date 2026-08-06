"""Tennis identity resolver — Phase 4E.1.

Audit finding: ``tennis_engine._player_hash(name)`` was being used as
the primary tennis identity baseline.  That is a placeholder — two
players who share a common substring can collide, and name variants
(accented / romanised) score inconsistently across refreshes.

This module resolves a stable identity per pick:

  1. Preferred: Sackmann integer ``player_id`` stored on the
     ``player_db_tennis`` collection.  This survives name-spelling
     drift, tour tags, and roster changes.

  2. Fallback: a **normalised** name key (lowercased, accent-stripped,
     whitespace-collapsed).  Explicitly labelled as ``name_fallback``
     so downstream code can cap confidence / Magic Tier and never
     treat the pick as if it had stable identity.

The resolver is READ-ONLY — it never writes back to the DB.  It also
does NOT invent identifiers.  When no match is found, the return dict
carries ``identity_source = "name_fallback"`` and
``stable_identity = False``.

Consumers:
  * ``tennis_engine.compute_components`` — stamps ``identity_source``
    onto the ``TennisComponents`` so the confidence pipeline and Magic
    Tier policy can cap picks with fallback identity.
  * ``services/tennis_data_quality`` — combines identity stability
    with feature coverage into an overall data-quality tier.

The old ``_player_hash`` remains in ``tennis_engine`` **only** as a
bounded ±0.05 deterministic noise term inside the surface / form /
serve / matchup micro-scores; it is no longer the identity.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import Optional

logger = logging.getLogger("lockscore.tennis_identity")

# ── Public identity tiers ────────────────────────────────────────────
IDENTITY_SOURCE_PROVIDER = "sackmann_id"     # stable
IDENTITY_SOURCE_NAME_FALLBACK = "name_fallback"  # not stable
IDENTITY_SOURCE_EMPTY = "empty"              # neither name nor ID

_STABLE_SOURCES = frozenset({IDENTITY_SOURCE_PROVIDER})


def normalize_name(name: Optional[str]) -> str:
    """Deterministic, accent-stripped, lower-cased name key.  This is
    what we use as the ``name_fallback`` identity when a provider ID
    is unavailable, and what we hash on to look up an ID."""
    if not name:
        return ""
    # NFKD → strip combining marks (é → e, ñ → n, etc.)
    stripped = unicodedata.normalize("NFKD", str(name))
    stripped = "".join(c for c in stripped if not unicodedata.combining(c))
    # Collapse whitespace + punctuation runs, lowercase.
    stripped = re.sub(r"[^a-zA-Z0-9]+", " ", stripped).strip().lower()
    return stripped


def _empty_identity(reason: str = "no_name") -> dict:
    return {
        "player_id": None,
        "name_key": "",
        "name_raw": "",
        "identity_source": IDENTITY_SOURCE_EMPTY,
        "stable_identity": False,
        "notes": [reason],
    }


async def resolve_tennis_identity(
    db,
    name: Optional[str],
    tour: Optional[str] = None,
) -> dict:
    """Resolve a stable tennis identity.

    Parameters
    ----------
    db : motor client (may be None → caller wants name-only resolution).
    name : raw player name from the pick / event string.
    tour : optional "ATP" / "WTA" hint for disambiguation.

    Returns
    -------
    dict with keys:
        player_id         — stable ID or None
        name_key          — normalised name (for hashing / fallback)
        name_raw          — original name
        identity_source   — one of {sackmann_id, name_fallback, empty}
        stable_identity   — True only when a provider ID resolved
        notes             — list[str] with any warnings / decisions
    """
    if not name or not str(name).strip():
        return _empty_identity("empty_name")

    key = normalize_name(name)
    if not key:
        return _empty_identity("unparseable_name")

    ident: dict = {
        "player_id": None,
        "name_key": key,
        "name_raw": str(name).strip(),
        "identity_source": IDENTITY_SOURCE_NAME_FALLBACK,
        "stable_identity": False,
        "notes": [],
    }

    if db is None:
        ident["notes"].append("no_db_provided:name_fallback")
        return ident

    # Sackmann lookup — `player_db_tennis` is the canonical collection.
    # Filter is defensive: sport tag, exact name match on the canonical
    # `name` field, optional tour tag.  We also try normalized_name if
    # that field exists (populated by the ingestor).
    try:
        q: dict = {"sport": "tennis"}
        if tour and tour.strip().upper() in ("ATP", "WTA"):
            q["tour"] = tour.strip().lower()
        # Try exact name match first.
        doc = await db.player_db_tennis.find_one(
            {**q, "name": ident["name_raw"]},
            {"_id": 0, "player_id": 1, "name": 1, "tour": 1},
        )
        # If no exact hit, try the normalised name key.
        if not doc:
            doc = await db.player_db_tennis.find_one(
                {**q, "normalized_name": key},
                {"_id": 0, "player_id": 1, "name": 1, "tour": 1},
            )
        # Final fallback: case-insensitive regex on `name`.  Kept only
        # for edge cases (e.g. "R. Nadal" vs "Rafael Nadal") — matches
        # by exact normalised name so we avoid substring collisions.
        if not doc:
            candidates = await db.player_db_tennis.find(
                q, {"_id": 0, "player_id": 1, "name": 1, "tour": 1},
            ).to_list(length=500)
            for c in candidates:
                if normalize_name(c.get("name") or "") == key:
                    doc = c
                    break
        if doc and doc.get("player_id"):
            ident["player_id"] = str(doc["player_id"])
            ident["identity_source"] = IDENTITY_SOURCE_PROVIDER
            ident["stable_identity"] = True
            return ident
        ident["notes"].append("no_sackmann_match")
    except Exception as e:
        # Never raise — this is best-effort.  Fall back to name identity
        # with an explicit marker so downstream can cap confidence.
        logger.warning("tennis identity lookup failed for %r: %s", name, e)
        ident["notes"].append(f"lookup_error:{type(e).__name__}")

    return ident


def is_stable_identity(ident: dict) -> bool:
    """Convenience predicate — True only when a provider ID was found."""
    if not ident:
        return False
    return (
        bool(ident.get("stable_identity"))
        and ident.get("identity_source") in _STABLE_SOURCES
    )


def deterministic_hash_from_identity(ident: dict) -> str:
    """Return a stable string key for hashing.  Prefers the provider
    ID; falls back to the normalised name key.  Never uses raw name.

    This replaces name-based hashing everywhere in ``tennis_engine``
    for identity-critical logic (H2H direction, per-player caches,
    etc.).  The bounded ±0.05 noise inside micro-score helpers still
    uses ``_player_hash`` for backwards-compat but is no longer the
    identity signal.
    """
    if not ident:
        return ""
    if ident.get("player_id"):
        return f"pid:{ident['player_id']}"
    if ident.get("name_key"):
        return f"nk:{ident['name_key']}"
    return ""


__all__ = [
    "IDENTITY_SOURCE_PROVIDER",
    "IDENTITY_SOURCE_NAME_FALLBACK",
    "IDENTITY_SOURCE_EMPTY",
    "normalize_name",
    "resolve_tennis_identity",
    "is_stable_identity",
    "deterministic_hash_from_identity",
]
