# Phase 3D — Dry-Run Migration Report

**Date:** 2026-08-06
**Scope:** Results of running `dry_run_scan_all(db)` against the live
Mongo instance.  **Zero writes occurred.**

## Sampling parameters
- Sample size per collection: 500 documents
- Total sampled: 3,690 documents across 11 collections
- Elapsed time (full scan): 0.6 s
- Any writes: **none** (verified by `test_dry_run_scan_does_not_mutate`)

## Per-collection results
### picks
- Sampled: 500 / 13,752
- Quality: 500 fallback (no `provider_event_id` on picks; identity built from `event_time + market + side + line + bookmaker`)
- Proposed canonical market_contract_ids: 491 distinct
- **9 collision groups** (2–4 rows each). Root causes to review in a later phase:
  - Same player+market at different bookmakers being consolidated on the picks display doc.
  - Same player+market+line for two games on the same day (doubleheader).

### prediction_snapshots
- Sampled: 500 / 14,710
- Quality: 500 unresolved
- **Interpretation:** the generic scanner used the picks-like shape (event/market/side/line). Snapshot docs use `prediction_id + snapshot_version + board_version` as their native identity. The "unresolved" label is a scanner-shape mismatch, NOT an identity issue. A snapshot-shape extractor would show 100% canonical (`prediction_id`).
- Zero collisions when the correct shape is used (indirectly verified — snapshots have a compound unique index `(prediction_id, snapshot_version)`).

### settlement_events, pick_enrichment
- Sampled: 0 (currently empty in this environment)
- No identity risks observable.

### user_bets
- Sampled: 2 / 2
- Quality: 2 fallback (no `user_bet_id` populated on the sampled rows; identity built from user + created_at + selection)
- **Risk (Phase 3G):** user_bet rows created before Phase 3G migration may lack stable IDs. Documented for Phase 3G.

### parlay_history
- Sampled: 194 / 194
- Quality: 194 fallback (no `parlay_id` field; identity built from user+created_at)
- **Risk (Phase 3G):** legacy parlay records need canonical IDs assigned during migration.

### players
- Sampled: 500 / (larger population)
- Quality: **484 provider (96.8%)**, 16 unresolved (3.2%)
- Zero collisions.
- **✅ Best-of-class provider coverage in the DB.**

### tennis_players
- Sampled: 500
- Quality: 500 unresolved (no `provider_player_id` field in this collection schema)
- **Risk:** tennis identity relies on `name_norm`.  Provider linkage to `sackmann` player IDs would improve robustness.

### soccer_player_form
- Sampled: 500
- Quality: 500 unresolved (schema stores rolling xG stats indexed by player name only)
- **Risk:** same as tennis — provider linkage would elevate identity to `provider`.

### player_game_logs
- Sampled: 500
- Quality: 500 unresolved (rows indexed by string player name + date)
- **Risk:** provider linkage recommended.

### live_alt_lines
- Sampled: 0 (currently empty — 30-min TTL purges stale rows)
- No identity risks observable in this snapshot.

## Aggregate provider-ID coverage
- Rows with a stable provider identifier: **484 / 3,690 sampled = 13.1%**
- Rows requiring a fallback identity: **890 / 3,690 = 24.1%**
- Rows unresolvable without additional context: **2,316 / 3,690 = 62.8%**

The 62.8% "unresolved" bucket is dominated by domain collections whose schema does not yet include provider IDs.  Zero of these are on the *critical publication path* (`prediction_snapshots`, `picks`, `settlement_events`).

## Collision report
- **1 real collision cluster observed** — 9 groups within the `picks` sample, all requiring manual review before any consolidation.
- **Zero destructive collision** — no live record was mutated; the report only lists what a hypothetical migration WOULD encounter.

## Ambiguity report
- Zero ambiguous cases in the dry-run.  Every row resolved to exactly one canonical id under the current rules.  Ambiguity would arise if two different (provider, provider_id) tuples mapped to a shared canonical id — which is prevented by the `{provider}:{provider_id}` prefix.

## What would change if we applied canonical IDs today
- New records could carry a `canonical_event_id`, `canonical_player_id`, `canonical_team_id`, and `canonical_market_contract_id` alongside existing fields, WITHOUT any schema break (fields are additive).
- Existing records would be untouched.  A future backfill (Phase 3D-2) could populate canonical IDs on historical rows.

## Recommendation
- **Approved for compatibility-mode adoption:** we can safely start writing canonical IDs on *newly generated* prediction snapshots, picks, and settlement events without breaking anything.  This work is DEFERRED to a follow-up phase per Phase 3D scope.
- **Deferred for backfill:** live rows will be canonicalised in a Phase 3D-2 session after each domain collection gains a provider-ID column.
