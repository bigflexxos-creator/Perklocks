# Phase 3G — Step 6 Canonical Writer Cutover Report

**Status:** COMPLETE
**Companion of:** `PHASE3G_STEP6_WRITER_AUDIT.md`
**Scope:** Route all new user-owned wager writes through
`services.user_bet_ledger` while preserving request/response envelopes.
Add optional `client_bet_id`. Keep parlay_history compatible via a
mirror. Do not touch settlement, readers, analytics, or learning writers.

---

## 1. Guardrails held

- ✅ No route changes to readers, settlement, or analytics.
- ✅ No prediction / pick-scoring / Magic-Tier / simulator changes.
- ✅ Learning writers (`plearn_*`) untouched. Verified statically +
  runtime.
- ✅ Existing request schemas remain accepted (`client_bet_id` is
  optional; old clients unchanged).
- ✅ Existing response envelopes preserved (all legacy keys present in
  the direct doc; FastAPI `response_model=TrackedBet` further filters
  to the pre-Step 6 field set for the frontend).
- ✅ Existing native + migrated `user_bets` rows untouched by track
  calls.
- ✅ `parlay_history` learning rows untouched.
- ✅ `prediction_snapshots` untouched.
- ✅ Partial-unique indexes remain `critical=False`.

## 2. Files created

| File | Purpose |
|------|---------|
| `backend/tests/test_iter135_writer_cutover.py` | 18 tests (all Step 6 invariants) |
| `PHASE3G_STEP6_WRITER_AUDIT.md` | Writer inventory + classification |
| `PHASE3G_STEP6_CUTOVER_REPORT.md` | This report |

## 3. Files changed

| File | Change |
|------|--------|
| `backend/routes/user_bets_routes.py` | `POST /api/user/bets/track` now routes through `UserBetLedger.create_bet` / `create_parlay`. Adds optional `client_bet_id` field. Stamps legacy alias fields via per-field `$exists:false OR null` guards (idempotent). |
| `backend/routes/parlay_history_routes.py` | `POST /api/parlay/save` now performs canonical write via `UserBetLedger.create_parlay` FIRST, then the legacy `parlay_history.save_parlay` compatibility mirror. Annotates the mirror row with `source="user_bet_ledger_mirror"`, `user_bet_id`, `mirrored_at`. Adds optional `client_bet_id`. |

## 4. Writer inventory (summary from audit)

- 2 writers converted: `track_bet`, `parlay_save`.
- 1 compatibility mirror: `parlay_history` for user-owned parlays.
- 3 learning/system writers untouched.
- 3 settlement writers deferred to Step 7.
- 2 delete-only routes unchanged.

## 5. Request compatibility

| Field | Straight bet | Parlay save |
|---|---|---|
| Existing fields | preserved verbatim | preserved verbatim |
| `client_bet_id` (new, optional) | accepted | accepted |
| Old clients that omit `client_bet_id` | fall back to server-computed `idempotency_key` | fall back to server-computed key |

## 6. Response compatibility

- `POST /api/user/bets/track`: returns `TrackedBet` with all legacy keys (`id`, `user_id`, `pick_id`, `bet_type`, `parlay_legs`, `stake_units`, `odds_at_bet`, `status`, `pnl_units`, `sport`, `market`, `event`, `selection`, `created_at`, `settled_at`, `notes`). FastAPI `response_model` filters to the pre-Step 6 shape.
- `POST /api/parlay/save`: returns the mirror `parlay_history` row unchanged. Keys `{id, user_id, created_at, mode, leg_ids, legs, combined_odds, stake, status, legs_won, legs_lost, legs_pending, settled_at, payout, cashout_estimate}` all present.

## 7. Idempotency implementation

- Primary: `(user_id, client_bet_id)` → `_find_by_idempotency` on the ledger.
- Fallback: `UBL.compute_idempotency_key(req)` — SHA-256 over user_id, wager_type, sorted stable leg identities, exact odds, sportsbook, placed_at (bucketed to minute). Never uses display text.
- User-scoped: different users may reuse the same `client_bet_id` without collision (verified by `test_client_bet_id_is_user_scoped`).

## 8. Concurrency behavior

Under 5 concurrent identical `track_bet` calls:
- Partial-unique index on `(user_id, client_bet_id)` (present via Phase 3C registry + `ensure_all_indexes`) causes duplicate-key errors on N-1 of them.
- Ledger's `insert_one` catches the race, re-reads the winner, and returns it.
- All 5 responses share the same `id`.
- Verified by `test_concurrent_duplicate_track_creates_one_wager`.

## 9. Compatibility mirror

Decision: **required for parlays only** (parlay_history reader still active). Design:
- Canonical write to `user_bets` via `UserBetLedger.create_parlay` first.
- Compatibility mirror to `parlay_history` via legacy `save_parlay` (unchanged) second.
- Mirror row stamped post-insert with `source="user_bet_ledger_mirror"`, `user_bet_id=<canonical>`, `mirrored_at=<utc>`.
- Idempotent — mirror rerun sees the same deterministic `p_*` id.
- **Straight bets are NOT mirrored** — no legacy reader requires this.
- **Learning rows are NEVER mirrored** — verified by `test_learning_rows_unchanged_after_writes`.
- Exit plan: mirror dropped in Step 7 when `/api/parlay/history` reads from `user_bets`.

## 10. Live verification (production `lockscore_db`)

Real HTTP smoke test with the demo admin user:

```
1. GET /api/picks/today?limit=1     → picked hot-b94baa8cca7967f28ee12f90
2. POST /api/user/bets/track {pick_id, stake_units: 0.25, client_bet_id: "live-step6-smoke-1"}
    → id: f5c1e69f-0e40-4bfa-bcff-8c3cebe01f87, status: pending
3. POST /api/user/bets/track (SAME payload)
    → id: f5c1e69f-0e40-4bfa-bcff-8c3cebe01f87  ← SAME  ← IDEMPOTENT ✓
4. DELETE /api/user/bets/{id}  → {"ok": true, "deleted_id": "..."}
```

Backend health post-cutover: `{"status":"ok"}`.

## 11. Test commands and results

```
python3 -m pytest tests/test_iter135_writer_cutover.py -q
    → 18 passed in 11.68s

python3 -m pytest tests/test_iter12*.py tests/test_iter13*.py -q
    → 272 passed in 17.67s   (254 pre-Step 6 + 18 new)
```

Every one of the 20 Step 6 invariants covered:
1–3: static route inspection asserts ledger usage + no direct inserts. 4–5: `client_bet_id` optional, idempotent per user. 6: user-scoped. 7: concurrent 5×, one wager. 8: fallback idempotency. 9-10: distinct lines/odds → distinct wagers (via ledger `compute_idempotency_key`). 11: `plearn_*` never enters. 12–13: request/response schemas stable. 14–15: existing native + migrated rows untouched. 16: learning rows untouched. 17: mirror idempotent + carries `user_bet_id`. 18: prediction_snapshots untouched. 19: settlement code static-verified untouched. 20: all prior Phase 3 tests still passing (272).

## 12. Remaining blockers for Step 7

- **B7.1** Reader cutover: `/api/parlay/history` still reads `parlay_history`. Step 7 introduces the canonical-read path.
- **B7.2** Settlement cutover: `resolve_saved_parlays` still owns user-owned parlay settlement; `propagate_pick_settlement` owns user_bets settlement. Both must be unified via ledger settle helpers.
- **B7.3** Mirror sunset: once reader flips, the parlay_history mirror can be disabled (its purpose is gone).
- **B7.4** Analytics cutover: `/api/user/analytics/*` still reads raw user_bets. No change needed until analytics uses ledger contracts.

## 13. Suggested Git commit message

```
Phase 3G Step 6 — canonical writer cutover

- Convert POST /api/user/bets/track to write through
  UserBetLedger.create_bet / create_parlay. Adds optional
  client_bet_id request field. Stamps legacy alias fields
  (id, pick_id, bet_type, parlay_legs, stake_units, odds_at_bet,
  pnl_units, sport, market, event, selection, notes) onto the
  canonical row via per-field $exists:false-guarded updates so
  response envelope is byte-parity with pre-Step-6 behaviour.

- Convert POST /api/parlay/save to canonical-write via
  UserBetLedger.create_parlay first, then compatibility mirror
  via the existing parlay_history.save_parlay. The mirror row
  is annotated with source="user_bet_ledger_mirror",
  user_bet_id=<canonical>, mirrored_at=<utc>. Idempotent by
  legacy deterministic p_ id + canonical (migration_source,
  migration_source_id) partial-unique. Straight bets and learning
  rows are never mirrored.

- Add tests/test_iter135_writer_cutover.py: 18 tests covering
  all 20 Step 6 invariants: ledger-usage static checks, no direct
  writer inserts, optional client_bet_id request compatibility,
  same-user idempotency, user-scoped client_bet_id, concurrent
  5-way race collapse to one wager (partial-unique index), fallback
  idempotency without client_bet_id, distinct odds -> distinct
  wagers, plearn exclusion, request/response schema preservation,
  native+migrated row immutability, learning-row immutability,
  parlay_history mirror idempotency + markers, and prediction
  snapshot immutability.

- Add PHASE3G_STEP6_WRITER_AUDIT.md classifying every writer.
- Add PHASE3G_STEP6_CUTOVER_REPORT.md summarising the cutover.

Live smoke on lockscore_db: track/retry with same client_bet_id
returns the identical wager id (idempotent); DELETE cleans up.
All 272 Phase 3 tests pass. Backend restart clean.

Guardrails held: no reader cutover, no settlement cutover, no
analytics cutover, no parlay_history deletion, no learning
writer changes, no route response schema changes, no index
promotion to critical.
```

## 14. Rollback instructions

**Code rollback** (reverts both route changes):

```bash
cd /app/backend
git checkout routes/user_bets_routes.py
git checkout routes/parlay_history_routes.py
rm tests/test_iter135_writer_cutover.py
rm /app/PHASE3G_STEP6_WRITER_AUDIT.md
rm /app/PHASE3G_STEP6_CUTOVER_REPORT.md
sudo supervisorctl restart backend
```

**Data cleanup** (only necessary if the mirror produced
compatibility-marker rows in `parlay_history` that you no longer want
after rollback — the marker rows continue to work as normal legacy
rows if left in place, so this is optional):

```
db.parlay_history.updateMany(
  {source: "user_bet_ledger_mirror"},
  {$unset: {source: "", user_bet_id: "", mirrored_at: ""}}
)
```

**No data-loss risk:** every canonical `user_bets` row created by the
Step 6 writer path continues to be valid ledger data. The legacy
`parlay_history` mirror rows continue to work independently.

---

**Step 6 stops here.** Waiting for review before proceeding to Step 7.

Not proceeding automatically to reader cutover, settlement cutover,
analytics cutover, mirror sunset, index promotion, `parlay_history`
deletion, or any Phase 3H / Phase 4 work.
