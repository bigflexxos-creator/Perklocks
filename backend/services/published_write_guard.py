"""Runtime write-guard — Phase 1b immutability enforcement.

Any code path that attempts to `$set`/`$inc`/`$unset` a `published_*`
field OR a legacy alias (lock_score, win_probability, edge_percent,
grade, confidence, book_odds, line, reasoning) on a `picks` document
that already has `published_at` stamped is a **contract violation**.

This module provides two enforcement helpers:

  1. `assert_no_published_mutation(update_ops, *, allow_snapshot_write)`
     — inline guard for anyone writing to `db.picks`.  Raises
     `PublishedFieldMutationError` on violation.

  2. `guarded_update_one(coll, filter, update, ...)` — wrapper around
     `AsyncIOMotorCollection.update_one` that applies the guard
     automatically.  Optional — writers may prefer to call
     `assert_no_published_mutation` directly.

Failure mode
────────────
Publication itself must be able to write these fields (that's what the
`PredictionPublicationService.publish()` call does during dual-write).
The escape hatch is a keyword flag `_publication_write=True` that the
publication service passes; anything else that touches published
fields on a published pick will raise.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Mapping

logger = logging.getLogger("lockscore.write_guard")

# Fields that become immutable after publication.
IMMUTABLE_FIELDS: frozenset[str] = frozenset({
    # published_* (contract fields — MUST never be written except by
    # the publication service)
    "published_probability", "published_edge", "published_lock_score",
    "published_grade", "published_confidence", "published_reasoning",
    "published_line", "published_odds",
    "board_version", "snapshot_version", "payload_hash",
    "idempotency_key", "publication_source", "published_at",
    # legacy aliases — MUST match the published value after Phase 1b.
    # After publication these become read-only projections; any
    # writer that changes them is bypassing the contract.
    "lock_score", "win_probability", "edge_percent",
    "grade", "confidence",
    "book_odds", "odds", "american_odds",
    "line",
    "reasoning",
    # Shadow lock_score fields — retired as of Phase 1b.  These are
    # no longer read by any endpoint; touching them means the writer
    # hasn't been migrated yet.
    "lock_score_v2", "lock_score_raw", "lock_score_peak",
})


class PublishedFieldMutationError(RuntimeError):
    """Raised when a writer tries to mutate an immutable published
    field.  Message includes the offending field(s) + caller hint."""

    def __init__(self, fields: Iterable[str], *, hint: str = ""):
        fs = sorted(set(fields))
        super().__init__(
            f"attempted to mutate immutable published field(s): {fs}"
            + (f"  — {hint}" if hint else "")
        )
        self.fields = fs


def collect_mutated_fields(update: Mapping[str, Any]) -> set[str]:
    """Return the set of field NAMES a Mongo update op would touch."""
    out: set[str] = set()
    if not isinstance(update, Mapping):
        return out
    for op, payload in update.items():
        if not isinstance(payload, Mapping):
            continue
        # $set / $unset / $inc / $mul / $min / $max / $rename — all
        # of them accept `{field_name: value}` as their payload.
        if op in ("$set", "$unset", "$inc", "$mul", "$min", "$max",
                  "$rename", "$setOnInsert"):
            out.update(payload.keys())
    return out


def assert_no_published_mutation(
    update: Mapping[str, Any], *,
    allow_publication_write: bool = False,
    caller: str = "unknown",
) -> None:
    """Raise `PublishedFieldMutationError` if `update` would mutate an
    immutable published field.

    `allow_publication_write=True` grants the publication service the
    escape hatch it needs to perform its own dual-write.  All other
    callers must migrate to `pick_enrichment` (Phase 1a side-car
    collection) or `settlement_events` (Phase 1c) instead of
    mutating `picks`.
    """
    if allow_publication_write:
        return
    touched = collect_mutated_fields(update)
    forbidden = touched & IMMUTABLE_FIELDS
    if forbidden:
        raise PublishedFieldMutationError(
            forbidden, hint=f"caller={caller}")


async def guarded_update_one(
    coll, filter_, update, *,
    allow_publication_write: bool = False,
    caller: str = "unknown",
    **kwargs,
):
    """Drop-in wrapper for `db.picks.update_one` that enforces the
    immutability contract.  Non-picks collections pass through
    unchecked.
    """
    if getattr(coll, "name", "") == "picks":
        assert_no_published_mutation(
            update,
            allow_publication_write=allow_publication_write,
            caller=caller,
        )
    return await coll.update_one(filter_, update, **kwargs)


__all__ = [
    "IMMUTABLE_FIELDS",
    "PublishedFieldMutationError",
    "collect_mutated_fields",
    "assert_no_published_mutation",
    "guarded_update_one",
]
