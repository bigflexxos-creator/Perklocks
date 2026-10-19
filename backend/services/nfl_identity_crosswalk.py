"""
NFL Identity Crosswalk — authoritative GSIS ↔ ESPN resolver.
=============================================================

P0 Historical Intelligence identity-namespace fix (2026-06).

Historical Intelligence writes NFL actuals keyed by ESPN athlete IDs
(``player_game_actuals.canonical_player_id ∈ {"4361741", "espn_4361741",
"00-00XXXXX"}``) while active NFL picks can carry a GSIS id (shape
``00-00NNNNN``) injected by ``services/pick_identity_authority.py`` via
``db.players``.  The two namespaces don't intersect, so
``compute_history_shadow()`` silently returns ``no_history`` for
GSIS-keyed picks that in fact have fresh ESPN-keyed history.

This module is the ONE authoritative crosswalk — any resolver added
after this one must consult the crosswalk instead of inventing a new
mapping.

Shape of the ``nfl_identity_crosswalk`` collection:

    {
      "_id":                 <canonical_player_id>,   # prefer GSIS when known, else espn_<N>
      "sport":               "nfl",
      "canonical_player_id": <same as _id>,
      "espn_player_id":      "4361741"      | None,
      "gsis_player_id":      "00-0037834"   | None,
      "name_norm":           "brock purdy",
      "team":                "San Francisco 49ers" | None,
      "aliases":             ["espn_4361741", "4361741", "00-0037834"],
      "provenance":          ["db.players", "nfl_player_weekly", ...],
      "updated_at":          "2026-06-18T07:31:00Z",
    }

The resolver is case-insensitive on ``name_norm`` and treats
``espn_<N>`` / ``<N>`` as equivalent — a single-player identity with
multiple external aliases, NOT duplicate canonical players.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

logger = logging.getLogger("lockscore.nfl_identity_crosswalk")

CROSSWALK_COLL = "nfl_identity_crosswalk"

# ─────────────────────────────────────────────────────────────────────
# Short-lived resolver cache (per-process).  Keyed by any id/alias we
# just resolved → the crosswalk row.  TTL kept small so repeat
# ingestion that would otherwise re-query 100 times per board build
# collapses into one DB round-trip without ever masking a fresh row.
# ─────────────────────────────────────────────────────────────────────

_RESOLVE_CACHE_TTL_S = 10.0
_resolve_cache: dict[str, tuple[float, Optional[dict]]] = {}


# ─────────────────────────────────────────────────────────────────────
# Normalisation helpers.
# ─────────────────────────────────────────────────────────────────────

_GSIS_RE = re.compile(r"^\d{2}-\d{7}$")
_ESPN_PREFIX_RE = re.compile(r"^espn_(\d+)$")
_RAW_NUM_RE = re.compile(r"^\d{1,8}$")


def _norm_name(s: Any) -> str:
    if not s:
        return ""
    return re.sub(r"[^a-z0-9]+", "", str(s).lower()).strip()


def classify_id(x: Any) -> str:
    """Return one of ``gsis`` / ``espn`` / ``espn_prefixed`` / ``unknown``."""
    if not x:
        return "unknown"
    s = str(x)
    if _GSIS_RE.match(s):
        return "gsis"
    if _ESPN_PREFIX_RE.match(s):
        return "espn_prefixed"
    if _RAW_NUM_RE.match(s):
        return "espn"
    return "unknown"


def expand_aliases(row: dict) -> list[str]:
    """All non-empty external forms for a crosswalk row."""
    out: list[str] = []
    espn = row.get("espn_player_id")
    gsis = row.get("gsis_player_id")
    cpid = row.get("canonical_player_id") or row.get("_id")
    if espn:
        out.append(str(espn))
        out.append(f"espn_{espn}")
    if gsis:
        out.append(str(gsis))
    if cpid and str(cpid) not in out:
        out.append(str(cpid))
    seen: set[str] = set()
    uniq: list[str] = []
    for a in out:
        if a not in seen:
            seen.add(a)
            uniq.append(a)
    return uniq


# ─────────────────────────────────────────────────────────────────────
# Resolver — the public entry point.
# ─────────────────────────────────────────────────────────────────────

async def resolve_identity(
    db,
    *,
    pick_cpid: Optional[str] = None,
    espn_id: Optional[str] = None,
    gsis_id: Optional[str] = None,
    name: Optional[str] = None,
    team: Optional[str] = None,
) -> Optional[dict]:
    """Resolve a player across identity namespaces.

    Returns the crosswalk row or ``None`` if the identity cannot be
    resolved.  **Fails closed** — never returns a name-only fuzzy match
    (name is used only as a last-resort authoritative lookup when a
    name UNIQUELY identifies a single crosswalk row).

    Short-lived in-process cache keeps repeated lookups fast; the
    caller does not need to batch.
    """
    # 1. Explicit identifiers (highest confidence).
    probe_keys: list[str] = []
    if pick_cpid:
        probe_keys.append(str(pick_cpid))
    if espn_id:
        probe_keys.append(str(espn_id))
    if gsis_id:
        probe_keys.append(str(gsis_id))

    now = time.time()
    for key in probe_keys:
        hit = _resolve_cache.get(key)
        if hit and (now - hit[0]) < _RESOLVE_CACHE_TTL_S:
            return hit[1]

    coll = db[CROSSWALK_COLL]

    # Primary: direct alias lookup.
    if probe_keys:
        q = {"$or": [{"aliases": {"$in": probe_keys}},
                     {"_id": {"$in": probe_keys}},
                     {"canonical_player_id": {"$in": probe_keys}},
                     {"espn_player_id": {"$in": probe_keys}},
                     {"gsis_player_id": {"$in": probe_keys}}]}
        row = await coll.find_one(q)
        if row:
            for k in probe_keys:
                _resolve_cache[k] = (now, row)
            return row

    # Secondary: authoritative name lookup — ONLY when the normalised
    # name maps to exactly ONE DISTINCT player identity.  Two
    # crosswalk rows for the same player (one keyed by ``espn_<N>``,
    # one by GSIS) will share the same ``espn_player_id`` and/or
    # ``gsis_player_id`` — those are the SAME identity, not an
    # ambiguous match.  We dedupe candidates on that identifier set
    # and never silently take a multi-match best guess across
    # truly-different players.
    if name:
        n = _norm_name(name)
        if n:
            cand = []
            async for r in coll.find({"sport": "nfl", "name_norm": n}).limit(10):
                cand.append(r)
            def _ident_key(r: dict) -> tuple:
                return ((r.get("espn_player_id") or ""),
                        (r.get("gsis_player_id") or ""))
            # Group candidates by (espn, gsis) identifier pair.  If
            # all candidates share one non-empty pair they are the
            # SAME player with multiple crosswalk rows.
            groups: dict[tuple, list[dict]] = {}
            for r in cand:
                groups.setdefault(_ident_key(r), []).append(r)
            # Collapse groups that are ``subsets`` of a richer one:
            # a row (espn="4361741", gsis="") is subsumed by
            # (espn="4361741", gsis="00-0037834").
            collapsed: list[list[dict]] = []
            keys = list(groups.keys())
            consumed: set[tuple] = set()
            for k in sorted(keys, key=lambda x: (-sum(1 for p in x if p), x)):
                if k in consumed:
                    continue
                merged = list(groups[k])
                for k2 in keys:
                    if k2 == k or k2 in consumed:
                        continue
                    # k2 is a subset of k if every non-empty element
                    # of k2 appears in k.
                    if all((v == "" or v in k) for v in k2):
                        merged.extend(groups[k2])
                        consumed.add(k2)
                consumed.add(k)
                collapsed.append(merged)
            # Prefer the row with BOTH identifiers populated.
            def _rank(r: dict) -> int:
                return (int(bool(r.get("gsis_player_id")))
                        + int(bool(r.get("espn_player_id"))))
            if len(collapsed) == 1:
                row = max(collapsed[0], key=_rank)
                for k in probe_keys:
                    _resolve_cache[k] = (now, row)
                _resolve_cache[f"name:{n}"] = (now, row)
                return row
            if len(collapsed) > 1 and team:
                # Disambiguate by team substring (handles the
                # ``SF`` ↔ ``San Francisco 49ers`` short-vs-long code
                # mismatch between nflverse and db.players).
                t = (team or "").lower()
                tfilter = [g for g in collapsed
                           if any(((r.get("team") or "").lower() == t
                                   or t in (r.get("team") or "").lower()
                                   or (r.get("team") or "").lower() in t)
                                  for r in g)]
                if len(tfilter) == 1:
                    row = max(tfilter[0], key=_rank)
                    for k in probe_keys:
                        _resolve_cache[k] = (now, row)
                    return row

    # Negative-cache the failed lookup briefly.
    for k in probe_keys:
        _resolve_cache[k] = (now, None)
    return None


async def resolve_history_keys(
    db,
    *,
    pick_cpid: Optional[str] = None,
    espn_id: Optional[str] = None,
    gsis_id: Optional[str] = None,
    name: Optional[str] = None,
    team: Optional[str] = None,
) -> tuple[Optional[list[str]], dict]:
    """Return (identity_aliases, telemetry) for ``player_game_actuals``
    lookup.  Telemetry carries ``identity_resolved`` plus the resolved
    IDs and provenance for the final bundle.
    """
    telem: dict[str, Any] = {
        "identity_resolved": False,
        "input_cpid": pick_cpid,
        "input_cpid_class": classify_id(pick_cpid),
        "resolved_canonical_player_id": None,
        "resolved_espn_player_id": None,
        "resolved_gsis_player_id": None,
        "resolver_provenance": None,
    }
    row = await resolve_identity(
        db,
        pick_cpid=pick_cpid, espn_id=espn_id, gsis_id=gsis_id,
        name=name, team=team,
    )
    if not row:
        return None, telem
    aliases = expand_aliases(row)
    telem.update({
        "identity_resolved": True,
        "resolved_canonical_player_id": row.get("canonical_player_id") or row.get("_id"),
        "resolved_espn_player_id": row.get("espn_player_id"),
        "resolved_gsis_player_id": row.get("gsis_player_id"),
        "resolver_provenance": row.get("provenance"),
        "aliases_tried": aliases,
    })
    return aliases, telem


# ─────────────────────────────────────────────────────────────────────
# Idempotent writer — used by the backfill script and by any runtime
# path that discovers a new (espn ↔ gsis) pairing (e.g. during pick
# enrichment that already has both IDs on hand).
# ─────────────────────────────────────────────────────────────────────

def _pick_canonical_id(gsis: Optional[str], espn: Optional[str]) -> Optional[str]:
    """Prefer GSIS as the stable internal canonical when known;
    otherwise fall back to ``espn_<N>``.  Returns None if neither."""
    if gsis:
        return str(gsis)
    if espn:
        return f"espn_{espn}"
    return None


async def upsert_identity(
    db,
    *,
    gsis_id: Optional[str] = None,
    espn_id: Optional[str] = None,
    name: Optional[str] = None,
    team: Optional[str] = None,
    provenance: Optional[Iterable[str]] = None,
) -> Optional[dict]:
    """Idempotent crosswalk upsert.  Preserves existing aliases and
    merges new provenance tags.  Returns the final doc or None if we
    don't have enough to form a canonical id.
    """
    canonical = _pick_canonical_id(gsis_id, espn_id)
    if not canonical:
        return None

    aliases = []
    if espn_id:
        aliases += [str(espn_id), f"espn_{espn_id}"]
    if gsis_id:
        aliases.append(str(gsis_id))
    aliases.append(canonical)
    aliases = list({a: None for a in aliases})  # dedupe, preserve order

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    name_norm = _norm_name(name)
    prov = list(provenance or [])

    set_fields: dict[str, Any] = {
        "sport": "nfl",
        "canonical_player_id": canonical,
        "updated_at": now,
    }
    # Only overwrite fields when we have a non-null value — this makes
    # the upsert strictly additive (never clears a previously-known id).
    if espn_id:  set_fields["espn_player_id"] = str(espn_id)
    if gsis_id:  set_fields["gsis_player_id"] = str(gsis_id)
    if name:     set_fields["name"] = name
    if name_norm:set_fields["name_norm"] = name_norm
    if team:     set_fields["team"] = team

    coll = db[CROSSWALK_COLL]
    await coll.update_one(
        {"_id": canonical},
        {"$set": set_fields,
         "$addToSet": {"aliases": {"$each": aliases},
                       "provenance": {"$each": prov}}},
        upsert=True,
    )
    return await coll.find_one({"_id": canonical})


async def ensure_indexes(db) -> None:
    """Index the hot lookup paths.  Idempotent."""
    coll = db[CROSSWALK_COLL]
    try:
        await coll.create_index([("aliases", 1)], name="ix_aliases")
    except Exception:
        pass
    try:
        await coll.create_index(
            [("sport", 1), ("name_norm", 1)], name="ix_sport_name_norm"
        )
    except Exception:
        pass
    try:
        await coll.create_index([("espn_player_id", 1)], name="ix_espn")
    except Exception:
        pass
    try:
        await coll.create_index([("gsis_player_id", 1)], name="ix_gsis")
    except Exception:
        pass


def clear_cache() -> None:
    _resolve_cache.clear()


__all__ = [
    "CROSSWALK_COLL",
    "classify_id",
    "expand_aliases",
    "resolve_identity",
    "resolve_history_keys",
    "upsert_identity",
    "ensure_indexes",
    "clear_cache",
]
