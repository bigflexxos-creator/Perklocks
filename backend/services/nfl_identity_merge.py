"""NFL identity merge: GSIS ↔ ESPN athlete alias resolution (B2 — item 18).

Known namespace split in the current data:

    GSIS:   01-0012345   (old nflverse / Pro Football Reference)
    ESPN:   3139477      (ESPN athlete API)

One NFL player must resolve to ONE canonical identity.  Historical
rows written under either namespace must merge chronologically for
the HI consumer.

API:
    merge_player_rows(gsis_rows, espn_rows) → chronological merged list
    merge_aliases(gsis_id, espn_id) → canonical_player_id string
    normalize_provider_ids(player) → nested ``provider_ids`` structure

The canonical namespace is ``nfl:p:{gsis_or_espn}`` where GSIS wins
when both are available.  Rows carry a ``provider_source`` field
(``gsis`` | ``espn``) so operators can audit lineage.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from services.hi_event_time_freshness import _row_event_time


def canonical_nfl_player_id(gsis_id: Optional[str] = None,
                             espn_id: Optional[str] = None) -> Optional[str]:
    """Return the canonical player id.  GSIS wins when both present."""
    if gsis_id:
        return f"nfl:p:{gsis_id}"
    if espn_id:
        return f"nfl:p:espn{espn_id}"
    return None


def normalize_provider_ids(player: dict) -> dict:
    """Attach a nested ``provider_ids`` block.  Idempotent."""
    gsis = (player.get("gsis_id")
             or player.get("gsis")
             or (player.get("provider_ids") or {}).get("gsis"))
    espn = (player.get("espn_id")
             or player.get("athlete_id")
             or (player.get("provider_ids") or {}).get("espn"))
    pids = dict(player.get("provider_ids") or {})
    if gsis:
        pids["gsis"] = str(gsis)
    if espn:
        pids["espn"] = str(espn)
    out = dict(player)
    if pids:
        out["provider_ids"] = pids
    cid = canonical_nfl_player_id(gsis_id=str(gsis) if gsis else None,
                                    espn_id=str(espn) if espn else None)
    if cid and not out.get("canonical_player_id"):
        out["canonical_player_id"] = cid
    return out


def merge_player_rows(gsis_rows: Iterable[dict],
                      espn_rows: Iterable[dict],
                      *,
                      canonical_player_id: Optional[str] = None,
                      ) -> list[dict]:
    """Merge rows from both namespaces into one chronological list.

    Dedupe policy: when two rows share the SAME ``(event_time,
    opponent)``, keep the GSIS row (richer historical schema).
    """
    merged: list[dict] = []
    gsis_rows = [dict(r) for r in (gsis_rows or [])]
    espn_rows = [dict(r) for r in (espn_rows or [])]
    for r in gsis_rows:
        r["provider_source"] = r.get("provider_source") or "gsis"
        if canonical_player_id:
            r["canonical_player_id"] = canonical_player_id
        merged.append(r)
    # ESPN rows: keep only those whose (event_time, opponent) is not
    # already covered by a GSIS row.
    key = lambda r: (_row_event_time(r) or "",
                      str(r.get("opponent") or r.get("canonical_opponent_id") or ""))
    gsis_keys = {key(r) for r in gsis_rows}
    for r in espn_rows:
        r["provider_source"] = r.get("provider_source") or "espn"
        if canonical_player_id:
            r["canonical_player_id"] = canonical_player_id
        if key(r) in gsis_keys:
            continue
        merged.append(r)
    merged.sort(key=lambda r: _row_event_time(r) or "")
    return merged


# ─────────────────────────────────────────────────────────────────────
# Current-season bootstrap (item 19)
# ─────────────────────────────────────────────────────────────────────
def detect_current_nfl_season(now_iso: str) -> int:
    """Return the NFL season year for an ISO timestamp.

    NFL season convention: season year == kickoff September year.  So
    anything from March → next February of a season counts as that
    year.  Pre-March rolls back to the previous season.
    """
    y = int(now_iso[:4])
    m = int(now_iso[5:7])
    # Jan/Feb → previous season (playoffs).
    if m <= 2:
        return y - 1
    return y


def is_current_season_covered(rows: Iterable[dict],
                               *, now_iso: str,
                               min_rows: int = 1) -> bool:
    """Return True iff the row list carries at least ``min_rows``
    events in the detected current NFL season.

    No hard-coded 2024 / 2025.  The season is derived from ``now_iso``.
    """
    current = detect_current_nfl_season(now_iso)
    count = 0
    for r in rows or []:
        et = _row_event_time(r)
        if not et:
            # season field as a last resort
            s = r.get("season")
            if s is not None:
                try:
                    if int(s) == current:
                        count += 1
                except Exception:
                    pass
            continue
        try:
            # Treat the first 4 chars as year (works for ISO date).
            yr = int(str(et)[:4])
            mo = int(str(et)[5:7])
            if mo <= 2:
                yr -= 1
            if yr == current:
                count += 1
        except Exception:
            continue
    return count >= min_rows


__all__ = [
    "canonical_nfl_player_id",
    "normalize_provider_ids",
    "merge_player_rows",
    "detect_current_nfl_season",
    "is_current_season_covered",
]
