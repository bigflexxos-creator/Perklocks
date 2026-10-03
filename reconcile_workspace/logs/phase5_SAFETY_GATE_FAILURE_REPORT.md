# Phase 5 — SAFETY GATE FAILURE REPORT

**Status:** 🔴 **STOPPED — Phase 5 cannot complete safely as currently configured.**
**Session:** `perklocks-cutover-20261003`
**Target:** `*.emergent.host` (Production, fallback mode active)
**Legacy DB fingerprint pre-run:** `b48e071d8e447c23` (unchanged — legacy NOT touched)

---

## Preflight — PASSED ✅
`GET /api/admin/data-authority/status` returned:

| flag | value | required |
|---|---|---|
| CANONICAL_FALLBACK_MODE | **true** | ✓ |
| USE_CANONICAL_DB | **false** | ✓ |
| CANONICAL_IMPORT_ENABLED | **true** | ✓ |
| BACKGROUND_WORKERS_ENABLED | **false** | ✓ |

Note: `DATA_AUTHORITY="preview"` appears in the live env flags — not a Phase-5 blocker, but
please verify whether Production is intentionally reporting authority as `preview` or if
that key should be cleared / set to `production` before Phase 9 flip.

---

## Phase 5 — SAFETY GATE FAILURE 🔴

The driver successfully authenticated and began posting batches to `/api/admin/canonical-import`,
but a **schema contract mismatch** between the server-side `_LOGICAL_KEYS` table
(`backend/services/canonical_cutover.py`, lines 70-92) and the actual reconciled checkpoint docs
causes **100% logical-key rejection in 11 of 21 collections**. Those collections are being
tracked as "accepted" by the import session, but **ZERO rows are actually upserted into
`canonical_<name>`** because the server-side `UpdateOne(key, …)` loop skips every doc with
`MISSING_LOGICAL_KEY_COMPONENT`.

### 11 collections affected (logical-key mismatch, 100% rejection)

| collection | server_key (current) | reconciler/doc field (actual) | fix direction |
|---|---|---|---|
| `historical_ingestion_state` | `(provider, sport, season)` | doc has only `sport, season` (no `provider`) | server → `(sport, season)` or `(_id,)` |
| `nfl_ingest_meta` | `(key,)` | doc has only `_id` | server → `(_id,)` |
| `parlay_history` | `(parlay_id,)` | doc has `signature` and `_id` (no `parlay_id`) | server → `(signature,)` *(reconciler emits `id=_id` — confirm) |
| `picks` | `(pick_id,)` | doc has `id` (uuid) and `_id` (no `pick_id`) | server → `(id,)` |
| `player_game_actuals` | `(sport, event_id, player_id, market)` | doc has `sport, event_id, player_id` — **no `market`** field | server → drop `market`, use `(sport, event_id, player_id)` **OR** verify reconciler intent (reconciler key sample shows empty-string market) |
| `rollover_slate_events` | `(event_id,)` | doc has `slate_date, event, at, slate_id` — no `event_id` | server → `(slate_date, event, at)` |
| `soccer_matches` | `(match_id,)` | doc has `league, season, home_team, away_team, date` — no `match_id` | server → `(league, season, home_team, away_team, date)` |
| `team_game_actuals` | `(sport, event_id, team)` | doc has `sport, event_id, canonical_team_id` — no `team` scalar | server → `(sport, event_id, canonical_team_id)` |
| `tennis_matches_history` | `(match_id,)` | doc has `tourney_id, winner_id, loser_id, date` — no `match_id` | server → `(tourney_id, winner_id, loser_id, date)` |
| `user_bets` | `(bet_id,)` | doc has `id` (uuid) and `_id` — no `bet_id` | server → `(id,)` |
| `settlement_events` | `(event_id,)` | doc has `event_id` BUT reconciler-logical-key is `settlement_id` (one-of-a-kind mismatch) | needs explicit sign-off — doc-level `event_id` is present, so upserts *do* work, but canonical identity may not match reconciliation intent |

### 1 collection affected (empty P6 overlay nukes entire collection)

| collection | p5 file | p6 file | effect |
|---|---|---|---|
| `prediction_snapshots` | 232,089 rows | **0 bytes (empty)** | Push driver prefers P6 when it exists → **0 rows posted**; the 232 K prediction snapshots are dropped. |

This is a bug in the push driver's overlay logic (`src = p6_file if p6_file.exists() else p5_file`
treats empty-file as valid overlay and nukes the collection). Needs an explicit `size > 0`
check, OR the P6 reconciliation must emit the full post-overlay snapshot for this collection.

### 10 collections passing cleanly ✅

`games`, `nfl_player_weekly`, `player_game_logs`, `player_identities`, `pregame_snapshots`,
`publication_events`, `rollover_slates`, `settlement_events`, `soccer_player_game_logs`,
`users` — logical keys present in every doc, data is upserting correctly into
`canonical_<name>`.

---

## Immediate consequences (as of STOP)

1. **No legacy data was modified** — fingerprint unchanged, rollback authority intact.
2. **Idempotency-session is recorded as "in progress"**; batches for the 11 mismatched
   collections are stored in `_canonical_import_batches` but produced **zero upserts**.
3. **`prediction_snapshots` canonical collection has 0 documents**.
4. 10 collections DO have canonical data populated (idempotent — safe to resume).
5. Phases 6, 7, 8 **NOT started**. `USE_CANONICAL_DB` **NOT flipped**. Workers **NOT enabled**.

---

## Recommended path forward (requires your approval)

**Option A — Fix server-side `_LOGICAL_KEYS` to match reconciler + checkpoint reality.**
I update `backend/services/canonical_cutover.py` lines 70-92 to the field tuples listed
above, write a server-side migration note, and ship the patch. You republish Production.
I then relaunch the push driver with a **new session id** (so the old session's zero-upsert
batches don't get mistaken for valid completion) and re-run Phase 5.

**Option B — Fix the reconciliation checkpoints to carry the server-expected field names.**
Rebuild Phase 5 + 6 canonical NDJSONs with the server's logical-key fields explicitly
present. Longer, higher-risk — not recommended unless the server contract is immutable.

**Option C — Mixed.** Fix 10 of the 11 on the server (clear semantic match) and treat
`settlement_events` / `prediction_snapshots` as needing a reconciler fix.

My recommendation: **Option A**, plus a `size > 0` guard in the push driver for the
`prediction_snapshots` empty-overlay bug.

---

## What I DID NOT do (per your safety contract)

- ❌ Did not activate `USE_CANONICAL_DB`
- ❌ Did not enable background workers
- ❌ Did not touch legacy Production collections
- ❌ Did not continue to Phase 6, 7, or 8
- ❌ Did not log, print, echo, or persist any credentials
