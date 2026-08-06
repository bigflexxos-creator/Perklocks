# Phase 3G — Step 5 Execution Report

**Status:** COMPLETE — controlled live migration executed and idempotent rerun verified
**Companion of:** all prior PHASE3G_* documents
**Machine-readable companion:** `/app/PHASE3G_STEP5_EXECUTION_REPORT.json`
**Scope:** Migrate the four eligible user-owned `p_*` records from
`parlay_history` into canonical `user_bets`. Verify every guardrail.

---

## 1. Guardrails held

- ✅ Executor required **BOTH** `--execute` **AND** `--confirm PRODUCTION`.
  All lesser invocations refused with exit code 2.
- ✅ Pre-execution gate re-ran the Step 4 dry-run **immediately before**
  writes; passed with `pre_gate_ok=true`, `blockers=[]`.
- ✅ `plearn_*` rows permanently excluded — 190/190 skipped.
- ✅ Uses `services.user_bet_ledger.map_legacy_user_parlay` — the pure
  Step 2 mapper. No re-implementation.
- ✅ Migration index (`migration_source + migration_source_id`
  partial-unique) present and matches Phase 3C registry exactly.
- ✅ `parlay_history` never modified. `prediction_snapshots` never
  modified. `settlement_events` never modified.
- ✅ Existing native `user_bets` rows untouched (verified byte-for-byte).
- ✅ Second `--execute --confirm PRODUCTION` invocation inserted **0**
  rows and marked all 4 as `skipped_existing`.
- ✅ Every migrated row carries `is_legacy=true`,
  `migration_source="parlay_history"`, `migration_source_id=<p_ id>`,
  `migration_version=1` — clean rollback filter.
- ✅ No route conversion (verified statically by
  `test_no_route_conversion_shipped_in_step_5`).

---

## 2. Files created

| File | Lines | Purpose |
|------|------:|---------|
| `backend/scripts/backfills/execute_parlay_history_p_to_user_bets.py` | ~360 | Controlled executor. Refuses without `--execute --confirm PRODUCTION`. |
| `backend/tests/test_iter134_legacy_parlay_execute.py` | ~570 | 23 tests covering all 20 Step 5 invariants (all passing). |
| `PHASE3G_STEP5_EXECUTION_REPORT.md` | this file | Human-readable report + rollback. |
| `PHASE3G_STEP5_EXECUTION_REPORT.json` | ~350 | Full machine-readable execution log. |

## 2b. Files changed

**None.** Step 5 introduced only new files.
The Step 4 dry-run script (`migrate_parlay_history_p_to_user_bets.py`)
remains permanently dry-run-only — untouched.

---

## 3. Pre-execution results (fresh dry-run at execute time)

Fresh preflight computed immediately before the first write:

```
index_preflight.ok:                       true
ledger_preflight.ok:                      true
eligible_p_star:                          4
excluded_plearn:                          190
counts_by_classification:
  migration_ready:                        4
  duplicate_existing:                     0
  manual_review:                          0
  unsafe:                                 0
  excluded_learning:                      190
  excluded_missing_user:                  0
  excluded_invalid_structure:             0
status_mapping_breakdown:
  legacy:won → canonical:won              1
  legacy:lost → canonical:lost            3
payout_gaps:
  won_missing_payout:                     0
  lost_missing_stake:                     0
leg_identity_coverage:
  prediction_id:                          19/19
  original_odds:                          19/19
  snapshot_id:                            0/19
  market_contract_id:                     0/19
  exact_line:                             0/19
```

`pre_gate_ok = true` · `pre_gate_blockers = []` → **execution authorised.**

---

## 4. Execution results (live production `lockscore_db`)

```
mode:                        execute
selected_count:              4
inserted_count:              4
skipped_existing_count:      0
forbidden_mutations:         []
```

Inserted rows (`legacy_id → user_bet_id`):

| Legacy `parlay_history.id` | New `user_bets.user_bet_id` |
|---|---|
| `p_4b0c7225f2fe81` | `bd10b40a-4357-4260-9b91-cfda0f76c195` |
| `p_e1af69205d77aa` | `606da1f0-1c55-4dc0-99a4-e9409b2e1248` |
| `p_e7a0f677d0298d` | `60223d44-784c-43f5-94d2-146ec00efc20` |
| `p_fbd8fdb05daf5b` | `112e5075-bb21-405a-bbd9-960c86c44f60` |

---

## 5. Idempotent rerun result

Second `--execute --confirm PRODUCTION` invocation:

```
selected_count:              0
inserted_count:              0
skipped_existing_count:      4
forbidden_mutations:         []
post_migrated_all_duplicate: true
```

Idempotent by `(migration_source, migration_source_id)` — provably safe
to rerun.

---

## 6. Post-execution collection counts

| Collection | Before | After execute | After idempotent rerun |
|---|---:|---:|---:|
| `user_bets` | 2 | **6** (2 native + 4 migrated) | **6** |
| `parlay_history` | 194 | **194** | **194** |
| `prediction_snapshots` | 14710 | **14710** | **14710** |
| `settlement_events` | 0 | **0** | **0** |

**Zero delta on every forbidden collection.** Only `user_bets` grew,
and only by the expected +4.

Native rows preserved unchanged:
```
id                                    status
────────────────────────────────────  ──────
05e6c1a0-8ed2-4fd1-8237-9333a872436f  pending
2f18de8b-f96f-4eaa-9992-6c9f7081f80e  lost
```

Both had zero fields modified between before/after.

---

## 7. Sample migrated row (verification)

```
user_bet_id            = 'bd10b40a-4357-4260-9b91-cfda0f76c195'
user_id                = 'c5195f25-…' (preserved from legacy)
wager_type             = 'parlay'
status                 = 'won'
original_status        = 'won'         ← preserved verbatim
combined_odds          = 285           ← preserved
stake_amount           = 10.0          ← preserved
actual_payout          = 28.5          ← preserved
profit_loss            = 28.5          ← preserved
migration_source       = 'parlay_history'
migration_source_id    = 'p_4b0c7225f2fe81'
is_legacy              = true
migration_version      = 1
source                 = 'backfill_p'
mode                   = 'standard'    ← preserved
clv_status             = 'unavailable' ← nullable default
clv_value              = None          ← never invented
sportsbook             = None          ← never invented
opening_line           = None          ← never invented
closing_line           = None          ← never invented
opening_odds           = None
closing_odds           = None
snapshot_id            = None          ← unavailable in legacy
market_contract_id     = None          ← unavailable in legacy
placed_at              = datetime(2026-06-21 15:59:47 +00:00)
settled_at             = datetime(2026-06-21 16:32:17 +00:00)
legs count             = 3
  leg[0] prediction_id = '8898c2c6-…' (preserved)
         original_odds = -200        (preserved)
         line          = None        (never invented)
         status        = 'won'
```

Every guardrail from the Step 5 prompt is satisfied.

---

## 8. Step 4 dry-run post-migration reclassification

Rerunning the Step 4 dry-run script after the migration reclassifies
every migrated row as `duplicate_existing` via primary match:

```
"post_migrated_all_duplicate": true
```

All 4 legacy ids now classify as `duplicate_existing / primary`. On
subsequent executes, these rows are skipped by both the pre-gate and
the per-row analyser.

---

## 9. Route / schema parity

- **`GET /api/user/bets`**: envelope unchanged (`{bets, count}`).
  Individual documents now include the canonical fields added in
  Step 3 (additive, no field removals). Migrated rows now readable
  for users who own them.
- **`GET /api/parlay/history`**: response shape unchanged (envelope
  `{parlays, count}`; per-parlay keys identical:
  `{id, user_id, created_at, mode, leg_ids, legs, combined_odds,
   stake, status, settled_at, payout, legs_won, legs_lost, legs_pending,
   cashout_estimate}`).
- **`GET /api/user/analytics/*`**: all endpoints functional, envelopes
  unchanged.
- **Backend health**: `GET /api/health → {"status":"ok"}` after service
  restart.

Static assertion: `routes/user_bets_routes.py` and
`routes/parlay_history_routes.py` do NOT import the ledger — no writer
or reader cutover has begun.

---

## 10. Test commands and results

```bash
cd /app/backend

# 23 new Step 5 tests
python3 -m pytest tests/test_iter134_legacy_parlay_execute.py -v
   → 23 passed in 0.96s

# All Phase 3 tests (3A-3K + Steps 2-5)
python3 -m pytest tests/test_iter12*.py tests/test_iter13*.py -q
   → 254 passed in 6.03s   (231 pre-Step 5 + 23 new)

# Refusal checks
python3 -m scripts.backfills.execute_parlay_history_p_to_user_bets
   → exit code 2, "requires BOTH --execute AND --confirm 'PRODUCTION'"

python3 -m scripts.backfills.execute_parlay_history_p_to_user_bets --execute
   → exit code 2, same message

python3 -m scripts.backfills.execute_parlay_history_p_to_user_bets \
   --execute --confirm yes
   → exit code 2, same message

# Real execution
python3 -m scripts.backfills.execute_parlay_history_p_to_user_bets \
   --execute --confirm PRODUCTION \
   --report-path /app/PHASE3G_STEP5_EXECUTION_REPORT.json
   → inserted=4, skipped=0, forbidden=[]

# Idempotent rerun
python3 -m scripts.backfills.execute_parlay_history_p_to_user_bets \
   --execute --confirm PRODUCTION
   → inserted=0, skipped=4, forbidden=[]
```

All 20 Step 5 invariants covered. All prior Phase 3 tests still pass.

---

## 11. Live-execution summary

| Metric | Value |
|---|---|
| pre_gate_ok | `true` |
| pre_gate_blockers | `[]` |
| selected_count | 4 |
| inserted_count | **4** |
| skipped_existing_count | 0 |
| inserted_legacy_ids | `[p_4b0c7225f2fe81, p_e1af69205d77aa, p_e7a0f677d0298d, p_fbd8fdb05daf5b]` |
| forbidden_mutations | `[]` |
| post_migrated_all_duplicate | `true` |
| idempotent rerun inserts | `0` |
| idempotent rerun skipped | `4` |
| parlay_history delta | `0` (194 → 194) |
| prediction_snapshots delta | `0` (14710 → 14710) |
| settlement_events delta | `0` (0 → 0) |
| native user_bets delta | `0` (both preserved) |
| Backend `/api/health` | `{"status":"ok"}` |
| Full Phase 3 pytest | 254 passed |

---

## 12. Rollback instructions

**Data rollback — precise, safe, idempotent.**

Every migrated row carries `migration_source="parlay_history"` and
`migration_version=1`. Rollback is a single Mongo delete:

```javascript
db.user_bets.deleteMany({
  migration_source: "parlay_history",
  migration_version: 1
})
// expected: deletedCount: 4
```

Alternatively, target by explicit user_bet_id list from the report:

```javascript
db.user_bets.deleteMany({
  user_bet_id: {$in: [
    "bd10b40a-4357-4260-9b91-cfda0f76c195",
    "606da1f0-1c55-4dc0-99a4-e9409b2e1248",
    "60223d44-784c-43f5-94d2-146ec00efc20",
    "112e5075-bb21-405a-bbd9-960c86c44f60"
  ]}
})
```

Either query is safe: it never touches native user_bets rows, never
touches `parlay_history`, never touches `prediction_snapshots`, and
never touches `settlement_events`.

**Code rollback:**

```bash
rm /app/backend/scripts/backfills/execute_parlay_history_p_to_user_bets.py
rm /app/backend/tests/test_iter134_legacy_parlay_execute.py
rm /app/PHASE3G_STEP5_EXECUTION_REPORT.md
rm /app/PHASE3G_STEP5_EXECUTION_REPORT.json
sudo supervisorctl restart backend
```

The Step 5 files are never imported by the live backend process, so
code rollback is safe and instantaneous.

---

## 13. Remaining decisions before Step 6

- **B6.1** Reader cutover strategy — should `/api/parlay/history` start
  reading from `user_bets` (canonical) with a shape adapter, or continue
  reading from `parlay_history` until dual-write is proven?
- **B6.2** Fixture capture — before flipping any reader, capture the
  exact JSON response of every parlay endpoint (already done informally;
  should we formalise as pytest fixtures?).
- **B6.3** Promote the 4 partial-unique `user_bets` indexes to
  `critical=True` now that we have 5 canonical writes proven safe?
  Recommendation: **NOT YET** — wait for dual-write in Step 7 to be
  live for 7 days per Step 2 policy.
- **B6.4** Admin diagnostics endpoint — surface
  `services.user_bet_ledger.safe_ledger_diagnostics()` as
  `/api/admin/ops/wager-ledger/diagnostics` (additive, admin-only)?

Nothing above blocks Step 6 planning.

---

## 14. Recommended Step 6 scope

**Step 6 — Reader cutover fixtures + parity harness (NO reader flip yet).**

- Add a pytest fixture pack that captures the exact JSON response shape
  of every parlay/history endpoint, using a curated snapshot of the
  live DB. Compares byte-for-byte against a canonical-read path.
- Add a canonical-read helper in `services/user_bet_ledger.py`:
  `list_parlays_for_history_view(user_id)` that returns the same shape
  as `/api/parlay/history`. This helper is **not wired to any route**.
- Add `backend/tests/test_iter135_reader_parity.py` proving:
  1. For every migrated row, the canonical read produces the same
     payload as the legacy read.
  2. For every native/pending row still in `parlay_history` (none in
     the current sample), the canonical read produces the same
     payload as the legacy read.
  3. Extra fields on the canonical read (e.g. `sportsbook`) are
     surfaced as `null`, matching the legacy default.
- Companion `PHASE3G_STEP6_READER_PARITY_REPORT.md`.
- Estimated LOC: helper ~180, tests ~350.

Reader cutover itself lives in Step 7. Step 6 is analysis-and-fixtures
only.

---

## 15. Suggested Git commit message

```
Phase 3G Step 5 — controlled legacy p_* → user_bets migration

- Add scripts/backfills/execute_parlay_history_p_to_user_bets.py:
  controlled executor. Requires BOTH --execute AND
  --confirm PRODUCTION. Runs a fresh Step 4 dry-run as a
  pre-execution gate (blocks on missing/conflicting index,
  ledger duplicates, any manual_review or unsafe row).
  Idempotent by (migration_source, migration_source_id).
  Never overwrites existing canonical rows. Never modifies
  parlay_history, prediction_snapshots, or settlement_events.
  Every row carries migration_version=1 for precise rollback.

- Add tests/test_iter134_legacy_parlay_execute.py: 23 tests
  covering all 20 Step 5 invariants including confirmation-flag
  requirements, missing/conflicting migration index blocks,
  manual_review/unsafe blocks, plearn_* exclusion, pure-mapper
  usage, idempotency, no-overwrite, preservation of status/
  timestamps/legs/odds/IDs, null-preservation for unavailable
  identity fields, void≠pushed, parlay_history immutability,
  prediction_snapshots + settlement_events immutability,
  zero-write on rerun, Step 4 reclassification to
  duplicate_existing, and route schema parity.

- Add PHASE3G_STEP5_EXECUTION_REPORT.md and .json with pre-gate
  results, insertion IDs, post-execution counts, idempotent
  rerun result, route parity, and rollback instructions.

Live production results:
  parlay_history:      194 → 194 (unchanged)
  prediction_snapshots: 14710 → 14710 (unchanged)
  settlement_events:   0 → 0 (unchanged)
  user_bets:           2 → 6 (+4 migrated, 2 native untouched)
  post_migrated_all_duplicate: true
  idempotent rerun:    0 inserted, 4 skipped as existing
All 254 Phase 3 tests pass.

Guardrails held: no dual-write, no writer/reader/settlement/analytics
cutover, no parlay_history modification, no unique-index promotion,
no independent Mongo client, no route changes.
```

---

**Step 5 stops here.** Waiting for review before proceeding to Step 6.

Not proceeding automatically to reader cutover, writer cutover, dual-write,
settlement cutover, index promotion, or any Phase 3H / Phase 4 work.
