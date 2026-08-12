"""MAGIC 3D.1 — Identity join helpers for MLB + Tennis.

Deterministic, exact-match after safe normalization.  ZERO fuzzy
scoring.  Missing / ambiguous → UNAVAILABLE.

Session-D rule: identity safety outranks coverage.
"""
from __future__ import annotations

import unicodedata
from typing import Optional


# ── Normalization primitives ────────────────────────────────────────

def normalize_name(name: str) -> str:
    """Unicode NFKD → ASCII fold → case fold → strip punctuation/
    trim whitespace.  Preserves word order."""
    if not name:
        return ""
    n = unicodedata.normalize("NFKD", str(name))
    n = "".join(c for c in n if not unicodedata.combining(c))
    # Strip trailing team parenthetical: "Aaron Judge (NYY)" → "Aaron Judge"
    if "(" in n:
        n = n.split("(", 1)[0]
    # Comma-order: "Judge, Aaron" → "Aaron Judge"
    if "," in n and n.count(",") == 1:
        parts = [p.strip() for p in n.split(",")]
        if len(parts) == 2 and parts[0] and parts[1]:
            n = f"{parts[1]} {parts[0]}"
    # Suffix strip
    for suffix in (" Jr", " Sr", " II", " III", " IV"):
        if n.endswith(suffix):
            n = n[: -len(suffix)]
    n = n.strip().lower()
    # Collapse internal whitespace
    n = " ".join(n.split())
    return n


def strip_tennis_prefix(canonical_id: str) -> str:
    """`tp:sorana cirstea` → `sorana cirstea`."""
    if not canonical_id:
        return ""
    s = str(canonical_id).strip()
    if s.lower().startswith("tp:"):
        return s[3:].strip().lower()
    return s.strip().lower()


# ── MLB: canonical_player_id (MLB Stats API ID) join ────────────────

async def mlb_source_row_for_pick(db, pick: dict, collection: str) -> Optional[dict]:
    """Return the row in `collection` (mlb_statcast_players /
    mlb_stuff_plus_players) that authoritatively matches this pick.

    Priority:
      A. Match by pick.canonical_player_id == row.player_id
         (both are MLB Stats API IDs — authoritative).
      B. Fallback to normalized-name exact match ONLY if:
           - the canonical_player_id is missing on the pick, AND
           - exactly one row matches the normalized name.
      C. Otherwise None (UNAVAILABLE).
    """
    cpid = pick.get("canonical_player_id")
    if cpid:
        row = await db[collection].find_one({"player_id": str(cpid)})
        if row:
            return row
    raw_pname = (pick.get("player_name")
                  or pick.get("selection") or "")
    pname_norm = normalize_name(raw_pname)
    if not pname_norm:
        return None
    # Exact-normalized match against ALL rows (source names are
    # already lowercase for MLB collections).
    rows: list[dict] = []
    async for r in db[collection].find({}, {"player_id": 1,
                                              "name": 1}):
        if normalize_name(r.get("name") or "") == pname_norm:
            rows.append(r)
            if len(rows) > 1:
                return None       # ambiguous → refuse
    if len(rows) != 1:
        return None
    # Re-fetch full row for the resolved player_id.
    return await db[collection].find_one(
        {"player_id": rows[0].get("player_id")})


# ── Tennis: canonical ID (tp:*) → tennis_player_stats.name join ────

async def tennis_stats_row_for_pick(db, pick: dict, *,
                                       surface: Optional[str] = None) -> Optional[dict]:
    """Return the tennis_player_stats row for a pick.

    Priority:
      A. strip 'tp:' prefix from pick.canonical_player_id → title-case →
         exact match to row.name.
      B. Fallback: normalized pick.player_name / selection → exact
         unique match to row.name (case-insensitive after normalize).
      C. Otherwise None.
    """
    cpid = pick.get("canonical_player_id")
    cand_names: list[str] = []
    if cpid and str(cpid).lower().startswith("tp:"):
        raw = strip_tennis_prefix(cpid)
        title = raw.title()
        cand_names.append(title)
    pn = pick.get("player_name") or pick.get("selection")
    if pn:
        cand_names.append(str(pn).strip())

    for cn in cand_names:
        # Surface-aware first
        q: dict = {"name": cn}
        if surface:
            q_surf = dict(q); q_surf["surface"] = surface
            r = await db.tennis_player_stats.find_one(q_surf)
            if r:
                return r
        # Any-surface fallback
        r = await db.tennis_player_stats.find_one(q)
        if r:
            return r

    # Deterministic normalize + unique match sweep
    norm = normalize_name(pn) if pn else ""
    if not norm and cpid:
        norm = strip_tennis_prefix(cpid)
    if not norm:
        return None
    rows: list[dict] = []
    async for r in db.tennis_player_stats.find({}, {"name": 1, "surface": 1}):
        if normalize_name(r.get("name") or "") == norm:
            rows.append(r)
            if len(rows) > 3:  # too many → ambiguous
                return None
    if not rows:
        return None
    # Prefer surface if present
    if surface:
        for r in rows:
            if r.get("surface") == surface:
                return r
    return rows[0] if len({r.get("name") for r in rows}) == 1 else None


__all__ = [
    "normalize_name", "strip_tennis_prefix",
    "mlb_source_row_for_pick", "tennis_stats_row_for_pick",
]
