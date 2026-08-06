# Phase 3G — Parity Report (Companion to Wager Ledger Audit)

**Status:** DRAFT · audit-only · no code changes shipped
**Companion of:** `PHASE3G_WAGER_LEDGER_AUDIT.md`
**Purpose:** Condensed field-level parity map with per-field migration
classification. Every field on the requested list is classified.

Legend:
- **exact** — same semantics + same encoding; safe rename.
- **derivable** — can be computed deterministically from present data.
- **legacy-only** — only exists in `parlay_history`; canonical must be extended.
- **canonical-only** — only exists in `user_bets`; legacy will get it during dual-write, otherwise dropped for legacy rows on migration.
- **missing** — neither store has it today.
- **ambiguous** — value exists on both sides but with different meaning, units, sign convention, or encoding.
- **unsafe** — cannot be auto-migrated without human review.

Scope caveats:
- "Legacy" throughout this document means the **`p_*` subset** of
  `parlay_history` (user-saved parlays). The `plearn_*` subset is **not** a
  wager ledger source and is excluded from parity.
- Values marked "derivable" are only derivable **on rows that already carry
  the source data** in the live dataset. Coverage numbers reference §11 of
  the audit doc.

---

## 1. Parity table (per user's field list)

| # | Field                            | `user_bets` field                                      | `parlay_history` (`p_*`) field                     | Classification | Notes / migration action                                                                                                              |
|---|----------------------------------|--------------------------------------------------------|----------------------------------------------------|----------------|----------------------------------------------------------------------------------------------------------------------------------------|
| 1 | user_id                          | `user_id` (UUID str)                                   | `user_id` (UUID str)                               | **exact**      | 100 % coverage in dev sample on both sides.                                                                                            |
| 2 | wager ID / parlay ID             | `id` (UUID v4)                                         | `id = "p_" + sha1(user_id | sorted legs)[:14]`     | **ambiguous**  | Different id styles. Migration retains legacy id as `migration_source_id`; canonical remains UUID.                                     |
| 3 | placed_at                        | `created_at` (BSON datetime)                           | `created_at` (ISO 8601 string)                     | **ambiguous**  | Type mismatch, semantics identical. Normalize to BSON datetime on migrate.                                                             |
| 4 | wager_type                       | `bet_type ∈ {straight, parlay}`                        | implicit `parlay` (row exists → parlay)            | **derivable**  | Fill `bet_type="parlay"` on backfill.                                                                                                  |
| 5 | status                           | `pending | won | lost | push`                          | `live | won | lost` (Writer A)                     | **ambiguous**  | Remap `live → pending`. See status map in audit §8b.                                                                                   |
| 6 | stake amount                     | `stake_units` (float, units)                           | `stake` (float, units by convention)               | **derivable**  | Rename `stake → stake_units` on migrate.                                                                                               |
| 7 | stake units                      | `stake_units`                                          | `stake`                                            | **derivable**  | Same as #6.                                                                                                                            |
| 8 | odds                             | `odds_at_bet` (int American; single-leg or combined)   | per-leg via `legs[i].book_odds`; combined via `combined_odds` | **derivable** | On parlays canonical `odds_at_bet = combined_odds`. Preserve per-leg odds under `legs[]` on canonical (new field).                    |
| 9 | odds format                      | American int                                           | American int                                       | **exact**      |                                                                                                                                        |
|10 | combined odds                    | `odds_at_bet` (holds combined when bet_type=parlay)    | `combined_odds` (int American)                     | **derivable**  | Populate `combined_odds` on canonical **as a new field**; keep `odds_at_bet` semantically = combined for parlays.                     |
|11 | potential payout                 | not stored (computed on read via `_american_to_profit`) | not stored (computed via `_payout_per_unit`)      | **missing**    | Formalize a shared helper on `services/user_bet_ledger.py` at read time; do not persist.                                              |
|12 | actual payout                    | `pnl_units` (signed float)                             | `payout` (float, positive-only, null pre-settle)   | **ambiguous**  | Mapping: won → `pnl_units = +payout`; lost → `pnl_units = -stake_units`; push → `pnl_units = 0`; live/pending → 0. Verify per-row.     |
|13 | sportsbook / book                | not stored                                             | not stored                                         | **missing**    | Neither store has this today. Deferred to a future extension.                                                                          |
|14 | prediction_id                    | `pick_id` (FK → `picks.id`)                            | `leg_ids[i]` (FK → `picks.id`)                     | **derivable**  | Rename `leg_ids → parlay_legs` on migrate (already the canonical field).                                                              |
|15 | snapshot_id                      | not stored                                             | not stored (partial data in `legs[i]`)             | **missing**    | Would require snapshot integration with `prediction_publication_service`. Deferred.                                                    |
|16 | event_id                         | not stored (only `event` display string)               | not stored (only `legs[i].event` display)          | **missing**    | Derivable at read time via `pick_id → picks.event_id`.                                                                                 |
|17 | sport                            | `sport` (denorm)                                       | `legs[i].sport` (per-leg)                          | **derivable**  | Canonical stores primary sport; per-leg preserved under a new `legs[]` field on canonical.                                            |
|18 | market                           | `market` (denorm; e.g. "3-leg parlay" for parlays)     | `legs[i].market`                                   | **derivable**  | Canonical rolls-up into a summary string; per-leg preserved.                                                                           |
|19 | selection                        | `selection` (denorm)                                   | `legs[i].selection`                                | **derivable**  | Similar to market.                                                                                                                     |
|20 | exact line                       | not stored                                             | not stored on wager (leg may have `line`)          | **missing**    | Would require enrichment at track time.                                                                                                |
|21 | original bet-time odds           | `odds_at_bet` (combined for parlays)                   | `legs[i].book_odds` (per-leg)                      | **derivable**  | Preserve per-leg odds under canonical `legs[]`.                                                                                        |
|22 | leg result                       | derived at read via `picks.status`                     | `legs[i].status` (persisted at settle)             | **ambiguous**  | Legacy persists last-seen leg status inside the snapshot; canonical must persist this on canonical `legs[]` to preserve resettle path. |
|23 | settled_at                       | `settled_at` (BSON datetime, nullable)                 | `settled_at` (ISO string, nullable)                | **ambiguous**  | Type mismatch. Normalize to BSON datetime on migrate.                                                                                  |
|24 | push / void handling             | `push` terminal + 0 P/L (canonical)                    | user-saved has no `push`; leg status may be `void` | **ambiguous**  | Formalize on canonical: `void` at leg-level collapses to `push` at parlay-level unless combined with a `lost` leg.                    |
|25 | CLV                              | not stored                                             | not stored                                         | **missing**    | Available on `picks.clv_value` post-settle; join at read time or backfill snapshot onto canonical (design decision open — audit §14). |
|26 | opening line                     | not stored                                             | not stored                                         | **missing**    | Deferred.                                                                                                                              |
|27 | closing line                     | not stored                                             | not stored                                         | **missing**    | Available on `picks.closing_odds` post-settle. Same treatment as CLV.                                                                  |
|28 | tags                             | not stored                                             | `mode` (informal tag: standard/advanced/high_risk/today) | **derivable** | Add `mode` (nullable) to canonical.                                                                                                    |
|29 | risk tier                        | not stored                                             | `mode` (informal)                                  | **derivable**  | Same as #28.                                                                                                                           |
|30 | correlation warning              | not stored                                             | not stored on `p_*`; only on `plearn_*` (out of scope) | **missing on wager** | Deferred.                                                                                                                       |
|31 | source                           | not stored                                             | not stored (implied by `id` prefix)                | **missing**    | Add `source ∈ {user_track, parlay_save, backfill_p, dual_write}` on canonical.                                                        |
|32 | migration source ID              | not stored                                             | not stored                                         | **missing**    | Add `migration_source_id` on canonical. Backfill sets it to the source `p_...` id.                                                     |
|33 | idempotency key                  | none (UUID)                                            | strong (`p_<sha1(user_id | sorted legs)>`)         | **ambiguous**  | Canonical target: option 4 (`client_bet_id`) preferred with option 1 fallback. See audit §9.                                          |

### Fields present on canonical but NOT on legacy
- `notes` (canonical-only; free-text ≤500 chars) — retained; legacy backfill sets `notes=null`.
- `bet_type` (canonical-only) — legacy backfill sets `bet_type="parlay"`.
- `pnl_units` (canonical-only, signed) — legacy backfill computes from
  `(status, stake, combined_odds, payout)`.

### Fields present on legacy but NOT on canonical (must be added under §12/§13)
- `legs[]` inline snapshot — REQUIRED for resettle-when-parent-pick-deleted.
- `combined_odds` — REQUIRED to avoid recomputing on every read.
- `mode` — REQUIRED to preserve mode-based analytics (`standard`/`advanced`/`high_risk`/`today`).
- `legs_won/legs_lost/legs_pending` — derivable roll-ups; recommend derived at read time rather than persisted.

---

## 2. Rules the audit tooling will follow (not yet built)

- **Do not invent missing values.** If a source row lacks a field required by
  the canonical schema, the migration script MUST mark it under
  "requires manual review" and skip.
- **Do not dedupe by display text.** No matching on `event`, `selection`,
  `player_name`, or rounded timestamps.
- **Preserve legacy IDs.** Every backfilled canonical row carries the
  original `p_*` id under `migration_source_id`. Rollback is a metadata flip.
- **Idempotency of the backfill.** Rerunning the script on the same source
  set must be a no-op (upsert keyed on `migration_source_id`).

---

## 3. Row-level classification of the live dev sample

Live counts for the `p_*` subset only:

| Classification            | Count | Rationale                                                                                          |
|----------------------------|------:|----------------------------------------------------------------------------------------------------|
| Safe to migrate            |   4  | All 4 rows carry `user_id`, `created_at`, `leg_ids`, `legs`, `combined_odds`, `stake`, `status`.   |
| Requires fallback mapping  |   0  | No rows missing derivable-from-present-fields data.                                                |
| Requires manual review     |   0  | No rows referencing deleted users, no legless rows, no rows with unparseable status.               |
| Unsafe to migrate          |   0  | (`plearn_*` are structurally excluded and not counted here.)                                        |

Production numbers will differ. The migration script (deferred) will emit
this exact breakdown during its `--dry-run` phase.

---

## 4. Idempotency-key collision matrix (design candidates)

| Candidate                                                       | Collision risk in dev (0-4 rows)         | Collision risk expected in prod  | Comments                                                        |
|-----------------------------------------------------------------|------------------------------------------|----------------------------------|----------------------------------------------------------------|
| `(user_id, sorted leg_ids)`                                     | 0                                        | Very low — matches legacy `p_*`  | User cannot deliberately re-track same parlay.                 |
| `(user_id, sorted leg_ids, placed_at_bucket_minute)`            | 0                                        | Zero                             | Allows deliberate re-tracks after a minute; higher volume.     |
| `(user_id, source_record_id)` for backfill                      | 0                                        | 0 by construction                | Ideal for the migration step.                                  |
| `client_bet_id` (UUID from client)                              | n/a today (not sent)                     | 0                                | Requires client change; safest go-forward.                     |
| `(user_id, identity-normalized leg ids, placed_at bucket)`      | n/a — Phase 3D not enforced yet          | Unknown until Phase 3D is live   | Future-proof but blocked by Phase 3D acceptance.               |
| **REJECTED**: `(user_id, event, selection, placed_at_rounded)`  | High — display drift                     | High                             | Explicitly disallowed per user's §5.                            |

No decision made. All candidates carried to the migration plan for review.

**End of Phase 3G Parity Report.**
