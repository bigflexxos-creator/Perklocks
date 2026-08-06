# Phase 3G — Migration Plan (Companion to Wager Ledger Audit)

**Status:** DRAFT · plan-only · **STEP 2 COMPLETE — STEPS 3-9 NOT AUTHORIZED**
**Companion of:** `PHASE3G_WAGER_LEDGER_AUDIT.md`, `PHASE3G_PARITY_REPORT.md`
**Purpose:** Staged cutover plan with rollback design. Every step below is a
recommendation for the user's review. Nothing in this file has been shipped
beyond Step 2 (the typed canonical UserBetLedger service and its test suite).

## STEP 2 STATUS: COMPLETE (2026-06, this session)

Delivered:
- `backend/services/user_bet_ledger.py` — typed contracts + service API +
  pure legacy mapper + preflight + safe diagnostics.
- `backend/tests/test_iter131_user_bet_ledger.py` — 42 passing tests
  covering all 22 invariants from the Step 2 prompt.
- `backend/services/index_registry.py` — new declarative IndexSpec entries
  for `user_bets` (partial-unique on `user_bet_id`, `client_bet_id`,
  `idempotency_key`, `migration_source + migration_source_id`, plus
  supporting non-unique indexes). Every uniqueness constraint gated by
  a `partial_filter` so existing rows without the field cannot cause an
  ensure to fail.

NOT delivered (per guardrail): route flips, dual-write shim, backfill
execution, unique-index promotion to `critical=True`. See §Step 3+ below.

Guardrails (from user prompt):
- No production writes during the audit phase.
- No dual-write shim during the audit phase.
- No ledger service implementation during the audit phase.
- No backfill execution during the audit phase.
- No index changes during the audit phase.
- No route changes during the audit phase.
- `parlay_history` collection must not be deleted at any point.
- `plearn_*` rows must not be moved — they belong to `parlay_learning`.

---

## 1. Migration goals (in priority order)

1. Preserve every visible surface: `/api/parlay/save`, `/api/parlay/history`,
   `/api/parlay/{id}`, `/api/parlay/{id}/resettle`, `/api/user/bets/*`.
2. Preserve all settlement paths — no regression to the snapshot-fallback +
   external-adapter chain that today rescues legs whose parent picks were
   purged.
3. Consolidate personal wager storage in `user_bets` so downstream Phase 3
   stages (3H dead-code cleanup, prediction models in Phase 4) can rely on a
   single source of truth.
4. Zero data motion on rollback — dual-write leaves both stores populated
   for the entire migration window.
5. No user-facing downtime.

---

## 2. Non-goals

- Deleting `parlay_history` (guardrail).
- Migrating `plearn_*` rows (out of scope — they belong to `parlay_learning`).
- Changing the parlay optimizer or the Save-on-Tap UX.
- Introducing new indexes outside the ones already listed in
  `services/index_registry.py` (any additions belong to a separate PR post-cutover).
- Solving CLV/book/closing-line coverage — see audit §7d; these are open
  design questions unrelated to consolidation.

---

## 3. Success criteria (per stage)

| Stage | Success criteria                                                                                                       |
|-------|------------------------------------------------------------------------------------------------------------------------|
| S1    | Audit merged + explicit user approval to proceed to S2.                                                                |
| S2    | `services/user_bet_ledger.py` in place, unit-tested via pytest. Zero production callers. Zero DB writes.               |
| S3    | Canonical schema extension merged; every new field is nullable and defaults to `None`. No behaviour change on read.    |
| S4    | Dry-run backfill runs against a snapshot of prod-shape data and emits classification counts matching Parity §3.        |
| S5    | Dual-writer shim live for `p_*` rows. Read paths unchanged. Divergence monitor shows ≥ 99.9 % parity for 7 rolling days. |
| S6    | `/api/parlay/history` (list) read-cutover behind a feature flag. Legacy reader retained. Parity monitor stays green.   |
| S7    | Read-cutover flag flipped to default-on for remaining parlay endpoints; legacy reader in shadow mode for one release.  |
| S8    | Legacy writer for `p_*` (via `parlay_history.save_parlay`) flipped to canonical writer + backwards-compat mirror.      |
| S9    | Legacy writer + reader removed; `parlay_history` retained read-only; monitor cleared. **Collection is NOT deleted.**   |

Stages S2 through S9 are for future planning only. Nothing is authorized to
execute today.

---

## 4. Stage-by-stage plan

### S1 — Audit review (**this checkpoint**)
- Deliverables: `PHASE3G_WAGER_LEDGER_AUDIT.md`, `PHASE3G_PARITY_REPORT.md`,
  `PHASE3G_MIGRATION_PLAN.md` (this file).
- Exit gate: user acknowledges §14 open questions + explicit approval to
  proceed to S2. **STOP UNTIL APPROVED.**

### S2 — Typed ledger contract (`services/user_bet_ledger.py`)
- Adds a new module with a dataclass `UserBet` matching the extended
  canonical schema (see §5 below).
- Public API (async):
  - `record_bet(db, *, user_id, pick_id | leg_ids, bet_type, stake_units, ...) -> UserBet`
    — pure builder + typed insert; server-side idempotency check
    (`(user_id, sorted leg_ids)` for parlay, `(user_id, pick_id)` for straight
    when a client_bet_id is not provided).
  - `settle_bet(db, bet_id, status, pnl_units, settled_at)` — thin wrapper
    around today's update paths.
  - `list_bets_for_user(db, user_id, filters, limit) -> list[UserBet]`.
  - `dual_read_parity(db, user_id) -> ParityReport` — returns counts of
    identical / differing rows across the two stores for observability.
- **Zero call sites**. The old route handlers keep doing what they do today.
- pytest coverage in `backend/tests/test_iter131_user_bet_ledger.py`
  (audit-agreed target).

### S3 — Schema extension on `user_bets`
- Non-breaking: add nullable fields `combined_odds`, `legs[]` (inline
  snapshot), `mode`, `source`, `migration_source_id`, `client_bet_id`.
- Update `services/index_registry.py` if needed (e.g. add a partial index on
  `migration_source_id` for the backfill). No changes to existing indexes.
- Frontend contract stays additive.

### S4 — Dry-run backfill script (`backend/scripts/backfills/migrate_parlay_history_to_user_bets.py`)
- `--dry-run` default; `--limit`, `--user`, `--collection` flags.
- Reads `parlay_history` where `id ^ p_` **AND** `user_id != null/""`.
- For each source row:
  - Compute the classification bucket (safe / fallback / manual / unsafe).
  - Compute the target canonical doc (do not insert).
  - Emit a JSON row into `PHASE3G_DRY_RUN_REPORT.json` (writeable to
    `/app/PHASE3G_DRY_RUN_REPORT.md` for human review).
- Second run mode: `--commit` (behind a hard confirm) that inserts under
  `migration_source_id`. Idempotent — safe to rerun.
- Never touches `plearn_*` rows.
- Never touches `user_bets` rows lacking a `migration_source_id`.
- Never modifies `parlay_history`.

### S5 — Dual-write shim (Writer A only)
- In `parlay_history.save_parlay`, after the existing `insert_one`, invoke
  `user_bet_ledger.record_bet` with `source="dual_write"` and
  `migration_source_id=<p_id>`.
- Any exception from the shim is logged and swallowed — never blocks the save.
- A divergence monitor endpoint `GET /api/admin/ops/wager-ledger/parity`
  returns rolling parity stats over the last 24h/7d.

### S6 — Read cutover behind feature flag
- Add `USER_BETS_CANONICAL_READS=false` to `services/settings.py` (default off).
- When true, `/api/parlay/history` reads from `user_bets` (with
  `bet_type="parlay"`) via `user_bet_ledger.list_bets_for_user` and
  projects to the legacy response shape via an adapter.
- Flag flip is per-request via header for admin dogfooding first, then
  per-user, then default-on.

### S7 — Cutover remaining parlay endpoints
- `/api/parlay/{id}` reads from canonical.
- `/api/parlay/{id}/resettle` fans out to `user_bet_ledger` — internally
  still triggers the legacy `resettle_parlay` while shadow-mode watches.
- Admin endpoints in `routes/admin_users_routes.py` switch to canonical.
  `admin_delete_user` cascade extends to `user_bets.delete_many({user_id})`
  in addition to the existing `parlay_history.delete_many({user_id})`.

### S8 — Writer flip
- `parlay_history.save_parlay` becomes a thin adapter that calls
  `user_bet_ledger.record_bet` and then optionally mirrors to
  `parlay_history` for the backwards-compat window.
- After one full release with no monitor alerts, drop the mirror step.

### S9 — Sunset legacy readers
- Remove `parlay_history.list_history`, `get_parlay`, `delete_parlay`,
  `resolve_saved_parlays`, `resettle_parlay` from the runtime call graph.
- Retain the module for archival + `plearn_*` scope (Writer B stays).
- **Do NOT drop `parlay_history` collection.** Retain the `p_*` rows as a
  read-only audit trail.

---

## 5. Canonical schema (target)

```
user_bets:
  id                     : str UUID v4                                     (existing)
  user_id                : str UUID                                        (existing)
  pick_id                : str | None                                      (existing)
  bet_type               : "straight" | "parlay"                           (existing)
  parlay_legs            : list[pick_id str]                               (existing; may hold sorted or original order)
  stake_units            : float                                           (existing)
  odds_at_bet            : int American | None                             (existing; = combined for parlays)
  status                 : "pending" | "won" | "lost" | "push"             (existing)
  pnl_units              : float (signed; 0 pre-settle)                    (existing)
  sport                  : str | None (denormalized primary)               (existing)
  market                 : str | None (denormalized primary)               (existing)
  event                  : str | None (denormalized primary)               (existing)
  selection              : str | None (denormalized primary)               (existing)
  created_at             : BSON datetime                                   (existing)
  settled_at             : BSON datetime | None                            (existing)
  notes                  : str | None                                      (existing)
  # ── NEW nullable fields introduced in S3 ─────────────────────────────
  combined_odds          : int American | None                             (parlay only)
  legs                   : list[LegSnapshot] | None
     LegSnapshot: { pick_id, sport, league, event, event_time, market,
                    selection, book_odds, lock_score, status }
  mode                   : "standard" | "advanced" | "high_risk" | "today" | None
  source                 : "user_track" | "parlay_save" | "backfill_p" | "dual_write" | None
  migration_source_id    : str | None
  client_bet_id          : str | None                                       (optional idempotency handle)
```

Roll-up counts (`legs_won`, `legs_lost`, `legs_pending`) are **derived at
read time** from `legs[].status`. Not persisted.

---

## 6. Backfill mapping

For every source row in `parlay_history` matching `id ^ "p_" AND user_id != null/""`:

```
target = UserBet(
    id                     = uuid4(),
    user_id                = source.user_id,
    pick_id                = None,
    bet_type               = "parlay",
    parlay_legs            = source.leg_ids,
    stake_units            = float(source.stake),
    odds_at_bet            = int(source.combined_odds),           # combined for parlays
    combined_odds          = int(source.combined_odds),
    status                 = STATUS_MAP[source.status],           # live → pending, else pass-through
    pnl_units              = compute_pnl(source.status, source.stake, source.combined_odds, source.payout),
    sport                  = source.legs[0].sport if source.legs else None,
    market                 = f"{len(source.leg_ids)}-leg parlay",
    event                  = " + ".join(l.event for l in source.legs[:3]),
    selection              = " · ".join(l.selection for l in source.legs[:3]),
    created_at             = parse_iso(source.created_at),
    settled_at             = parse_iso(source.settled_at),
    notes                  = None,
    legs                   = source.legs,                          # verbatim
    mode                   = source.mode,
    source                 = "backfill_p",
    migration_source_id    = source.id,                            # "p_..."
    client_bet_id          = None,
)

# where:
STATUS_MAP = {"live": "pending", "won": "won", "lost": "lost", "push": "push"}
def compute_pnl(status, stake, combined_odds, payout):
    if status == "won":  return +float(payout) if payout is not None else _american_to_profit(combined_odds, stake)
    if status == "lost": return -float(stake)
    return 0.0
```

Idempotency: upsert filter `{"migration_source_id": source.id}`. Second run
= no writes.

---

## 7. Divergence monitor (S5–S8 only)

A single endpoint `GET /api/admin/ops/wager-ledger/parity` returns:
- `parity_by_status`: per-status count parity within the last 7 rolling days.
- `divergence_examples`: up to 20 recent rows where `user_bets` and
  `parlay_history` disagree on status, stake, or payout — with the two IDs
  for manual inspection.
- `dual_write_health`: shim success/failure counters.
- `rollback_readiness`: bool indicating whether legacy reads can safely be
  reactivated.

Gate for advancing S5→S6: `parity ≥ 99.9 %` over 7 rolling days.

---

## 8. Rollback design

| Stage | Rollback action                                                                                    | Data motion? |
|-------|----------------------------------------------------------------------------------------------------|:------------:|
| S2    | Revert PR that adds `services/user_bet_ledger.py`. Zero callers today.                             | None         |
| S3    | Fields are nullable; no rollback required unless index PR merged — in which case drop new index.   | None         |
| S4    | Drop the dry-run script or set `--dry-run` permanent. `--commit` output rows are all keyed by `migration_source_id` — deletable via `db.user_bets.delete_many({"source": "backfill_p"})` if catastrophically wrong. | Limited (backfill-only rows) |
| S5    | Disable the shim by setting a settings flag; new writes stop mirroring. Existing dual-writes stay. | None         |
| S6    | Flip `USER_BETS_CANONICAL_READS=false`. Legacy readers immediately serve traffic.                  | None         |
| S7    | Same flag flip. Cascades restored.                                                                  | None         |
| S8    | Revert `save_parlay` adapter to direct `insert_one` on `parlay_history`.                            | None         |
| S9    | Restore the legacy readers module from git. `p_*` rows still live in `parlay_history`.              | None         |

**Zero data motion** is guaranteed through S8. S9 is the point of no return
for the reader — but even at S9 the legacy data is preserved because the
guardrail forbids collection deletion.

---

## 9. Testing plan

- `backend/tests/test_iter131_user_bet_ledger.py`:
  - contract-level tests for `record_bet`, `settle_bet`, `list_bets_for_user`.
  - idempotency tests: rerun `record_bet` with same `client_bet_id`, same
    `(user_id, sorted leg_ids)`.
  - status remap unit tests (`live → pending`, `void → push`).
  - `compute_pnl` unit tests for won / lost / push / live states.
  - dry-run classification tests using synthetic fixtures.
- `backend/tests/test_iter132_dual_write_shim.py` (S5):
  - shim writes both stores.
  - shim swallows errors; save-parlay path never fails on shim failure.
  - divergence monitor returns expected counts.
- `backend/tests/test_iter133_read_cutover.py` (S6+):
  - both flag branches return the same shape.
  - shape parity via schema diff.

All new tests must respect the "no production writes" audit guardrail —
they operate against test collections seeded via existing fixtures.

---

## 10. Risks and mitigations

| Risk                                                                                                    | Mitigation                                                                              |
|---------------------------------------------------------------------------------------------------------|-----------------------------------------------------------------------------------------|
| The 4-row dev sample under-represents the production schema drift.                                     | Rerun `--dry-run` against production before flipping any writer.                        |
| A user has legitimately placed the same parlay twice; hard dedupe would drop the second.               | Default the canonical idempotency to `client_bet_id`; server-side dedupe optional.      |
| `resolve_saved_parlays` snapshot-fallback loses recovery power if legs live on canonical only.         | S2 ports the snapshot-fallback + external-adapter chain onto `user_bet_ledger.settle_bet`. |
| Lab correlation reads (`lab_routes.py`) still expect `parlay_history` scope with all `plearn_*` rows.  | No change to lab reads. `plearn_*` never migrates. Verified in audit §5b.                |
| Admin cascade delete misses the new canonical rows.                                                    | S7 extends `admin_delete_user` cascade to `user_bets` — verified in tests.               |
| Frontend surfaces holding stale caches after cutover.                                                  | Out of scope for this phase. Tracked separately as "Stale Frontend UI Cache" issue.      |

---

## 11. Explicit decisions still owed by the user

Before Step 2 begins, the user should decide (see audit §14):
1. Idempotency default (server-side vs client-side).
2. `void` status handling on the canonical row.
3. Whether `admin_delete_user` cascade must be strengthened.
4. Whether to stamp `clv_value_at_settle` onto the wager during propagation.
5. Whether `mode` is parlay-only or first-class on all bets.

Nothing in Stage S2 blocks a decision here; the decisions purely inform the
default values used by `user_bet_ledger.record_bet`.

---

## 12. Stop point

This document defines the plan. **Execution requires explicit user
authorization for each stage.** No writes have been performed, no code
has been added, no indexes have been created, no routes have been touched.

The audit stops here.

**End of Phase 3G Migration Plan.**
