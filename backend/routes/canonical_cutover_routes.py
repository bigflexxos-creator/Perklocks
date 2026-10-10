"""canonical_cutover_routes — admin surface for the Perklocks managed
Mongo cutover.

Endpoints (all under /api/admin/, all admin+token-gated):

  POST /api/admin/canonical-import
      Accept a batch of up to 5,000 canonical documents.  Validates
      against the 21-collection allowlist, strips excluded/quarantined
      rows, upserts by logical identity, records the batch for
      idempotent replay.

  GET  /api/admin/canonical-import/status
      Returns per-collection progress of the import session: expected,
      received, accepted, rejected, unique logical IDs seen,
      completion.  No credentials.

  POST /api/admin/canonical-cutover/create-indexes
      Create the required canonical indexes on the active target.
      Fails loudly if a unique constraint cannot be enforced.

  POST /api/admin/canonical-cutover/copy-legacy-collection
      Server-side copy of ONE legacy collection to canonical.  Refuses
      the 21 reconciled, the env-state denylist, and rebuildable
      caches.

  GET  /api/admin/data-authority/status
      Safe diagnostic snapshot of DB routing + env flags for both
      legacy and canonical.  Never exposes URIs, passwords, tokens.

Security gates (every endpoint):
  * ``CANONICAL_IMPORT_ENABLED`` must equal ``"true"`` for mutating
    endpoints.  Default: disabled.
  * Header ``X-Canonical-Import-Token`` must equal
    ``CANONICAL_IMPORT_TOKEN`` (constant-time compare).  Never logged.
  * Admin JWT user via existing ``require_admin_user`` chain.
"""
from __future__ import annotations

import hmac
import logging
import os
import secrets
import time
from datetime import datetime, timezone
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from auth import UserPublic, require_admin_user, oauth2_scheme
from services.database import (
    get_canonical_database, get_legacy_database, safe_database_diagnostics,
    use_canonical_db_enabled, active_database_name, canonical_fallback_enabled,
)
from services.canonical_cutover import (
    RECONCILIATION_COLLECTIONS, ENVIRONMENT_STATE_COLLECTIONS,
    import_batch, collection_fingerprint, ensure_canonical_indexes,
    copy_legacy_collection, logical_key_fields, extract_logical_key,
    SESSION_COLLECTION, BATCH_COLLECTION, AUDIT_COLLECTION,
    audit_log as _audit_log_impl,
)
from services.canonical_dedupe import (
    APPLY_DEDUPE_SCOPE,
    DEDUPE_AUDIT_COLLECTION,
    EXACT_DUPLICATE, CONFLICTING_DUPLICATE,
    canonical_doc_fingerprint,
    classify_group,
    scan_duplicates,
    apply_dedupe_group,
)


async def audit_log_local(event: str, meta: dict) -> None:
    """Thin wrapper that routes audit entries through the active
    canonical database.  Keeps the Phase-5-R3 reset / cert endpoints
    free of direct DB-handle plumbing."""
    try:
        await _audit_log_impl(get_canonical_database(), event, meta)
    except Exception:
        logger.exception("audit_log_local failed for event=%s", event)

logger = logging.getLogger("perklocks.cutover_routes")

router = APIRouter(prefix="/api/admin", tags=["canonical-cutover"])


# ─── Auth helpers ────────────────────────────────────────────────────
def _import_enabled() -> bool:
    return (os.environ.get("CANONICAL_IMPORT_ENABLED") or "false").strip().lower() == "true"


def _expected_token() -> str:
    return (os.environ.get("CANONICAL_IMPORT_TOKEN") or "").strip()


def _verify_import_token(provided: Optional[str]) -> None:
    expected = _expected_token()
    if not expected:
        raise HTTPException(status_code=503, detail="CANONICAL_IMPORT_TOKEN unset")
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="invalid canonical-import token")


async def _require_admin(token: Optional[str] = Depends(oauth2_scheme)) -> UserPublic:
    # Admin check always runs against the legacy DB for user records
    # pre-cutover; after cutover both DBs have the users collection, but
    # admin identity is anchored to the active-DB user record.
    from services.database import get_database
    return await require_admin_user(get_database(), token)


# ─── Pydantic models ─────────────────────────────────────────────────
class ImportBatchRequest(BaseModel):
    session_id:         str = Field(..., min_length=8, max_length=128)
    collection:         str
    batch_no:           int = Field(..., ge=0)
    docs:               list[dict]
    source_checkpoints: dict = Field(default_factory=dict)


class CopyLegacyRequest(BaseModel):
    collection: str
    confirm:    bool = Field(default=False)


class CreateIndexesRequest(BaseModel):
    collections: list[str] = Field(default_factory=lambda: list(RECONCILIATION_COLLECTIONS))


class ResetCanonicalDatasetRequest(BaseModel):
    """Scoped reset of canonical dataset. REFUSES to touch legacy or any
    collection outside the 21-collection reconciliation allowlist.

    Phase-5-R3 resumable-retry fix: setting ``drop_canonical_collections=False``
    keeps every ``canonical_<name>`` collection intact and only clears
    the session's batch/session bookkeeping — used to switch batch
    size on an already-partially-imported session without triggering
    ``ALTERED_REPLAY_REJECTED`` on content_hash change.
    """
    confirm:                                 bool = Field(default=False)
    i_understand_this_drops_canonical:       str  = Field(default="")
    only_canonical_prefix_collections_allowed: bool = Field(default=False)
    session_id_to_clear_bookkeeping_for:     Optional[str] = Field(default=None)
    drop_canonical_collections:              bool = Field(default=True)


class R3CertificationRequest(BaseModel):
    session_id: str = Field(..., min_length=8, max_length=128)
    # Expected counts come from the client's SHA-verified checkpoint audit.
    # Server verifies every one of the 21 collections has an entry; any
    # unexplained delta FAILS cert.
    expected: dict[str, dict] = Field(default_factory=dict)
    #   expected[coll] = {
    #     "source_rows":                int,
    #     "unique_logical_identities":  int,
    #     "excluded_rows":              int,
    #     "quarantined_rows":           int,
    #     "null_logical_key_rows":      int,
    #     "expected_canonical_rows":    int,
    #     "expected_source_fingerprint": Optional[str],
    #   }
    tolerance_rows: int = Field(default=0, ge=0)


# ─── Endpoints ───────────────────────────────────────────────────────
@router.post("/canonical-import")
async def canonical_import(
    req: ImportBatchRequest,
    admin: Annotated[UserPublic, Depends(_require_admin)],
    x_canonical_import_token: Annotated[Optional[str], Header()] = None,
):
    """Import one batch into lockscore_canonical."""
    if not _import_enabled():
        raise HTTPException(status_code=403, detail="CANONICAL_IMPORT_ENABLED=false")
    _verify_import_token(x_canonical_import_token)
    if len(req.docs) > 5000:
        raise HTTPException(status_code=413, detail="BATCH_TOO_LARGE_MAX_5000")

    canon_db = get_canonical_database()
    try:
        result = await import_batch(
            canon_db,
            collection=req.collection,
            docs=req.docs,
            session_id=req.session_id,
            batch_no=req.batch_no,
            source_checkpoints=req.source_checkpoints,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        # Surface detail to admin caller — the reliability middleware would
        # otherwise swallow this behind a generic 500.  Not a secret: the
        # exception class name + message are only visible to authenticated
        # admins who already have the import token.
        import traceback as _tb
        logger.error("canonical_import failed: %s\n%s", e, _tb.format_exc())
        raise HTTPException(
            status_code=500,
            detail={"error_class": type(e).__name__, "error": str(e)[:800]},
        )
    return result


@router.get("/canonical-import/status")
async def canonical_import_status(
    admin: Annotated[UserPublic, Depends(_require_admin)],
    session_id: Optional[str] = None,
):
    """Session + per-collection progress."""
    canon_db = get_canonical_database()
    query: dict = {}
    if session_id:
        query["session_id"] = session_id

    sessions = []
    async for s in canon_db[SESSION_COLLECTION].find(query).sort("started_at", -1).limit(10):
        s["_id"] = str(s["_id"])
        sessions.append(s)

    per_coll_agg: dict[str, dict] = {}
    cursor = canon_db[BATCH_COLLECTION].find(query) if session_id else \
             canon_db[BATCH_COLLECTION].find({})
    async for b in cursor:
        coll = b.get("collection")
        a = per_coll_agg.setdefault(coll, {
            "batches":        0,
            "batches_succeeded": 0,
            "batches_failed":    0,
            "batches_in_progress": 0,
            "accepted":       0,
            "rejected":       0,
            "total_docs":     0,
            "upserted":       0,
            "matched":        0,
        })
        a["batches"] += 1
        status_ = b.get("status") or (
            # Phase-5-R1/R2 compat: records that pre-date the ``status``
            # field were implicitly treated as succeeded.  Phase-5-R3
            # explicitly writes ``status`` on every record.
            "succeeded" if "recorded_at" in b and "status" not in b else "unknown"
        )
        if status_ == "succeeded":   a["batches_succeeded"] += 1
        elif status_ == "failed":    a["batches_failed"] += 1
        elif status_ == "in_progress": a["batches_in_progress"] += 1
        elif status_ == "incomplete_write": a["batches_failed"] += 1
        a["accepted"]  += b.get("accepted", 0) or b.get("accepted_count", 0)
        a["rejected"]  += b.get("rejected", 0) or b.get("rejected_count", 0)
        a["total_docs"] += b.get("doc_count", 0)
        a["upserted"]  += int(b.get("upserted_count") or 0)
        a["matched"]   += int(b.get("matched_count")  or 0)

    # Current canonical counts + unique logical IDs
    per_coll_current = {}
    for coll in RECONCILIATION_COLLECTIONS:
        try:
            cnt = await canon_db[coll].count_documents({})
            per_coll_current[coll] = {
                "canonical_count": cnt,
                "logical_key_fields": list(logical_key_fields(coll)),
            }
        except Exception:
            per_coll_current[coll] = {"canonical_count": -1}

    return {
        "sessions":            sessions,
        "aggregated_batches":  per_coll_agg,
        "current_canonical":   per_coll_current,
        "import_enabled":      _import_enabled(),
    }


# ─── Lightweight read-only per-batch manifest (R3 Resume #11+) ──────
#
# Why this endpoint exists
# ────────────────────────
# R3 Resume #10 Phase 5 failed because the accelerated resume driver
# repartitioned existing batches in the authoritative session using a
# fresh 250-record slicing plan.  That produced different payload
# hashes for already-known ``batch_no`` values, which Production
# correctly rejected with ``ALTERED_REPLAY_REJECTED`` (HTTP 409).
#
# The resume driver needs an authoritative, deterministic snapshot of
# the server-side batch bookkeeping so it can
#   1. preserve existing batch identities + layout
#   2. compute the local payload hash BEFORE sending
#   3. STOP locally with a ``BATCH_LAYOUT_MISMATCH`` instead of
#      spraying hundreds of known-to-fail altered-replay 409s.
#
# ``/canonical-import/status`` is intentionally session-wide and
# aggregated.  ``/canonical-import/forensic-audit`` is per-collection
# but also scans the canonical collection and audit log (expensive).
# This new endpoint is the smallest possible read-only slice:
# ``_canonical_import_batches`` filtered by (session_id, collection),
# returning exactly the fields needed to deterministically reconstruct
# each prior batch's payload.
#
# Contract (READ-ONLY):
#   * NO writes.  NO mutation.  NO flag flips.  NO replay-contract
#     changes.  NO session resets.  NO bookkeeping deletes.
#   * Admin JWT required (same bar as other admin diagnostics).
#   * ``X-Canonical-Import-Token`` NOT required — this endpoint cannot
#     cause state change on canonical data.
@router.get("/canonical-import/batch-manifest")
async def canonical_import_batch_manifest(
    admin: Annotated[UserPublic, Depends(_require_admin)],
    session_id: str,
    collection: str,
):
    """READ-ONLY authoritative per-batch metadata for one
    (session_id, collection).  Minimum fields required so an external
    resume driver can deterministically reconstruct the exact prior
    batch layout and verify its local payload hash matches the
    server's stored ``content_hash`` BEFORE issuing any POST.

    Response shape::

        {
          "session_id": "...",
          "collection": "...",
          "logical_key_fields": [...],
          "batches": [
            {
              "batch_no":        0,
              "content_hash":    "sha256-hex",
              "status":          "succeeded"|"failed"|"in_progress"|"incomplete_write"|"unknown",
              "doc_count":       250,
              "accepted_count":  250,
              "rejected_count":  0,
              "upserted_count":  0,
              "matched_count":   250,
              "attempt_count":   3,
              "first_seen_at":   "...",
              "last_attempt_at": "..."
            },
            ...
          ],
          "total_batches":        N,
          "max_batch_no":         N-1,
          "max_doc_count":        250,
          "distinct_doc_counts":  [250, 73]
        }

    The response is deterministically sorted by ``batch_no``.  Nothing
    outside ``_canonical_import_batches`` is read, so the endpoint
    stays well under the 85 s middleware envelope for any session.
    """
    if not session_id or len(session_id) < 8 or len(session_id) > 128:
        raise HTTPException(status_code=400,
            detail="session_id must be 8..128 chars")
    if collection not in RECONCILIATION_COLLECTIONS:
        raise HTTPException(status_code=400,
            detail=f"UNKNOWN_COLLECTION: {collection!r}")

    canon_db = get_canonical_database()
    batches: list[dict] = []
    cursor = canon_db[BATCH_COLLECTION].find(
        {"session_id": session_id, "collection": collection}
    ).sort("batch_no", 1)
    async for b in cursor:
        status_ = b.get("status") or (
            "succeeded" if "recorded_at" in b and "status" not in b else "unknown"
        )
        batches.append({
            "batch_no":        int(b.get("batch_no", -1)),
            "content_hash":    b.get("content_hash"),
            "status":          status_,
            "doc_count":       int(b.get("doc_count") or 0),
            "accepted_count":  int(b.get("accepted_count") or b.get("accepted") or 0),
            "rejected_count":  int(b.get("rejected_count") or b.get("rejected") or 0),
            "upserted_count":  int(b.get("upserted_count") or 0),
            "matched_count":   int(b.get("matched_count")  or 0),
            "attempt_count":   int(b.get("attempt_count")  or 0),
            "first_seen_at":   str(b.get("first_seen_at") or ""),
            "last_attempt_at": str(b.get("last_attempt_at") or b.get("recorded_at") or ""),
            # R3 Resume #13 diagnostic — expose the source_checkpoints
            # that the ORIGINAL successful import used, so an external
            # canary can detect checkpoint drift between the original
            # NDJSON and the currently-pinned checkpoint SHAs (one of
            # the top candidate root causes for a per-batch hash
            # mismatch when batch size + membership count are correct).
            "source_checkpoints": b.get("source_checkpoints") or {},
        })

    doc_counts = sorted({b["doc_count"] for b in batches if b["doc_count"] > 0})
    max_doc_count = max(doc_counts) if doc_counts else 0
    max_batch_no = max((b["batch_no"] for b in batches), default=-1)

    return {
        "session_id":          session_id,
        "collection":          collection,
        "logical_key_fields":  list(logical_key_fields(collection)),
        "batches":             batches,
        "total_batches":       len(batches),
        "max_batch_no":        max_batch_no,
        "max_doc_count":       max_doc_count,
        "distinct_doc_counts": doc_counts,
    }


# ─── Forensic read-only audit (Phase-5-R2 settlement_events investigation) ──
@router.get("/canonical-import/forensic-audit")
async def canonical_import_forensic_audit(
    admin: Annotated[UserPublic, Depends(_require_admin)],
    session_id: str,
    collection: str,
):
    """READ-ONLY forensic diagnostic — never writes, never activates.
    Returns per-session+collection:
      * batch records with (batch_no, accepted, rejected, content_hash)
      * audit log aggregates for batch_accepted (sum of upserted/matched)
      * audit log counts for bulk_write_error and altered_replay_rejected
      * current canonical collection count
      * per-identity sample (first 20) with is_active split so we can
        prove whether the active/historical ledger rows survived.
    """
    canon_db = get_canonical_database()
    # Batch records
    batches: list[dict] = []
    bcur = canon_db[BATCH_COLLECTION].find(
        {"session_id": session_id, "collection": collection}
    ).sort("batch_no", 1)
    async for b in bcur:
        batches.append({
            "batch_no":     b.get("batch_no"),
            "accepted":     b.get("accepted"),
            "rejected":     b.get("rejected"),
            "doc_count":    b.get("doc_count"),
            "content_hash": b.get("content_hash"),
            "recorded_at":  str(b.get("recorded_at")),
        })

    # Audit log aggregates
    sum_upserted = sum_matched = n_batch_accepted = 0
    n_bulk_error = n_altered_replay = n_replay_idempotent = 0
    recent_errors: list[dict] = []
    acur = canon_db[AUDIT_COLLECTION].find(
        {"meta.session_id": session_id, "meta.collection": collection}
    )
    async for a in acur:
        event = a.get("event")
        meta = a.get("meta", {}) or {}
        if event == "batch_accepted":
            n_batch_accepted += 1
            sum_upserted += int(meta.get("upserted") or 0)
            sum_matched  += int(meta.get("matched")  or 0)
        elif event == "bulk_write_error":
            n_bulk_error += 1
            if len(recent_errors) < 5:
                recent_errors.append({
                    "batch_no": meta.get("batch_no"),
                    "details":  str(meta.get("details"))[:400],
                })
        elif event == "altered_replay_rejected":
            n_altered_replay += 1
        elif event == "batch_replay_idempotent":
            n_replay_idempotent += 1

    # Current canonical collection count
    try:
        canonical_count = await canon_db[collection].count_documents({})
    except Exception:
        canonical_count = -1

    # Logical-key distribution sanity: unique keys among the first 50 K docs
    unique_lk_sample = 0
    is_active_true = 0
    is_active_false = 0
    is_active_null = 0
    missing_lk = 0
    lk_fields = list(logical_key_fields(collection))
    try:
        projection = {f: 1 for f in lk_fields}
        projection["_id"] = 0
        projection["is_active"] = 1
        seen = set()
        async for d in canon_db[collection].find({}, projection).limit(500_000):
            lk = tuple(d.get(f) for f in lk_fields)
            if any(v is None for v in lk):
                missing_lk += 1
            else:
                seen.add(lk)
            a = d.get("is_active")
            if a is True: is_active_true += 1
            elif a is False: is_active_false += 1
            else: is_active_null += 1
        unique_lk_sample = len(seen)
    except Exception:
        pass

    return {
        "session_id":           session_id,
        "collection":           collection,
        "logical_key_fields":   lk_fields,
        "batch_count":          len(batches),
        "sum_accepted":         sum(b["accepted"] or 0 for b in batches),
        "sum_rejected":         sum(b["rejected"] or 0 for b in batches),
        "sum_doc_count":        sum(b["doc_count"] or 0 for b in batches),
        "audit": {
            "n_batch_accepted_events":    n_batch_accepted,
            "n_batch_replay_idempotent":  n_replay_idempotent,
            "n_bulk_write_error_events":  n_bulk_error,
            "n_altered_replay_rejected":  n_altered_replay,
            "sum_upserted_count":         sum_upserted,
            "sum_matched_count":          sum_matched,
            "recent_bulk_write_errors":   recent_errors,
        },
        "canonical_now": {
            "count_total":                canonical_count,
            "unique_logical_key_sampled": unique_lk_sample,
            "sample_missing_logical_key": missing_lk,
            "is_active_true":             is_active_true,
            "is_active_false":            is_active_false,
            "is_active_null_or_absent":   is_active_null,
        },
        "first_10_batches":  batches[:10],
        "last_10_batches":   batches[-10:],
    }



# ─── Scoped duplicate-key check (surgical replacement for the broad
# certification scan when applying indexes is the goal) ──────────────
#
# Context (2026-10-06): the broad GET /canonical-cutover/certification
# iterates all 21 RECONCILIATION_COLLECTIONS, each requiring a full-
# collection $group aggregation (no unique index exists yet on the
# three target collections, so this is a COLLSCAN per collection).
# On a hot Production dataset this exceeds the 85 s middleware
# timeout and returns 504, blocking the index-repair workflow BEFORE
# it can even check the three target collections.
#
# This endpoint computes the duplicate-key count for ONE collection
# at a time, bounded by the collection's logical_key_fields.  Paired
# with the 300 s route-timeout override installed in
# backend/middleware/resilience.py, this allows the index-repair
# workflow to run three cheap, independent checks instead of one
# all-collections scan.  No index creation here — read-only.
@router.get("/canonical-cutover/dup-check")
async def canonical_dup_check(
    admin: Annotated[UserPublic, Depends(_require_admin)],
    collection: str,
):
    """READ-ONLY duplicate-logical-key count for a single canonical
    collection.  Takes ``collection`` from the request allowlist
    (RECONCILIATION_COLLECTIONS).  Returns:

        {
          "collection":          "<name>",
          "logical_key_fields":  [...],
          "canonical_count":     <int>,
          "duplicate_logical_identities": <int>,
          "no_duplicates":       <bool>,
          "elapsed_ms":          <int>
        }

    Never writes.  Never activates.  Never touches other collections.
    """
    if collection not in RECONCILIATION_COLLECTIONS:
        raise HTTPException(status_code=400,
                            detail=f"collection '{collection}' not in RECONCILIATION_COLLECTIONS")

    from services.canonical_cutover import logical_key_fields as _lkf

    canon_db = get_canonical_database()
    key_fields = list(_lkf(collection))
    if not key_fields:
        raise HTTPException(status_code=400,
                            detail=f"collection '{collection}' has no logical_key_fields")

    _t0 = time.perf_counter()

    try:
        canonical_count = await canon_db[collection].count_documents({})
    except Exception as e:
        raise HTTPException(status_code=500,
                            detail=f"count_documents failed: {type(e).__name__}: {str(e)[:200]}")

    # Aggregate duplicate groups.  allowDiskUse keeps the server
    # resident set bounded for large collections.
    pipeline = [
        {"$group": {"_id": {f: f"${f}" for f in key_fields},
                     "n":   {"$sum": 1}}},
        {"$match": {"n": {"$gt": 1}}},
        {"$count": "dup_count"},
    ]
    dup_count = 0
    try:
        cur = canon_db[collection].aggregate(pipeline, allowDiskUse=True)
        async for d in cur:
            dup_count = int(d.get("dup_count", 0) or 0)
    except Exception as e:
        raise HTTPException(status_code=500,
                            detail=f"dup aggregation failed: {type(e).__name__}: {str(e)[:200]}")

    elapsed_ms = int((time.perf_counter() - _t0) * 1000.0)
    return {
        "collection":                   collection,
        "logical_key_fields":           key_fields,
        "canonical_count":              canonical_count,
        "duplicate_logical_identities": dup_count,
        "no_duplicates":                dup_count == 0,
        "elapsed_ms":                   elapsed_ms,
    }


@router.post("/canonical-cutover/create-indexes")
async def canonical_create_indexes(
    req: CreateIndexesRequest,
    admin: Annotated[UserPublic, Depends(_require_admin)],
    x_canonical_import_token: Annotated[Optional[str], Header()] = None,
):
    if not _import_enabled():
        raise HTTPException(status_code=403, detail="CANONICAL_IMPORT_ENABLED=false")
    _verify_import_token(x_canonical_import_token)
    canon_db = get_canonical_database()
    results = []
    for coll in req.collections:
        if coll not in RECONCILIATION_COLLECTIONS:
            results.append({"collection": coll, "ok": False,
                             "error": "NOT_IN_ALLOWLIST"})
            continue
        results.append(await ensure_canonical_indexes(canon_db, coll))
    return {"results": results,
            "all_ok": all(r.get("ok", False) for r in results)}


# ─── Temporary NON-UNIQUE performance indexes (2026-10-06 directive) ─
#
# While Perklocks R3 is still migrating forward and duplicate logical
# keys exist on three collections (49 + 66 + 9 groups), we cannot
# force the final unique indexes without data loss / rejection.  But
# every write to these three collections is COLLSCANning the lookup
# path which has blown out the request budget and is now producing
# more failures than successes.
#
# This endpoint creates the SAME key patterns the final unique
# indexes will use, but with:
#   * ``unique=False``
#   * distinct index names (ix_*_r3) so they do NOT collide with the
#     intended unique indexes (ux_*) in services.canonical_cutover._INDEX_SPECS
#   * create_indexes([IndexModel(...)]) — the authoritative MongoDB
#     path.  Hybrid builds; writes continue during build.
#
# Hard-coded index list — no caller-controlled schema.  Admin AND
# X-Canonical-Import-Token gated identically to create-indexes.
# Idempotent: if the ix_*_r3 index already exists with the same spec,
# Mongo returns success without rebuilding.
_PERFORMANCE_INDEXES_R3: list[dict[str, Any]] = [
    {
        "collection": "prediction_snapshots",
        "name":       "ix_prediction_snapshot_version_r3",
        "keys":       [("prediction_id", 1), ("snapshot_version", 1)],
    },
    {
        "collection": "publication_events",
        "name":       "ix_payload_hash_r3",
        "keys":       [("payload_hash", 1)],
    },
    {
        "collection": "settlement_events",
        "name":       "ix_settlement_id_r3",
        "keys":       [("settlement_id", 1)],
    },
    # ── R3 Resume #16 — Support-confirmed import-lookup indexes ──
    # These three collections were observed by Emergent Support to
    # have only ``_id`` indexed in Production and the canonical
    # import's bulk_write upsert ops filter by the collection's
    # logical-key fields (services.canonical_cutover.import_batch
    # line 536-546: ``key = {f: doc.get(f) for f in key_fields}``).
    # Without these indexes, every upsert does a COLLSCAN on
    # 13K-306K-doc collections, which is why 136 picks timeouts
    # happened at Prod scale.
    #
    # Lookup filter traced from services/canonical_cutover.py:
    #   canonical_picks:
    #       filter = {"id": doc.get("id")}
    #       logical_key_fields("picks") = ("id",)
    #   canonical_player_identities:
    #       filter = {"canonical_player_id": doc.get("canonical_player_id")}
    #       logical_key_fields("player_identities") = ("canonical_player_id",)
    #   canonical_pregame_snapshots:
    #       filter = {"snapshot_hash": doc.get("snapshot_hash")}
    #       logical_key_fields("pregame_snapshots") = ("snapshot_hash",)
    #
    # Indexes are NON-UNIQUE (per user directive — uniqueness is
    # enforced in a later phase after duplicate reconciliation).
    {
        "collection": "picks",
        "name":       "ix_picks_id_r3",
        "keys":       [("id", 1)],
    },
    {
        "collection": "player_identities",
        "name":       "ix_player_identities_canonical_player_id_r3",
        "keys":       [("canonical_player_id", 1)],
    },
    {
        "collection": "pregame_snapshots",
        "name":       "ix_pregame_snapshots_snapshot_hash_r3",
        "keys":       [("snapshot_hash", 1)],
    },
]


@router.post("/canonical-cutover/create-performance-indexes")
async def canonical_create_performance_indexes(
    admin: Annotated[UserPublic, Depends(_require_admin)],
    x_canonical_import_token: Annotated[Optional[str], Header()] = None,
):
    """Create the three NON-UNIQUE performance indexes on the exact
    same key patterns the final unique indexes will use, with
    distinct ``ix_*_r3`` names so they don't collide with the
    intended ``ux_*`` unique indexes.  Temporary — Phase-8 cert
    will replace them once duplicates are cleaned.

    Response:
        {
          "results": [
            { "collection":   "<name>",
              "index_name":   "ix_*_r3",
              "key_pattern":  [["field", 1], ...],
              "unique":       false,
              "action":       "created" | "existed",
              "live_ready":   true/false,
              "elapsed_ms":   <int>
            }, ...
          ],
          "all_ok": <bool>,
          "note":   "TEMPORARY non-unique performance indexes — unique indexes will replace them in Phase 8 after duplicates are reconciled."
        }
    """
    if not _import_enabled():
        raise HTTPException(status_code=403, detail="CANONICAL_IMPORT_ENABLED=false")
    _verify_import_token(x_canonical_import_token)

    from pymongo import IndexModel  # local import; pymongo already pulled by motor

    canon_db = get_canonical_database()
    results: list[dict] = []
    for spec in _PERFORMANCE_INDEXES_R3:
        coll = spec["collection"]
        name = spec["name"]
        keys = spec["keys"]
        _t0 = time.perf_counter()

        # 1. List current indexes to decide create-vs-existed cleanly.
        existing_names: set[str] = set()
        existing_matching_name: Optional[dict] = None
        try:
            async for idx in canon_db[coll].list_indexes():
                nm = idx.get("name") or ""
                existing_names.add(nm)
                if nm == name:
                    existing_matching_name = idx
        except Exception as e:
            results.append({
                "collection": coll, "index_name": name,
                "key_pattern": [[k, v] for k, v in keys],
                "unique": False, "action": "error",
                "error": f"list_indexes failed: {type(e).__name__}: {str(e)[:200]}",
                "live_ready": False,
                "elapsed_ms": int((time.perf_counter() - _t0) * 1000.0),
            })
            continue

        # 2. If an index with the same name already exists, verify its
        #    key pattern + unique flag match.  Different spec with same
        #    name → fail loudly rather than silently diverging.
        if existing_matching_name is not None:
            got_key = [[k, int(v)] for k, v in existing_matching_name.get("key", {}).items()]
            want_key = [[k, int(v)] for k, v in keys]
            got_unique = bool(existing_matching_name.get("unique", False))
            if got_key == want_key and got_unique is False:
                results.append({
                    "collection": coll, "index_name": name,
                    "key_pattern": want_key, "unique": False,
                    "action": "existed", "live_ready": True,
                    "elapsed_ms": int((time.perf_counter() - _t0) * 1000.0),
                })
                continue
            # Same name, different spec — surface the conflict.
            results.append({
                "collection": coll, "index_name": name,
                "key_pattern": want_key, "unique": False,
                "action": "error",
                "error": (f"existing index '{name}' has conflicting spec "
                           f"keys={got_key} unique={got_unique}; refusing "
                           f"to drop/rebuild automatically"),
                "live_ready": False,
                "elapsed_ms": int((time.perf_counter() - _t0) * 1000.0),
            })
            continue

        # 3. Create.  pymongo's create_indexes([IndexModel]) ack returns
        #    only after the index is built (hybrid build on primary;
        #    writes continue during build).
        try:
            await canon_db[coll].create_indexes([
                IndexModel(keys, name=name, unique=False)
            ])
            action = "created"
            live = True
        except Exception as e:
            results.append({
                "collection": coll, "index_name": name,
                "key_pattern": [[k, v] for k, v in keys],
                "unique": False, "action": "error",
                "error": f"create_indexes failed: {type(e).__name__}: {str(e)[:200]}",
                "live_ready": False,
                "elapsed_ms": int((time.perf_counter() - _t0) * 1000.0),
            })
            continue

        results.append({
            "collection": coll, "index_name": name,
            "key_pattern": [[k, v] for k, v in keys],
            "unique": False, "action": action,
            "live_ready": live,
            "elapsed_ms": int((time.perf_counter() - _t0) * 1000.0),
        })

    all_ok = all(r.get("live_ready") for r in results)
    return {
        "results": results,
        "all_ok":  all_ok,
        "note": ("TEMPORARY non-unique performance indexes — the final "
                  "unique indexes (ux_*) will replace them in Phase 8 "
                  "after duplicate logical keys are reconciled."),
    }


@router.get("/canonical-cutover/explain-logical-key-lookup")
async def canonical_explain_logical_key_lookup(
    admin: Annotated[UserPublic, Depends(_require_admin)],
    collection: str,
):
    """READ-ONLY ``explain()`` of a find() keyed by the collection's
    canonical logical key.  Used to prove that upsert lookups now
    use an IXSCAN rather than a COLLSCAN after the performance
    indexes are live.  No writes.  No mutation.

    Returns:
        {
          "collection":          <str>,
          "logical_key_fields":  [...],
          "winning_plan_stage":  "IXSCAN" | "COLLSCAN" | "FETCH" | ...
          "index_name_used":     "<index>" | null,
          "uses_index":          <bool>,
          "explain_raw":         <trimmed winning plan dict>,
        }
    """
    if collection not in RECONCILIATION_COLLECTIONS:
        raise HTTPException(status_code=400,
                            detail=f"collection '{collection}' not in RECONCILIATION_COLLECTIONS")

    from services.canonical_cutover import logical_key_fields as _lkf

    canon_db = get_canonical_database()
    key_fields = list(_lkf(collection))
    if not key_fields:
        raise HTTPException(status_code=400,
                            detail=f"collection '{collection}' has no logical_key_fields")

    # Query shape mirrors how the import batch upsert actually looks
    # up docs: equality on every logical-key field.  A string sentinel
    # value is irrelevant for the planner — explain() returns the plan
    # chosen for the query shape, not for the specific value.
    query = {f: "__explain_probe__" for f in key_fields}

    # ── R3 Resume #17 — resolve physical collection name via the
    # canonical-DB proxy BEFORE issuing the raw ``explain`` command.
    #
    # Why: in canonical-fallback mode (CANONICAL_FALLBACK_MODE=true,
    # active in Production), ``canon_db[coll]`` goes through
    # ``_PrefixedDatabase.__getitem__`` which rewrites ``pregame_snapshots``
    # → ``canonical_pregame_snapshots`` (the real physical collection in
    # the legacy DB).  BUT ``canon_db.command(...)`` is a pass-through
    # attribute (see services/database.py `_PrefixedDatabase.__getattr__`
    # whitelist including "command") — it hits the underlying DB
    # with the literal name string and does NOT get rewritten.
    #
    # Without this fix the explain probe for ``pregame_snapshots``
    # queried the LEGACY unprefixed collection (empty or without the
    # new r3 index) and reported COLLSCAN even though
    # ``ix_pregame_snapshots_snapshot_hash_r3`` was correctly built on
    # ``canonical_pregame_snapshots`` by create-performance-indexes.
    # For ``picks`` / ``player_identities``, legacy pre-canonical copies
    # existed with same-named incidental indexes (``id_1``,
    # ``canonical_player_id_uniq``), which falsely masked the bug.
    #
    # Motor Collection objects expose ``.name`` as the physical name;
    # in fallback mode that returns ``canonical_<coll>``, in normal
    # mode it returns ``<coll>`` unchanged — same code path, both
    # correct.
    physical_name = getattr(canon_db[collection], "name", collection)

    def _walk(node, out):
        """Collect every stage name + the first index name seen."""
        if not isinstance(node, dict):
            return
        s = node.get("stage")
        if s:
            out["stages"].append(s)
        if node.get("indexName") and not out.get("index_name"):
            out["index_name"] = node["indexName"]
        for v in node.values():
            if isinstance(v, dict):
                _walk(v, out)
            elif isinstance(v, list):
                for it in v:
                    if isinstance(it, dict):
                        _walk(it, out)

    try:
        plan = await canon_db.command({
            "explain": {
                "find":   physical_name,
                "filter": query,
                "limit":  1,
            },
            "verbosity": "queryPlanner",
        })
    except Exception as e:
        raise HTTPException(status_code=500,
                            detail=f"explain failed: {type(e).__name__}: {str(e)[:200]}")

    walked = {"stages": [], "index_name": None}
    winning = (plan.get("queryPlanner") or {}).get("winningPlan") or {}
    _walk(winning, walked)
    winning_stage = walked["stages"][0] if walked["stages"] else "UNKNOWN"
    # ── R3 Resume #17 — accept both standard IXSCAN and modern
    # EXPRESS_IXSCAN (and any future ``*IXSCAN`` planner variant) as
    # indexed execution.  COLLSCAN explicitly fails this check because
    # it does not end in ``IXSCAN``.  We deliberately do NOT trust
    # ``index_name_used`` alone — the stage name is the authoritative
    # proof of indexed execution; the index name is reported for
    # operator visibility only.
    uses_index = any(
        isinstance(s, str) and s.endswith("IXSCAN")
        for s in walked["stages"]
    )

    return {
        "collection":         collection,
        "physical_collection": physical_name,
        "logical_key_fields": key_fields,
        "winning_plan_stage": winning_stage,
        "all_stages":         walked["stages"],
        "index_name_used":    walked["index_name"],
        "uses_index":         uses_index,
        "explain_raw":        winning,
    }


# ─── Phase-5-R3: scoped canonical-dataset reset ─────────────────────
@router.post("/canonical-cutover/reset-canonical-dataset")
async def canonical_reset_canonical_dataset(
    req: ResetCanonicalDatasetRequest,
    admin: Annotated[UserPublic, Depends(_require_admin)],
    x_canonical_import_token: Annotated[Optional[str], Header()] = None,
):
    """Drops ONLY the canonical dataset for the 21 reconciled collections
    (plus the specified session bookkeeping records).  REFUSES to touch:
      * the legacy database
      * any collection outside RECONCILIATION_COLLECTIONS
      * other sessions' bookkeeping records
      * users / live legacy authority

    In fallback mode the canonical database proxy maps
    ``collection → canonical_<collection>`` automatically; we verify
    the physical name starts with ``canonical_`` before invoking
    ``drop_collection``.  Fail-closed on any unexpected physical name.
    """
    if not _import_enabled():
        raise HTTPException(status_code=403, detail="CANONICAL_IMPORT_ENABLED=false")
    _verify_import_token(x_canonical_import_token)
    if not req.confirm:
        raise HTTPException(status_code=400, detail="confirm=true required")
    if req.i_understand_this_drops_canonical != "YES-DROP-CANONICAL-DATASET":
        raise HTTPException(status_code=400,
                            detail="i_understand_this_drops_canonical must equal 'YES-DROP-CANONICAL-DATASET'")
    if not req.only_canonical_prefix_collections_allowed:
        raise HTTPException(status_code=400,
                            detail="only_canonical_prefix_collections_allowed must be true")

    canon_db = get_canonical_database()
    fallback = canonical_fallback_enabled()
    results = []
    if req.drop_canonical_collections:
        for coll in RECONCILIATION_COLLECTIONS:
            physical_name = getattr(canon_db[coll], "name", coll)
            if fallback and not physical_name.startswith("canonical_"):
                results.append({"collection": coll,
                                 "physical": physical_name,
                                 "dropped":  False,
                                 "error":    "REFUSED_NON_CANONICAL_PREFIX_IN_FALLBACK"})
                continue
            try:
                n_before = await canon_db[coll].count_documents({})
                await canon_db[coll].drop()
                n_after  = await canon_db[coll].count_documents({})
                results.append({"collection": coll,
                                 "physical":   physical_name,
                                 "n_before":   n_before,
                                 "n_after":    n_after,
                                 "dropped":    (n_after == 0)})
            except Exception as e:   # noqa: BLE001
                results.append({"collection": coll,
                                 "physical":   physical_name,
                                 "dropped":    False,
                                 "error":      f"{type(e).__name__}: {str(e)[:200]}"})
    else:
        # Soft reset — bookkeeping only, canonical data preserved.
        for coll in RECONCILIATION_COLLECTIONS:
            physical_name = getattr(canon_db[coll], "name", coll)
            results.append({"collection": coll,
                             "physical":   physical_name,
                             "dropped":    False,
                             "preserved":  True})

    # Session-scoped bookkeeping cleanup (optional).  NEVER wipes
    # bookkeeping for OTHER sessions — R1/R2 records are preserved.
    bookkeeping_cleared = {"batches_deleted": 0, "sessions_deleted": 0}
    if req.session_id_to_clear_bookkeeping_for:
        sid = req.session_id_to_clear_bookkeeping_for
        if len(sid) < 8 or len(sid) > 128:
            raise HTTPException(status_code=400, detail="session_id_to_clear invalid length")
        bres = await canon_db[BATCH_COLLECTION].delete_many({"session_id": sid})
        sres = await canon_db[SESSION_COLLECTION].delete_many({"session_id": sid})
        bookkeeping_cleared["batches_deleted"]  = bres.deleted_count
        bookkeeping_cleared["sessions_deleted"] = sres.deleted_count

    await audit_log_local("canonical_dataset_reset", {
        "admin_id":   admin.id,
        "fallback":   fallback,
        "results":    results,
        "bookkeeping_cleared": bookkeeping_cleared,
    })
    return {
        "fallback_mode":      fallback,
        "soft_reset":         not req.drop_canonical_collections,
        "results":            results,
        "all_dropped":        (req.drop_canonical_collections
                                 and all(r.get("dropped") for r in results)),
        "bookkeeping_cleared": bookkeeping_cleared,
    }


# ─── Phase-5-R3: completeness certification ─────────────────────────
@router.post("/canonical-cutover/r3-certification")
async def canonical_r3_completeness_certification(
    req: R3CertificationRequest,
    admin: Annotated[UserPublic, Depends(_require_admin)],
    x_canonical_import_token: Annotated[Optional[str], Header()] = None,
):
    """R3 PHASE-8 COMPLETENESS CERTIFICATION.

    Previous Phase-8 passed on logical-identity checks alone — but
    silently allowed massive row loss.  This endpoint rejects any
    unexplained row-count delta vs the client-supplied certified
    source counts.
    """
    if not _import_enabled():
        raise HTTPException(status_code=403, detail="CANONICAL_IMPORT_ENABLED=false")
    _verify_import_token(x_canonical_import_token)
    canon_db = get_canonical_database()

    # Session batch state
    agg: dict[str, dict] = {}
    async for b in canon_db[BATCH_COLLECTION].find({"session_id": req.session_id}):
        coll = b.get("collection")
        a = agg.setdefault(coll, {
            "batches": 0, "succeeded": 0, "failed": 0, "in_progress": 0,
            "upserted": 0, "matched": 0, "accepted": 0, "rejected": 0,
        })
        a["batches"] += 1
        s = b.get("status") or "unknown"
        if s == "succeeded":   a["succeeded"] += 1
        elif s == "failed":    a["failed"] += 1
        elif s == "in_progress": a["in_progress"] += 1
        elif s == "incomplete_write": a["failed"] += 1
        a["upserted"] += int(b.get("upserted_count") or 0)
        a["matched"]  += int(b.get("matched_count")  or 0)
        a["accepted"] += int(b.get("accepted_count") or b.get("accepted") or 0)
        a["rejected"] += int(b.get("rejected_count") or b.get("rejected") or 0)

    results: list[dict] = []
    overall_pass = True
    for coll in RECONCILIATION_COLLECTIONS:
        exp = req.expected.get(coll, {})
        expected_source_rows        = int(exp.get("source_rows") or 0)
        unique_source_lk            = int(exp.get("unique_logical_identities") or 0)
        excluded_rows               = int(exp.get("excluded_rows") or 0)
        quarantined_rows            = int(exp.get("quarantined_rows") or 0)
        null_lk_rows                = int(exp.get("null_logical_key_rows") or 0)
        expected_canonical_rows     = int(exp.get("expected_canonical_rows")
                                            or max(0, unique_source_lk - excluded_rows - quarantined_rows))

        # Canonical observed
        canonical_rows = await canon_db[coll].count_documents({})
        # Count unique logical identities actually present
        from services.canonical_cutover import logical_key_fields as _lkf
        lk = list(_lkf(coll))
        pipeline = [
            {"$group": {"_id": {f: f"${f}" for f in lk}}},
            {"$count": "n"},
        ]
        unique_canonical_lk = 0
        try:
            doc = await canon_db[coll].aggregate(pipeline).to_list(1)
            if doc: unique_canonical_lk = int(doc[0].get("n") or 0)
        except Exception as e:   # noqa: BLE001
            unique_canonical_lk = -1
        duplicate_canonical_ids = (canonical_rows - unique_canonical_lk) if unique_canonical_lk >= 0 else -1

        # Collection fingerprint (identity-hash)
        try:
            fp = await collection_fingerprint(canon_db, coll)
        except Exception:
            fp = None

        batches_info = agg.get(coll, {})
        diff          = canonical_rows - expected_canonical_rows
        diff_pct      = (abs(diff) / expected_canonical_rows * 100.0
                           if expected_canonical_rows else 0.0)
        row_ok        = abs(diff) <= req.tolerance_rows
        lk_ok         = (unique_canonical_lk == expected_canonical_rows)
        no_dups       = (duplicate_canonical_ids == 0)
        batches_ok    = batches_info.get("failed", 0) == 0 and batches_info.get("in_progress", 0) == 0
        exc_ok        = True  # The exclusion filter is server-enforced; see audit log.
        status_       = ("PASS" if (row_ok and lk_ok and no_dups and batches_ok) else "FAIL")
        if status_ == "FAIL": overall_pass = False
        results.append({
            "collection":                    coll,
            "certified_source_rows":         expected_source_rows,
            "unique_source_logical_ids":     unique_source_lk,
            "excluded_rows":                 excluded_rows,
            "quarantined_rows":              quarantined_rows,
            "null_logical_key_rows":         null_lk_rows,
            "expected_canonical_rows":       expected_canonical_rows,
            "canonical_rows":                canonical_rows,
            "unique_canonical_logical_ids":  unique_canonical_lk,
            "duplicate_canonical_ids":       duplicate_canonical_ids,
            "difference":                    diff,
            "difference_percent":            round(diff_pct, 3),
            "canonical_fingerprint":         fp,
            "batches":                       batches_info,
            "row_count_match":               row_ok,
            "logical_id_match":              lk_ok,
            "no_duplicates":                 no_dups,
            "all_batches_succeeded":         batches_ok,
            "status":                        status_,
        })

    # Specific settlement_events assertions required by R3 contract
    settlement = next((r for r in results if r["collection"] == "settlement_events"), None)
    settlement_contract = None
    if settlement:
        settlement_contract = {
            "source_rows":                settlement["certified_source_rows"],
            "canonical_rows":             settlement["canonical_rows"],
            "source_unique_settlement_id": settlement["unique_source_logical_ids"],
            "canonical_unique_settlement_id": settlement["unique_canonical_logical_ids"],
            "difference_matches_expected_exclusions_only":
                (settlement["canonical_rows"] ==
                 (settlement["unique_source_logical_ids"]
                   - settlement["excluded_rows"]
                   - settlement["quarantined_rows"]
                   - settlement["null_logical_key_rows"])),
            "no_legitimate_ledger_rows_lost":   settlement["row_count_match"],
            "no_duplicate_settlement_id_rows":  settlement["no_duplicates"],
        }

    return {
        "session_id":           req.session_id,
        "overall_pass":         overall_pass,
        "collections":          results,
        "settlement_contract":  settlement_contract,
    }


@router.post("/canonical-cutover/copy-legacy-collection")
async def canonical_copy_legacy(
    req: CopyLegacyRequest,
    admin: Annotated[UserPublic, Depends(_require_admin)],
    x_canonical_import_token: Annotated[Optional[str], Header()] = None,
):
    if not _import_enabled():
        raise HTTPException(status_code=403, detail="CANONICAL_IMPORT_ENABLED=false")
    _verify_import_token(x_canonical_import_token)
    if not req.confirm:
        raise HTTPException(status_code=400,
                             detail="set confirm=true to execute copy")
    legacy_db = get_legacy_database()
    canon_db  = get_canonical_database()
    try:
        return await copy_legacy_collection(legacy_db, canon_db, req.collection)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/data-authority/status")
async def data_authority_status(
    admin: Annotated[UserPublic, Depends(_require_admin)],
):
    """Safe routing + env-flag diagnostics for both DBs.  No URIs/secrets."""
    diag = safe_database_diagnostics()

    legacy_db = get_legacy_database()
    canon_db  = get_canonical_database()

    async def _summarize(db) -> dict:
        out: dict = {"collections": {}}
        for coll in RECONCILIATION_COLLECTIONS:
            try:
                out["collections"][coll] = await db[coll].count_documents({})
            except Exception:
                out["collections"][coll] = -1
        return out

    legacy_summary    = await _summarize(legacy_db)
    canonical_summary = await _summarize(canon_db)

    # Lightweight canonical fingerprint for a stable identifier (count-based)
    def _fp(summary: dict) -> str:
        import hashlib, json
        return hashlib.sha256(json.dumps(summary, sort_keys=True).encode()).hexdigest()[:16]

    # Build / commit identifier so the post-Publish benchmark workflow
    # can prove it's talking to the newly-published code (Emergent
    # Support 2026-10-05 event-loop-starvation fix).  Resolution order:
    #   1. BUILD_SHA env var (set by the deployment pipeline if any)
    #   2. /app/BUILD_SHA file (opt-in operator-written marker)
    #   3. /app/.git/refs/heads/main (if the git dir survives Publish)
    #   4. "unknown"
    # Never returns secret material.  Short SHA only (12 chars) and the
    # 40-char full to let the client decide.
    def _build_sha() -> dict:
        import pathlib
        sha = (os.environ.get("BUILD_SHA") or "").strip()
        if not sha:
            try:
                p = pathlib.Path("/app/BUILD_SHA")
                if p.exists():
                    sha = p.read_text().strip()
            except Exception:
                pass
        if not sha:
            try:
                p = pathlib.Path("/app/.git/refs/heads/main")
                if p.exists():
                    sha = p.read_text().strip()
            except Exception:
                pass
        if not sha:
            return {"full": "unknown", "short": "unknown", "source": "none"}
        source = ("env" if os.environ.get("BUILD_SHA") else
                   "file" if pathlib.Path("/app/BUILD_SHA").exists() else
                   "git")
        return {"full": sha, "short": sha[:12], "source": source}

    return {
        "routing":             diag,
        "env_flags": {
            "USE_CANONICAL_DB":            use_canonical_db_enabled(),
            "CANONICAL_IMPORT_ENABLED":    _import_enabled(),
            "CANONICAL_FALLBACK_MODE":     canonical_fallback_enabled(),
            "BACKGROUND_WORKERS_ENABLED":  (os.environ.get("BACKGROUND_WORKERS_ENABLED") or "false").strip().lower() == "true",
            "DATA_AUTHORITY":              os.environ.get("DATA_AUTHORITY") or "",
            "CANONICAL_WRITE_ENABLED":     (os.environ.get("CANONICAL_WRITE_ENABLED") or "false").strip().lower() == "true",
        },
        "build":               _build_sha(),
        "legacy_summary":      legacy_summary,
        "canonical_summary":   canonical_summary,
        "legacy_fingerprint":  _fp(legacy_summary),
        "canonical_fingerprint": _fp(canonical_summary),
        "active_db_name":      active_database_name(),
    }


@router.get("/canonical-cutover/certification")
async def canonical_cutover_certification(
    admin: Annotated[UserPublic, Depends(_require_admin)],
):
    """Compute full certification snapshot — per-collection count,
    fingerprint, and duplicate-key check."""
    canon_db = get_canonical_database()
    results: list[dict] = []
    import_disabled = not _import_enabled()
    duplicate_identities_total = 0

    for coll in RECONCILIATION_COLLECTIONS:
        try:
            count = await canon_db[coll].count_documents({})
            # Count duplicates by logical key
            key_fields = logical_key_fields(coll)
            agg = canon_db[coll].aggregate([
                {"$group": {"_id": {f: f"${f}" for f in key_fields},
                             "n":   {"$sum": 1}}},
                {"$match": {"n": {"$gt": 1}}},
                {"$count": "dup_count"},
            ])
            dup_count = 0
            async for d in agg:
                dup_count = d.get("dup_count", 0)
            duplicate_identities_total += dup_count
            # Check for excluded/quarantined that leaked through
            excluded_leak = await canon_db[coll].count_documents(
                {"$or": [{"excluded_from_canonical_runtime": True},
                          {"status": "UNRESOLVED_IMMUTABLE_CONFLICT"}]})
            fp = await collection_fingerprint(canon_db, coll)
            results.append({
                "collection":          coll,
                "count":               count,
                "logical_key_fields":  list(key_fields),
                "fingerprint":         fp,
                "duplicate_logical_identities": dup_count,
                "excluded_or_quarantined_leaked": excluded_leak,
            })
        except Exception as e:
            results.append({"collection": coll, "error": str(e)[:300]})

    total_excluded_leaked = sum(r.get("excluded_or_quarantined_leaked", 0) for r in results)
    return {
        "certified_at":                     datetime.now(timezone.utc).isoformat(),
        "collections":                      results,
        "duplicate_logical_identities_total": duplicate_identities_total,
        "excluded_or_quarantined_leaked_total": total_excluded_leaked,
        "import_endpoint_disabled":         import_disabled,
        "overall_pass": (duplicate_identities_total == 0 and
                           total_excluded_leaked == 0),
    }


# ─── R3 Phase-7 duplicate reconciliation ────────────────────────────
# Two endpoints driven by the perklocks-r3-phase7-dedupe-finalize
# workflow:
#
#   GET  /canonical-cutover/dup-census
#     Read-only.  Enumerates every duplicate logical-key group across
#     all 21 canonical collections with per-doc _ids + fingerprints +
#     EXACT/CONFLICTING classification.  Produces the authoritative
#     evidence an operator inspects before authorising APPLY.
#
#   POST /canonical-cutover/apply-dedupe
#     Destructive.  Reconciles duplicate groups ONE AT A TIME against
#     a pinned-source authoritative fingerprint supplied by the
#     driver.  Hard constraints:
#       * collection must be in APPLY_DEDUPE_SCOPE (the 8 that failed
#         Phase-7 unique-index creation).
#       * confirm_phrase must equal APPLY_DEDUPE_CONFIRM_PHRASE.
#       * every mutation writes a durable audit record to
#         ``canonical_dedupe_audit`` BEFORE the delete.
#       * pre-delete guard: survivor fingerprint == source.
#       * post-delete guard: logical-key count == 1.
#       * FAIL CLOSED on any ambiguity (no "latest timestamp wins").
APPLY_DEDUPE_CONFIRM_PHRASE: str = "APPLY_PERKLOCKS_DEDUPE_R3_PHASE7_V1"


@router.get("/canonical-cutover/dup-census")
async def canonical_cutover_dup_census(
    admin:               Annotated[UserPublic, Depends(_require_admin)],
    collection:          str,
    offset:              int = 0,
    limit:               int = 100,
    max_docs_per_group:  int = 50,
):
    """R3 Phase-7 duplicate census — READ-ONLY, per-collection, paged.

    (Rewrite of the pre-#20 all-21-in-one-call census that returned
    HTTP 500 on large collections because ``$group{$push: $_id}``
    materialised a push-array for EVERY logical key before
    ``$match{count>1}`` filtered them.)

    Pagination contract:
      * ``collection`` is REQUIRED and must be in
        ``RECONCILIATION_COLLECTIONS``.
      * ``offset`` / ``limit`` page a deterministic sorted list of
        duplicate keys for the single collection.
      * Collection-level stats (``total_docs``, ``logical_key_count``,
        ``duplicate_group_count``) are returned on every page.
      * ``has_more`` + ``next_offset`` drive the driver's loop.

    Structured error reporting: on any exception the endpoint
    returns a 500 with a JSON detail
    ``{collection, stage, exception_type, error}`` so GH-runner
    operators can see WHY the scan failed rather than a generic
    "Something went wrong — please retry."
    """
    if collection not in RECONCILIATION_COLLECTIONS:
        raise HTTPException(status_code=400,
            detail=f"UNKNOWN_COLLECTION: {collection}")
    canon_db = get_canonical_database()
    import re as _re
    try:
        summary = await scan_duplicates(
            canon_db, collection,
            offset=offset, limit=limit,
            max_docs_per_group=max_docs_per_group,
        )
    except Exception as e:
        # Parse the per-stage marker if the error came through
        # scan_duplicates' structured RuntimeError.
        emsg  = str(e)
        stage = "scan_duplicates"
        m = _re.search(r"CENSUS_STAGE_FAIL\s+stage=(\S+)", emsg)
        if m:
            stage = m.group(1)
        raise HTTPException(status_code=500, detail={
            "collection":     collection,
            "offset":         offset,
            "limit":          limit,
            "stage":          stage,
            "exception_type": type(e).__name__,
            "error":          emsg[:800],
        })
    return {
        "generated_at":         datetime.now(timezone.utc).isoformat(),
        "apply_scope":          sorted(APPLY_DEDUPE_SCOPE),
        "apply_confirm_phrase": APPLY_DEDUPE_CONFIRM_PHRASE,
        **summary,
    }


class _ApplyDedupePlan(BaseModel):
    collection:                str
    logical_key_values:        dict
    classification:            str
    all_ids:                   list
    authoritative_fingerprint: str
    survivor_hint_order:       Optional[list] = None


class _ApplyDedupeBody(BaseModel):
    session_id:              str
    workflow_run_id:         str
    confirm_phrase:          str
    plans:                   list[_ApplyDedupePlan]


@router.post("/canonical-cutover/apply-dedupe")
async def canonical_cutover_apply_dedupe(
    body:                 _ApplyDedupeBody,
    admin:                Annotated[UserPublic, Depends(_require_admin)],
    x_canonical_import_token: Annotated[Optional[str],
                              Header(convert_underscores=True)] = None,
):
    """R3 Phase-7 duplicate reconciliation APPLY — destructive.

    * Admin JWT required.
    * ``CANONICAL_IMPORT_ENABLED=true`` required.
    * ``X-Canonical-Import-Token`` header required.
    * ``confirm_phrase`` must equal ``APPLY_DEDUPE_CONFIRM_PHRASE``.
    * Every plan's collection must be in ``APPLY_DEDUPE_SCOPE``.
    * Processes plans one at a time; on first exception the run
      stops with partial-progress details (all preceding plans are
      durable because audit + delete are per-group atomic).
    """
    if not _import_enabled():
        raise HTTPException(status_code=403, detail="CANONICAL_IMPORT_ENABLED=false")
    _verify_import_token(x_canonical_import_token)
    if body.confirm_phrase != APPLY_DEDUPE_CONFIRM_PHRASE:
        raise HTTPException(
            status_code=400,
            detail=f"BAD_CONFIRM_PHRASE: expected='{APPLY_DEDUPE_CONFIRM_PHRASE}'")

    out_of_scope = [p.collection for p in body.plans
                     if p.collection not in APPLY_DEDUPE_SCOPE]
    if out_of_scope:
        raise HTTPException(
            status_code=400,
            detail=f"OUT_OF_SCOPE_COLLECTIONS: {sorted(set(out_of_scope))}")

    canon_db = get_canonical_database()
    results:   list[dict] = []
    halted_at: Optional[int] = None
    halt_err:  Optional[str] = None

    for idx, plan in enumerate(body.plans):
        try:
            r = await apply_dedupe_group(
                canon_db,
                collection=plan.collection,
                logical_key_values=plan.logical_key_values,
                classification=plan.classification,
                all_ids=plan.all_ids,
                authoritative_fingerprint=plan.authoritative_fingerprint,
                workflow_run_id=body.workflow_run_id,
                session_id=body.session_id,
                survivor_hint_order=plan.survivor_hint_order,
            )
            results.append(r)
        except Exception as e:
            halted_at = idx
            halt_err  = f"{type(e).__name__}: {str(e)[:600]}"
            results.append({
                "collection":         plan.collection,
                "logical_key_values": plan.logical_key_values,
                "error":              halt_err,
                "halted":             True,
            })
            await audit_log_local("dedupe_apply_halted", {
                "workflow_run_id": body.workflow_run_id,
                "session_id":      body.session_id,
                "plan_index":      idx,
                "collection":      plan.collection,
                "logical_key_values": plan.logical_key_values,
                "error":           halt_err,
            })
            break

    await audit_log_local("dedupe_apply_summary", {
        "workflow_run_id":   body.workflow_run_id,
        "session_id":        body.session_id,
        "total_plans":       len(body.plans),
        "processed":         len(results),
        "halted_at":         halted_at,
        "halt_err":          halt_err,
    })

    return {
        "generated_at":     datetime.now(timezone.utc).isoformat(),
        "session_id":       body.session_id,
        "workflow_run_id":  body.workflow_run_id,
        "total_plans":      len(body.plans),
        "processed":        len(results),
        "halted_at":        halted_at,
        "halt_err":         halt_err,
        "results":          results,
        "ok":               halted_at is None,
    }


# ────────────────────────────────────────────────────────────────────
#   POST /canonical-cutover/patch-divergent-group
# ────────────────────────────────────────────────────────────────────
#
# TEMPORARY, HARD-CODED surgical endpoint created on 2026-10-10 to
# unblock the Phase-7 APPLY for exactly two ``player_game_logs``
# EXACT-divergence groups confirmed by Perklocks Support. Both groups
# diverge from the pinned ``v2_phase5/canonical/player_game_logs.ndjson``
# authority on a single business field (``shots``): Production stores
# ``null``/missing, the pinned authoritative source stores ``0``.
#
# This endpoint:
#   * mutates ONLY those two logical keys (hard-coded allowlist below),
#   * mutates ONLY the single field ``shots``,
#   * accepts ONLY the transition ``null|missing → 0``,
#   * requires the current duplicate count to be EXACTLY 2,
#   * requires the current ``_id`` set to equal the two canary-proven
#     ObjectId strings per group,
#   * requires EVERY doc in the group to have ``shots`` null/missing
#     before mutation,
#   * writes a ``DIVERGENCE_PATCH`` audit record BEFORE mutate and an
#     ``UPDATE_RESULT`` audit record AFTER mutate,
#   * verifies the post-patch fingerprint of EACH surviving doc
#     independently matches the pinned authoritative fingerprint,
#   * fails closed on any precondition miss with zero mutation,
#   * runs transactionally when Production Mongo supports transactions
#     (replica-set); otherwise enforces matched_count=2 /
#     modified_count=2 and immediately re-reads both exact IDs.
#
# No other logical key, field, old-value, or new-value is reachable
# through this endpoint. The allowlist is byte-literal and cannot be
# bypassed by the request body.
PATCH_DIVERGENT_CONFIRM_PHRASE: str = "PATCH_PERKLOCKS_DIVERGENT_NHL_SHOTS_V1"
_PATCH_DIVERGENT_AUDIT_EVENT:     str = "DIVERGENCE_PATCH"
_PATCH_DIVERGENT_RESULT_EVENT:    str = "UPDATE_RESULT"


# Hard-coded allowlist of the EXACTLY TWO groups eligible for patch.
# Any request whose (collection, logical_key_values) tuple is not in
# this table is refused.
_PATCH_DIVERGENT_ALLOWED_GROUPS: tuple[dict, ...] = (
    {
        "collection":                 "player_game_logs",
        "logical_key_values":         {
            "sport":     "nhl",
            "game_id":   "nhl_2025020042",
            "player_id": "nhl_8480113",
        },
        "expected_before_fingerprint": "c348e4ee99c2e5848b1ae5562483a93426e7c447b4a379167a74e981d3c160a4",
        "expected_authoritative_fingerprint": "14492740b54d0a86d682b4dc624ba08d6e23efa17dc057a9a9ff44e9c55def98",
        "expected_doc_ids":           (
            "6ac449987944462a0d2a4278",
            "6ac449987944462a0d2a4279",
        ),
    },
    {
        "collection":                 "player_game_logs",
        "logical_key_values":         {
            "sport":     "nhl",
            "game_id":   "nhl_2025020055",
            "player_id": "nhl_8480798",
        },
        "expected_before_fingerprint": "56c611c24137eeccef810887246d64c10c6ae3d29bb0da9e210cd36be2204e58",
        "expected_authoritative_fingerprint": "641515526acaf694a4e788260e1de4da5cac52d7d7e8629ecb2874c9f9318d92",
        "expected_doc_ids":           (
            "6ac449bf7944462a0d2a5328",
            "6ac449bf7944462a0d2a532d",
        ),
    },
)

# The ONLY field this endpoint will ever write.
_PATCH_DIVERGENT_FIELD:      str = "shots"
_PATCH_DIVERGENT_OLD_ALLOWED: tuple = (None,)   # also matches "missing"
_PATCH_DIVERGENT_NEW_VALUE:    int = 0


def _match_allowed_group(collection: str,
                         logical_key_values: dict) -> Optional[dict]:
    """Return the allowlist entry for the (collection, key) tuple, or
    ``None`` if the pair is not whitelisted."""
    for g in _PATCH_DIVERGENT_ALLOWED_GROUPS:
        if g["collection"] != collection:
            continue
        if g["logical_key_values"] == logical_key_values:
            return g
    return None


class _PatchDivergentBody(BaseModel):
    session_id:                 str = Field(..., min_length=8, max_length=128)
    workflow_run_id:            str = Field(..., min_length=1, max_length=128)
    confirm_phrase:             str
    collection:                 str
    logical_key_values:         dict
    field:                      str
    old_value_sentinel:         Optional[Any] = None  # must be null
    new_value:                  Any
    authoritative_fingerprint:  str


@router.post("/canonical-cutover/patch-divergent-group")
async def canonical_cutover_patch_divergent_group(
    body:                     _PatchDivergentBody,
    admin:                    Annotated[UserPublic, Depends(_require_admin)],
    x_canonical_import_token: Annotated[Optional[str],
                              Header(convert_underscores=True)] = None,
):
    """Narrow hard-coded patch endpoint — see module docstring above.

    Fail-closed contract: on ANY precondition miss (unknown group,
    wrong field, wrong old/new value, wrong duplicate count, wrong
    document ids, wrong current fingerprint, wrong post-patch
    fingerprint, missing audit write) this endpoint raises
    ``HTTPException`` and performs ZERO intentional mutation. If the
    deployment target supports transactions the entire patch is
    executed inside a transaction and aborted on any verification
    failure; otherwise the endpoint enforces strict
    ``matched_count=2`` / ``modified_count=2`` and re-reads both
    exact IDs for post-verification.
    """
    # ── Security envelope (same as apply-dedupe) ────────────────────
    if not _import_enabled():
        raise HTTPException(status_code=403, detail="CANONICAL_IMPORT_ENABLED=false")
    _verify_import_token(x_canonical_import_token)
    if body.confirm_phrase != PATCH_DIVERGENT_CONFIRM_PHRASE:
        raise HTTPException(
            status_code=400,
            detail=f"BAD_CONFIRM_PHRASE: expected='{PATCH_DIVERGENT_CONFIRM_PHRASE}'")

    # ── Hard-coded allowlist check ──────────────────────────────────
    group = _match_allowed_group(body.collection, body.logical_key_values)
    if group is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "GROUP_NOT_IN_ALLOWLIST: this endpoint only patches the "
                "two Phase-7 NHL divergence groups confirmed by Support "
                f"(got collection={body.collection!r} key={body.logical_key_values})"
            ),
        )
    if body.field != _PATCH_DIVERGENT_FIELD:
        raise HTTPException(
            status_code=400,
            detail=f"FIELD_NOT_ALLOWED: only field '{_PATCH_DIVERGENT_FIELD}' may be patched (got {body.field!r})")
    if body.old_value_sentinel not in _PATCH_DIVERGENT_OLD_ALLOWED:
        raise HTTPException(
            status_code=400,
            detail=f"OLD_VALUE_NOT_ALLOWED: only null→0 transitions are permitted (got old={body.old_value_sentinel!r})")
    if body.new_value != _PATCH_DIVERGENT_NEW_VALUE:
        raise HTTPException(
            status_code=400,
            detail=f"NEW_VALUE_NOT_ALLOWED: only {_PATCH_DIVERGENT_NEW_VALUE} is permitted (got {body.new_value!r})")
    if body.authoritative_fingerprint != group["expected_authoritative_fingerprint"]:
        raise HTTPException(
            status_code=400,
            detail=(
                "AUTHORITATIVE_FINGERPRINT_MISMATCH: "
                f"caller={body.authoritative_fingerprint[:16]}… "
                f"expected={group['expected_authoritative_fingerprint'][:16]}…"
            ),
        )

    # ── Normalize expected object ids ──────────────────────────────
    from bson import ObjectId
    try:
        expected_oids = tuple(ObjectId(h) for h in group["expected_doc_ids"])
    except Exception as e:  # pragma: no cover - defensive
        raise HTTPException(status_code=500, detail=f"INTERNAL_BAD_ALLOWLIST: {e}")

    canon_db = get_canonical_database()
    coll     = canon_db[body.collection]

    # ── Precondition 1: logical-key count == exactly 2 ──────────────
    lk_count = await coll.count_documents(body.logical_key_values)
    if lk_count != 2:
        raise HTTPException(
            status_code=409,
            detail=(
                f"LOGICAL_KEY_COUNT_UNEXPECTED: "
                f"collection={body.collection} key={body.logical_key_values} "
                f"actual_count={lk_count} expected=2"
            ),
        )

    # ── Precondition 2: _id set equals the two whitelisted _ids ─────
    docs: list[dict] = []
    async for d in coll.find(body.logical_key_values).sort("_id", 1):
        docs.append(d)
    found_oids = {d["_id"] for d in docs}
    if found_oids != set(expected_oids):
        raise HTTPException(
            status_code=409,
            detail=(
                "DOC_IDS_UNEXPECTED: "
                f"found={[str(x) for x in sorted(found_oids, key=str)]} "
                f"expected={list(group['expected_doc_ids'])}"
            ),
        )

    # ── Precondition 3: BOTH docs currently have shots null/missing ─
    for d in docs:
        current = d.get(_PATCH_DIVERGENT_FIELD, None)
        if current is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"CURRENT_VALUE_NOT_NULL: _id={str(d['_id'])} "
                    f"{_PATCH_DIVERGENT_FIELD}={current!r} (expected null/missing)"
                ),
            )

    # ── Precondition 4: before-fingerprints match the one proven by
    #     the successful CANARY_ONLY run ────────────────────────────
    from services.canonical_dedupe import canonical_doc_fingerprint
    before_fps = {str(d["_id"]): canonical_doc_fingerprint(d) for d in docs}
    for _id, fp in before_fps.items():
        if fp != group["expected_before_fingerprint"]:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"BEFORE_FINGERPRINT_MISMATCH: _id={_id} "
                    f"actual={fp[:16]}… "
                    f"expected={group['expected_before_fingerprint'][:16]}…"
                ),
            )

    # ── Pre-mutate audit (DIVERGENCE_PATCH) ─────────────────────────
    patch_audit_meta = {
        "event":                     _PATCH_DIVERGENT_AUDIT_EVENT,
        "collection":                body.collection,
        "logical_key_values":        body.logical_key_values,
        "doc_ids":                   list(group["expected_doc_ids"]),
        "field":                     body.field,
        "old_value":                 body.old_value_sentinel,
        "new_value":                 body.new_value,
        "before_fingerprint":        group["expected_before_fingerprint"],
        "expected_after_fingerprint": group["expected_authoritative_fingerprint"],
        "workflow_run_id":           body.workflow_run_id,
        "session_id":                body.session_id,
        "created_at":                datetime.now(timezone.utc),
    }
    try:
        await canon_db[DEDUPE_AUDIT_COLLECTION].insert_one(dict(patch_audit_meta))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AUDIT_WRITE_FAILED: {type(e).__name__}: {e}")

    # ── Perform the patch, transactionally if supported ─────────────
    # Transaction support requires a replica-set / sharded cluster;
    # Mongo standalone raises ``OperationFailure(code=20, …)``
    # ("Transaction numbers are only allowed on a replica set member
    # or mongos") on the first operation inside the transaction. We
    # catch that specific error and fall through to the non-tx path.
    # Any other failure during the transaction (count mismatch,
    # fingerprint mismatch) causes the tx to abort and the handler
    # to fail closed.
    update_filter = {
        "_id":            {"$in": list(expected_oids)},
        **body.logical_key_values,
        # Only update docs that are still null/missing — belt-and-braces
        # guard against a race between the preflight read and the write.
        "$or": [
            {_PATCH_DIVERGENT_FIELD: None},
            {_PATCH_DIVERGENT_FIELD: {"$exists": False}},
        ],
    }
    update_doc  = {"$set": {_PATCH_DIVERGENT_FIELD: _PATCH_DIVERGENT_NEW_VALUE}}

    from pymongo.errors import OperationFailure

    async def _patch_in_tx() -> dict:
        client = canon_db.client
        async with await client.start_session() as sess:
            async with sess.start_transaction():
                r = await coll.update_many(update_filter, update_doc, session=sess)
                if r.matched_count != 2 or r.modified_count != 2:
                    raise RuntimeError(
                        f"UPDATE_COUNTS_UNEXPECTED: "
                        f"matched={r.matched_count} modified={r.modified_count} (expected 2/2)"
                    )
                post_docs: list[dict] = []
                async for d in coll.find(
                    {"_id": {"$in": list(expected_oids)}},
                    session=sess,
                ):
                    post_docs.append(d)
                if len(post_docs) != 2:
                    raise RuntimeError(
                        f"POST_READ_COUNT_UNEXPECTED: n={len(post_docs)} expected=2"
                    )
                for d in post_docs:
                    fp = canonical_doc_fingerprint(d)
                    if fp != group["expected_authoritative_fingerprint"]:
                        raise RuntimeError(
                            f"POST_FINGERPRINT_MISMATCH: _id={str(d['_id'])} "
                            f"got={fp[:16]}… "
                            f"expected={group['expected_authoritative_fingerprint'][:16]}…"
                        )
                return {"matched_count":  r.matched_count,
                         "modified_count": r.modified_count,
                         "tx":             True}

    tx_supported   = False
    update_result  = None
    try:
        update_result = await _patch_in_tx()
        tx_supported  = True
    except OperationFailure as e:
        # Standalone Mongo cannot start a transaction. Any other
        # OperationFailure during the tx is a real failure.
        if int(getattr(e, "code", 0) or 0) == 20 or "replica set" in str(e).lower():
            tx_supported = False
        else:
            raise HTTPException(
                status_code=409,
                detail=f"TX_PATCH_FAILED: {type(e).__name__}: {e}",
            )
    except Exception as e:
        # Transaction aborted — DB is at its original state.
        raise HTTPException(
            status_code=409,
            detail=f"TX_PATCH_FAILED: {type(e).__name__}: {e}",
        )

    if not tx_supported:
        # Non-transactional path for standalone Mongo deployments.
        r = await coll.update_many(update_filter, update_doc)
        update_result = {"matched_count": r.matched_count,
                          "modified_count": r.modified_count,
                          "tx": False}
        if r.matched_count != 2 or r.modified_count != 2:
            # Audit the failure and refuse to proceed with next steps.
            await canon_db[DEDUPE_AUDIT_COLLECTION].insert_one({
                "event":              _PATCH_DIVERGENT_RESULT_EVENT,
                "collection":         body.collection,
                "logical_key_values": body.logical_key_values,
                "doc_ids":            list(group["expected_doc_ids"]),
                "field":              body.field,
                "old_value":          body.old_value_sentinel,
                "new_value":          body.new_value,
                "matched_count":      r.matched_count,
                "modified_count":     r.modified_count,
                "post_verification":  "FAILED_COUNTS",
                "workflow_run_id":    body.workflow_run_id,
                "session_id":         body.session_id,
                "created_at":         datetime.now(timezone.utc),
            })
            raise HTTPException(
                status_code=409,
                detail=(
                    f"UPDATE_COUNTS_UNEXPECTED: "
                    f"matched={r.matched_count} modified={r.modified_count} (expected 2/2)"
                ),
            )
        # Immediate post-read + per-doc fingerprint re-verification.
        post_docs = []
        async for d in coll.find({"_id": {"$in": list(expected_oids)}}):
            post_docs.append(d)
        if len(post_docs) != 2:
            await canon_db[DEDUPE_AUDIT_COLLECTION].insert_one({
                "event":              _PATCH_DIVERGENT_RESULT_EVENT,
                "collection":         body.collection,
                "logical_key_values": body.logical_key_values,
                "post_verification":  "FAILED_POST_READ",
                "post_read_count":    len(post_docs),
                "workflow_run_id":    body.workflow_run_id,
                "session_id":         body.session_id,
                "created_at":         datetime.now(timezone.utc),
            })
            raise HTTPException(
                status_code=409,
                detail=f"POST_READ_COUNT_UNEXPECTED: n={len(post_docs)} expected=2",
            )
        for d in post_docs:
            fp = canonical_doc_fingerprint(d)
            if fp != group["expected_authoritative_fingerprint"]:
                await canon_db[DEDUPE_AUDIT_COLLECTION].insert_one({
                    "event":              _PATCH_DIVERGENT_RESULT_EVENT,
                    "collection":         body.collection,
                    "logical_key_values": body.logical_key_values,
                    "post_verification":  "FAILED_FINGERPRINT",
                    "offending_id":       str(d["_id"]),
                    "post_fingerprint":   fp,
                    "expected_after_fingerprint": group["expected_authoritative_fingerprint"],
                    "workflow_run_id":    body.workflow_run_id,
                    "session_id":         body.session_id,
                    "created_at":         datetime.now(timezone.utc),
                })
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"POST_FINGERPRINT_MISMATCH: _id={str(d['_id'])} "
                        f"got={fp[:16]}… "
                        f"expected={group['expected_authoritative_fingerprint'][:16]}…"
                    ),
                )

    # ── Post-mutate audit (UPDATE_RESULT, success) ──────────────────
    await canon_db[DEDUPE_AUDIT_COLLECTION].insert_one({
        "event":                _PATCH_DIVERGENT_RESULT_EVENT,
        "collection":           body.collection,
        "logical_key_values":   body.logical_key_values,
        "doc_ids":              list(group["expected_doc_ids"]),
        "field":                body.field,
        "old_value":            body.old_value_sentinel,
        "new_value":            body.new_value,
        "matched_count":        update_result["matched_count"],
        "modified_count":       update_result["modified_count"],
        "transactional":        update_result["tx"],
        "before_fingerprint":   group["expected_before_fingerprint"],
        "after_fingerprint":    group["expected_authoritative_fingerprint"],
        "post_verification":    "OK",
        "workflow_run_id":      body.workflow_run_id,
        "session_id":           body.session_id,
        "created_at":           datetime.now(timezone.utc),
    })

    return {
        "ok":                   True,
        "collection":           body.collection,
        "logical_key_values":   body.logical_key_values,
        "doc_ids":              list(group["expected_doc_ids"]),
        "field":                body.field,
        "old_value":            body.old_value_sentinel,
        "new_value":            body.new_value,
        "matched_count":        update_result["matched_count"],
        "modified_count":       update_result["modified_count"],
        "transactional":        update_result["tx"],
        "before_fingerprint":   group["expected_before_fingerprint"],
        "after_fingerprint":    group["expected_authoritative_fingerprint"],
        "generated_at":         datetime.now(timezone.utc).isoformat(),
    }

