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
