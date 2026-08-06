# Phase 3G — Step 6 Writer Audit

**Status:** COMPLETE
**Scope:** Inventory of every writer that creates user-owned wagers.
Classifies each into "convert to UserBetLedger", "compatibility mirror
required", "learning/system writer (out of scope)", "dead/unused", or
"manual review".

---

## 1. Method

- `grep`-based inventory over `backend/**/*.py` for direct
  `db.user_bets.<write>` and `db.parlay_history.<write>` calls.
- Cross-referenced against every FastAPI route module.
- Cross-referenced against Step 1 writer inventory
  (`PHASE3G_WAGER_LEDGER_AUDIT.md` §4).

## 2. Writer inventory

| Writer (source location) | Endpoint / trigger | Collection | Row type | Step 6 classification |
|---|---|---|---|---|
| `routes/user_bets_routes.py::track_bet` (line 139) | `POST /api/user/bets/track` | `user_bets` | native straight/parlay | **convert to UserBetLedger** |
| `parlay_history.py::save_parlay` (invoked by `routes/parlay_history_routes.py::parlay_save` line 28) | `POST /api/parlay/save` | `parlay_history` | user-owned `p_*` | **convert to UserBetLedger + compatibility mirror** |
| `parlay_learning.py::record_parlay_shown` | auto on any `GET /api/picks/parlay` | `parlay_history` | learning `plearn_*` | **learning/system writer, out of scope** |
| `parlay_learning.py::settle_parlays` | scheduled job | `parlay_history` | learning `plearn_*` | learning/system writer, out of scope (Step 7 for user-owned settle) |
| `parlay_history.py::resolve_saved_parlays` | scheduled job | `parlay_history` | user-owned `p_*` | settlement writer, out of scope (Step 7) |
| `parlay_history.py::resettle_parlay` | `POST /api/parlay/{id}/resettle` | `parlay_history` | user-owned `p_*` | settlement writer, out of scope (Step 7) |
| `routes/user_bets_routes.py::propagate_pick_settlement` (line ~360) | invoked by all sport settlers | `user_bets` | any | settlement writer, out of scope (Step 7) |
| `routes/user_bets_routes.py::delete_user_bet` | `DELETE /api/user/bets/{id}` | `user_bets` | any | delete-only, no new insert; leave as-is |
| `routes/admin_users_routes.py::admin_delete_user` (cascade delete_many) | `DELETE /api/admin/users/{id}` | `user_bets`, `parlay_history` | any | delete-only; deferred (Step 7 cascade extension) |
| `services/parlay_intelligence/learning_loop.py::record_completed_parlay` | scheduled loop | `parlay_completions` | learning | writes to `parlay_completions`, NOT `parlay_history` or `user_bets` — **out of scope** |
| `scripts/backfills/user_bets_add_canonical_fields.py` | one-shot admin | `user_bets` | Step 3 in-place update | one-shot backfill, already executed |
| `scripts/backfills/execute_parlay_history_p_to_user_bets.py` | one-shot admin | `user_bets` | Step 5 migration | one-shot migration, already executed |

## 3. Classification summary

| Classification | Count |
|---|---:|
| Convert to UserBetLedger | **2** |
| Compatibility mirror required | **1** (parlay save; mirror goes to `parlay_history`) |
| Learning/system writer, out of scope | 3 |
| Settlement writer, out of scope (Step 7) | 3 |
| Delete-only, no changes | 2 |
| One-shot backfill/migration (already executed) | 2 |
| Dead/unused | 0 |
| Manual review | 0 |

## 4. Compatibility-mirror decision

**Required for parlay saves — YES.**

Reason: `GET /api/parlay/history`, `GET /api/parlay/{id}`,
`POST /api/parlay/{id}/resettle`, `DELETE /api/parlay/{id}`, and
`parlay_history.resolve_saved_parlays` (scheduled settler) all read
from `parlay_history` today. If we route parlay saves ONLY into
`user_bets`, the very next `GET /api/parlay/history` for that user
would return no rows — breaking the frontend.

Mirror design:
- Canonical write to `user_bets` via `UserBetLedger.create_parlay` runs FIRST.
- Compatibility mirror to `parlay_history` runs SECOND via the existing
  `parlay_history.save_parlay` path (unchanged) so all downstream readers
  and settlers keep working.
- The mirror row is annotated post-insert with:
  - `source = "user_bet_ledger_mirror"`
  - `user_bet_id = <canonical id>`
  - `mirrored_at = <utc>`
- **Learning rows are NEVER mirrored** — `record_parlay_shown` writes
  directly to `parlay_history` with `plearn_*` ids and is untouched by
  Step 6.
- Straight bets are NOT mirrored to `parlay_history` — no legacy
  reader requires this.
- Mirror is idempotent because `save_parlay` computes a deterministic
  `p_<sha1(user_id+sorted leg_ids)>` id and uses `insert_one` guarded
  by `find_one` (existing behaviour).

**Exit plan for the mirror**: dropped in Step 7 once
`/api/parlay/history` reads from `user_bets`.

## 5. Frontend impact

Every existing client works unchanged. The optional `client_bet_id`
field is additive — old clients that omit it fall back to the
server-side deterministic `idempotency_key`.

No response envelope changes. Every existing key on every write
response is preserved.

## 6. Learning/system writers untouched

- `parlay_learning.record_parlay_shown` — plearn_* writer.
- `parlay_learning.settle_parlays` — plearn_* settler.
- `parlay_history.resolve_saved_parlays` — user-owned p_* settler (Step 7).

All three continue to operate on their existing collections with no
changes in Step 6.
