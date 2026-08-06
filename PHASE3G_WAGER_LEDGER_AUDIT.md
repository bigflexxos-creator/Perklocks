# Phase 3G — Wager Ledger Audit (Read-Only)

**Status:** DRAFT · audit-only · no code changes shipped
**Owner:** Backend architecture cleanup (Phase 3G)
**Date:** 2026-06 (this session)
**Scope:** `user_bets` and `parlay_history` collections
**Guardrail confirmation:** No production writes. No dual-write shim. No ledger service.
No backfill execution. No index changes. No route changes.

---

## 1. Executive summary

The system currently maintains **two separate personal-wager stores** that were
introduced at different times for different reasons:

| Collection       | Origin                                          | Bet types      | User-scoped?           |
|------------------|-------------------------------------------------|----------------|------------------------|
| `user_bets`      | 2026-07-21 personal-tracking mandate            | straight + parlay | Yes — all rows have `user_id` |
| `parlay_history` | Older "Save-on-Tap" parlay ticket store         | parlay only    | Partially — dual-writer collection (see §3) |

`parlay_history` is **dual-purpose**: it stores both user-saved parlays
(`p_<hash>` ids, `user_id` present) **and** learning-loop auto-recorded parlays
(`plearn_<hash>` ids, `signature` present, `user_id` absent) that the parlay
optimizer emits on every request. Any Phase 3G design must treat these as two
logical tables that happen to share a physical collection.

### Verdict on canonicality
`user_bets` is the correct **long-term** canonical wager ledger because it:
- covers both straight and parlay bets in a single schema,
- has strict `user_id` scoping at the DB query level,
- already receives real-time settlement propagation from every sport's
  settlement path (`propagate_pick_settlement`),
- has a shape that closes cleanly on personal ROI/PnL.

However `user_bets` in its **current** form is **NOT yet suitable to absorb
`parlay_history` untouched**. Missing fields, softer idempotency, no leg
snapshot, no cash-out estimator, and no signature-based dedupe would silently
regress the Save-on-Tap experience. See §7 & §10.

Recommended cutover path: **augment `user_bets` first, then dual-write, then
read-cutover**, with `parlay_history` retained as-is throughout (per Phase 3G
guardrail). See `PHASE3G_MIGRATION_PLAN.md`.

---

## 2. Live document counts

Measured against `lockscore_db` at the time of this audit (single-node dev
Mongo running under supervisor). These numbers are a **snapshot for scale
context only** — they do not gate any migration decision.

| Metric                                           | Count |
|--------------------------------------------------|------:|
| `user_bets` — total documents                    |     2 |
| `user_bets` — distinct `user_id`                 |     2 |
| `user_bets` — `bet_type=straight`                |     2 |
| `user_bets` — `bet_type=parlay`                  |     0 |
| `user_bets` — status `pending`                   |     1 |
| `user_bets` — status `won`                       |     0 |
| `user_bets` — status `lost`                      |     1 |
| `user_bets` — status `push`                      |     0 |
| `parlay_history` — total documents               |   194 |
| `parlay_history` — user-saved (id `^p_`)         |     4 |
| `parlay_history` — learning-loop (id `^plearn_`) |   190 |
| `parlay_history` — distinct user-saved `user_id` |     2 |
| `parlay_history` — status `live`                 |     0 |
| `parlay_history` — status `pending`              |   143 |
| `parlay_history` — status `won`                  |    12 |
| `parlay_history` — status `lost`                 |    22 |
| `parlay_history` — status `push`                 |    17 |
| Distinct users overlapping between collections   |     0 |

**Notable observations**
- The dev DB has no exact-overlap same-wager rows between the two collections
  (0-user overlap). This is dev-only and MUST be re-checked in prod before
  cutover. See §7 for the collision analysis and §10.4 for the audit query.
- Only 4 rows in `parlay_history` are actually user-saved. The vast majority
  (190/194 = **97.9 %**) are `plearn_*` learning-loop rows and are OUT OF SCOPE
  for the canonical wager ledger — they belong to `parlay_learning` and its
  synergy map.

---

## 3. `parlay_history` collection is dual-purpose (critical)

Two distinct writers share the collection with **incompatible schemas**:

### 3a. Writer A — `parlay_history.save_parlay`
Trigger: `POST /api/parlay/save` (user taps "Save" on a generated parlay card).
Path: `parlay_history.py::save_parlay` → `db.parlay_history.insert_one`.

Schema:
```
{
  id:            "p_<sha1(user_id|sorted_leg_ids)[:14]>",   # deterministic
  user_id:       "<uuid>",
  created_at:    ISO 8601 UTC string,
  mode:          "standard" | "advanced" | "high_risk" | "today",
  leg_ids:       [pick_id, ...],
  legs:          [snapshot obj, ...],   # inline snapshot
  combined_odds: int (American),
  stake:         float (units, default 1.0),
  status:        "live" | "won" | "lost",
  legs_won, legs_lost, legs_pending: int,
  settled_at:    ISO or null,
  payout:        float or null,          # profit-per-unit, filled on win
}
```

### 3b. Writer B — `parlay_learning.record_parlay_shown`
Trigger: any `GET /api/picks/parlay` response (auto).
Path: `parlay_learning.py::record_parlay_shown` → `db.parlay_history.update_one(..., upsert=True)` keyed by `signature`.

Schema:
```
{
  id:            "plearn_<sha1(signature)[:14]>",           # deterministic
  signature:     "<sha1(sorted leg ids)[:24]>",
  legs:          [compressed leg objects],                  # different shape
  leg_count:     int,
  mode:          str,
  sport_mode:    str,
  status:        "pending" | "won" | "lost" | "push",
  shown_at:      ISO,
  last_shown_at: ISO,
  shown_count:   int,
  settled_at:    ISO or null,
  leg_statuses:  {pick_id: status},
  survival_pct:  float,
  combined_american_odds: str/int,
  ranking_snapshot:    dict,
  correlation_snapshot: dict,
  user_id:       MISSING (never set on this branch)
}
```

### Key incompatibilities between the two writers
| Concern              | Writer A (`p_`)         | Writer B (`plearn_`)          |
|----------------------|-------------------------|-------------------------------|
| owner user           | `user_id` (required)    | absent                        |
| dedupe key           | user_id + sorted legs   | signature (sorted legs only)  |
| status vocabulary    | `live/won/lost`         | `pending/won/lost/push`       |
| stake                | `stake` (float, units)  | absent                        |
| combined odds field  | `combined_odds`         | `combined_american_odds`      |
| leg snapshot fields  | full pick fields        | compressed learning fields    |
| timestamp field      | `created_at`            | `shown_at` / `last_shown_at`  |
| pending signal       | none — starts at `live` | starts at `pending`           |
| survival_pct present | 0/4                     | 190/190                       |
| ranking_snapshot     | 0/4                     | 190/190                       |
| leg_ids present      | 4/4                     | 155/190 (older rows only)     |

There are already two settlers guarding against cross-contamination:
- `parlay_history.resolve_saved_parlays` scopes to `{"user_id": {$exists:true, $ne:null, $ne:""}}` — only touches Writer A rows.
- `parlay_learning.settle_parlays` scopes to `{"status": "pending"}` + `shown_at` cutoff — only touches Writer B rows because Writer A rows use `status="live"` not `"pending"`.

**The historic "156 plearn_* rows falsely marked WON" bug** (see comment
in `parlay_history.py` around line 145) came from these two paths fighting
over the collection. Any Phase 3G work MUST preserve this scoping and MUST
NOT move the `plearn_*` rows.

---

## 4. Writer inventory

### 4a. `user_bets` writers
| Location                                       | Operation                          | Notes                                                        |
|------------------------------------------------|------------------------------------|--------------------------------------------------------------|
| `routes/user_bets_routes.py::track_bet`        | `insert_one` (create pending)      | Only ingress path. UUID id. User-scoped.                     |
| `routes/user_bets_routes.py::delete_user_bet`  | `delete_one` (pending only)        | Rejects settled deletes.                                     |
| `routes/user_bets_routes.py::propagate_pick_settlement` | `update_one × N` (settle straights + parlays) | Called by settlement callers below.  |
| `routes/user_bets_routes.py::_ensure_indexes`  | `create_index × 4`                 | Also declared in `services/index_registry.py`.               |

### 4b. `user_bets` settlement callers (invoke `propagate_pick_settlement`)
| Caller                                    | Sport / market                     |
|-------------------------------------------|------------------------------------|
| `settlement_engine.py:547`                | ML/spreads/totals across leagues   |
| `prop_settlement.py:1366`                 | player props / MLB props           |
| `espn_settlement.py:707`                  | NFL/NBA/CFB ESPN outcomes          |
| `soccer_espn_settle.py:872`               | Soccer full-time + market outcomes |
| `tennis_extra/settle.py:216`              | Tennis matches/props               |
| `kbo_settlement.py:290`                   | KBO baseball                       |

All six callers wrap the call in `try/except` and log at DEBUG on failure —
propagation never blocks the pick's own settlement.

### 4c. `parlay_history` writers
| Location                                                          | Operation                              | Row kind |
|-------------------------------------------------------------------|----------------------------------------|----------|
| `parlay_history.py::save_parlay`                                  | `insert_one` (idempotent by `p_` id)   | `p_`     |
| `parlay_history.py::resolve_saved_parlays`                        | `update_one × N` (leg roll-up)         | `p_`     |
| `parlay_history.py::resettle_parlay`                              | `update_one` (single ticket)           | `p_`     |
| `parlay_learning.py::record_parlay_shown`                         | `update_one(upsert=True)` by signature | `plearn_`|
| `parlay_learning.py::settle_parlays`                              | `update_one × N`                       | `plearn_`|
| `routes/admin_users_routes.py::admin_delete_user` (cascade)       | `delete_many` by user_id               | `p_`     |
| `services/parlay_intelligence/learning_loop.py::record_completed_parlay` | (writes to `parlay_completions`; **not** `parlay_history`) | (n/a — verified read-only against `parlay_history`) |

### 4d. Index writers
| Location                                    | Coll             | Notes                                                                |
|---------------------------------------------|------------------|----------------------------------------------------------------------|
| `services/index_registry.py`                | `user_bets`, `parlay_history` | Canonical spec (Phase 3C).                          |
| `routes/user_bets_routes.py::_ensure_indexes` | `user_bets`    | Legacy lazy-ensure — duplicates registry entries; safe (idempotent). |
| `server.py:3639-3641`                       | `parlay_history` | Legacy startup ensure — duplicates registry entries; safe (idempotent). |

These duplicates are known and are eligible for cleanup in a later phase — not
part of this audit's scope.

---

## 5. Reader inventory

### 5a. `user_bets` readers
| Location                                                  | Purpose                                       |
|-----------------------------------------------------------|-----------------------------------------------|
| `routes/user_bets_routes.py::list_user_bets`              | `/user/bets` list (filterable)                |
| `routes/user_bets_routes.py::delete_user_bet`             | Ownership check on delete                     |
| `routes/user_bets_routes.py::user_analytics_summary`      | Personal ROI card                             |
| `routes/user_bets_routes.py::user_analytics_by_sport`     | Personal by-sport breakdown                   |
| `routes/user_bets_routes.py::user_analytics_by_market`    | Personal by-market breakdown                  |
| `routes/user_bets_routes.py::user_analytics_history`      | Personal chronological history                |
| `services/identity_resolver.py::dry_run_scan_collection`  | Identity contract dry-run (read-only)         |
| `tests/test_iter116_regression_scaffold.py`               | Regression scaffold (test-only)               |

### 5b. `parlay_history` readers
| Location                                                        | Row kind read        | Purpose                                    |
|-----------------------------------------------------------------|----------------------|--------------------------------------------|
| `routes/parlay_history_routes.py::parlay_history_list`          | `p_` (user-saved)    | `GET /api/parlay/history`                  |
| `routes/parlay_history_routes.py::parlay_detail`                | `p_`                 | `GET /api/parlay/{id}`                     |
| `routes/parlay_history_routes.py::parlay_remove`                | `p_`                 | `DELETE /api/parlay/{id}`                  |
| `routes/parlay_history_routes.py::parlay_resettle`              | `p_`                 | `POST /api/parlay/{id}/resettle`           |
| `parlay_history.py::list_history / get_parlay / delete_parlay`  | `p_`                 | Data-layer helpers                         |
| `parlay_history.py::resolve_saved_parlays`                      | `p_` (scoped)        | Settler for user-saved parlays             |
| `parlay_learning.py::settle_parlays`                            | `plearn_` (scoped)   | Settler for learning-loop rows             |
| `parlay_learning.py::compute_synergy_map`                       | `plearn_`            | Aggregates settled `plearn_*` for synergy  |
| `services/parlay_intelligence/parlay_backtester.py::_collect_settled` | `plearn_`     | 60-day backtest snapshot                   |
| `services/parlay_intelligence/learning_loop.py`                 | `plearn_`            | Learning-loop event attribution            |
| `lab_routes.py:203, 555`                                        | **both** (queries `status ∈ {won,lost}`) | Correlation lab reads across all rows including plearn_ |
| `routes/admin_users_routes.py::admin_overview / admin_user_detail` | `p_` (filtered by user_id) | Admin totals + per-user recent parlays |
| `services/identity_resolver.py::dry_run_scan_collection`        | both                 | Identity contract dry-run (read-only)      |

**Ambiguity risk detected in `lab_routes.py`**: the correlation-lab queries do
NOT filter by `id ^ p_` or `user_id`. They read every settled `parlay_history`
row (mostly `plearn_*`). This is **almost certainly correct** for the learning
signal — the lab is meant to learn from every parlay slate the optimizer emits,
not just the tiny subset users saved — but it does mean a Phase 3G migration
that **moves** `plearn_*` rows would silently break the lab. See §10.

---

## 6. Settlement flow inventory

```
┌─ pick reaches terminal state ─┐
│  (won/lost/push, per sport)   │
└──────────────┬────────────────┘
               │
               ├──▶ settlement_engine (ML/spread/total)
               │       ├─ writes picks.status, units_risked, units_profit, clv_value
               │       └─ propagate_pick_settlement()  ─▶  user_bets settle path
               │
               ├──▶ prop_settlement (player props)
               │       └─ propagate_pick_settlement() ─▶  user_bets settle path
               │
               ├──▶ espn_settlement (NFL/NBA/CFB)
               │       └─ propagate_pick_settlement()
               │
               ├──▶ soccer_espn_settle (Soccer)
               │       └─ propagate_pick_settlement()
               │
               ├──▶ tennis_extra/settle
               │       └─ propagate_pick_settlement()
               │
               └──▶ kbo_settlement (KBO)
                       └─ propagate_pick_settlement()

parlay_history user-saved rows settle via a separate loop:
  ┌─▶ resolve_saved_parlays(db)  (invoked on the settlement schedule)
  │     ├─ pick lookup ─▶ snapshot fallback ─▶ external adapter (parlay_leg_settle)
  │     └─ rolls parlay leg statuses into parlay.status + payout

parlay_history plearn_ rows settle via yet another loop:
  └─▶ parlay_learning.settle_parlays(db)
        ├─ requires ALL leg statuses to be terminal
        └─ feeds parlay_completions + synergy_map (compute_synergy_map)
```

**Observation**: `propagate_pick_settlement` handles both straight and parlay
`user_bets`. `resolve_saved_parlays` handles `parlay_history` (`p_`) parlays.
These two are **structurally parallel but never share code paths**. Any
consolidation must preserve the semantics of both — notably the snapshot-match
fallback and the external-adapter chain in `parlay_history` which are absent
from `user_bets`.

---

## 7. ROI / units / CLV / payout tracking

### 7a. `user_bets`
- Stake stored as `stake_units` (float, unit-based, default 1.0).
- P/L stored as `pnl_units` on settle (signed; negative for lost, 0 for push/pending, positive for won).
- No `units_risked` / `units_profit` split — analytics computes those on the fly.
- No CLV field. No opening/closing line. No book/sportsbook field.
- Uses `odds_at_bet` snapshot to compute pay-outs — stable against later re-pricing on the pick.

### 7b. `parlay_history` (`p_` user-saved)
- Stake stored as `stake` (float).
- Only `payout` on wins (positive-only, profit-per-unit). Losses do NOT record a `-stake` field — implied from `status=lost` + `stake`.
- No `pnl_units`, no `units_risked`, no `units_profit`.
- No CLV, no opening/closing line, no book.
- `combined_odds` snapshotted at save time (int American, computed from leg book_odds).
- Live cash-out estimate computed on the fly (`_cashout_estimate`) — never persisted.

### 7c. `parlay_history` (`plearn_` learning-loop) — NOT a wager
- No stake, no payout. `payout` field is 0.0 on old rows; not a wager amount.
- `combined_american_odds` stored as string with sign (e.g. `"+900"`).
- Only used to feed synergy / correlation learning.

### 7d. CLV coverage on wager rows
| Coverage                                | `user_bets`                | `parlay_history` (`p_`)    |
|-----------------------------------------|----------------------------|----------------------------|
| CLV value stored on wager row           | **0 %** (no field)         | **0 %** (no field)         |
| CLV value stored on the underlying pick | 100 % after settle (via `settlement_engine`) — read-time joinable |

Consequence: **any future CLV surface for personal ROI must join back to the
`picks` collection at read time or backfill snapshotted CLV onto the wager
row.** The audit does not decide which — deferred to migration plan §M4.

---

## 8. Field-by-field schema comparison (parity table)

Legend for each cell in the "classification" column:

- **exact** — same semantics, same units, safely renameable.
- **derivable** — one side can be computed from data on the other, deterministically.
- **legacy-only** — present in `parlay_history` (or `plearn_*`) with no equivalent in `user_bets`.
- **canonical-only** — present in `user_bets` with no equivalent in `parlay_history`.
- **missing** — neither store carries it today.
- **ambiguous** — value on both sides but semantics differ (e.g. same field name, different units or sign).
- **unsafe** — cannot be auto-migrated without human-in-the-loop review.

| Field group          | `user_bets` field  | `parlay_history` (`p_`) field     | Classification | Notes |
|----------------------|--------------------|-----------------------------------|----------------|-------|
| user identity        | `user_id`          | `user_id`                         | exact          | UUID string on both. |
| wager id             | `id` (UUID v4)     | `id` = `p_<sha1(user_id+legs)>`   | ambiguous      | Different id styles; see idempotency §9. |
| placed timestamp     | `created_at` (BSON datetime) | `created_at` (ISO 8601 str) | ambiguous      | Type mismatch; both are UTC. Normalize to BSON datetime canonically. |
| wager type           | `bet_type` = `straight|parlay` | implied `parlay` (row exists → parlay) | derivable | Legacy is parlay-only. |
| status               | `pending/won/lost/push` | `live/won/lost` (Writer A); `pending/won/lost/push` (plearn_) | ambiguous | See §8b status map. |
| stake amount         | `stake_units` (float, units) | `stake` (float, units)     | derivable      | Same units in practice; rename on migrate. |
| stake units          | `stake_units`      | `stake`                           | derivable      | Same as above. |
| single-leg odds      | `odds_at_bet` (int American) | n/a (parlay-only in legacy) | canonical-only |       |
| combined odds        | `odds_at_bet` (holds combined when `bet_type=parlay`) | `combined_odds` (int American) | derivable | Semantics identical for parlays; use `combined_odds` on migrate. |
| odds format          | American int       | American int                      | exact          |       |
| potential payout     | not stored (computed) | not stored (computed)          | missing        | Both compute via `_american_to_profit` at settle time. |
| actual payout        | `pnl_units` (signed profit) | `payout` (positive-only)   | ambiguous      | `payout` is profit-per-unit AND null on loss. Map: won → `payout` = `pnl_units`; lost → `pnl_units = -stake`, `payout=0.0`; push → both 0. Verify per-row. |
| sportsbook / book    | not stored         | not stored                        | missing        |       |
| prediction_id        | `pick_id` (FK to `picks.id`) | `leg_ids[i]` (FK to `picks.id`) | derivable | Rename on migrate. |
| snapshot_id          | not stored         | not stored (partial snapshot in `legs[]`) | missing | Wire-up requires prediction_publication_service coupling — deferred. |
| event_id             | not stored (has `event` display string) | not stored (has `legs[i].event`) | missing | Cross-collection join by `pick_id → picks.event_id`. |
| sport                | `sport` (denorm)   | `legs[i].sport`                   | derivable      | Legacy has per-leg; canonical stores primary/first-leg sport. |
| market               | `market` (denorm)  | `legs[i].market`                  | derivable      | Similar; canonical uses e.g. "3-leg parlay" for parlays. |
| selection            | `selection` (denorm) | `legs[i].selection`             | derivable      | Same. |
| exact line           | not stored         | not stored on wager (leg may not have `line`) | missing | Would require snapshot enrichment. |
| original bet-time odds | `odds_at_bet` (combined) | `legs[i].book_odds` (per leg) | derivable | Per-leg preserved in legacy; canonical stores only combined. |
| leg result           | derived via `db.picks[leg].status` | `legs[i].status` (persisted) | ambiguous | Legacy persists last-seen leg status inside the snapshot. |
| settled_at           | `settled_at` (BSON datetime, nullable) | `settled_at` (ISO str, nullable) | ambiguous | Type mismatch. |
| push / void handling | 'push' as terminal, 0 P/L | 'push' seen in plearn_ only; user-saved uses live/won/lost — no 'push' terminal today | ambiguous | Need to formalize push on user-saved before migrating. |
| CLV                  | not stored         | not stored                        | missing        | Available via `picks.clv_value` post-settle. |
| opening line         | not stored         | not stored                        | missing        |       |
| closing line         | not stored         | not stored                        | missing        | Available via `picks.closing_odds` post-settle. |
| tags                 | not stored         | `mode` (informal tag: standard/advanced/high_risk/today) | derivable | Only in legacy. |
| risk tier            | not stored         | `mode` (informal)                 | derivable      | Only in legacy. |
| correlation warning  | not stored         | not stored on `p_`; present on `plearn_` as `correlation_snapshot` | missing on wager | Not part of user-saved. |
| source               | not stored         | not stored (implied by id prefix) | missing        | Recommend `source: "user_track" | "parlay_save" | "backfill"` on canonical row. |
| migration source id  | not stored         | not stored                        | missing        | Recommend `migration_source_id` if backfilling from `p_*`. |
| idempotency key      | none (UUID)        | `p_<sha1(user_id|sorted leg_ids)>` (implicit) | ambiguous | See §9. |
| notes                | `notes` (nullable, ≤500) | not stored                | canonical-only |       |
| leg IDs              | `parlay_legs: [pick_id, ...]` (empty for straight) | `leg_ids: [pick_id, ...]` | derivable | Rename on migrate. |
| leg snapshot         | not stored (join via `picks`) | `legs: [{pick_id, sport, league, event, event_time, market, selection, book_odds, lock_score, status}, ...]` | legacy-only | Critical for resettle fallback + display when parent picks are deleted. |
| roll-up counts       | derived on read    | `legs_won/legs_lost/legs_pending` | derivable      | Rebuild post-hoc from `legs[i].status`. |
| survival_pct         | not stored         | not stored (Writer A); `plearn_` only | missing on wager | n/a for user-saved. |
| combined_win_probability | not stored     | not stored (Writer A)             | missing        |       |
| mode / sport_mode    | not stored         | `mode` (Writer A); both (`plearn_`) | derivable    | Migrate onto canonical row as `mode`. |
| cash-out estimate    | not stored (n/a — straight-mostly today) | computed at read time by `_cashout_estimate` | derivable | Recompute at read time on canonical. |
| pnl_units            | `pnl_units` (signed) | not stored                      | canonical-only | Reconstruct from `status + stake + combined_odds` on backfill. |
| units_risked         | not stored (derived) | not stored                      | missing        | Derived at read time. |
| units_profit         | not stored (== `pnl_units` semantically) | not stored           | derivable       | Rename convention. |

### 8b. Status vocabulary map
| Canonical (`user_bets`) | Legacy `parlay_history` (`p_`) | Legacy `parlay_history` (`plearn_`) | Notes |
|--------------------------|--------------------------------|--------------------------------------|-------|
| `pending`                | `live`                         | `pending`                            | Legacy A uses `live`; must remap on migrate. |
| `won`                    | `won`                          | `won`                                | exact |
| `lost`                   | `lost`                         | `lost`                               | exact |
| `push`                   | (not observed on `p_`)         | `push`                               | Legacy A has no push branch today. Formalize on canonical. |
| `void`                   | not observed on wager rows     | not observed on wager rows           | Present in leg snapshot only (`legs[i].status`). Treat as `push` on the ledger. |

---

## 9. Idempotency analysis

### 9a. Current strategies
| Store                    | Current key                                                     | Collision risk today            |
|---------------------------|-----------------------------------------------------------------|---------------------------------|
| `user_bets`               | `id = uuid.uuid4()` per insert; no dedupe check                 | If user double-taps *Track*, two rows are created for the same (user_id, pick_id). Confirmed by code inspection — no upsert path in `track_bet`. |
| `parlay_history` (`p_`)   | `p_<sha1(user_id | sorted leg_ids)[:14]>` (strong, deterministic) | Same user + same leg set = same id → idempotent. |
| `parlay_history` (`plearn_`) | `signature = sha1(sorted leg ids)[:24]` with `$setOnInsert` upsert | Idempotent across shows.        |

### 9b. Proposed canonical idempotency key candidates (NO decision yet)
Per the user's instruction we do NOT lock in a design until §10.4 collision
analysis is verified against production data. Candidates to evaluate:

1. **`(user_id, sorted leg_ids)`** — matches legacy `p_*` behaviour. **Risk**:
   a user CANNOT place the "same parlay twice" (e.g. Team A ML twice today).
   Historically fine because parlays are unique-leg by design, but breaks if
   we later allow repeat-tracking. Same-legs-different-day is already handled
   because leg ids are date-scoped pick UUIDs.
2. **`(user_id, sorted leg_ids, placed_at_bucket_minute)`** — allows deliberate
   re-tracking after some time. Higher key volume, still deterministic.
3. **`(user_id, source_record_id)`** — if migrating from `p_*`, use the source
   id itself as the migration idempotency key (`migration_source_id`). Best
   choice for the backfill phase; leaves live-write idempotency to option 1
   or 2.
4. **Client-generated request id** — safest for real-time writes. Requires
   frontend to send `client_bet_id` on `POST /api/user/bets/track`. Zero
   collision risk if UUID-generated per tap.
5. **`(user_id, normalized leg identities via `services/identity_resolver`, placed_at bucket)`** — future-proof once Phase 3D identity contracts are stable enough to normalize legs across renames/aliases.

Do **NOT** dedupe by display text (`event`, `selection`, `player_name`) — those
strings are locale/format-sensitive and drift.

### 9c. Recommended combination
For migration (`backfill_parlay_history_to_user_bets` — future step, NOT this
audit): use `(migration_source_id = source `_id`_ or `id`)` as the primary
dedupe. For go-forward live writes: **prefer option 4 (client_bet_id)** with
option 1 as a server-side fallback. Final decision belongs to the migration
plan review, not this audit.

---

## 10. Duplicate-display and cross-collection overlap risks

### 10.1 Definition
Two records refer to the same physical wager if all of the following hold:
- Same `user_id`.
- Same `bet_type` (straight vs parlay).
- Same leg composition (sorted `leg_ids` in the parlay case; single `pick_id` in the straight case).

### 10.2 Cross-collection overlap in the live dev DB
- 0 users appear in both collections. Overlap is **0 rows** in dev.
- This does NOT mean the collections never overlap — it means the current dev
  sample does not exercise the overlap. Production **must** be re-scanned.

### 10.3 Duplicate-candidate risk classes (design-level, not row-level)
| Risk class                                                                              | Where triggered            | Severity      |
|-----------------------------------------------------------------------------------------|----------------------------|--------------|
| A user saved a parlay via `/api/parlay/save` **and** tracked it via `/api/user/bets/track` (with `bet_type=parlay` and matching `parlay_legs`) | dev + prod today | **Real** — both write today; dedupe must be part of migration. |
| Same-user double-tap of "Track" on the same pick                                        | `user_bets`                | Real; unfixed. Currently produces duplicate `user_bets` rows. |
| Same-user re-save of the same parlay legs (already deduped by `p_` id)                  | `parlay_history` (`p_`)    | Not a risk — deterministic id. |
| Learning-loop `plearn_*` treated as a user wager                                        | `parlay_history` (`plearn_`) | Would be **catastrophic** (190 rows would be attributed to no user). Prevented by ID-prefix + `user_id` filter. |

### 10.4 Recommended production duplicate audit (READ-ONLY, DO NOT RUN IN THIS PHASE)
```
# For every (user_id) that appears in user_bets and also in parlay_history:
db.user_bets.aggregate([
  {$match: {bet_type: "parlay"}},
  {$project: {user_id:1, legs_sorted: {$sortArray: {input: "$parlay_legs", sortBy: 1}}}}
])
# → join against
db.parlay_history.aggregate([
  {$match: {id: {$regex: "^p_"}}},
  {$project: {user_id:1, legs_sorted: {$sortArray: {input: "$leg_ids", sortBy: 1}}}}
])
# where (user_id, legs_sorted) match → potential same wager in both stores.
```
Run this against production to produce a real duplicate-candidate count
BEFORE writing the migration script. Expected in prod today: **very low** —
`user_bets` is very new (2026-07-21 mandate) and parlay-tracking on the client
prefers the Save-on-Tap surface.

---

## 11. Unresolved / missing / unsafe rows (audit-only pre-computation)

| Metric                                          | `user_bets`          | `parlay_history` (`p_` only)  | Interpretation |
|-------------------------------------------------|----------------------|-------------------------------|----------------|
| Total rows                                      | 2                    | 4                             |                |
| Missing `user_id`                               | 0                    | 0                             | Safe.          |
| Missing placed timestamp (`created_at`)         | 0                    | 0                             | Safe.          |
| Missing legs (parlay rows)                      | 0 (n/a — no parlays) | 0                             | Safe.          |
| Missing odds context                            | 0 (`odds_at_bet` set on all rows) | 0 (`combined_odds` set on all rows) | Safe. |
| Missing stake                                   | 0                    | 0                             | Safe.          |
| CLV coverage                                    | 0 %                  | 0 %                           | Missing everywhere — see §7d. |
| Book / sportsbook coverage                      | 0 %                  | 0 %                           | Missing.       |
| Line / exact-line coverage                      | 0 %                  | 0 %                           | Missing on the ledger; some legs carry line inside the snapshot. |
| Payout coverage (settled only)                  | won: `pnl_units` set (0/0 rows) | won: `payout` set (4/4 rows) | Safe. `pnl_units` semantics differ (§8). |
| prediction_id coverage                          | 100 % (`pick_id`)    | 100 % (`leg_ids`)             | Safe.          |
| snapshot_id coverage                            | 0 %                  | 0 %                           | Missing everywhere. |

**Classification of the 4 legacy `p_` rows for migration purposes:**
- **Safe to migrate as-is**: **4 / 4** — all rows have `user_id`, `created_at`, `leg_ids`, `legs[]`, `combined_odds`, `stake`, and a terminal or `live` status.
- **Requires fallback mapping**: 0 (dev sample). Any prod row missing one of the required fields lands in this bucket.
- **Requires manual review**: 0 (dev sample). Would include: rows with `user_id` referencing a user that has been deleted, rows whose `leg_ids` no longer resolve to any pick and whose snapshots lack `event_time` (would fail the ±36h event-time guard).
- **Unsafe to migrate automatically**: rows written by Writer B (`plearn_*`) — 190/190 stay in place (out of scope for the ledger).

---

## 12. Cutover-strategy sketch (recommendations, NOT decisions)

*All items below are recommendations for the migration plan. This audit does
not authorize any of them.*

### 12a. Write cutover
- **Do not** rewrite the `/api/parlay/save` endpoint yet. Continue writing to
  `parlay_history` (`p_*`) as canonical for the parlay-save UX.
- Add a dual-write shim (Phase 3G Step 4) that mirrors every new `p_*` row
  into `user_bets` under a stable `migration_source_id`. Read-side stays on
  `parlay_history`.
- Similarly, dual-write from `/api/user/bets/track` when `bet_type=parlay` —
  optionally mirror into `parlay_history` — TBD.

### 12b. Read cutover (deferred until parity is verified)
- Route `/api/parlay/history` (list) to read from `user_bets` with a
  `bet_type=parlay` filter, projected into the legacy response shape via an
  adapter layer.
- Keep `/api/parlay/{id}` reading the same collection to preserve resettle.
- Sunset `parlay_history.list_history` after two release cycles of read-parity
  telemetry.

### 12c. Settlement cutover
- Continue running BOTH settlers (`resolve_saved_parlays` for `p_` and
  `propagate_pick_settlement` for user_bets) during dual-write.
- Post-cutover: a single settler on user_bets is sufficient IF and only if the
  snapshot-fallback + external-adapter chain is ported over. Otherwise
  parlays whose picks have been dedup'd lose the recovery path.

### 12d. Migration sequence (order of operations)
1. Merge audit docs → user review (**this step**).
2. Build typed `services/user_bet_ledger.py` contract (**deferred**).
3. Extend `user_bets` schema (new nullable fields: `source`, `migration_source_id`, `mode`, `combined_odds`, `legs[]` snapshot).
4. Dry-run migration script (`--dry-run` default) that walks `p_*` rows and shows what would be written to `user_bets`.
5. Manual review of dry-run report; adjust field mappings.
6. Enable dual-write shim (writers only — readers unchanged).
7. Wait N days for real-world parity.
8. Cutover reads endpoint-by-endpoint.
9. Freeze `parlay_history` writes for `p_*` rows.
10. **Do NOT delete `parlay_history` collection** — retained per guardrail. Only add a `deprecated_at` marker at the collection level (audit-only tag).

### 12e. Rollback design
- Every dual-write row on `user_bets` carries `source` and
  `migration_source_id`. Rollback = flip the reader back to `parlay_history`;
  the dual-writer keeps state fresh in both.
- If a rollback is needed AFTER read-cutover: last-good `user_bets` snapshot
  is not required because the legacy `parlay_history` (`p_*`) rows are
  preserved by guardrail — the reader adapter is the only surface to revert.
- **Rollback plan requires zero data motion** — that is the design guarantee
  of the dual-write phase.

---

## 13. Is `user_bets` truly suitable as canonical?

**Yes — with the following pre-conditions met before Step 2 begins:**

1. Extend the schema with these new nullable fields:
   - `combined_odds` (int American, for parlay bet_type; equals `odds_at_bet` semantically today).
   - `legs` (list of leg snapshots — port the legacy shape verbatim so
     `resettle_parlay` semantics can be preserved).
   - `mode` (str, from legacy `mode` — one of `standard/advanced/high_risk/today`, nullable).
   - `source` (str, one of `user_track | parlay_save | backfill_p | dual_write`, nullable).
   - `migration_source_id` (str, for backfill traceability, nullable).
   - `client_bet_id` (str, optional idempotency handle from client, nullable).
2. Formalize `status="void"` handling (treat as `push` on the ledger).
3. Formalize the double-tap idempotency behaviour (either dedupe on
   `(user_id, pick_id, bet_type)` for straight bets or accept multiple rows
   as intentional — user decision).
4. Rebuild `pnl_units` semantics on backfill to preserve legacy `payout`
   values as `pnl_units` on wins and `-stake` on losses.
5. Ensure the settlement propagator becomes leg-snapshot aware so parlays
   whose parent picks were purged can still settle via the same
   snapshot→external-adapter chain that `resolve_saved_parlays` implements
   today.

If any of these are not addressed, `user_bets` will **regress** the Save-on-Tap
experience during the read-cutover phase.

---

## 14. Open questions for user review

1. **Idempotency default** — should the canonical write path enforce dedupe on
   `(user_id, bet_type, sorted leg_ids)`, accept a client-generated
   `client_bet_id`, or allow deliberate duplicates (current behaviour)?
2. **`status="void"` handling** — collapse to `push` on the ledger, or add a
   new terminal status?
3. **Legacy delete semantics** — `admin_delete_user` currently cascades
   `parlay_history.delete_many({user_id})`. Should the ledger cascade
   `user_bets.delete_many({user_id})` in the same handler after cutover?
4. **CLV snapshot on the wager** — should Phase 3G stamp `clv_value_at_settle`
   onto the `user_bets` row during `propagate_pick_settlement`, or continue
   joining to `picks.clv_value` at read time?
5. **`mode` as a first-class field** — worth surfacing on straight bets too
   (e.g. `mode=today_1_5h`) or parlay-only?

These questions gate Step 2 (`services/user_bet_ledger.py` design). No answer
is required to accept this audit.

## 14a. STEP 2 DECISIONS (approved by user; supersede §14 questions above)

The following decisions are the SOURCE OF TRUTH going forward. The Step 2
implementation in `services/user_bet_ledger.py` reflects them exactly.

1. **Idempotency (Q1)** — Client-generated `client_bet_id` is the primary
   handle. Server-side deterministic `idempotency_key` (SHA-256 over stable
   normalized fields) is the fallback. Never dedupe on display text alone.
2. **`void` vs `push` (Q2)** — Kept **analytically and operationally
   distinct**. `void` (invalidated/cancelled game) NEVER maps to `pushed`
   (graded on the exact line). Canonical vocabulary now includes both:
   `pending, won, lost, pushed, void, partially_settled, cancelled`.
3. **Admin delete cascade (Q3)** — Deferred to Step 7 (route cutover). Not
   part of Step 2. Step 2 does NOT modify `admin_delete_user`.
4. **CLV snapshot (Q4)** — CLV/opening/closing-line fields are declared as
   NULLABLE canonical fields. Never invented from unrelated data. When
   closing information is not captured, `clv_status="unavailable"` and
   `clv_value=null`. Populating from future frozen bet-time records belongs
   to a later implementation phase.
5. **`mode` (Q5)** — First-class field on the canonical wager (nullable),
   applicable to both straight and parlay wagers.

## 14b. STEP 2 CANONICAL FIELDS (finalized)

Canonical wager fields (`UserBet` dataclass in `services/user_bet_ledger.py`):
- Identity: `user_bet_id`, `client_bet_id`, `idempotency_key`, `user_id`.
- Type + status: `wager_type` (straight|parlay), `status`, `original_status`.
- Money: `stake_amount`, `stake_units`, `odds`, `odds_format`, `combined_odds`,
  `potential_payout`, `actual_payout`, `profit_loss`.
- Book: `sportsbook`.
- Time: `placed_at`, `settled_at`, `created_at`, `updated_at`.
- Provenance: `source`, `migration_version`, `migration_source`,
  `migration_source_id`, `is_legacy`.
- Discretionary: `mode`, `tags`, `risk_tier`, `correlation_warning`, `notes`.
- Reference: `prediction_id`, `snapshot_id`, `market_contract_id`,
  `board_version`, `event_id`, `sport_key`.
- Nullable future-line fields: `opening_line`, `opening_odds`, `closing_line`,
  `closing_odds`, `clv_value`, `clv_status`.
- `legs: list[UserBetLeg]` — parlay legs.
- `settlement_events: list[UserBetSettlementEvent]` — immutable audit trail.

Canonical parlay leg fields (`UserBetLeg`):
- `leg_id`, `prediction_id`, `snapshot_id`, `market_contract_id`, `event_id`,
  `sport_key`, `participant_id`, `market`, `selection`, `side`, `line`,
  `original_odds`, `sportsbook`, `status`, `original_status`, `actual_result`,
  `settled_at`.

Lines and odds are captured at wager creation and MUST NOT be rewritten later
with current market data (enforced by test #16 in
`tests/test_iter131_user_bet_ledger.py`).

---

## 15. What this audit did NOT do (per guardrail)

- No production writes.
- No `create_index` calls.
- No route changes.
- No dual-write shim.
- No implementation of `services/user_bet_ledger.py`.
- No backfill run (dry or otherwise).
- No modification to `parlay_history`.
- No deletion of any collection.
- No Phase 3H / Phase 4 / CFB / MLB shorthand / Line Shop / frontend cache /
  late-night west-coast prop work.

---

## 16. Cross-references

- `PHASE3G_PARITY_REPORT.md` — condensed field-by-field parity table with
  migration classifications (companion doc).
- `PHASE3G_MIGRATION_PLAN.md` — proposed staged cutover with rollback design
  (companion doc, plan-only, not authorized to execute).
- `PHASE3D_IDENTITY_AUDIT.md` — identity-contract audit that already flags
  `user_bets` and `parlay_history` as identity-eligible (see
  `services/identity_resolver.py::DRY_RUN_CRITICAL_COLLECTIONS`).
- `PHASE3C_INDEX_AUDIT.md` — index registry containing both collections'
  index specs (`services/index_registry.py:176-198`).

**End of Phase 3G Wager Ledger Audit.**
