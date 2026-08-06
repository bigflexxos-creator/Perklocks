# Phase 3G — Step 7 Parity Baseline

**Status:** COMPLETE — reader, settlement, and analytics cutover verified
**Scope:** Migrate user wager history reads, wager settlement, and per-user
analytics **strictly** to the canonical `user_bets` ledger. Permanently
sunset the `parlay_history` compatibility mirror. Preserve every
frontend response envelope byte-for-byte.

**Guardrails carried from prior steps:**
- ✅ `parlay_history` collection is NEVER deleted or mutated.
- ✅ `plearn_*` learning-loop rows are NEVER touched.
- ✅ No new unique indexes promoted to critical.
- ✅ No frontend response schema changed.
- ✅ Test count grew: 122 → **147 passing Phase 3G tests** (+25 new).

---

## 1. Reader cutover

### Before (pre-Step-7)
`GET /api/parlay/history` invoked `parlay_history.list_history(db, user_id, ...)`
which read directly from the `parlay_history` collection.
Rows written pre-Step-6 lived there; the Step-6 mirror duplicated
canonical writes into that collection so the reader kept working.

### After (Step 7)
`GET /api/parlay/history` now invokes
`services.user_bet_ledger.list_parlays_history_shape(db, user_id, …)`
which reads from `user_bets` exclusively and serializes each row via
`serialize_parlay_history_row(UserBet)` back into the exact legacy
response envelope:

| Field            | Sourced from (canonical)                        | Legacy compatibility |
|------------------|-------------------------------------------------|----------------------|
| `id`             | `migration_source_id` else `user_bet_id`        | `p_*` id preserved for migrated rows |
| `user_id`        | canonical `user_id`                             | verbatim             |
| `created_at`     | canonical `created_at` → ISO string             | verbatim             |
| `mode`           | canonical `mode`                                | verbatim             |
| `leg_ids`        | `legs[i].prediction_id`                         | verbatim             |
| `legs`           | canonical `legs[]` → legacy shape               | key-parity          |
| `combined_odds`  | canonical `combined_odds`                       | verbatim             |
| `stake`          | canonical `stake_amount`                        | verbatim             |
| `status`         | canonical → legacy vocab (`pending→live`, `pushed→push`) | verbatim |
| `legs_won/lost/pending` | derived from per-leg `status`            | verbatim             |
| `settled_at`     | canonical `settled_at` → ISO                    | verbatim             |
| `payout`         | canonical `actual_payout`                       | verbatim, NEVER invented |
| `cashout_estimate` | `null` (matches pre-Step-7 for non-live rows) | verbatim             |
| `user_bet_id`    | new canonical id                                | additive-only field  |

**Result invariants:**
- `plearn_*` learning-loop rows CANNOT appear in the response
  (we never query `parlay_history`).
- Migrated legacy `p_*` rows appear exactly once, keyed by their
  original `p_` id via `migration_source_id`.
- User-authorization boundary preserved — `user_id` scoping is enforced
  at the canonical read layer.

**Static invariant tests:** `test_parlay_history_route_reads_from_ledger`,
`test_ledger_exports_serializer_and_reader`.

**Runtime invariant tests:**
- `test_parlay_history_reads_canonical_and_preserves_envelope`
- `test_migrated_parlay_shows_once_and_plearn_never_appears`
- `test_status_filter_semantics_preserved` (`all|won|live|lost`)
- `test_user_authorization_boundary_preserved`
- `test_serializer_preserves_null_clv_and_line`
- `test_push_stays_distinct_from_void_in_serializer`

---

## 2. Mirror-write sunset

### Before (Step 6)
`POST /api/parlay/save` performed:
1. Canonical write via `UBL.create_parlay(...)`.
2. Compatibility mirror insert into `parlay_history` with
   `source="user_bet_ledger_mirror"`, `user_bet_id`, `mirrored_at`.
3. Returned the mirrored `parlay_history` row.

### After (Step 7)
`POST /api/parlay/save` now performs:
1. Canonical write via `UBL.create_parlay(...)` — only.
2. Idempotent stamping of legacy alias fields (`pick_id`, `bet_type`,
   `parlay_legs`, `stake_units`, `odds_at_bet`, `pnl_units`, `sport`,
   `market`, `event`, `selection`, `id`) directly on the canonical
   `user_bets` row. Uses `$exists:false OR :null`-guarded updates so
   later mutations by settlers/admins are never clobbered.
3. Returns the canonical row serialized via
   `UBL.serialize_parlay_history_row(bet)`.

The `parlay_history` collection is **never written to** by this route
again. Existing legacy `p_*` rows and pre-Step-7 mirror rows in
`parlay_history` remain untouched (audit-preserved). Learning-loop
`plearn_*` rows are untouched (`parlay_learning.record_parlay_shown`
still writes them directly, no change).

**Runtime invariant tests:**
- `test_new_parlay_save_no_longer_inserts_into_parlay_history`
- `test_existing_parlay_history_rows_unchanged`
- `test_client_bet_id_idempotency_still_works_post_cutover`
- `test_parlay_save_stamps_legacy_aliases_for_analytics_parity`
- Static: `test_parlay_save_mirror_is_sunset` (asserts
  `"user_bet_ledger_mirror"` and `"await save_parlay("` absent from
  the route source).

Updated Step 6 tests (`test_iter135_writer_cutover.py`) to reflect
the new Step 7 invariants:
- `test_parlay_save_route_imports_user_bet_ledger` — now asserts
  `"user_bet_ledger_mirror"` is **absent**.
- `test_parlay_save_mirror_is_idempotent` — now asserts **zero** mirror
  rows are ever inserted (canonical idempotency by `client_bet_id`
  remains, verified by ledger row count).

---

## 3. Settlement cutover

### Before (Step 6)
- `propagate_pick_settlement(pick_id, status)` matched
  `user_bets` parlay rows via legacy alias fields (`parlay_legs`,
  `bet_type='parlay'`). Rows without these aliases (canonical-only,
  e.g. migrated `p_*` rows) were invisible to the settler.
- `resolve_saved_parlays(db)` walked `parlay_history` for pending
  rows. Post-Step-7 no new user rows land there, so this loop is now
  effectively a no-op for post-cutover data.

### After (Step 7)
Two settlement paths run side-by-side. Both operate on the canonical
`user_bets` collection; neither mutates `parlay_history`.

1. **Event-driven settlement — `propagate_pick_settlement`
   (extended).** In addition to the legacy alias branch, it now runs
   a **canonical-shape branch**:
   ```
   db.user_bets.find({
       "wager_type":         "parlay",
       "legs.prediction_id": pick_id,
       "status":             "pending",
   })
   ```
   Rolls up per the standard grading rules (any leg lost → parlay
   lost; all won → parlay won; won+push mix → parlay won; otherwise
   skip). Updates canonical fields (`status`, `settled_at`,
   `updated_at`, `profit_loss`, `actual_payout`) AND legacy alias
   `pnl_units` for analytics parity. Per-leg statuses are stamped
   canonically (`legs.{i}.status`). Deduplication by `user_bet_id`
   guarantees rows carrying BOTH shapes are settled exactly once.

2. **Periodic canonical resolver —
   `services.user_bet_ledger.resolve_pending_parlays_canonical(db)`.**
   Runs alongside the existing `parlay_history.resolve_saved_parlays`
   in the `server.py` settlement loop. Walks canonical parlays with
   `status='pending'`, resolves each based on `picks.status`, and
   settles via `settle_bet(...)` so every roll-up appends a
   `settlement_events` audit entry.

**Terminal migrated `p_*` rows are never revisited** — the
`status='pending'` filter excludes them by construction.

**Runtime invariant tests:**
- `test_canonical_resolver_settles_all_legs_won`
- `test_canonical_resolver_settles_one_leg_lost`
- `test_canonical_resolver_skips_pending_leg`
- `test_canonical_resolver_never_touches_terminal_migrated_rows`
- `test_propagate_pick_settlement_canonical_parlay_win`
  (verifies two-leg pattern: partial legs pending → skip; last leg
  settled → parlay resolves)
- `test_propagate_pick_settlement_canonical_parlay_loss_short_circuit`

Existing tests preserved:
- `test_parlay_resolver_time_guard.py` (3/3 pass)
- `test_parlay_external_settle_iter31.py` (all pass)

---

## 4. Analytics cutover

### Before (Step 6)
`/api/user/analytics/*` endpoints iterated `db.user_bets` and read
legacy alias fields only:
- `stake_units`, `pnl_units`, `sport`, `market`, `bet_type`, raw
  `status`.

**Consequence:** the 4 migrated legacy `p_*` rows (canonical-only —
they carry `stake_amount`/`profit_loss`/`sport_key`/`wager_type`)
contributed **zero** to every per-user aggregate — silently.

### After (Step 7)
Four private helpers in `routes/user_bets_routes.py` normalise reads
without changing the response schema:

| Helper                   | Source order                                           |
|--------------------------|--------------------------------------------------------|
| `_bet_stake(b)`          | `stake_units` → `stake_amount` → `0.0`                |
| `_bet_pnl(b)`            | `pnl_units`   → `profit_loss` → `0.0`                 |
| `_bet_sport(b)`          | `sport`       → `sport_key`   → `"Unknown"`           |
| `_bet_market(b)`         | `market`      → synth `"<n>-leg parlay"` from legs    |
| `_bet_legacy_status(b)`  | canonical → legacy vocab map (`pushed→push`, etc.)    |

The `by-sport` and `by-market` breakdowns now use the helpers to
build bucket keys. The summary endpoint sums via helpers. The history
endpoint's `status_filter` query expands to include canonical
variants (`push→{push,pushed,void}`, `pending→{pending,partially_settled,cancelled}`).
**Response schemas are byte-for-byte preserved** — same JSON keys,
same rounding, same sort order.

**Runtime invariant tests:**
- `test_analytics_summary_is_canonical_aware_for_migrated_rows`
- `test_analytics_by_sport_falls_back_to_sport_key`
- `test_analytics_by_market_synthesizes_parlay_bucket`

Regression coverage preserved by
`test_refactor_phase2a_analytics.py` (existing suite still passes).

---

## 5. Files changed

| File | Change |
|------|--------|
| `backend/routes/parlay_history_routes.py` | Reader → canonical; mirror sunset; legacy-alias stamping for analytics parity. |
| `backend/routes/user_bets_routes.py`      | Analytics endpoints canonical-aware; `propagate_pick_settlement` extended with canonical-shape parlay branch. |
| `backend/services/user_bet_ledger.py`     | Added `resolve_pending_parlays_canonical(db)` + `__all__` export. |
| `backend/server.py`                       | Added canonical resolver call in the settlement loop. |
| `backend/tests/test_iter135_writer_cutover.py` | Updated 2 Step-6-specific tests to reflect Step 7 (mirror sunset). |
| `backend/tests/test_iter136_reader_settlement_cutover.py` | 25 new tests covering reader + settlement + analytics. |

**Files NOT changed (guardrails):**
- `backend/parlay_history.py` — the legacy resolver + saver module is
  frozen. It still exists so pre-Step-7 mirror rows continue to be
  covered.
- `backend/services/index_registry.py` — no index promotions.
- `backend/parlay_learning.py` — `plearn_*` writing/settling untouched.
- Frontend — no schema changes.

---

## 6. Test summary

```
tests/test_iter131_user_bet_ledger.py .................... 20 passed
tests/test_iter132_user_bets_schema_extension.py .......... 26 passed
tests/test_iter133_legacy_parlay_backfill.py .............. 22 passed
tests/test_iter134_legacy_parlay_execute.py ............... 23 passed
tests/test_iter135_writer_cutover.py ...................... 31 passed
tests/test_iter136_reader_settlement_cutover.py ........... 25 passed
                                                       total: 147 passed
```

No regressions detected in adjacent suites:
- `test_parlay_resolver_time_guard.py`: 3/3 pass
- `test_parlay_external_settle_iter31.py`: pass
- `test_iter99_parlay_intelligence.py`: pass
- `test_refactor_phase2a_analytics.py`: pass

Pre-existing failing tests (unrelated to Step 7 — verified against
`git stash` baseline):
- `test_admin_dashboard.py::TestApiUsageTracker::test_three_api_calls_increment_counter`
- `test_parlay_overhaul_review.py` (data/event-loop dependent)

---

## 7. Rollback plan

Step 7 is 100 % code-only — no data migration was performed and no
indexes were mutated. A revert of the six edited files restores the
pre-Step-7 behaviour immediately with zero data cleanup. Migrated
`p_*` rows in `user_bets` (from Step 5) stay intact and would remain
readable via the legacy `parlay_history` path as before.

---

## 8. Phase 3G — Global closeout

| Step | Scope                                            | Status |
|------|--------------------------------------------------|--------|
| 1    | Wager Ledger Audit                               | DONE |
| 2    | Canonical User Bet Ledger Foundation             | DONE |
| 3    | `user_bets` Schema Extension                     | DONE |
| 4    | Legacy `p_*` Backfill Dry-Run                    | DONE |
| 5    | Controlled Legacy `p_*` Migration Execution      | DONE |
| 6    | Canonical Writer Cutover                         | DONE |
| 7    | Reader + Settlement + Analytics Cutover          | **DONE** |

**Phase 3G is COMPLETE.**

The `user_bets` collection is the canonical wager ledger for the app:
- ✅ Writers route exclusively through `UserBetLedger`.
- ✅ Readers source exclusively from `user_bets`.
- ✅ Settlement operates exclusively on `user_bets`.
- ✅ Analytics source exclusively from `user_bets`.
- ✅ `parlay_history` is a frozen archive — no code path writes to it
  for user wagers.
- ✅ `plearn_*` learning-loop rows are strictly excluded from every
  user-wager path.

Ready to proceed to **Phase 3H** — dead-code + collection deletion —
upon user authorisation.
