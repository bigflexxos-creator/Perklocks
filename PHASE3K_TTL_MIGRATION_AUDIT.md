# Phase 3K — publication_mismatch_report TTL Migration Audit

**Date:** 2026-08-06
**Retention decision:** 30 days (2,592,000 seconds)

## Pre-migration state
| Metric | Value |
|---|---|
| Total rows | 42,756 |
| Rows with `logged_at` (ISO string) | 42,756 (100%) |
| Rows with `logged_at_dt` (BSON Date) | 0 |
| Rows missing `logged_at` | 0 |
| Invalid `logged_at` timestamps (2000-row sample) | 0 |
| Oldest logged_at | 2026-08-06T07:09:07Z |
| Newest logged_at | 2026-08-06T16:59:xx Z |
| Rows older than 30 days | 0 |
| Existing indexes | `_id_`, `mismatch_prediction_board_idx`, `mismatch_logged_at_idx` |

## Post-migration state
| Metric | Value |
|---|---|
| Total rows | 42,760 (grew during migration — writers still active) |
| Rows with `logged_at_dt` | 42,760 (**100%**) |
| Rows pending backfill | 0 |
| Invalid rows | 0 |
| TTL index present | ✅ `mismatch_logged_at_dt_ttl` |
| TTL expireAfterSeconds | **2,592,000** (30 days) |
| Rows older than 30 days | 0 (nothing eligible yet — TTL prevents future growth) |

## Migration order executed
1. ✅ Writer updated to emit `logged_at_dt` on every new insert (both ISO string + BSON Date).
2. ✅ Backend redeployed; new rows verified to carry `logged_at_dt`.
3. ✅ Dry-run backfill (`--limit 500`, no writes): 500 scanned, 500 would_migrate, 0 invalid, 0 mutations verified.
4. ✅ Invalid row review: **0 invalid** in the entire collection.
5. ✅ Full backfill executed (`--execute --batch-size 1000`): **42,756 rows migrated**, 0 invalid, ~15 s runtime.
6. ✅ Coverage verified: **100.00%**.
7. ✅ TTL index added via `services/index_registry.py`; created automatically on next backend restart via Phase 3C `ensure_all_indexes()`.
8. ✅ Mongo accepted the TTL — `index_information()` confirms `expireAfterSeconds=2592000` on `logged_at_dt`.
9. ✅ Idempotency verified: rerunning `--execute` reports `pending=0, migrated=0`.
10. ✅ No documents were manually deleted at any stage.

## Deliverables
- `backend/scripts/backfills/backfill_publication_mismatch_logged_at_dt.py` — dry-run-first, resumable, idempotent
- `backend/services/prediction_publication_service.py` — writer updated (`_now.isoformat()` + `_now` BSON Date)
- `backend/services/index_registry.py` — replaced the Phase 3C block declaration with the active TTL spec
- `backend/routes/ops_routes.py` — `GET /api/admin/ops/mismatch-ttl/status`
- `backend/tests/test_iter130_phase3k_ttl.py` — 10-test contract
- `backend/tests/test_iter126_phase3c_index_registry.py::test_publication_mismatch_ttl_applied_in_phase3k` — Phase 3C block-guardrail flipped to an applied-guardrail

## Rollback safety
The migration was purely ADDITIVE:
- `logged_at` ISO string kept intact on every row.
- `logged_at_dt` added; if we drop the TTL index, no data is lost.
- Backfill script has no delete/drop path at all.
