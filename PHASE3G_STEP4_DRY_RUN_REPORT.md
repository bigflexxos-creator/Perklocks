# Phase 3G — Step 4 Legacy Parlay Backfill DRY-RUN Report

**Status:** COMPLETE — dry-run only, `--execute` HARD-DISABLED
**Companion of:** `PHASE3G_WAGER_LEDGER_AUDIT.md`, `PHASE3G_PARITY_REPORT.md`,
`PHASE3G_MIGRATION_PLAN.md`, `PHASE3G_STEP3_SCHEMA_REPORT.md`
**Machine-readable companion:** `/app/PHASE3G_STEP4_DRY_RUN_REPORT.json`
**Scope:** Analyze eligible `p_*` rows in `parlay_history` and report what
would be inserted into `user_bets` on execute. No writes anywhere.

---

## 1. Guardrails held

- ✅ `--dry-run` is the default; `--execute` is HARD-DISABLED (exit code 2).
- ✅ Zero writes across every collection (`user_bets`, `parlay_history`,
  `prediction_snapshots`, `settlement_events`).
- ✅ `plearn_*` rows are hard-rejected via
  `services.user_bet_ledger.is_learning_row` (multi-signal detection).
- ✅ Uses `user_bet_ledger.map_legacy_user_parlay` — the pure Step 2 mapper.
  No duplicate mapping logic in the backfill script.
- ✅ Shared Phase 3B DB lifecycle only — no independent Mongo client.
- ✅ Never dedupes on player display names / selection text / same-total-odds
  alone (verified by `test_low_confidence_secondary_match_not_auto_duplicate`).
- ✅ Preserves existing `actual_payout` / `profit_loss` when present.
  Never treats missing payout as zero.
- ✅ `void` remains distinct from `pushed`.
- ✅ No route conversion (verified by
  `test_no_route_conversion_shipped_in_step_4`).

---

## 2. Files created

| File | Lines | Purpose |
|------|------:|---------|
| `backend/scripts/backfills/migrate_parlay_history_p_to_user_bets.py` | ~490 | Dry-run-only analysis script. `--execute` refused with exit 2. |
| `backend/tests/test_iter133_legacy_parlay_backfill.py` | ~530 | 20 tests covering all 19 Step 4 invariants (all passing). |
| `PHASE3G_STEP4_DRY_RUN_REPORT.md` | this file | Human-readable report. |
| `PHASE3G_STEP4_DRY_RUN_REPORT.json` | 168 | Full machine-readable dry-run report. |

## 2b. Files changed

**None.** No other file in the repo was modified during Step 4.

---

## 3. Index preflight result

**Status:** OK (index present, keys match, unique, partial filter present).

```
name:                 migration_source_1_migration_source_id_1_uniq_partial
present:              true
keys_match:           true
unique:               true
partial_filter_present: true
conflict_note:        null
```

Registry spec:
- `keys`  : `[("migration_source", 1), ("migration_source_id", 1)]`
- `unique`: `true`
- `partial_filter`: `{"migration_source": {"$exists": true, "$type": "string"},
                     "migration_source_id": {"$exists": true, "$type": "string"}}`

Live index information:
- `keys`  : `[("migration_source", 1), ("migration_source_id", 1)]`
- `unique`: `true`
- `partial_filter_present`: `true`

**Future `--execute` (in Step 5) MUST refuse to run if this index is missing
or conflicting.** The dry-run report field `production_execute_blocked=true`
is unconditionally set — this is Step 4.

Ledger preflight (`user_bets` unique/partial indexes):
```
{ok: true, total_user_bets: 2,
 duplicate_user_bet_id: 0, duplicate_client_bet_id_per_user: 0,
 duplicate_idempotency_key_per_user: 0, duplicate_migration_source_id: 0,
 conflicts: []}
```

---

## 4. Live dry-run counts (production `lockscore_db`)

| Metric | Value |
|---|---:|
| Total `parlay_history` rows | 194 |
| Excluded `plearn_*` rows | 190 |
| Eligible `p_*` rows | **4** |
| Classification: `migration_ready` | **4** |
| Classification: `duplicate_existing` | 0 |
| Classification: `manual_review` | 0 |
| Classification: `unsafe` | 0 |
| Classification: `excluded_learning` | 190 |
| Classification: `excluded_missing_user` | 0 |
| Classification: `excluded_invalid_structure` | 0 |

Status mapping breakdown (eligible only):
- Legacy `won` → canonical `won`: 1
- Legacy `lost` → canonical `lost`: 3
- No `push`, `void`, `live`, or unknown statuses in the live sample.

Payout / PnL gaps:
- `won_missing_payout`: 0
- `lost_missing_stake`: 0

Leg identity coverage across the 19 total legs in the 4 eligible rows:
- `prediction_id`: **19/19** (100 %)
- `original_odds`: **19/19** (100 %)
- `snapshot_id`: 0/19 (never captured on legacy — expected)
- `market_contract_id`: 0/19 (never captured on legacy — expected)
- `exact_line`: 0/19 (never captured on legacy legs — expected)

---

## 5. Row-by-row classification

| Legacy ID | user_id present | Original status | Canonical status | Legs | Stake | Combined odds | Payout | ProfitLoss | Prediction id cov | Original odds cov | Duplicate | Classification |
|---|:-:|---|---|---:|:-:|:-:|:-:|:-:|---|---|---|---|
| `p_4b0c7225f2fe81` | ✅ | `won`  | `won`  | 3 | ✅ | ✅ | ✅ | ✅ | 3/3 | 3/3 | — | migration_ready |
| `p_e1af69205d77aa` | ✅ | `lost` | `lost` | 7 | ✅ | ✅ | (n/a — lost) | ✅ | 7/7 | 7/7 | — | migration_ready |
| `p_e7a0f677d0298d` | ✅ | `lost` | `lost` | 7 | ✅ | ✅ | (n/a — lost) | ✅ | 7/7 | 7/7 | — | migration_ready |
| `p_fbd8fdb05daf5b` | ✅ | `lost` | `lost` | 2 | ✅ | ✅ | (n/a — lost) | ✅ | 2/2 | 2/2 | — | migration_ready |

Every eligible row carries:
- Deterministic `migration_source_id` = its legacy `p_*` id.
- Deterministic proposed `user_bet_id` — a freshly generated UUID per row
  (the Step 5 execute would upsert on
  `(migration_source, migration_source_id)`; the UUID is stable within a
  single mapping call).

All 190 `plearn_*` rows are classified as `excluded_learning` at the
top of the pipeline; they never reach the mapper.

---

## 6. Duplicate report

- **Primary matches** (`migration_source` + `migration_source_id` already
  in `user_bets`): **0**.
- **Secondary high-confidence matches** (same `user_id` + sorted `leg_ids` +
  same `placed_at` to the minute + same `combined_odds` and `stake` when
  present): **0**.
- **Low-confidence** matches deliberately downgraded to `manual_review`
  by design: **not applicable** in the current sample.

The current live user set has **zero user_id overlap** between `user_bets`
and the eligible `p_*` subset of `parlay_history` (confirmed earlier in
Step 1 audit §2). No collisions can occur on the current data.

---

## 7. Payout / profit-loss review

All 4 eligible rows have deterministic settlement meaning already
captured in the legacy source:

| Row | Legacy `payout` | Legacy `profit_loss` | Verdict |
|---|:-:|:-:|---|
| `p_4b0c7225f2fe81` (won) | present | — | preserve as-is via mapper |
| `p_e1af69205d77aa` (lost) | (n/a) | mapper computes `-stake` | preserve |
| `p_e7a0f677d0298d` (lost) | (n/a) | mapper computes `-stake` | preserve |
| `p_fbd8fdb05daf5b` (lost) | (n/a) | mapper computes `-stake` | preserve |

Rule enforcement:
- **No row required the American-odds formula fallback** (would have
  triggered `manual_review` under Step 4 policy).
- **No won row lacked `payout`** in the live sample.
- **`payout=null` is never treated as zero** — tests
  `test_payout_null_is_never_treated_as_zero` +
  `test_won_payout_null_becomes_manual_review` enforce this.

---

## 8. Leg identity coverage

Across 19 total legs in the 4 eligible rows:

| Field | Coverage | Notes |
|---|---|---|
| `prediction_id` (per-leg) | **19/19** | mapped from `legs[i].pick_id` (canonical alias) |
| `original_odds`           | **19/19** | preserved from `legs[i].book_odds` |
| `sport_key`               | 19/19 (from legs[i].sport) | preserved verbatim |
| `snapshot_id`             | 0/19 | never captured on legacy legs — expected |
| `market_contract_id`      | 0/19 | not part of legacy schema — Phase 3D wiring belongs to a future step |
| `exact_line`              | 0/19 | legacy legs carry `book_odds` but not `line` — expected |
| `event_id`                | 0/19 | derivable at read time from `pick_id → picks.event_id` |
| `settled_at` (per-leg)    | 0/19 | never captured on legacy legs |

**No leg identity was invented.** Missing fields are `None`.

---

## 9. Zero-write verification

Collection counts before and after the dry-run were identical:

| Collection | Before | After | Delta |
|---|---:|---:|---:|
| `user_bets` | 2 | 2 | 0 |
| `parlay_history` | 194 | 194 | 0 |
| `prediction_snapshots` | 14710 | 14710 | 0 |
| `settlement_events` | 0 | 0 | 0 |

Report flags:
- `zero_write_verified: true`
- `forbidden_mutations: []`
- `production_execute_blocked: true`

Enforced by tests:
- `test_dry_run_performs_zero_writes` — before/after counts identical.
- `test_prediction_snapshots_never_changed`.
- `test_settlement_events_never_changed`.
- `test_dry_run_is_default`.
- `test_execute_flag_is_rejected`.

---

## 10. Test commands and results

```bash
cd /app/backend

# 20 new Step 4 tests
python3 -m pytest tests/test_iter133_legacy_parlay_backfill.py -v
# → 20 passed in 0.69s

# All Phase 3 tests (3A-3K + Steps 2, 3, 4)
python3 -m pytest tests/test_iter12*.py tests/test_iter13*.py -q
# → 231 passed in 4.95s

# Live dry-run (with the report file)
python3 -m scripts.backfills.migrate_parlay_history_p_to_user_bets \
   --report-path /app/PHASE3G_STEP4_DRY_RUN_REPORT.json --verbose
# → eligible_p_star=4  migration_ready=4  excluded_plearn=190
# → zero_write_verified=true  forbidden_mutations=[]
# → index_preflight_ok=true

# --execute refusal check
python3 -m scripts.backfills.migrate_parlay_history_p_to_user_bets --execute
# → exit code 2, message "Phase 3G Step 4: --execute is HARD-DISABLED..."
```

All 19 Step 4 invariants covered (#18 asserted via the full Phase 3 suite
staying green).

---

## 11. Production-execution blockers

Step 4 explicitly blocks production execution. Before advancing to
Step 5, all of the below must be confirmed by the user:

- ⚠️ **B4.1** Approval of the American-odds formula for the payout fallback
  (currently not exercised because all 4 rows have complete settlement data).
- ⚠️ **B4.2** Approval to promote the four Step 2 partial-unique indexes
  from `critical=False` → `critical=True` in Step 5. Preflight has been
  clean for the full dry-run.
- ⚠️ **B4.3** Confirmation on whether the Step 5 script should also handle
  `push` legacy rows differently from `won/lost` (none present in the
  sample, but architectural).
- ⚠️ **B4.4** Confirmation on backfill scope order — Step 5 will offer
  `--user-id` filtering for staged rollout.
- ⚠️ **B4.5** Any concerns about the 0/19 coverage for `snapshot_id` /
  `market_contract_id` / `exact_line` — those fields remain `None` on the
  migrated rows and are surfaced via `clv_status="unavailable"` and the
  admin diagnostics endpoint.

None of the above blocks Step 4 completion; they gate Step 5.

---

## 12. Recommended Step 5 scope

**Step 5 — Legacy `p_*` backfill EXECUTE (in a staging window).**

Recommended architecture:
- New CLI flag `--allow-execute` OR a companion executor script; the
  Step 4 script itself remains dry-run-only forever.
- Preflight requirements (pre-`--execute`):
  1. `migration_source + migration_source_id` unique-partial index MUST
     be present and match registry (already verified in Step 4).
  2. Ledger preflight must be clean (0 duplicates).
  3. No `manual_review` rows in the fresh dry-run (all 4 currently pass).
- Execution:
  - Batched upserts filtered by `{migration_source, migration_source_id}`;
    idempotent by construction.
  - Progress log per batch; resumable via `--resume-from`.
  - Emits a companion `PHASE3G_STEP5_EXECUTE_REPORT.md/.json`.
- Post-execute verification:
  - Rerun the Step 4 dry-run — the 4 rows must reclassify as
    `duplicate_existing` (primary match).
  - Confirm `parlay_history` count unchanged.
  - Confirm `prediction_snapshots` / `settlement_events` unchanged.
  - Route response parity across `/api/parlay/history`, `/api/parlay/{id}`,
    `/api/user/bets`, `/api/user/analytics/*`.
- Companion tests: `test_iter134_legacy_backfill_execute.py`.

**Live `--execute` is NOT part of Step 5's implementation itself** — it
belongs to a subsequent step after the user reviews the Step 5 diff.

---

## 13. Suggested Git commit message

```
Phase 3G Step 4 — legacy p_* → user_bets DRY-RUN

- Add scripts/backfills/migrate_parlay_history_p_to_user_bets.py:
  dry-run-only analysis. --execute is HARD-DISABLED (exit code 2).
  Uses services.user_bet_ledger.map_legacy_user_parlay (pure Step 2
  mapper). Classifies every parlay_history row into one of seven
  buckets: migration_ready, duplicate_existing, manual_review,
  unsafe, excluded_learning, excluded_missing_user,
  excluded_invalid_structure. Duplicate detection via primary
  (migration_source + migration_source_id) and high-confidence
  secondary (same user + sorted legs + placed_at + combined_odds +
  stake). Never dedupes by display text alone. Payout=null is never
  treated as zero. Voids remain distinct from pushes. Uses Phase 3B
  shared DB lifecycle.

- Add tests/test_iter133_legacy_parlay_backfill.py: 20 tests covering
  all 19 Step 4 invariants including --execute refusal, zero writes,
  plearn_* exclusion, missing-user-id exclusion, deterministic
  classification, void/push distinction, payout-null handling,
  duplicate detection, missing/conflicting migration index handling,
  and prediction_snapshots + settlement_events immutability.

- Add PHASE3G_STEP4_DRY_RUN_REPORT.md and .json with live dry-run
  results: 4 eligible p_* → all 4 migration_ready; 190 excluded
  plearn_*; 19/19 prediction_id + original_odds leg coverage; zero
  writes; index preflight OK.

Guardrails held: no dual-write, no writer/reader/settlement cutover,
no historical migration executed, no writes to any collection, no
independent Mongo client, no route changes.
All 231 Phase 3 tests pass.
```

---

## 14. Rollback instructions

Because Step 4 introduced only new files and performed zero writes:

**Code rollback**:
```bash
rm /app/backend/scripts/backfills/migrate_parlay_history_p_to_user_bets.py
rm /app/backend/tests/test_iter133_legacy_parlay_backfill.py
rm /app/PHASE3G_STEP4_DRY_RUN_REPORT.md
rm /app/PHASE3G_STEP4_DRY_RUN_REPORT.json
sudo supervisorctl restart backend
```

**Data rollback**: **not applicable** — no writes were performed.
The DB is byte-identical to its pre-Step-4 state.

Backend restart is only required as a defensive hygiene step; the
Step 4 files are never imported by the live backend process.

---

**Step 4 stops here.** Waiting for review before proceeding to Step 5
(execute the backfill in a controlled window) or any other work.

Not proceeding automatically to writer cutover, dual-write, historical
migration, reader cutover, settlement cutover, index promotion, or any
downstream Phase 3G/3H/4 work.
