"""Immutable Pregame Snapshot (§8).

The authoritative frozen pregame truth lives in a dedicated
collection ``db.pregame_snapshots`` and MUST NOT be a mutable
sub-document inside ``db.picks``.

Guarantees
----------
* **Immutable** — writes are append-only.  Attempts to overwrite an
  existing snapshot raise ``PregameSnapshotImmutable``.
* **Hash-sealed** — every snapshot carries a deterministic SHA-256
  hash over its canonical JSON serialisation.  The hash is stored
  in the document itself, and the caller can re-compute it later to
  verify integrity.
* **Append-only** — settlement / analytics NEVER modify the snapshot.
  Historical results NEVER modify pregame truth.  Amendments require
  a NEW snapshot linked to the same ``canonical_prediction_id`` via
  ``supersedes``.
* Legacy records that predate this contract do NOT crash — the
  read side simply returns ``None`` (mapped to UNKNOWN upstream).

The functions are async-first because the database driver is Motor,
but a small ``_seal`` helper is synchronous and can be unit-tested
without a DB.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Optional


PREGAME_SNAPSHOTS_COLLECTION = "pregame_snapshots"

# Fields preserved in every snapshot (superset — missing fields
# are simply absent, never faked to zero).
SNAPSHOT_FIELDS: tuple[str, ...] = (
    "canonical_prediction_id",
    "pick_id",
    "sport",
    "market",
    "selection",
    "line",
    "book",
    "book_odds",
    "odds_provenance",
    "model_probability",
    "sim_probability",
    "calibrated_probability",
    "edge",
    "lock_score",
    "tier",
    "evidence",
    "contradictions",
    "data_quality",
    "publication_timestamp",
    "engine_version",
    "model_version",
    "provenance",
    "commence_time",
    "player_name",
    "canonical_player_id",
    "current_team",
    "historical_team",
    "home_team",
    "away_team",
)


class PregameSnapshotImmutable(RuntimeError):
    """Raised when code attempts to mutate an existing snapshot."""


def _canonicalize(payload: dict) -> str:
    """Deterministic JSON encoding used as the hash pre-image.

    * Keys sorted lexicographically.
    * ``separators`` fixed to ensure no whitespace variance.
    * ``default=str`` handles datetimes / UUIDs.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                        default=str, ensure_ascii=False)


def compute_snapshot_hash(payload: dict) -> str:
    """SHA-256 over the canonical JSON of the snapshot payload."""
    return hashlib.sha256(_canonicalize(payload).encode("utf-8")).hexdigest()


def build_snapshot_payload(pick: dict) -> dict:
    """Extract only the SNAPSHOT_FIELDS that are present on the
    pick.  Missing fields are omitted — never coerced to zero (§4).
    """
    payload: dict[str, Any] = {}
    for key in SNAPSHOT_FIELDS:
        if key in pick and pick[key] is not None:
            payload[key] = pick[key]
    # Always record the freeze timestamp — it is intrinsic to the
    # snapshot identity, distinct from the pick's publication_timestamp.
    payload["frozen_at"] = datetime.now(timezone.utc).isoformat()
    return payload


def seal_snapshot(pick: dict) -> dict:
    """Return a hash-sealed snapshot dict ready to insert.

    This is synchronous and side-effect-free so tests can validate
    determinism without touching Mongo.
    """
    payload = build_snapshot_payload(pick)
    snapshot_hash = compute_snapshot_hash(payload)
    return {**payload, "snapshot_hash": snapshot_hash}


def verify_snapshot_hash(snapshot: dict) -> bool:
    """Re-compute the hash from the stored payload and compare.

    Returns False when the snapshot has been tampered with.  A
    missing ``snapshot_hash`` also returns False (never crashes).
    """
    stored = snapshot.get("snapshot_hash")
    if not stored:
        return False
    payload = {k: v for k, v in snapshot.items() if k != "snapshot_hash"
                and k != "_id"}
    return compute_snapshot_hash(payload) == stored


async def freeze_pregame(db, pick: dict, *,
                           supersedes: Optional[str] = None) -> dict:
    """Insert a hash-sealed pregame snapshot into ``pregame_snapshots``.

    Raises ``PregameSnapshotImmutable`` when a snapshot already
    exists for the ``canonical_prediction_id`` unless ``supersedes``
    is passed — in which case a NEW snapshot is inserted linked to
    the previous one (never mutating it).
    """
    cpid = pick.get("canonical_prediction_id")
    if not cpid:
        raise ValueError(
            "canonical_prediction_id is required to freeze a snapshot")

    existing = await db[PREGAME_SNAPSHOTS_COLLECTION].find_one(
        {"canonical_prediction_id": cpid, "supersedes": None},
        {"_id": 1, "snapshot_hash": 1},
    )
    if existing and not supersedes:
        raise PregameSnapshotImmutable(
            f"pregame snapshot already exists for {cpid} "
            f"(hash={existing.get('snapshot_hash')})")

    snapshot = seal_snapshot(pick)
    snapshot["canonical_prediction_id"] = cpid    # ensure present
    snapshot["pick_id"] = pick.get("id") or pick.get("external_id")
    snapshot["supersedes"] = supersedes    # None for the first freeze

    await db[PREGAME_SNAPSHOTS_COLLECTION].insert_one(snapshot)
    # Strip the ObjectId (mongo inserts _id in-place) before returning.
    snapshot.pop("_id", None)
    return snapshot


async def read_pregame_snapshot(
    db,
    *,
    canonical_prediction_id: Optional[str] = None,
    pick_id: Optional[str] = None,
) -> Optional[dict]:
    """Read the latest (non-superseded) snapshot for a prediction.

    Returns None when no snapshot exists — the caller must treat
    that as UNKNOWN, never as "no freeze needed".
    """
    query: dict = {"supersedes": None}
    if canonical_prediction_id:
        query["canonical_prediction_id"] = canonical_prediction_id
    elif pick_id:
        query["pick_id"] = pick_id
    else:
        return None
    doc = await db[PREGAME_SNAPSHOTS_COLLECTION].find_one(query)
    if doc:
        doc.pop("_id", None)
    return doc


async def ensure_pregame_indexes(db) -> None:
    """Idempotent index creation — safe to call at startup."""
    coll = db[PREGAME_SNAPSHOTS_COLLECTION]
    await coll.create_index("canonical_prediction_id")
    await coll.create_index("pick_id")
    await coll.create_index("snapshot_hash")


__all__ = [
    "PREGAME_SNAPSHOTS_COLLECTION",
    "SNAPSHOT_FIELDS",
    "PregameSnapshotImmutable",
    "compute_snapshot_hash",
    "build_snapshot_payload",
    "seal_snapshot",
    "verify_snapshot_hash",
    "freeze_pregame",
    "read_pregame_snapshot",
    "ensure_pregame_indexes",
]
