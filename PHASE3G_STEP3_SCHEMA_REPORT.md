# Phase 3G — Step 3 Schema Extension Report

**Status:** COMPLETE — dry-run + live execute + idempotent re-run all clean
**Companion of:** `PHASE3G_WAGER_LEDGER_AUDIT.md`, `PHASE3G_PARITY_REPORT.md`, `PHASE3G_MIGRATION_PLAN.md`
**Scope:** Extend the two live `user_bets` rows with canonical nullable fields
required by `services/user_bet_ledger.py`. No route changes, no dual-write,
no historical migration, no writer/reader cutover.

---

## 1. Guardrails held

- ✅ No route conversion (verified by `test_no_route_conversion_shipped_in_step_3`).
- ✅ No dual-write.
- ✅ No `parlay_history` writes (verified statically + at runtime).
- ✅ No `prediction_snapshots` writes (verified statically + at runtime).
- ✅ No unique index promotion — Step 2 partial-unique indexes remain
  `critical=False`.
- ✅ Shared Phase 3B DB lifecycle only — no independent Mongo client.
- ✅ No settlement cutover.
- ✅ Existing populated values never overwritten.
- ✅ Never invented data (sportsbook / lines / CLV / IDs / payouts / stakes).

---

## 2. Files created

| File | Purpose |
|------|---------|
| `backend/scripts/backfills/user_bets_add_canonical_fields.py` | Idempotent, resumable, batch-based schema-extension migration (`--dry-run` default). Static forbidden-write guard + runtime forbidden-collection count guard. |
| `backend/tests/test_iter132_user_bets_schema_extension.py` | 19 tests covering all 18 Step 3 invariants (all passing). |
| `PHASE3G_STEP3_SCHEMA_REPORT.md` | This document. |
| `PHASE3G_STEP3_EXECUTE_REPORT.json` | Machine-readable live-execute report (JSON). |

## 2b. Files changed

None. No other file in the repo was modified during Step 3.

---

## 3. Pre-migration audit (live database, `lockscore_db`)

| Metric | Value |
|---|---:|
| Total `user_bets` rows | 2 |
| Rows missing `user_id` | 0 |
| Rows with unknown/legacy status (e.g. `"live"`) | 0 |
| Rows with existing populated values that would conflict with canonical | 0 |
| Rows with ambiguous `wager_type` | 0 |
| Rows requiring manual review | 0 |

Field presence BEFORE the run (canonical fields only):

| Canonical field | Present |
|---|---:|
| user_bet_id, client_bet_id, idempotency_key, wager_type | 0 |
| original_status, stake_amount, combined_odds | 0 |
| profit_loss, sportsbook, source, migration_version | 0 |
| is_legacy, mode, tags, prediction_id, snapshot_id | 0 |
| opening/closing line + odds, clv_value, clv_status | 0 |
| legs, settlement_events | 0 |

Existing stable ids present: 2/2 — `05e6c1a0-…-9333a872436f`,
`2f18de8b-…-6c9f7081f80e`. Both preserved and adopted as canonical
`user_bet_id`.

## 3b. Proposed updates (from dry-run)

- 2 rows would receive the missing canonical fields.
- 0 rows in manual review.
- 0 conflicts.
- No forbidden-collection writes.

---

## 4. Dry-run results

```
mode:              dry-run
total_scanned:     2
total_updated:     2      # would-update count
total_skipped:     0
total_manual_review: 0
forbidden_touched: []
counts_before == counts_after (all three collections unchanged)
```

Zero writes performed (verified by inspecting document contents pre/post
in `test_dry_run_performs_zero_writes`).

---

## 5. Execution results (live)

```
mode:              execute
total_scanned:     2
total_updated:     2
total_skipped:     0
total_manual_review: 0
manual_review_rows: []
conflict_rows:     []
forbidden_touched: []

collection_counts_before: {"user_bets": 2, "parlay_history": 194, "prediction_snapshots": 14710}
collection_counts_after:  {"user_bets": 2, "parlay_history": 194, "prediction_snapshots": 14710}
```

Post-migration sample row (`_id` field elided):

```
id                     = '05e6c1a0-8ed2-4fd1-8237-9333a872436f'
user_bet_id            = '05e6c1a0-8ed2-4fd1-8237-9333a872436f'   ← preserved from legacy `id`
user_id                = '151f530d-72e8-45c1-9a04-20f4110536cc'
bet_type               = 'straight'                                (legacy — preserved)
wager_type             = 'straight'                                (derived from bet_type)
pick_id                = '95026ade-…'                              (legacy — preserved)
prediction_id          = '95026ade-…'                              (canonical alias)
status                 = 'pending'                                 (owned by route — untouched)
original_status        = 'pending'
stake_units            = 1.5                                       (legacy — preserved)
stake_amount           = 1.5                                       (canonical alias)
pnl_units              = 0.0                                       (legacy — preserved)
profit_loss            = 0.0                                       (canonical alias, never recomputed)
clv_value              = None                                      (nullable, never invented)
clv_status             = 'unavailable'
is_legacy              = False                                     (native row)
source                 = 'user_track'                              (derivable — native writer)
migration_version      = 1
migration_source       = None
migration_source_id    = None
tags                   = []
legs                   = []
settlement_events      = []
sportsbook             = None                                      (never invented)
opening_line/closing_line/opening_odds/closing_odds = None
```

---

## 6. Field coverage report (post-execute)

Every canonical field defined in the Step 3 spec is now present on both
rows, except **`combined_odds`** which is intentionally 0/2:

| Field | Coverage | Notes |
|---|---:|---|
| user_bet_id, wager_type, is_legacy, source | 2/2 | derived cleanly |
| client_bet_id, idempotency_key, migration_source, migration_source_id | 2/2 | populated with canonical `null` (both rows are native, no idempotency history) |
| stake_amount, profit_loss, original_status, prediction_id, sport_key | 2/2 | from legacy source fields (stake_units, pnl_units, status, pick_id, sport) |
| potential_payout, actual_payout, sportsbook, snapshot_id, market_contract_id, board_version, event_id | 2/2 | populated with canonical `null` — **never invented** |
| opening_line, opening_odds, closing_line, closing_odds, clv_value | 2/2 | canonical `null` — never invented |
| clv_status | 2/2 | canonical `"unavailable"` |
| tags, legs, settlement_events | 2/2 | canonical `[]` |
| mode, risk_tier, correlation_warning | 2/2 | canonical `null` |
| migration_version | 2/2 | canonical `1` |
| **combined_odds** | **0/2** | **Both rows are `bet_type="straight"`** — combined_odds has no meaning for straight bets, so the script correctly leaves it absent (never invents). Expected behaviour. |

---

## 7. Manual-review records

**0 rows** required manual review in either the dry-run or the live execute
run. No conflicts detected. No rows carry a value that violates the canonical
contract.

If any row required manual review in future runs, its `resume_key` and
manual-review reasons would appear in `report.manual_review_rows` in the
JSON output.

---

## 8. Idempotency verification

Second invocation of `--execute` against the same DB:

```
total_scanned:     2
total_updated:     0
total_skipped:     2
forbidden_touched: []
```

The compound `{$exists:false OR value:null}` filter on every candidate field
prevents any re-write. Rerun is provably a no-op.

Verified by `test_migration_is_idempotent`.

---

## 9. Collection-count comparison

| Collection | Before | After (execute) | After (idempotent re-run) |
|---|---:|---:|---:|
| `user_bets` | 2 | 2 | 2 |
| `parlay_history` | 194 | 194 | 194 |
| `prediction_snapshots` | 14710 | 14710 | 14710 |

Zero delta in every collection except the intentional in-place update on
`user_bets`.

---

## 10. Index preflight result (post-execute)

```
{
  "ok": true,
  "total_user_bets": 2,
  "duplicate_user_bet_id": 0,
  "duplicate_client_bet_id_per_user": 0,
  "duplicate_idempotency_key_per_user": 0,
  "duplicate_migration_source_id": 0,
  "conflicts": []
}
```

All partial-unique indexes remain safe. **No index promotion performed.**
The four partial-unique specs from Step 2 continue to be
`critical=False` — promotion to `critical=True` is explicitly deferred
per Step 3 §Decision #2 until writer cutover has been live for ≥ 7 days.

---

## 11. Route / schema parity result

- `GET /api/user/bets` → shape unchanged (empty list for demo user; response
  keys `{bets: [], count: 0}` intact).
- `GET /api/user/analytics/summary` → shape unchanged
  (`total_bets`, `pending`, `won`, `lost`, `push`, `hit_rate_pct`,
  `units_risked`, `pnl_units`, `roi_pct` — all present).
- `GET /api/user/analytics/by-sport` → shape unchanged (`{rows: []}`).
- `GET /api/parlay/history` → shape unchanged (returned `p_4b0c7225f2fe81`
  with `legs[]` inline snapshot intact).
- `GET /api/admin/users?page=1&page_size=5` → shape unchanged.

Backend startup clean; no import errors after full restart.

Static assertions in `test_no_route_conversion_shipped_in_step_3` verify:
- `routes/user_bets_routes.py` does not import `services.user_bet_ledger`.
- `routes/parlay_history_routes.py` does not import
  `services.user_bet_ledger`.
- Every existing endpoint literal is still present in the route source.

---

## 12. Test commands and results

```bash
cd /app/backend

# 19 new Step 3 tests
python3 -m pytest tests/test_iter132_user_bets_schema_extension.py -v
# → 19 passed in 0.77s

# All Phase 3 tests (3A-3K + Step 2 + Step 3)
python3 -m pytest tests/test_iter12*.py tests/test_iter13*.py -q
# → 211 passed in 4.42s

# Live dry-run
python3 -m scripts.backfills.user_bets_add_canonical_fields --dry-run
# → mode=dry-run  scanned=2  updated=2  skipped=0  manual=0  forbidden=[]

# Live execute
python3 -m scripts.backfills.user_bets_add_canonical_fields --execute \
   --report-path /app/PHASE3G_STEP3_EXECUTE_REPORT.json
# → mode=execute  scanned=2  updated=2  skipped=0  manual=0  forbidden=[]

# Live idempotent re-run
python3 -m scripts.backfills.user_bets_add_canonical_fields --execute
# → mode=execute  scanned=2  updated=0  skipped=2  manual=0  forbidden=[]
```

All 18 invariants required by the Step 3 prompt are covered by tests
(#18 is asserted via the full Phase 3 test-suite run staying green).

---

## 13. Live verification summary

| Check | Result |
|---|---|
| Records scanned | 2 |
| Records updated (execute) | 2 |
| Records skipped | 0 |
| Records requiring manual review | 0 |
| Canonical-field coverage | 33/34 fields at 2/2 (combined_odds intentionally 0/2 — straight-bet-only sample) |
| Idempotent re-run delta | 0 writes, 2 skipped |
| `user_bets` count before/after | 2 / 2 |
| `parlay_history` count before/after | 194 / 194 |
| `prediction_snapshots` count before/after | 14710 / 14710 |
| Route response parity | unchanged |
| Backend health after restart | `{"status":"ok"}` |

---

## 14. Remaining decisions before Step 4

Step 4 will introduce the `--dry-run` **legacy-parlay backfill script**
(`p_*` rows in `parlay_history` → canonical `user_bets` under
`migration_source_id`). Before that, please confirm:

- **D4.1** Should the Step 4 script propose all 4 eligible `p_*` rows for
  backfill in the first `--dry-run`, or should we scope narrower (single
  user first, all pushed/void first, then win/lost)?
- **D4.2** For legacy rows where the `payout` field is null on won parlays
  (edge case not present in dev sample), should the mapper compute
  `profit_loss` from `combined_odds + stake`, or emit a manual-review row?
- **D4.3** Should `--commit` on the Step 4 script also carry a preflight
  check that the new `migration_source + migration_source_id` partial-unique
  index has been created? (Currently the index will be lazily created on
  next `ensure_all_indexes()`; a preflight would make it explicit.)
- **D4.4** Once the legacy backfill is complete, should we auto-run the
  Step 3 schema-extension script on the freshly-inserted rows, or should
  the Step 4 script write canonical fields inline?
  - Recommended: Step 4 writes canonical fields inline (uses
    `user_bet_ledger.map_legacy_user_parlay` + `to_document()`) so a
    separate schema-extension pass is not required.

Nothing above blocks Step 4 planning.

---

## 15. Recommended Step 4 scope

**Step 4 — Legacy `p_*` backfill (dry-run only).**

- New script `backend/scripts/backfills/migrate_parlay_history_p_to_user_bets.py`.
- Reads eligible `p_*` rows from `parlay_history` (guardrailed against
  `plearn_*`).
- Uses `services.user_bet_ledger.map_legacy_user_parlay` (the Step 2 pure
  mapper).
- `--dry-run` default; `--execute` for writes.
- Idempotent via `migration_source + migration_source_id` partial-unique
  index.
- Batch-based + resumable + report-path options (mirrors Step 3 shape).
- Static + runtime guards forbidding writes to `parlay_history` or
  `prediction_snapshots`.
- Companion test file `test_iter133_legacy_backfill.py`:
  1. Eligible `p_*` rows are mapped and inserted.
  2. `plearn_*` rows are rejected (as usual).
  3. Rerun is idempotent (0 additional writes).
  4. `parlay_history` count unchanged.
  5. Migrated rows deserialize cleanly via `UserBet.from_document`.
  6. Migrated row's `migration_source == "parlay_history"`.
  7. Preflight remains clean.
- Estimated LOC: script ~350, tests ~250.
- Live execution is **NOT** part of Step 4. That belongs to a subsequent
  step after user review of the dry-run report.

---

## 16. Suggested Git commit message

```
Phase 3G Step 3 — user_bets canonical schema extension

- Add scripts/backfills/user_bets_add_canonical_fields.py:
  idempotent, dry-run-default, batch-based, resumable schema
  extension for user_bets. Static + runtime forbidden-collection
  guards (parlay_history, prediction_snapshots). Never overwrites
  populated values. Never invents sportsbook/line/odds/CLV/IDs/
  payouts/stake/event identity. Uses Phase 3B shared DB lifecycle.

- Add tests/test_iter132_user_bets_schema_extension.py: 19 tests
  covering all 18 Step 3 invariants (dry-run zero writes, execute
  scope, forbidden-collection immutability, no-overwrite, defaults,
  clv unavailable, void≠pushed, unknown-status preservation,
  native is_legacy=false, idempotency, resume+limit, ledger
  deserialization, preflight clean, no route conversion).

- Add PHASE3G_STEP3_SCHEMA_REPORT.md with pre/post audit, dry-run
  and execute results, field coverage, idempotency proof.

Live results: 2/2 rows updated on first execute, 0/2 on rerun.
Zero delta on parlay_history (194→194) and prediction_snapshots
(14710→14710). Preflight clean. All 211 Phase 3 tests pass.
Route response shapes unchanged. Backend restarts clean.

Guardrails held: no route flip, no dual-write, no historical
p_* migration, no settlement cutover, no index promotion, no
independent Mongo client, no writes to parlay_history or
prediction_snapshots.
```

---

## 17. Rollback instructions

**Data rollback (undo the field additions on the 2 live rows).** Runs
against a fresh admin connection; targets **only** the fields this
migration added.

```
mongo lockscore_db --eval '
db.user_bets.updateMany({}, {$unset: {
  user_bet_id: "", client_bet_id: "", idempotency_key: "", wager_type: "",
  original_status: "", stake_amount: "", combined_odds: "", potential_payout: "",
  actual_payout: "", profit_loss: "", sportsbook: "", source: "",
  migration_version: "", migration_source: "", migration_source_id: "",
  is_legacy: "", mode: "", tags: "", risk_tier: "", correlation_warning: "",
  prediction_id: "", snapshot_id: "", market_contract_id: "", board_version: "",
  event_id: "", sport_key: "", opening_line: "", opening_odds: "",
  closing_line: "", closing_odds: "", clv_value: "", clv_status: "",
  legs: "", settlement_events: ""
}});
'
```

Original legacy fields (`id`, `bet_type`, `stake_units`, `odds_at_bet`,
`pnl_units`, `pick_id`, `sport`, `status`, `event`, `market`, `selection`,
`created_at`, `settled_at`, `notes`, `user_id`, `parlay_legs`) are
untouched by this migration, so they survive the unset.

**Code rollback** (undo the Step 3 diff):

```bash
cd /app/backend
rm scripts/backfills/user_bets_add_canonical_fields.py
rm tests/test_iter132_user_bets_schema_extension.py
rm /app/PHASE3G_STEP3_SCHEMA_REPORT.md
rm /app/PHASE3G_STEP3_EXECUTE_REPORT.json
sudo supervisorctl restart backend
```

Because the ledger has zero production callers, and Step 3 did not
modify any route, service, or index-registry file, removing these
files changes no user-visible behaviour.

**Step 3 stops here.** Waiting for your review before proceeding to
Step 4 (legacy `p_*` backfill dry-run) or any other work.
