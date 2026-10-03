"""canonical_cutover — services for Perklocks managed-Mongo cutover.

Implements:
  * Collection allowlist (the 21 certified reconciliation collections).
  * Environment/runtime collection denylist (never carried as canonical).
  * Deterministic logical-identity resolution per collection.
  * Required canonical indexes.
  * Secure canonical-import batch processing with session tracking.
  * Legacy -> canonical server-side copy for out-of-scope collections.
"""
from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timezone
from typing import Any, Iterable

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ReturnDocument, UpdateOne, ASCENDING, DESCENDING, HASHED
from pymongo.errors import BulkWriteError, DuplicateKeyError

logger = logging.getLogger("perklocks.canonical_cutover")

# ─── 21 certified reconciliation collections (exact allowlist) ───────
RECONCILIATION_COLLECTIONS: tuple[str, ...] = (
    "games",
    "historical_ingestion_state",
    "nfl_ingest_meta",
    "nfl_player_weekly",
    "parlay_history",
    "picks",
    "player_game_actuals",
    "player_game_logs",
    "player_identities",
    "prediction_snapshots",
    "pregame_snapshots",
    "publication_events",
    "rollover_slate_events",
    "rollover_slates",
    "settlement_events",
    "soccer_matches",
    "soccer_player_game_logs",
    "team_game_actuals",
    "tennis_matches_history",
    "user_bets",
    "users",
)

# ─── Environment / runtime denylist (never canonical) ─────────────────
ENVIRONMENT_STATE_COLLECTIONS: frozenset[str] = frozenset({
    "canonical_worker_leases",
    "scheduled_jobs",
    "provider_budget_state",
    "provider_request_intents",
    "board_generations",
})

# ─── Rebuildable caches/logs that should NOT be blindly copied ───────
REBUILDABLE_COLLECTIONS: frozenset[str] = frozenset({
    "funnel_telemetry", "espn_cache", "sportdb_cache", "fotmob_cache",
    "weather_cache", "injury_cache", "odds_history_cache", "lines_cache",
    "cache_tokens", "diag_logs", "operator_evidence_cache",
})


# ─── Logical-identity resolution per collection ──────────────────────
# Mirrors the Phase 2 reconciliation contract.  Determines which
# field(s) form the canonical upsert key.  Fallback is `_id`.
_LOGICAL_KEYS: dict[str, tuple[str, ...]] = {
    "games":                       ("game_id",),
    "historical_ingestion_state":  ("provider", "sport", "season"),
    "nfl_ingest_meta":             ("key",),
    "nfl_player_weekly":           ("player_id", "season", "week"),
    "parlay_history":              ("parlay_id",),
    "picks":                       ("pick_id",),
    "player_game_actuals":         ("sport", "event_id", "player_id", "market"),
    "player_game_logs":            ("sport", "game_id", "player_id"),
    "player_identities":           ("canonical_player_id",),
    "prediction_snapshots":        ("prediction_id", "snapshot_version"),
    "pregame_snapshots":           ("snapshot_hash",),
    "publication_events":          ("payload_hash",),
    "rollover_slate_events":       ("event_id",),
    "rollover_slates":             ("slate_id",),
    "settlement_events":           ("event_id",),
    "soccer_matches":              ("match_id",),
    "soccer_player_game_logs":     ("match_id", "player_id"),
    "team_game_actuals":           ("sport", "event_id", "team"),
    "tennis_matches_history":      ("match_id",),
    "user_bets":                   ("bet_id",),
    "users":                       ("id",),
}


def logical_key_fields(coll: str) -> tuple[str, ...]:
    """Return the authoritative logical-key field tuple for a canonical
    collection, or ('_id',) as a safe fallback for unknown collections.
    """
    return _LOGICAL_KEYS.get(coll, ("_id",))


def extract_logical_key(coll: str, doc: dict) -> tuple[Any, ...]:
    """Extract the logical-key tuple from a document for a given
    canonical collection."""
    return tuple(doc.get(f) for f in logical_key_fields(coll))


# ─── Canonical indexes per collection ────────────────────────────────
# Specifications must include EVERYTHING required for correctness —
# unique constraints, TTL expirations, partial filters, collations.
_INDEX_SPECS: dict[str, list[dict[str, Any]]] = {
    "games": [
        {"keys": [("game_id", ASCENDING)], "name": "ux_game_id", "unique": True},
        {"keys": [("sport", ASCENDING), ("event_date", DESCENDING)], "name": "ix_sport_date"},
    ],
    "users": [
        {"keys": [("id", ASCENDING)], "name": "ux_user_id", "unique": True},
        {"keys": [("email", ASCENDING)], "name": "ux_user_email", "unique": True, "sparse": True},
    ],
    "user_bets": [
        {"keys": [("bet_id", ASCENDING)], "name": "ux_bet_id", "unique": True},
        {"keys": [("user_id", ASCENDING), ("created_at", DESCENDING)], "name": "ix_user_created"},
    ],
    "picks": [
        {"keys": [("pick_id", ASCENDING)], "name": "ux_pick_id", "unique": True},
        {"keys": [("sport", ASCENDING), ("published_at", DESCENDING)], "name": "ix_sport_published"},
        {"keys": [("board_version", ASCENDING)], "name": "ix_board_version"},
    ],
    "prediction_snapshots": [
        {"keys": [("prediction_id", ASCENDING), ("snapshot_version", ASCENDING)],
         "name": "ux_prediction_snapshot_version", "unique": True},
    ],
    "publication_events": [
        {"keys": [("payload_hash", ASCENDING)], "name": "ux_payload_hash", "unique": True},
        {"keys": [("published_at", DESCENDING)], "name": "ix_published_at"},
    ],
    "pregame_snapshots": [
        {"keys": [("snapshot_hash", ASCENDING)], "name": "ux_snapshot_hash", "unique": True},
    ],
    "settlement_events": [
        {"keys": [("event_id", ASCENDING)], "name": "ux_settlement_event_id", "unique": True},
        {"keys": [("sport", ASCENDING), ("settled_at", DESCENDING)], "name": "ix_sport_settled"},
    ],
    "player_game_actuals": [
        {"keys": [("sport", ASCENDING), ("event_id", ASCENDING),
                  ("player_id", ASCENDING), ("market", ASCENDING)],
         "name": "ux_pga_identity", "unique": True},
    ],
    "player_game_logs": [
        {"keys": [("sport", ASCENDING), ("game_id", ASCENDING), ("player_id", ASCENDING)],
         "name": "ux_pgl_identity", "unique": True},
    ],
    "player_identities": [
        {"keys": [("canonical_player_id", ASCENDING)], "name": "ux_pi_canonical", "unique": True},
    ],
    "team_game_actuals": [
        {"keys": [("sport", ASCENDING), ("event_id", ASCENDING), ("team", ASCENDING)],
         "name": "ux_tga_identity", "unique": True},
    ],
    "soccer_matches": [
        {"keys": [("match_id", ASCENDING)], "name": "ux_sm_match_id", "unique": True},
    ],
    "soccer_player_game_logs": [
        {"keys": [("match_id", ASCENDING), ("player_id", ASCENDING)],
         "name": "ux_spgl_identity", "unique": True},
    ],
    "tennis_matches_history": [
        {"keys": [("match_id", ASCENDING)], "name": "ux_tmh_match_id", "unique": True},
    ],
    "nfl_player_weekly": [
        {"keys": [("player_id", ASCENDING), ("season", ASCENDING), ("week", ASCENDING)],
         "name": "ux_nflpw_identity", "unique": True},
    ],
    "nfl_ingest_meta": [
        {"keys": [("key", ASCENDING)], "name": "ux_nflim_key", "unique": True},
    ],
    "parlay_history": [
        {"keys": [("parlay_id", ASCENDING)], "name": "ux_parlay_id", "unique": True},
    ],
    "rollover_slates": [
        {"keys": [("slate_id", ASCENDING)], "name": "ux_rollover_slate_id", "unique": True},
    ],
    "rollover_slate_events": [
        {"keys": [("event_id", ASCENDING)], "name": "ux_rollover_event_id", "unique": True},
    ],
    "historical_ingestion_state": [
        {"keys": [("provider", ASCENDING), ("sport", ASCENDING), ("season", ASCENDING)],
         "name": "ux_his_identity", "unique": True},
    ],
}


def required_indexes_for(coll: str) -> list[dict[str, Any]]:
    """Return required index specifications for a canonical collection."""
    return list(_INDEX_SPECS.get(coll, []))


async def ensure_canonical_indexes(db: AsyncIOMotorDatabase, coll: str) -> dict[str, Any]:
    """Create required indexes on a canonical collection.  Returns a
    status dict.  Raises if a unique index cannot be created because
    the data violates uniqueness — we never silently weaken an index.
    """
    created: list[str] = []
    existed: list[str] = []
    failed:  list[dict] = []
    for spec in required_indexes_for(coll):
        name = spec.get("name")
        kwargs = {k: v for k, v in spec.items() if k != "keys"}
        try:
            # create_index is idempotent when the spec matches an
            # existing one; Mongo raises if the spec conflicts.
            res = await db[coll].create_index(spec["keys"], **kwargs)
            if res == name:
                created.append(name)
            else:
                existed.append(name)
        except DuplicateKeyError as e:
            failed.append({"name": name, "error": "DUPLICATE_KEY", "details": str(e)[:400]})
        except Exception as e:
            failed.append({"name": name, "error": type(e).__name__, "details": str(e)[:400]})
    return {"collection": coll, "created": created, "existed": existed,
            "failed": failed, "ok": len(failed) == 0}


# ─── Document hygiene ────────────────────────────────────────────────
EXCLUSION_FIELD      = "excluded_from_canonical_runtime"
QUARANTINE_STATUSES  = {"UNRESOLVED_IMMUTABLE_CONFLICT"}


def is_excluded(doc: dict) -> tuple[bool, str]:
    """Return (True, reason) if doc must be excluded from canonical
    runtime import; otherwise (False, '')."""
    if doc.get(EXCLUSION_FIELD) is True:
        return True, "excluded_from_canonical_runtime=true"
    status = doc.get("status")
    if status in QUARANTINE_STATUSES:
        return True, f"quarantined status: {status}"
    return False, ""


def batch_content_hash(coll: str, batch: list[dict]) -> str:
    """Deterministic hash of a batch for idempotency / replay detection.
    Sorts documents by extracted logical-key before hashing so equivalent
    batches with different input ordering produce the same hash."""
    rows = []
    for doc in batch:
        lk = extract_logical_key(coll, doc)
        rows.append((lk, doc))
    rows.sort(key=lambda r: tuple(str(x) for x in r[0]))
    h = hashlib.sha256()
    import json
    for lk, doc in rows:
        h.update(json.dumps([list(lk), doc], default=str, sort_keys=True).encode())
    return h.hexdigest()


# ─── Import session storage ──────────────────────────────────────────
SESSION_COLLECTION       = "_canonical_import_sessions"
BATCH_COLLECTION         = "_canonical_import_batches"
AUDIT_COLLECTION         = "_canonical_import_audit"


async def record_batch(
    db: AsyncIOMotorDatabase,
    *,
    session_id: str,
    collection: str,
    batch_no: int,
    content_hash: str,
    doc_count: int,
    accepted: int,
    rejected: int,
    source_checkpoints: dict,
) -> dict:
    """Record a batch outcome.  Idempotent re-record requires exact same
    content_hash.  Mismatch raises ``RuntimeError`` — a safety gate
    for altered-replay detection."""
    key = {"session_id": session_id, "collection": collection, "batch_no": batch_no}
    existing = await db[BATCH_COLLECTION].find_one(key)
    if existing is not None:
        if existing.get("content_hash") != content_hash:
            raise RuntimeError(
                f"ALTERED_REPLAY_REJECTED: session={session_id} "
                f"coll={collection} batch={batch_no} "
                f"old_hash={existing.get('content_hash')!r} "
                f"new_hash={content_hash!r}"
            )
        return existing, True
    doc = dict(key, **{
        "content_hash":      content_hash,
        "doc_count":         doc_count,
        "accepted":          accepted,
        "rejected":          rejected,
        "source_checkpoints": source_checkpoints,
        "recorded_at":       datetime.now(timezone.utc),
    })
    await db[BATCH_COLLECTION].insert_one(doc)
    return doc, False


async def upsert_session(
    db: AsyncIOMotorDatabase,
    *,
    session_id: str,
    source_checkpoints: dict,
) -> dict:
    """Create-or-update a session envelope.  Idempotent."""
    now = datetime.now(timezone.utc)
    await db[SESSION_COLLECTION].update_one(
        {"session_id": session_id},
        {"$setOnInsert": {"session_id": session_id, "started_at": now,
                           "source_checkpoints": source_checkpoints},
         "$set":          {"last_updated_at": now}},
        upsert=True,
    )
    return await db[SESSION_COLLECTION].find_one({"session_id": session_id})


async def audit_log(db: AsyncIOMotorDatabase, event: str, meta: dict) -> None:
    """Structured audit log — never records document payloads or secrets."""
    safe = {k: v for k, v in meta.items() if k not in ("docs", "payload", "token")}
    await db[AUDIT_COLLECTION].insert_one({
        "event":       event,
        "at":          datetime.now(timezone.utc),
        "meta":        safe,
    })


# ─── Canonical import batch processor ────────────────────────────────
async def import_batch(
    db:                 AsyncIOMotorDatabase,
    *,
    collection:         str,
    docs:               list[dict],
    session_id:         str,
    batch_no:           int,
    source_checkpoints: dict,
    max_batch_size:     int = 5000,
) -> dict:
    """Validate + upsert a batch of canonical documents.

    Semantics:
      * Rejects unknown collection (not in allowlist).
      * Rejects environment-state collections outright.
      * Rejects per-doc any row with ``excluded_from_canonical_runtime=true``
        or quarantined status.
      * Logical-key upsert using the collection's certified identity.
      * Idempotent on exact replay; altered replay with same batch_no
        raises ``ALTERED_REPLAY_REJECTED``.
    """
    if collection not in RECONCILIATION_COLLECTIONS:
        if collection in ENVIRONMENT_STATE_COLLECTIONS:
            raise ValueError(f"ENVIRONMENT_STATE_COLLECTION_REJECTED: {collection}")
        raise ValueError(f"UNKNOWN_COLLECTION_REJECTED: {collection}")
    if not isinstance(docs, list):
        raise ValueError("docs must be a list")
    if len(docs) > max_batch_size:
        raise ValueError(f"BATCH_TOO_LARGE: {len(docs)} > {max_batch_size}")
    if not docs:
        return {"collection": collection, "accepted": 0, "rejected": 0,
                 "batch_no": batch_no, "idempotent_replay": False,
                 "content_hash": batch_content_hash(collection, [])}

    # Per-doc filtering (exclusions, quarantine)
    accepted: list[dict] = []
    rejected: list[dict] = []
    for doc in docs:
        exc, reason = is_excluded(doc)
        if exc:
            rejected.append({"logical_key": list(extract_logical_key(collection, doc)),
                              "reason": reason})
            continue
        accepted.append(doc)

    # Content hash of the ACCEPTED subset (idempotency key)
    content_hash = batch_content_hash(collection, accepted)

    # Session + batch tracking
    await upsert_session(db, session_id=session_id, source_checkpoints=source_checkpoints)
    try:
        batch_record, is_replay = await record_batch(
            db,
            session_id=session_id,
            collection=collection,
            batch_no=batch_no,
            content_hash=content_hash,
            doc_count=len(docs),
            accepted=len(accepted),
            rejected=len(rejected),
            source_checkpoints=source_checkpoints,
        )
    except RuntimeError as e:
        # Altered replay — do NOT write anything.
        await audit_log(db, "altered_replay_rejected", {
            "session_id": session_id, "collection": collection,
            "batch_no": batch_no, "err": str(e)[:400]})
        raise

    # Idempotent replay: content_hash matched an existing recorded batch.
    if is_replay:
        await audit_log(db, "batch_replay_idempotent", {
            "session_id": session_id, "collection": collection,
            "batch_no": batch_no, "accepted": len(accepted),
            "rejected": len(rejected)})
        return {"collection": collection, "accepted": len(accepted),
                "rejected": len(rejected), "rejections": rejected[:50],
                "batch_no": batch_no, "idempotent_replay": True,
                "content_hash": content_hash}

    # Upsert using logical identity
    key_fields = logical_key_fields(collection)
    ops: list[UpdateOne] = []
    for doc in accepted:
        key = {f: doc.get(f) for f in key_fields}
        if any(v is None for v in key.values()):
            rejected.append({"logical_key": list(key.values()),
                              "reason": "MISSING_LOGICAL_KEY_COMPONENT"})
            continue
        # Strip _id if present so Mongo doesn't fight the logical key upsert
        payload = {k: v for k, v in doc.items() if k != "_id"}
        ops.append(UpdateOne(key, {"$set": payload}, upsert=True))

    upserted = 0
    matched  = 0
    if ops:
        try:
            res = await db[collection].bulk_write(ops, ordered=False)
            upserted = res.upserted_count
            matched  = res.matched_count
        except BulkWriteError as e:
            # Partial failure — surface detail but do not retry.
            await audit_log(db, "bulk_write_error", {
                "session_id": session_id, "collection": collection,
                "batch_no": batch_no, "details": str(e)[:800]})
            raise

    await audit_log(db, "batch_accepted", {
        "session_id": session_id, "collection": collection,
        "batch_no": batch_no, "accepted": len(accepted),
        "rejected": len(rejected), "upserted": upserted, "matched": matched,
        "content_hash": content_hash})

    return {"collection": collection, "accepted": len(accepted),
            "rejected": len(rejected), "rejections": rejected[:50],
            "upserted": upserted, "matched": matched,
            "batch_no": batch_no, "idempotent_replay": False,
            "content_hash": content_hash}


# ─── Collection fingerprint ──────────────────────────────────────────
async def collection_fingerprint(
    db: AsyncIOMotorDatabase,
    coll: str,
    *,
    limit: int | None = None,
) -> str:
    """Deterministic fingerprint of a canonical collection: SHA-256 of
    sorted logical-key tuples.  Independent of document content/order."""
    keys: list[tuple] = []
    cursor = db[coll].find({}, {f: 1 for f in logical_key_fields(coll)})
    if limit:
        cursor = cursor.limit(limit)
    async for doc in cursor:
        keys.append(extract_logical_key(coll, doc))
    keys.sort(key=lambda k: tuple(str(x) for x in k))
    h = hashlib.sha256()
    import json
    for k in keys:
        h.update(json.dumps(list(k), default=str).encode())
    return h.hexdigest()


# ─── Legacy → canonical copy (out-of-scope collections) ──────────────
async def copy_legacy_collection(
    legacy_db:  AsyncIOMotorDatabase,
    canon_db:   AsyncIOMotorDatabase,
    coll:       str,
    *,
    batch_size: int = 1000,
) -> dict:
    """Server-side copy of a non-reconciled collection from legacy to
    canonical.  Resumable (upserts by _id).  Never deletes from legacy.
    Refuses to copy denylist/rebuildable collections."""
    if coll in RECONCILIATION_COLLECTIONS:
        raise ValueError(f"RECONCILED_COLLECTION_USE_IMPORT_ENDPOINT: {coll}")
    if coll in ENVIRONMENT_STATE_COLLECTIONS:
        raise ValueError(f"ENVIRONMENT_STATE_COLLECTION_REFUSED: {coll}")
    if coll in REBUILDABLE_COLLECTIONS:
        raise ValueError(f"REBUILDABLE_COLLECTION_REFUSED: {coll}")

    source_count = await legacy_db[coll].count_documents({})
    target_before = await canon_db[coll].count_documents({})

    copied = 0
    buf: list[UpdateOne] = []
    async for doc in legacy_db[coll].find({}, no_cursor_timeout=True):
        payload = {k: v for k, v in doc.items() if k != "_id"}
        buf.append(UpdateOne({"_id": doc["_id"]}, {"$set": payload}, upsert=True))
        if len(buf) >= batch_size:
            await canon_db[coll].bulk_write(buf, ordered=False)
            copied += len(buf)
            buf.clear()
    if buf:
        await canon_db[coll].bulk_write(buf, ordered=False)
        copied += len(buf)

    target_after = await canon_db[coll].count_documents({})

    # Also copy indexes (names only; create via create_indexes with
    # explicit specs filtered to supported options).
    legacy_indexes = await legacy_db[coll].index_information()
    index_copy = []
    for name, spec in legacy_indexes.items():
        if name == "_id_":
            continue
        try:
            keys = spec["key"]
            kw = {k: v for k, v in spec.items() if k in ("unique", "sparse",
                   "expireAfterSeconds", "partialFilterExpression", "collation")}
            await canon_db[coll].create_index(keys, name=name, **kw)
            index_copy.append({"name": name, "status": "created"})
        except Exception as e:
            index_copy.append({"name": name, "status": "failed",
                                "error": str(e)[:300]})

    return {"collection": coll, "source_count": source_count,
            "target_before": target_before, "target_after": target_after,
            "copied_batches": copied, "indexes": index_copy,
            "ok": target_after >= source_count}


# ─── Preview-authority guard ─────────────────────────────────────────
def refuse_if_preview_authority() -> None:
    """Raise if this process is running under preview authority.
    Canonical mutation workers must NEVER run in preview."""
    da = (os.environ.get("DATA_AUTHORITY") or "").strip().lower()
    if da == "preview":
        raise RuntimeError(
            "REFUSED: canonical mutation attempted while DATA_AUTHORITY=preview"
        )
