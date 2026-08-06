# Phase 3G — Step 7 Execution Report

**Companion of:** `PHASE3G_STEP7_PARITY_BASELINE.md`
**Status:** COMPLETE — reader + settlement + analytics cutover shipped

---

## 1. Guardrails held

- ✅ `parlay_history` collection: **zero writes** by Step 7 code paths;
      **zero deletes** from `parlay_history`.
- ✅ `plearn_*` learning-loop rows: **never touched** by any read,
      write, resolver, or serializer added in Step 7.
- ✅ `services/index_registry.py`: **not modified**. No new critical
      indexes promoted (asserted by
      `test_no_new_critical_indexes_promoted_in_step_7`).
- ✅ `prediction_snapshots`, `settlement_events`, `picks`: **not
      modified** by Step 7 code (only *read* to grade parlays).
- ✅ Frontend response schemas: **unchanged**. Reader envelope and
      analytics envelope byte-parity verified.
- ✅ Migrated `p_*` rows (Step 5): **untouched** post-Step-7.
      Terminal-status filter guarantees canonical resolver skips them.

## 2. Files created

| File                                                  | Purpose |
|-------------------------------------------------------|---------|
| `PHASE3G_STEP7_PARITY_BASELINE.md`                    | Human-readable cutover baseline. |
| `PHASE3G_STEP7_EXECUTION_REPORT.md`                   | This file. |
| `backend/tests/test_iter136_reader_settlement_cutover.py` | 25 tests covering reader + settlement + analytics cutover. |

## 2b. Files changed

| File                                                  | Delta |
|-------------------------------------------------------|-------|
| `backend/routes/parlay_history_routes.py`             | Reader flip, mirror sunset, legacy-alias stamping. |
| `backend/routes/user_bets_routes.py`                  | Canonical parlay branch in `propagate_pick_settlement`; analytics helpers `_bet_stake`/`_bet_pnl`/`_bet_sport`/`_bet_market`/`_bet_legacy_status`; history status_filter expanded. |
| `backend/services/user_bet_ledger.py`                 | New `resolve_pending_parlays_canonical(db)`; `__all__` export. |
| `backend/server.py`                                   | Canonical resolver hook in the settlement loop. |
| `backend/tests/test_iter135_writer_cutover.py`        | 2 Step-6-invariant tests updated to reflect Step 7 (mirror sunset). |

## 3. Test results

```
tests/test_iter131_user_bet_ledger.py                20 passed
tests/test_iter132_user_bets_schema_extension.py     26 passed
tests/test_iter133_legacy_parlay_backfill.py         22 passed
tests/test_iter134_legacy_parlay_execute.py          23 passed
tests/test_iter135_writer_cutover.py                 31 passed
tests/test_iter136_reader_settlement_cutover.py      25 passed
─────────────────────────────────────────────────────────────
Total Phase 3G suite                                147 passed
```

Adjacent regression coverage:
```
tests/test_parlay_resolver_time_guard.py              3 passed
tests/test_parlay_external_settle_iter31.py           pass
tests/test_iter99_parlay_intelligence.py              pass
tests/test_refactor_phase2a_analytics.py              pass
```

Pre-existing failures (unrelated to Step 7 — confirmed via git stash
baseline):
- `test_admin_dashboard.py::TestApiUsageTracker::test_three_api_calls_increment_counter`
- `test_parlay_overhaul_review.py` (data-availability/event-loop
   dependent)

## 4. Runtime verification

Backend restarted successfully after edits:

```
sudo supervisorctl restart backend
backend: stopped
backend: started

GET /api/health → {"status":"ok","ts":"2026-08-06T19:21:45.011148+00:00"}
```

Startup logs show every scheduled loop is armed and no import errors.

## 5. Behavioural invariants (asserted by tests)

| # | Invariant                                                                  | Test                                                                 |
|---|----------------------------------------------------------------------------|----------------------------------------------------------------------|
| 1 | Reader queries ONLY `user_bets`, never `parlay_history`                    | `test_parlay_history_route_reads_from_ledger`                        |
| 2 | Response envelope keys unchanged                                           | `test_parlay_history_reads_canonical_and_preserves_envelope`         |
| 3 | Migrated `p_*` rows appear exactly once, keyed by legacy id                | `test_migrated_parlay_shows_once_and_plearn_never_appears`           |
| 4 | `plearn_*` rows never appear in the reader                                 | `test_migrated_parlay_shows_once_and_plearn_never_appears`           |
| 5 | Status filter (`all`, `won`, `live`, `lost`) semantics preserved           | `test_status_filter_semantics_preserved`                             |
| 6 | User-scoping boundary enforced                                             | `test_user_authorization_boundary_preserved`                         |
| 7 | New `parlay_save` inserts ZERO rows in `parlay_history`                    | `test_new_parlay_save_no_longer_inserts_into_parlay_history`         |
| 8 | Existing `parlay_history` rows (including `plearn_*`) unchanged            | `test_existing_parlay_history_rows_unchanged`                        |
| 9 | `client_bet_id` idempotency preserved after cutover                        | `test_client_bet_id_idempotency_still_works_post_cutover`            |
| 10| Serializer preserves null CLV / null line                                  | `test_serializer_preserves_null_clv_and_line`                        |
| 11| `void` never coerced to `push` in serializer                               | `test_push_stays_distinct_from_void_in_serializer`                   |
| 12| Canonical resolver: all-won parlay settled with correct payout             | `test_canonical_resolver_settles_all_legs_won`                       |
| 13| Canonical resolver: one leg lost → parlay lost, PnL = −stake               | `test_canonical_resolver_settles_one_leg_lost`                       |
| 14| Canonical resolver: any pending leg → skip                                 | `test_canonical_resolver_skips_pending_leg`                          |
| 15| Terminal migrated rows never revisited                                     | `test_canonical_resolver_never_touches_terminal_migrated_rows`       |
| 16| `propagate_pick_settlement` handles canonical parlays end-to-end           | `test_propagate_pick_settlement_canonical_parlay_win`                |
| 17| Loss short-circuit works for canonical parlays                             | `test_propagate_pick_settlement_canonical_parlay_loss_short_circuit` |
| 18| `parlay_save` writes legacy alias fields onto canonical row                | `test_parlay_save_stamps_legacy_aliases_for_analytics_parity`        |
| 19| Analytics summary is canonical-aware for migrated rows                     | `test_analytics_summary_is_canonical_aware_for_migrated_rows`        |
| 20| Analytics by-sport falls back to `sport_key`                               | `test_analytics_by_sport_falls_back_to_sport_key`                    |
| 21| Analytics by-market synthesises `<n>-leg parlay` bucket                    | `test_analytics_by_market_synthesizes_parlay_bucket`                 |
| 22| No new critical indexes promoted in Step 7                                 | `test_no_new_critical_indexes_promoted_in_step_7`                    |
| 23| Ledger exports the canonical resolver                                      | `test_ledger_exports_resolver`                                       |
| 24| Server calls canonical resolver alongside legacy                           | `test_server_calls_canonical_resolver_alongside_legacy`              |
| 25| Serializer + reader are exported public API                                | `test_ledger_exports_serializer_and_reader`                          |

## 6. Rollback

Step 7 is 100 % code-only. Revert the six edited files; no data
migration or index change to unwind. Migrated `p_*` rows (from Step 5)
remain in `user_bets`; `parlay_history` archive rows remain intact.

## 7. Phase 3G status

**Phase 3G is COMPLETE.** All 7 sub-steps shipped. The `user_bets`
collection is the app's single canonical wager ledger. `parlay_history`
is a frozen archive whose only remaining consumer is the legacy resolver
loop (belt-and-braces coverage for any pre-Step-6 mirror rows still
there). Ready for Phase 3H (dead-code and collection deletion) upon
user authorisation.
