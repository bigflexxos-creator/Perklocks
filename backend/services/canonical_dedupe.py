"""canonical_dedupe — services for Perklocks R3 Phase-7 duplicate
reconciliation (Support-confirmed design, pinned-source authoritative,
fail-closed on conflict).

Why this exists
───────────────
Phase 7 (`create-indexes`, unique indexes from `_INDEX_SPECS`) failed
with E11000 ``DUPLICATE_KEY`` in 8 of the 21 canonical collections:

    picks                  ux_pick_id
    player_game_actuals    ux_pga_identity
    player_game_logs       ux_pgl_identity
    player_identities      ux_pi_canonical
    prediction_snapshots   ux_prediction_snapshot_version
    pregame_snapshots      ux_snapshot_hash
    publication_events     ux_payload_hash
    settlement_events      ux_settlement_id

The 8 collections hold EXTRA physical rows that share a logical key —
not because the import semantics are broken (Phase 5 upserts are
idempotent on the logical key) but because historical preview-era
imports pre-dating Phase-5-R3 wrote by `_id` and the subsequent R3
canonical imports wrote by logical-key, leaving duplicate physical
rows for the same logical identity.

This module provides a strict, hash-authoritative dedupe engine:
  * read-only ``scan_duplicates`` — enumerate every duplicate group
    across all 21 canonical collections, classify each as
    ``EXACT_DUPLICATE`` or ``CONFLICTING_DUPLICATE``.
  * authoritative ``apply_dedupe_group`` — delete only redundant
    physical rows that match the pinned-source authoritative
    fingerprint; FAIL CLOSED on any ambiguity.

Guarantees
──────────
  * No whole-collection deletes.
  * No blanket dedupe.
  * No "latest timestamp wins".
  * Hash equality against the pinned source is the sole acceptance
    rule.
  * Every mutation writes a durable audit record to
    ``canonical_dedupe_audit`` BEFORE performing the delete.
  * Pre-delete guard: survivor fingerprint == authoritative source.
  * Post-delete guard: logical-key count == 1 after reconciliation.
  * APPLY is restricted to a hard-coded allowlist of 8 collections
    (``APPLY_DEDUPE_SCOPE``); any other collection is refused.
  * Idempotent: re-running after a successful group dedupe is a
    no-op (group already has count 1).

The dedupe engine itself has no knowledge of collection names as
behaviour — rules apply uniformly, selection is driven exclusively
by hash equality.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from .canonical_cutover import (
    extract_logical_key,
    logical_key_fields,
    RECONCILIATION_COLLECTIONS,
)

logger = logging.getLogger("perklocks.canonical_dedupe")


# ─── APPLY-mode allowlist (the 8 that failed Phase-7 unique-index) ──
APPLY_DEDUPE_SCOPE: frozenset[str] = frozenset({
    "picks",
    "player_game_actuals",
    "player_game_logs",
    "player_identities",
    "prediction_snapshots",
    "pregame_snapshots",
    "publication_events",
    "settlement_events",
})

# Audit collection — durable record of every dedupe mutation.
DEDUPE_AUDIT_COLLECTION: str = "canonical_dedupe_audit"

# ─── ObjectId ⇄ string normalization (JSON round-trip support) ──────
# Phase-7 Support fix (2026-10-10):
# FastAPI JSON-serialises the census response AFTER the handler returns.
# Raw ``bson.ObjectId`` instances in ``groups[*]._ids`` cause a late
# serialization failure that is converted by ``_ReliabilityMiddleware``
# into the generic "Something went wrong — please retry." 500. The
# response therefore represents every ``_id`` as a JSON-safe string
# (``_jsonable_id``) and the APPLY path re-hydrates 24-char hex
# ObjectId strings back into ``bson.ObjectId`` instances before any
# Mongo find/delete op (``_normalize_mongo_id``).  Non-ObjectId ``_id``
# values (UUID strings, numeric scalars) are preserved byte-identical.
# This change is strictly a transport-level serialization fix: no
# fingerprinting, logical-key, authority, or dedupe-rule behaviour is
# modified.
def _is_valid_objectid_str(x: Any) -> bool:
    """True iff ``x`` is a 24-char hex string that parses as a Mongo
    ObjectId. Non-strings and strings of any other length return False.
    """
    if not isinstance(x, str) or len(x) != 24:
        return False
    try:
        from bson import ObjectId
        return bool(ObjectId.is_valid(x))
    except Exception:
        return False


def _normalize_mongo_id(x: Any) -> Any:
    """Re-hydrate a transport-serialised Mongo ``_id`` for use in find/
    delete operations.

    * 24-char hex string that is a valid ``bson.ObjectId``  →  ObjectId
    * every other value                                     →  unchanged

    This mirrors the census round-trip exactly: ``_jsonable_id`` emits
    a JSON-safe string on the way out, and this helper rebuilds the
    native Mongo type on the way back in for the APPLY call. For
    collections whose documents use string/UUID/scalar ``_id`` values
    the input is preserved byte-identical.
    """
    if _is_valid_objectid_str(x):
        from bson import ObjectId
        return ObjectId(x)
    return x


def _jsonable_id(x: Any) -> Any:
    """Return a JSON-serialisable representation of a Mongo ``_id``:
    ``bson.ObjectId`` → its 24-char hex string; every other type is
    returned unchanged.
    """
    try:
        from bson import ObjectId
        if isinstance(x, ObjectId):
            return str(x)
    except Exception:  # pragma: no cover - defensive
        pass
    return x


# Fingerprint config — exclude Mongo _id + any underscore-prefixed
# bookkeeping (``_canonical_*``, ``_import_*``, ``_resolution_*``,
# ``_reconciled_at``, etc.).  Business fields never start with "_" in
# Perklocks canonical collections (verified against `_LOGICAL_KEYS`
# plus manual audit of the 21 pinned v2_phase5/canonical/*.ndjson
# schemas).
def _is_bookkeeping_field(key: str) -> bool:
    """A field is bookkeeping iff it is ``_id`` or any key starting
    with ``_``.  Canonical business fields never start with ``_``."""
    return key == "_id" or key.startswith("_")


def canonical_doc_fingerprint(doc: dict) -> str:
    """Deterministic SHA-256 fingerprint of a canonical document's
    business content.  Excludes ``_id`` and all underscore-prefixed
    bookkeeping fields.  Sorted-key JSON serialisation with
    ``default=str`` for non-JSON scalars (datetime, ObjectId, etc.)
    so two docs with equivalent business content from different
    storage layouts hash identically.
    """
    business = {k: v for k, v in doc.items() if not _is_bookkeeping_field(k)}
    payload = json.dumps(business, default=str, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ─── Group classification ────────────────────────────────────────────
EXACT_DUPLICATE:       str = "EXACT_DUPLICATE"
CONFLICTING_DUPLICATE: str = "CONFLICTING_DUPLICATE"
_CLASSIFICATIONS = (EXACT_DUPLICATE, CONFLICTING_DUPLICATE)


def classify_group(fingerprints: list[str]) -> str:
    """A duplicate group where every doc shares one fingerprint is
    EXACT_DUPLICATE; otherwise CONFLICTING_DUPLICATE.  No hard-coded
    collection semantics — classification is purely by content hash
    equality within the group.
    """
    return (EXACT_DUPLICATE if len(set(fingerprints)) == 1
            else CONFLICTING_DUPLICATE)


# ─── Read-only census ────────────────────────────────────────────────
async def scan_duplicates(
    db:            AsyncIOMotorDatabase,
    coll:          str,
    *,
    offset:                int = 0,
    limit:                 int = 100,
    max_docs_per_group:    int = 50,
) -> dict:
    """Return a PAGED, read-only duplicate-group census for ``coll``.

    Memory-safe design (R3 Phase-7 census-500 regression fix):

      1. Compute collection stats via lightweight aggregations that
         never ``$push`` document arrays:
           total_docs            — ``count_documents({})``
           logical_key_count     — ``$group`` on logical key, no push,
                                   final ``$count``.
           duplicate_group_count — same, filtered to ``count > 1``.
      2. Materialise only the requested PAGE of duplicate keys via
         ``$group{count} → $match{>1} → $sort → $skip → $limit``,
         still with no ``$push``.
      3. For the page's keys, build ONE batched ``$or`` find to pull
         every dup-group document in a SINGLE collection scan (serves
         the entire page) and group them back by logical key in
         Python — bounded at ``max_docs_per_group`` samples per group
         and ``MAX_DOCS_PER_PAGE_CAP`` total per page.
      4. Compute fingerprint + classification per group.

    Pagination contract (deterministic):
        * groups are sorted by ``_id`` (the logical-key dict); the
          driver's cursor advances by ``next_offset``.
        * ``has_more`` is True while ``offset + len(groups) < duplicate_group_count``.
        * ``page_exact_count`` + ``page_conflicting_count`` are
          PER-PAGE; the driver aggregates them across pages so the
          server never has to recompute a full-collection classification.

    Args:
        offset:            0-based page cursor on the sorted dup-key list.
        limit:             groups per page.  Hard cap 500.
        max_docs_per_group: docs sampled per group for fingerprinting.
    """
    MAX_DOCS_PER_PAGE_CAP = 10_000
    limit = max(1, min(int(limit), 500))
    max_docs_per_group = max(1, min(int(max_docs_per_group), 200))

    key_fields = list(logical_key_fields(coll))
    group_id = {f: f"${f}" for f in key_fields}

    # ── Per-stage try/except — R3 Phase-7 census-500 diagnostics ───
    # Every aggregation stage is wrapped so a failure returns a
    # structured error identifying EXACTLY which stage failed.  The
    # endpoint raises 500 with these details so the GH runner / audit
    # can root-cause without rerunning blind.
    stage = "unknown"
    try:
        stage = "count_documents"
        total_docs = await db[coll].count_documents({})

        stage = "logical_key_count"
        pipe_lk_count = [
            {"$group": {"_id": group_id}},
            {"$count": "total"},
        ]
        lk_count_doc = None
        async for d in db[coll].aggregate(pipe_lk_count, allowDiskUse=True):
            lk_count_doc = d
        logical_key_count = int((lk_count_doc or {}).get("total") or 0)

        stage = "duplicate_group_count"
        pipe_dup_count = [
            {"$group": {"_id": group_id, "count": {"$sum": 1}}},
            {"$match": {"count": {"$gt": 1}}},
            {"$count": "total"},
        ]
        dup_count_doc = None
        async for d in db[coll].aggregate(pipe_dup_count, allowDiskUse=True):
            dup_count_doc = d
        duplicate_group_count = int((dup_count_doc or {}).get("total") or 0)
    except Exception as e:
        # Re-raise with structured context so the endpoint can
        # surface a non-ambiguous 500 payload.
        raise RuntimeError(
            f"CENSUS_STAGE_FAIL stage={stage} collection={coll} "
            f"exception={type(e).__name__}: {str(e)[:400]}"
        ) from e

    # ── Fast path: no duplicates ────────────────────────────────────
    if duplicate_group_count == 0 or offset >= duplicate_group_count:
        return {
            "collection":              coll,
            "logical_key_fields":      key_fields,
            "total_docs":              total_docs,
            "logical_key_count":       logical_key_count,
            "duplicate_group_count":   duplicate_group_count,
            "page_offset":             offset,
            "page_limit":              limit,
            "page_group_count":        0,
            "page_exact_count":        0,
            "page_conflicting_count":  0,
            "has_more":                False,
            "next_offset":             None,
            "groups":                  [],
        }

    # ── Paged dup-key fetch (still no $push on docs) ────────────────
    try:
        stage = "paged_duplicate_key_fetch"
        pipe_page = [
            {"$group": {"_id": group_id, "count": {"$sum": 1}}},
            {"$match": {"count": {"$gt": 1}}},
            {"$sort":  {"_id": 1}},
            {"$skip":  offset},
            {"$limit": limit},
        ]
        page_keys: list[dict] = []
        async for d in db[coll].aggregate(pipe_page, allowDiskUse=True):
            page_keys.append(d)
    except Exception as e:
        raise RuntimeError(
            f"CENSUS_STAGE_FAIL stage={stage} collection={coll} "
            f"offset={offset} limit={limit} "
            f"exception={type(e).__name__}: {str(e)[:400]}"
        ) from e

    if not page_keys:
        return {
            "collection":              coll,
            "logical_key_fields":      key_fields,
            "total_docs":              total_docs,
            "logical_key_count":       logical_key_count,
            "duplicate_group_count":   duplicate_group_count,
            "page_offset":             offset,
            "page_limit":              limit,
            "page_group_count":        0,
            "page_exact_count":        0,
            "page_conflicting_count":  0,
            "has_more":                offset < duplicate_group_count,
            "next_offset":             None,
            "groups":                  [],
        }

    # ── Batched $or fetch — single collection scan serves the page ──
    try:
        stage = "document_fetch"
        or_clauses = [k["_id"] for k in page_keys]
        cursor = db[coll].find({"$or": or_clauses}).sort("_id", 1)
        collected: dict[tuple, list[dict]] = {}
        overflow = False
        total_fetched = 0
        async for d in cursor:
            total_fetched += 1
            if total_fetched > MAX_DOCS_PER_PAGE_CAP:
                overflow = True
                break
            lk_tuple = tuple(d.get(f) for f in key_fields)
            bucket = collected.setdefault(lk_tuple, [])
            if len(bucket) < max_docs_per_group:
                bucket.append(d)
    except Exception as e:
        raise RuntimeError(
            f"CENSUS_STAGE_FAIL stage={stage} collection={coll} "
            f"offset={offset} page_keys={len(page_keys)} "
            f"exception={type(e).__name__}: {str(e)[:400]}"
        ) from e

    # ── Build per-group output ──────────────────────────────────────
    try:
        stage = "fingerprint_and_classify"
        groups: list[dict] = []
        exact_cnt = 0
        conflicting_cnt = 0
        for k in page_keys:
            lk_dict = k["_id"]
            dup_count = int(k["count"])
            lk_tuple = tuple(lk_dict.get(f) for f in key_fields)
            docs = collected.get(lk_tuple, [])
            # Phase-7 Support fix: serialise ObjectId _ids to strings so
            # FastAPI JSON serialisation succeeds. See ``_jsonable_id``
            # docstring above for the full round-trip contract.
            ids = [_jsonable_id(d["_id"]) for d in docs]
            fps = [canonical_doc_fingerprint(d) for d in docs]
            cls = classify_group(fps) if fps else "UNKNOWN"
            if cls == EXACT_DUPLICATE:
                exact_cnt += 1
            elif cls == CONFLICTING_DUPLICATE:
                conflicting_cnt += 1
            groups.append({
                "collection":                 coll,
                "logical_key":                lk_dict,
                "logical_key_fields":         key_fields,
                "dup_count":                  dup_count,
                "_ids":                       ids,
                "_ids_truncated":             len(ids) < dup_count,
                "fingerprints":               fps,
                "distinct_fingerprint_count": len(set(fps)) if fps else 0,
                "classification":             cls,
            })
    except Exception as e:
        raise RuntimeError(
            f"CENSUS_STAGE_FAIL stage={stage} collection={coll} "
            f"exception={type(e).__name__}: {str(e)[:400]}"
        ) from e

    end = offset + len(groups)
    return {
        "collection":              coll,
        "logical_key_fields":      key_fields,
        "total_docs":              total_docs,
        "logical_key_count":       logical_key_count,
        "duplicate_group_count":   duplicate_group_count,
        "page_offset":             offset,
        "page_limit":              limit,
        "page_group_count":        len(groups),
        "page_exact_count":        exact_cnt,
        "page_conflicting_count":  conflicting_cnt,
        "page_fetch_overflow":     overflow,
        "has_more":                end < duplicate_group_count,
        "next_offset":             (end if end < duplicate_group_count else None),
        "groups":                  groups,
    }


# ─── Pre-delete audit record ─────────────────────────────────────────
async def _persist_audit(
    db:                    AsyncIOMotorDatabase,
    *,
    collection:            str,
    logical_key_values:    dict,
    all_ids:               list,
    survivor_id,
    deleted_ids:           list,
    survivor_reason:       str,
    source_fingerprint:    str,
    doc_fingerprints:      dict,
    workflow_run_id:       str,
    session_id:            str,
    classification:        str,
) -> None:
    """Write a durable audit record to ``canonical_dedupe_audit`` in
    the canonical database BEFORE performing the delete.  Captures
    every piece of evidence that will later let an operator
    reconstruct the decision made by the dedupe engine."""
    await db[DEDUPE_AUDIT_COLLECTION].insert_one({
        "collection":            collection,
        "logical_key_values":    logical_key_values,
        "all_ids":               all_ids,
        "survivor_id":           survivor_id,
        "deleted_ids":           deleted_ids,
        "survivor_reason":       survivor_reason,
        "source_fingerprint":    source_fingerprint,
        "doc_fingerprints":      doc_fingerprints,
        "classification":        classification,
        "workflow_run_id":       workflow_run_id,
        "session_id":            session_id,
        "created_at":            datetime.now(timezone.utc),
    })


# ─── Apply a single group ────────────────────────────────────────────
# Survivor reason codes (deterministic, auditable):
REASON_MATCH_PINNED_SOURCE:   str = "match_pinned_source"
REASON_EARLIEST_CANONICAL:    str = "earliest_canonical"
REASON_LOWEST_ID:             str = "lowest_id_fallback"


async def apply_dedupe_group(
    db:                       AsyncIOMotorDatabase,
    *,
    collection:               str,
    logical_key_values:       dict,
    classification:           str,
    all_ids:                  list,
    authoritative_fingerprint: str,
    workflow_run_id:          str,
    session_id:               str,
    survivor_hint_order:      list[str] | None = None,
) -> dict:
    """Reconcile exactly one duplicate group.  Fail closed on anything
    that would mutate data without unambiguous authoritative
    justification.

    Args:
        collection: canonical collection name.  MUST be in
            ``APPLY_DEDUPE_SCOPE``; otherwise raises.
        logical_key_values: dict of ``{field: value}`` for the
            group's logical key.  Must match the collection's
            ``logical_key_fields``.
        classification: must equal the server-recomputed
            classification of the group (prevents the caller from
            smuggling in a wrong class).
        all_ids: every physical _id in the group.
        authoritative_fingerprint: canonical business-content
            fingerprint of the pinned-source document for this
            logical key.  Computed client-side from
            ``v2_phase5/canonical/<coll>.ndjson``.
        workflow_run_id, session_id: audit metadata.
        survivor_hint_order: optional ordered list of candidate _ids
            for the "earliest canonical" tiebreak.  If omitted, the
            server derives earliest-canonical from the lowest _id.

    Returns:
        A dict describing the reconciled group — survivor, deleted
        ids, audit document, pre/post guards passed.

    Raises:
        ValueError on:
          * out-of-scope collection
          * logical_key_values fields do not match
          * classification mismatch (server-recomputed vs claimed)
          * CONFLICTING with no authoritative match
          * pre-delete guard: survivor fingerprint != authoritative
          * post-delete guard: logical-key count != 1
          * EXACT with non-identical fingerprints
    """
    # ── Scope guard ─────────────────────────────────────────────────
    if collection not in APPLY_DEDUPE_SCOPE:
        raise ValueError(
            f"OUT_OF_SCOPE_COLLECTION: {collection} is not in "
            f"APPLY_DEDUPE_SCOPE {sorted(APPLY_DEDUPE_SCOPE)}"
        )
    # ── Logical-key shape guard ─────────────────────────────────────
    expected_fields = set(logical_key_fields(collection))
    got_fields = set(logical_key_values.keys())
    if got_fields != expected_fields:
        raise ValueError(
            f"LOGICAL_KEY_SHAPE_MISMATCH: expected={sorted(expected_fields)} "
            f"got={sorted(got_fields)}"
        )
    if classification not in _CLASSIFICATIONS:
        raise ValueError(f"BAD_CLASSIFICATION: {classification!r}")

    # ── Normalize transport-serialised ids back to native Mongo type ─
    # The census JSON response represents ObjectId ``_id`` values as
    # 24-char hex strings. On the APPLY call we re-hydrate those back
    # to ``bson.ObjectId`` so Mongo find/delete predicates match the
    # original stored documents. Non-ObjectId ``_id`` values (UUID or
    # scalar strings) pass through unchanged. See ``_normalize_mongo_id``
    # above for the full contract. This normalization is transport-only
    # and never touches logical keys, fingerprints, authority, or any
    # dedupe/business rule.
    normalized_all_ids = [_normalize_mongo_id(i) for i in all_ids]

    # ── Load current docs for the group (hash-authoritative) ────────
    docs: list[dict] = []
    async for d in db[collection].find({"_id": {"$in": list(normalized_all_ids)}}):
        docs.append(d)
    found_ids = {d["_id"] for d in docs}
    missing   = [i for i in normalized_all_ids if i not in found_ids]
    if missing:
        raise ValueError(
            f"GROUP_IDS_MISSING_FROM_DB: {collection} "
            f"logical_key={logical_key_values} "
            f"missing={[_jsonable_id(i) for i in missing[:10]]}"
        )
    # Shape sanity: every doc actually belongs to this logical key.
    for d in docs:
        key_from_doc = {f: d.get(f) for f in sorted(expected_fields)}
        key_expected = {f: logical_key_values[f] for f in sorted(expected_fields)}
        if key_from_doc != key_expected:
            raise ValueError(
                f"DOC_LOGICAL_KEY_MISMATCH: {collection} "
                f"_id={_jsonable_id(d['_id'])} "
                f"got={key_from_doc} expected={key_expected}"
            )

    # ── Server-recompute fingerprints and classification ────────────
    doc_fps = {d["_id"]: canonical_doc_fingerprint(d) for d in docs}
    fps_list = list(doc_fps.values())
    server_class = classify_group(fps_list)
    if server_class != classification:
        raise ValueError(
            f"CLASSIFICATION_MISMATCH: claimed={classification} "
            f"server_recomputed={server_class} "
            f"collection={collection} key={logical_key_values}"
        )

    # ── Survivor selection ──────────────────────────────────────────
    # Rank candidates: 1) match pinned source  2) earliest canonical
    # (per hint)  3) lowest _id.  Hash equality is the SOLE authority
    # for #1; collection name is never a selection rule.
    matching_source = [i for i, fp in doc_fps.items()
                       if fp == authoritative_fingerprint]

    survivor_id = None
    survivor_reason = ""

    if len(matching_source) == 1:
        survivor_id = matching_source[0]
        survivor_reason = REASON_MATCH_PINNED_SOURCE
    elif len(matching_source) > 1:
        # EXACT_DUPLICATE path AND they all match the pinned source —
        # pick the earliest canonical / lowest _id tiebreak within
        # the matching set.
        if classification != EXACT_DUPLICATE:
            # Impossible: multiple-match in CONFLICTING would mean
            # distinct_fingerprints > 1 but ≥2 of them equal the
            # source — contradiction.  Guard anyway.
            raise ValueError(
                f"IMPOSSIBLE_MULTI_MATCH_IN_CONFLICTING_GROUP: "
                f"{collection} key={logical_key_values}"
            )
        if survivor_hint_order:
            # Re-hydrate transport-serialised ids in the hint order
            # using the same contract as ``normalized_all_ids`` above
            # so string ObjectIds compare equal to native ObjectId
            # entries in ``matching_source``.
            normalized_hint = [_normalize_mongo_id(i) for i in survivor_hint_order]
            ordered = [i for i in normalized_hint if i in matching_source]
            if ordered:
                survivor_id = ordered[0]
                survivor_reason = REASON_EARLIEST_CANONICAL
        if survivor_id is None:
            survivor_id = sorted(matching_source, key=lambda x: str(x))[0]
            survivor_reason = REASON_LOWEST_ID
    else:
        # len(matching_source) == 0 — no doc matches pinned source.
        if classification == CONFLICTING_DUPLICATE:
            # ** FAIL CLOSED ** per Support rule #6 of this phase:
            # "If no unique authoritative winner can be proven: FAIL
            # CLOSED and report that exact group."
            raise ValueError(
                f"NO_AUTHORITATIVE_WINNER: {collection} "
                f"key={logical_key_values} "
                f"authoritative_fingerprint={authoritative_fingerprint[:16]}… "
                f"doc_fingerprints={[fp[:16]+'…' for fp in fps_list]}"
            )
        # EXACT with 0 pinned-source matches ⇒ EVERY doc has the same
        # fingerprint but it differs from the pinned source.  This is
        # a canonical/source divergence — fail closed.  Operator must
        # investigate before any deletion is allowed.
        raise ValueError(
            f"EXACT_GROUP_DIVERGES_FROM_PINNED_SOURCE: {collection} "
            f"key={logical_key_values} "
            f"all_docs_fingerprint={fps_list[0][:16]}… "
            f"authoritative_fingerprint={authoritative_fingerprint[:16]}…"
        )

    # ── Pre-delete guard: survivor fingerprint == source ────────────
    if doc_fps[survivor_id] != authoritative_fingerprint:
        raise ValueError(
            f"PRE_DELETE_GUARD_FAIL: survivor fingerprint "
            f"{doc_fps[survivor_id][:16]}… != authoritative "
            f"{authoritative_fingerprint[:16]}… "
            f"({collection} key={logical_key_values})"
        )

    deleted_ids = [i for i in normalized_all_ids if i != survivor_id]

    # Idempotent fast path: nothing to delete.
    if not deleted_ids:
        return {
            "collection":             collection,
            "logical_key_values":     logical_key_values,
            "classification":         classification,
            "survivor_id":            _jsonable_id(survivor_id),
            "deleted_ids":            [],
            "deleted_count":          0,
            "survivor_reason":        survivor_reason,
            "pre_delete_guard":       "pass",
            "post_delete_guard":      "pass_noop",
            "idempotent_rerun":       True,
        }

    # ── Persist audit BEFORE delete ─────────────────────────────────
    # Audit ``_id`` fields are stored as JSON-safe strings so the audit
    # document can be re-serialised unchanged into deploy-log mirrors,
    # workflow summaries, and the GH-Actions run artefacts.
    await _persist_audit(
        db,
        collection=collection,
        logical_key_values=logical_key_values,
        all_ids=[_jsonable_id(i) for i in normalized_all_ids],
        survivor_id=_jsonable_id(survivor_id),
        deleted_ids=[_jsonable_id(i) for i in deleted_ids],
        survivor_reason=survivor_reason,
        source_fingerprint=authoritative_fingerprint,
        doc_fingerprints={_jsonable_id(k): v for k, v in doc_fps.items()},
        workflow_run_id=workflow_run_id,
        session_id=session_id,
        classification=classification,
    )

    # ── Delete redundant physical rows ──────────────────────────────
    res = await db[collection].delete_many({"_id": {"$in": deleted_ids}})
    if res.deleted_count != len(deleted_ids):
        # Partial delete — serious.  Audit already recorded the
        # intent so operator can reconcile manually.
        raise RuntimeError(
            f"PARTIAL_DELETE: {collection} key={logical_key_values} "
            f"requested_delete={len(deleted_ids)} actual={res.deleted_count}"
        )

    # ── Post-delete guard: exactly one survivor remains ─────────────
    remaining = await db[collection].count_documents(logical_key_values)
    if remaining != 1:
        raise RuntimeError(
            f"POST_DELETE_GUARD_FAIL: {collection} "
            f"key={logical_key_values} remaining={remaining} (expected 1)"
        )

    return {
        "collection":             collection,
        "logical_key_values":     logical_key_values,
        "classification":         classification,
        "survivor_id":            _jsonable_id(survivor_id),
        "deleted_ids":            [_jsonable_id(i) for i in deleted_ids],
        "deleted_count":          len(deleted_ids),
        "survivor_reason":        survivor_reason,
        "pre_delete_guard":       "pass",
        "post_delete_guard":      "pass",
        "idempotent_rerun":       False,
    }
