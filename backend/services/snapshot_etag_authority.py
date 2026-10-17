"""Snapshot / ETag one-authority chain (B3 — items 58-62).

There must be ONE committed board identity that drives:

    board snapshot validity
      → ETag
      → X-Board-Version
      → truth_manifest
      → canonical_revision
      → generation_id
      → frontend acceptance

Content hash remains DIAGNOSTIC only — never the driver.

Known required behaviors:

  * Known version mismatch (committed != cached-snapshot version)
    → invalidate immediately.  TTL may NOT override a known move.
  * TTL fallback allowed only when the committed authority is
    genuinely unavailable (DB outage, etc.) — the response MUST
    then be truthfully marked ``fallback_stale=True``.

API:
  committed_identity(**fields)          — builds the one authoritative tuple
  advance_committed_identity(prev)      — bumps a new generation
  snapshot_decision(client_if_none_match, committed, cached_snapshot)
                                         — returns the decision for /api/picks/today

The three ETag test cases (item 60):
  A. snapshot=V1, committed=V2, If-None-Match=V1 → MUST NOT 304.
  B. snapshot=V2, committed=V2, If-None-Match=V2 → 304 allowed.
  C. committed authority unavailable → fallback snapshot allowed,
     truthfully marked.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Optional
import hashlib
import time


@dataclass(frozen=True)
class CommittedIdentity:
    """ONE authoritative identity for a committed board generation.

    All downstream metadata (ETag, X-Board-Version, truth_manifest,
    canonical_revision, generation_id, frontend acceptance) MUST come
    from THIS object.  Never mix fields from a newer board with a
    stale snapshot (item 61).
    """
    board_version:        str   # monotonic increment
    canonical_revision:   str   # bumps whenever canonical picks move
    generation_id:        str   # unique per generation run
    content_hash:         str   # DIAGNOSTIC only — NEVER drives decisions

    def etag(self) -> str:
        return f'W/"{self.board_version}:{self.canonical_revision}"'

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def new_identity(*,
                 board_version: Optional[str] = None,
                 canonical_revision: Optional[str] = None,
                 generation_id: Optional[str] = None,
                 content_hash: Optional[str] = None,
                 picks_fingerprint: Optional[str] = None,
                 ) -> CommittedIdentity:
    """Convenience factory.  Any missing field is derived from the
    current timestamp (ns) to ensure monotonicity.  ``content_hash``
    may be derived from ``picks_fingerprint`` when supplied."""
    ns = str(int(time.time_ns()))
    bv = board_version or f"v{ns[-9:]}"
    cr = canonical_revision or f"r{ns[-7:]}"
    gid = generation_id or f"g{ns[-11:]}"
    ch = content_hash or (
        hashlib.sha256(
            (picks_fingerprint or ns).encode("utf-8")
        ).hexdigest()[:12]
    )
    return CommittedIdentity(board_version=bv,
                              canonical_revision=cr,
                              generation_id=gid,
                              content_hash=ch)


def advance(prev: Optional[CommittedIdentity] = None,
            *, picks_fingerprint: Optional[str] = None,
            ) -> CommittedIdentity:
    """Return a FRESH identity strictly newer than ``prev`` (if any).

    Semantics:
      * ``board_version`` strictly > prev.board_version.
      * ``canonical_revision`` strictly > prev.canonical_revision.
      * ``generation_id`` is always unique.
    """
    ident = new_identity(picks_fingerprint=picks_fingerprint)
    if prev is None:
        return ident
    # Ensure monotonic strings.
    bv = ident.board_version
    if bv <= prev.board_version:
        bv = prev.board_version + "+"
    cr = ident.canonical_revision
    if cr <= prev.canonical_revision:
        cr = prev.canonical_revision + "+"
    return CommittedIdentity(board_version=bv,
                              canonical_revision=cr,
                              generation_id=ident.generation_id,
                              content_hash=ident.content_hash)


# ─────────────────────────────────────────────────────────────────────
# Snapshot decision — the ONE authority for /api/picks/today ETag
# ─────────────────────────────────────────────────────────────────────
def snapshot_decision(*,
                      client_if_none_match: Optional[str],
                      committed: Optional[CommittedIdentity],
                      cached_snapshot: Optional[CommittedIdentity],
                      ) -> dict[str, Any]:
    """Return the response decision for a /api/picks/today request.

    Shape::

        {
          "status":        200 | 304,
          "serve":         "committed" | "cached_snapshot" | None,
          "fallback_stale": bool,
          "etag":          "<W/.../>" | None,
          "reason":        "<human string>",
        }

    Rules:
      * committed == None AND cached_snapshot == None → 503 (both
        unavailable).  Caller handles.
      * committed is present:
          - If client If-None-Match == committed.etag() → 304.
          - Else 200, serve committed.
          - cached_snapshot version mismatch is irrelevant here —
            committed is the authority.
      * committed is None (unavailable), cached_snapshot is present:
          - fallback_stale=True.
          - 200 only; a stale snapshot MUST NOT 304 an If-None-Match
            that was issued against the committed authority (we
            cannot prove the client already has this stale snapshot).
    """
    if committed is None and cached_snapshot is None:
        return {"status": 503,
                 "serve": None,
                 "fallback_stale": False,
                 "etag": None,
                 "reason": "committed_and_snapshot_both_unavailable"}
    if committed is not None:
        etag = committed.etag()
        if client_if_none_match and client_if_none_match.strip() == etag:
            return {"status": 304,
                     "serve": "committed",
                     "fallback_stale": False,
                     "etag": etag,
                     "reason": "etag_match_committed_authority"}
        reason = "committed_authority"
        if cached_snapshot is not None and (
                cached_snapshot.board_version != committed.board_version):
            # Known mismatch — invalidate immediately (item 59).
            reason = "committed_authority_overrides_known_stale_snapshot"
        return {"status": 200,
                 "serve": "committed",
                 "fallback_stale": False,
                 "etag": etag,
                 "reason": reason}
    # committed is None → genuine fallback (item 60 case C).
    assert cached_snapshot is not None
    return {"status": 200,
             "serve": "cached_snapshot",
             "fallback_stale": True,
             "etag": None,          # DO NOT advertise an ETag for a stale fallback
             "reason": "committed_unavailable_falling_back_to_cached_snapshot"}


def response_metadata(identity: CommittedIdentity) -> dict[str, Any]:
    """Return the FULL set of consistent response-metadata fields
    (board_version, canonical_revision, generation_id, truth_manifest
    anchors, X-Board-Version, ETag).  All drawn from ONE identity so
    item 61 (consistency) is proven by construction."""
    return {
        "board_version":       identity.board_version,
        "canonical_revision":  identity.canonical_revision,
        "generation_id":       identity.generation_id,
        "content_hash":        identity.content_hash,   # diagnostic
        "X-Board-Version":     identity.board_version,
        "ETag":                identity.etag(),
    }


__all__ = [
    "CommittedIdentity",
    "new_identity", "advance",
    "snapshot_decision", "response_metadata",
]
