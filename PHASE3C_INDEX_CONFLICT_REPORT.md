# Phase 3C — Index Conflict Report

**Date:** 2026-08-06
**Scope:** All conflicts, duplicates, and blocked migrations surfaced
by the central index registry when applied to the live database.

---

## Same-name conflicts on registry-owned collections
**Count:** 0

None. Every live index whose name matches a registry declaration also
matches its keys, unique/sparse flags, and TTL settings.

## Equivalent duplicates on registry-owned collections
**Count:** 0

No collection has a live index that shares a spec's key-set under a
different name.

---

## Newly created indexes at Phase 3C startup
### `provider_request_intents.request_key_idx`
- **Declared in the old `provider_budget.py` `ensure_indices()`** with `partialFilterExpression={"request_key": {"$exists": True, "$ne": None}}`.
- **Reality on live DB before Phase 3C:** *the index did not exist* — the partial filter used `$ne: null` which MongoDB rejects inside partial indexes because it expands to `$not: {$eq: null}` and `$not` is not among the supported operators for partial indexes.
- **The old code path silently swallowed the error** (`logger.debug("intents index: %s", e)`), so nobody noticed.
- **Fix applied in Phase 3C:** the registry declaration changed the partial filter to the supported `{"request_key": {"$exists": True}}` form. Mongo now creates the index successfully.
- **Behaviour change risk:** none. The index only makes queries filtered by `request_key` faster; no production code relies on the *absence* of this index. Idempotency of intent lookups is unaffected.

---

## `publication_mismatch_report` 30-day TTL — BLOCKED
**Decision (from user):** retain mismatch rows for 30 days.
**Registry status:** TTL implementation deliberately BLOCKED. `publication_mismatch_report` has two registry-declared indexes, **neither with `expire_after_seconds`**.

### Root cause
The `logged_at` field on `publication_mismatch_report` documents is stored as an **ISO 8601 string**, not a BSON `Date`. Live sample as of 2026-08-06:
- Total docs: 42,736
- `logged_at` as `type: string`: **42,736**
- `logged_at` as `type: date`: **0**

MongoDB's TTL monitor only expires documents whose indexed field is a BSON `Date`. Creating a TTL on a string field would produce a **broken TTL** that never deletes anything — worse than doing nothing because it introduces an implicit expectation that never fires.

### Safe migration plan (out of scope for Phase 3C — belongs to Phase 3K)
1. Add a new field `logged_at_dt` (BSON Date) to every new mismatch row written by `services/prediction_publication_service.py`. Old writers continue to also write `logged_at` for compat.
2. Backfill script (dry-run first) that reads each existing row's `logged_at` string, parses it to a `datetime`, and writes `logged_at_dt`. Use a small batch size and skip rows already migrated.
3. Once backfill completes, add the TTL index via the registry: `expire_after_seconds=2592000` (30 days) on `logged_at_dt`.
4. Drop the legacy string-column TTL declaration only after the new field is fully populated.
5. Optionally: rewrite writers to use only `logged_at_dt` after a documented deprecation window.

### Registry declaration reflects the block
```python
IndexSpec(
    "publication_mismatch_report", "mismatch_logged_at_idx",
    keys=(("logged_at", 1),), critical=False,
    owner_service="prediction_publication_service",
    purpose="chronological audit scan",
    migration_notes=(
        "PHASE3C: TTL declined here — logged_at is stored as ISO 8601 "
        "STRING, not BSON Date.  The 30-day TTL decision is captured "
        "but implementation is BLOCKED until a logged_at_dt BSON Date "
        "field is added to new writes and old rows migrated.  See "
        "PHASE3C_INDEX_CONFLICT_REPORT.md."
    ),
),
```
A dedicated test (`test_publication_mismatch_ttl_blocked_with_migration_notes`) asserts this configuration remains in place so a future accidental "just add the TTL" edit will fail loudly.

---

## Duplicate-data blockers on unique indexes
None encountered during Phase 3C. All existing unique declarations
(`users.email_1`, `picks.id_1`, `prediction_snapshots.prediction_snapshot_version_uniq`, `provider_budget_state.provider_month_uniq`, etc.) already exist on the live DB with no duplicate-key rejections.

The registry's `create_missing_indexes` function is defensive: unique-key creation failures raise on the Mongo side, are caught, and are **logged** at WARN level. No documents are ever deleted. Test `test_unique_index_duplicate_data_blocks_without_deleting` verifies this on a synthetic collection.

---

## Not-yet-in-registry (auxiliary collections)
The following auxiliary domain collections still have their indexes managed by ad-hoc `create_index` calls under `on_startup()` in `server.py`. They are NOT hot-path; they run only once per process start. They are queued for migration in a future Phase 3 session:

- `fusion_predictions`, `learning_log`
- `soccer_matches`, `soccer_predictions`, `soccer_accuracy`, `soccer_player_form`
- `players`, `games`, `player_game_logs`, `season_totals`, `team_form`
- `historical_ingestion_state`, `props_history`
- `espn_team_meta`, `espn_injury_notes`
- `tennis_players`

Because the ad-hoc calls run within `on_startup()` — the same lifecycle stage as the registry — the guardrail test does not flag them.

---

## Final status
- ✅ **63 registry-declared indexes present on live DB with matching definitions.**
- ✅ **1 previously-broken index now correctly created.**
- ✅ **Zero same-name conflicts.**
- ✅ **Zero equivalent duplicates on registry-owned collections.**
- ✅ **All 23 critical indexes verified — `critical_ok: True`.**
- 🟡 **`publication_mismatch_report` TTL deferred to Phase 3K with documented migration.**
