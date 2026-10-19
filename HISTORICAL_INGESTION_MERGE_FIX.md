# NFL + CFB Historical Ingestion Merge Fix — Closure Report

**Scope:** Fix the DuplicateKeyError stall in NFL/CFB historical ingestion that has blocked NFL ingestion since 2026-10-11. Apply the identical structural fix to CFB.

**Guardrails honoured:** no push · no deploy · no Lock Score changes · no model weights touched · no canonical migration/session · no board-generation logic changes · no NHL changes · no frontend changes · no Production data touched · no unique-index changes · board-drop is **not** declared fixed by this work.

---

## Files changed

| File | Change |
| --- | --- |
| `backend/historical/nfl.py` | `_ingest_summary`: collect every ESPN stat block per (player, game) into one in-memory doc, then upsert ONCE with filter `{sport, game_id, player_id}`. No `stat_block` in filter. Preserves every stat family truthfully via namespaced raw keys (`nfl_passing_yds`, `nfl_rushing_yds`, …) + normalized family fields. Defensive duplicate-key resolver for pre-fix orphan rows. |
| `backend/historical/cfb.py` | Identical structural fix with `cfb_` prefix. |
| `backend/nfl_safe_engine.py` | Readers that previously filtered by `stat_block == <family>` now accept BOTH shapes via `$or` (legacy per-block rows + new merged rows via `stat_blocks` array membership). Aggregation pipeline updated with `$cond`/`$unwind` so (player, family) pair enumeration spans both shapes. ATD synthesis prefers namespaced family keys (`nfl_rushing_td`, `nfl_receiving_td`) with flat `nfl_td` fallback. |
| `backend/services/nfl_rationale.py` | Same `$or` compat on the recent-form query. |
| `backend/tests/test_historical_adapter_merge.py` | New 13-test regression suite. |

**Not touched** (verified via grep): `services/history_intelligence.py`, `services/historical_intelligence_projectors.py`, lock-score code, model weights, canonical slate / board-generation logic, NHL adapter, frontend. The authoritative unique shape `(sport, game_id, player_id)` was NOT changed.

---

## Root cause (as proven by Emergent Support, re-verified here)

`historical/nfl.py:_ingest_summary` wrote one Mongo doc per `(player_id, game_id, stat_block)` using an upsert filter that **included `stat_block`**. The authoritative unique shape on this collection is `(sport, game_id, player_id)` — no `stat_block`. When a dual-threat QB (Dak Prescott, Brock Purdy, Trevor Lawrence, …) appears in both `passing` AND `rushing` blocks of the same game, the second upsert’s filter did NOT match the first row (filter differs on `stat_block`) → the upsert attempted an INSERT → the insert collided against the unique shape → `DuplicateKeyError` → the whole ingestion for that event aborted. CFB inherited the same adapter pattern and the same defect.

---

## Regression test suite (13 / 13 passed)

```
tests/test_historical_adapter_merge.py
  ✓ test_nfl_qb_passing_plus_rushing_merges_to_one_doc
  ✓ test_nfl_skill_player_rushing_plus_receiving_merges_to_one_doc
  ✓ test_nfl_merged_doc_carries_both_stat_families
  ✓ test_nfl_rush_recv_merged_doc_preserves_both_families
  ✓ test_nfl_repeated_ingest_is_idempotent
  ✓ test_nfl_upsert_filter_shape_is_canonical
  ✓ test_nfl_unique_index_on_sport_game_player_can_be_enforced
  ✓ test_nfl_legacy_per_block_rows_resolved_on_merge
  ✓ test_nfl_no_duplicate_key_error_without_index
  ✓ test_cfb_qb_dual_threat_merges_to_one_doc
  ✓ test_cfb_rush_recv_merges_to_one_doc
  ✓ test_cfb_unique_index_enforced_and_idempotent
  ✓ test_merged_doc_matches_legacy_stat_block_in_predicate
```

**Full affected-suite sweep (68 / 68 passed)** including `test_commit_b5c_hi_adoption` + `test_commit_b2_history_telemetry` + `test_final_history_expansion` + `test_historical_intelligence_status_semantics` + `test_history_gap_closure` + `test_history_zero_route_cap` + the P0 soak suite (26/26 + 33/33).

---

## Canary proof (live ESPN data, scratch Mongo namespace, Production data untouched)

### NFL

| Player | Game | ESPN blocks seen | Merged `stat_blocks` | Normalized fields | Idempotent? |
| --- | --- | --- | --- | --- | --- |
| **Brock Purdy** (`espn_4361741`) | `espn_401872958` | `passing`, `rushing` | `['passing', 'rushing']` | `passing_yards=297`, `rushing_yards=34`, `any_td=1` | ✅ 2 runs → 1 row |
| **Dak Prescott** (`espn_2577417`) | `espn_401872960` | `passing`, `rushing` | *(stored legacy; next ingest cycle will merge)* | `passing_yards=276`, `rushing_yards=19` | ✅ Verified via fixture replay |
| **Trevor Lawrence** (`espn_4360310`) | `espn_401872957` | `passing`, `rushing` | *(stored legacy; next ingest cycle will merge)* | `passing_yards=182`, `rushing_yards=25` | ✅ Verified via fixture replay |

For each NFL canary:
- **newest real completed game** — present in `games` for the player's team.
- **newest stored game-log** — currently legacy per-block rows (2 rows / game for dual-threat QBs). The merge fix produces ONE merged row per game on the next ingest cycle (proven live for Purdy against `espn_401872958`).
- **newest HI row (`player_game_actuals`)** — populated by the `project_nfl_actuals` projector reading from `nfl_player_weekly` (independent of the `player_game_logs` adapter). Not affected by this fix.
- **newest HI API row** — same path as above; no regression.

### CFB

| Player | Game | ESPN blocks seen | Merged `stat_blocks` | Normalized fields | Idempotent? |
| --- | --- | --- | --- | --- | --- |
| **Emory Jones** (`espn_cfb_4360230`) QB | `espn_cfb_401404003` | `passing`, `rushing` | `['passing', 'rushing']` | `passing_yards=243`, `rushing_yards=-5` | ✅ 2 runs → 1 row |
| **Jayden McGowan** (`espn_cfb_4883774`) skill | `espn_cfb_401856813` | `rushing`, `receiving`, `puntReturns` | `['rushing', 'receiving', 'puntreturns']` | `rushing_yards=2`, `receiving_yards=34`, `receptions=2` | ✅ 2 runs → 1 row |

The McGowan case additionally proves the fix handles **more than two** stat blocks per player per game without regression.

---

## HI parity invariant (unchanged)

- `services/history_intelligence.py:query_historical` reads from `player_game_actuals` which is populated by the projector from `nfl_player_weekly` (NOT from `player_game_logs`). The merge fix therefore does NOT change HI output shape or ordering.
- `test_commit_b2_history_telemetry.py` + `test_commit_b5c_hi_adoption.py` all pass with my changes applied — current-season rows continue to outrank prior-season rows in HI.
- `hi_freshness_verifier.py` + `historical_intelligence_projectors.py` behaviour untouched.

---

## Rollback plan (if ever needed)

Single revert of the two adapter files + two reader files restores the previous shape — the DB rows written under the merged shape remain valid under the old filter (filter `{player_id, game_id, stat_block}` just matches zero rows and the next ingest cycle recreates per-block rows). No destructive migration needed either way.

---

**Explicitly NOT declared fixed by this work:** board drop. This task is historical-ingestion-only.
