# BLOCK 2A.5.2 — MLB HITTER-PROP PRODUCTION REACHABILITY — CORRECTED

Return token: **`BLOCK2A5_MLB_BASELINE_CORRECTED`**

Date: 2026-06 (current session)
Status: **DONE**

────────────────────────────────────────────────────────────────

## 1. Diagnosis — the exact drop points

### DROP POINT #1 (P0 — TOTAL BASES HARD-DROPPED)
- File: `backend/sports_engine.py` line 4128 (pre-fix)
- Symptom: EVERY `batter_total_bases` / `batter_total_bases_alternate`
  outcome was unconditionally `continue`d inside `_props_picks_from_event`.
- Root cause: A stale defensive filter from a 2026-06-19 removal that
  was never reverted when Total Bases markets were re-added to
  `PLAYER_PROP_MARKETS["MLB"]` on 2026-06-24. Wiring matrix
  (`services.pipeline_diagnostic._WIRING_EVIDENCE`) still classifies
  TB as `FULLY_WIRED` (COPY_OF_HITS) — the runtime contradicted the
  matrix.

### DROP POINT #2 (P1 — LINEUP PROVENANCE ABSENT AT EMISSION)
- File: `backend/services/game_context.py` — hitter enrichment loop
  populated `ctx["hitters"][name]` from MLB StatsAPI `feed/live`
  `battingOrder`, but did NOT stamp lineup provenance flags
  (`lineup_confirmed`, `is_starter`, `lineup_slot`, `lineup_source`).
- File: `backend/sports_engine.py` — the MLB hitter branch of
  `_props_picks_from_event` never invoked `classify_lineup_status`
  from `services.mlb_gates`. The helper existed and was tested, but
  was not wired into emission. As a result, hitter picks reached the
  board WITHOUT explicit lineup provenance and WITHOUT the
  `data_quality_cap_for_status` cap.

### NON-DROP (LATENT MAGIC BUG surfaced by fix)
- File: `backend/services/magic_tier_policy._extract_lineup_certainty`
  assumed `pick["lineup_status"]` was a bare STRING, but
  `services.enrichment.lineups.enrich_pick_with_lineup` (production
  path) stamps a DICT. Any pick that reached Magic AFTER lineup
  enrichment therefore crashed with `AttributeError: 'dict' object
  has no attribute 'lower'`. Pre-existing latent bug that our new
  dict-shaped `lineup_status` also exposed, so it was fixed inline.

────────────────────────────────────────────────────────────────

## 2. Authoritative lineup source (existing, not added)

`MLB StatsAPI /game/{gamePk}/feed/live` → `boxscore.teams.{home,away}.battingOrder`.
- Populated when the CONFIRMED starting lineup is posted (~1 h
  pre-first-pitch).
- Empty otherwise → **fail-closed**: `ctx["hitters"]` stays empty →
  `has_enough_real_data("hitter_prop")` returns False → no hitter
  candidate emits. This is the correct behavior per spec ("fail
  closed if unavailable/unauthorized").
- Repo does NOT ship an independent "projected-lineup" provider.
  Provenance is stamped as `confirmed_starter` when battingOrder is
  present; the `projected_starter` LINEUP_STATE path in
  `mlb_gates` remains dormant infrastructure ready for a future
  provider (no synthetic projections invented).

────────────────────────────────────────────────────────────────

## 3. Production fixes (minimum-viable, surgical)

### Fix A — `backend/sports_engine.py` (line 4128 area)
Replaced the unconditional TB drop with:
```python
if mk in ("batter_total_bases", "batter_total_bases_alternate"):
    try:
        if float(point) == 0.5:
            continue
    except (TypeError, ValueError):
        continue
```
Only line 0.5 (equivalent to Hits 0.5) is dropped. Every other
real TB line (1.5, 2.5, 3.5, alt near-locks) survives to candidate
generation.

### Fix B — `backend/services/game_context.py` (hitter enrichment loop)
Each `hitter_row` populated from `battingOrder` now carries:
- `lineup_confirmed = True`
- `is_starter       = True`
- `lineup_slot      = <1-9 batting-order slot>`
- `lineup_source    = "statsapi_feed_live_batting_order"`

Provenance is now explicit and inspectable downstream.

### Fix C — `backend/sports_engine.py` (MLB hitter branch of `_props_picks_from_event`)
BEFORE `build_mlb_hitter_factors`, invoke
`classify_lineup_status` from `services.mlb_gates` using the
hitter_row flags. Behavior:
- `bench` / `scratched` → hard drop with `mlb_gates.record_rejection`.
- `confirmed_starter` → 99.0 cap (no effective cap).
- `projected_starter` → 92.0 cap.
- `unknown` → 79.0 cap (< 85 board floor → fails closed on any
  enrichment gap).

At `_build_pick` time, stamp:
```python
new_pick["lineup_status"] = {
    "status":     "<confirmed_starter|projected_starter|unknown>",
    "lineup_pos": <int|None>,
    "source":     "statsapi_feed_live_batting_order",
}
```
Also applies `data_quality_cap_for_status` and records the cap
under `caps_applied`. Original score preserved in
`lock_score_uncapped` for audit.

### Fix D — `backend/services/magic_tier_policy.py`
`_extract_lineup_certainty` now handles BOTH the dict form (as
stamped by `enrich_pick_with_lineup` and by Block 2A.5.2 hitter
emission) AND the legacy string form. Prevents Magic from crashing
on any pick that has traversed lineup enrichment.

────────────────────────────────────────────────────────────────

## 4. What we did NOT change
- Lock Score formula
- 85/86 threshold
- 99 Lock
- APEX 100
- Magic weighting
- MLB totals neutrality fix (Block 2A.5.1)
- Any unrelated scoring / ranking
- No new provider added; no fabricated lineups, alt lines, odds.

────────────────────────────────────────────────────────────────

## 5. End-to-end proof (integration test)

File: `backend/tests/test_block2a5_2_mlb_hitter_reachability.py`

28 tests / 9 sections:
- §A  Total Bases regression fixture (hard-drop removed)
- §B  Lineup gate wired at emission
- §C  Feature engine coverage gate (fail-closed on empty ctx)
- §D  Wrong player / wrong event rejected
- §E  Magic tier wiring for Hits + Total Bases
- §F  Canonical publication + `BoardProjectionService` projection
  proves Hits, Total Bases, HR, RBI reach the Locks board.
- §G  Real-line integrity (no `book_odds` → ineligible)
- §H  Alt-lines preserved as distinct market candidates
- §I  Wiring matrix declares all four hitter markets

**Mandatory E2E fixtures (Hits + Total Bases) both project via
`BoardProjectionService().project_ids([pick])`:**
```
tests/test_block2a5_2_mlb_hitter_reachability.py::TestReachesBoardProjectionEndToEnd::test_hits_pick_reaches_board            PASSED
tests/test_block2a5_2_mlb_hitter_reachability.py::TestReachesBoardProjectionEndToEnd::test_total_bases_pick_reaches_board     PASSED
tests/test_block2a5_2_mlb_hitter_reachability.py::TestReachesBoardProjectionEndToEnd::test_home_run_pick_reaches_board        PASSED
tests/test_block2a5_2_mlb_hitter_reachability.py::TestReachesBoardProjectionEndToEnd::test_rbi_pick_reaches_board             PASSED
```
Full file result: **28 passed in 0.12s**.

Block 2 regression suite (all Block 2A/2B/2C/2D/2E files +
`test_phase4c_mlb.py`): **261 passed, 1 skipped** (env-only).

────────────────────────────────────────────────────────────────

## 6. Known pre-existing failures — classified

Re-ran the four failures noted in prior handoff:
- `test_phase2_elite_gate_and_h2h.py::test_elite_gate_demoted_pick_above_85_remains_on_board`
- `test_phase2_elite_gate_and_h2h.py::test_locks_contract_still_strictly_gt_85`
- `test_mlb_grading_fix_iter71.py::TestPostFixDbState::test_no_remaining_grade_disagreement_flags`
- `test_mlb_grading_fix_iter71.py::TestPostFixDbState::test_machado_2026_07_09_hits_lost`

Classification: **PRE_EXISTING / TEST_ISOLATION**. Verified by
running the same tests against the base commit `b62459ee` (before
Block 2A.5.2 edits) with `git stash` — all four fail identically.
No new regression introduced by this block. Root causes are
outside 2A.5.2 scope (`main_board_eligibility` threshold arithmetic
and Machado 2026-07-09 grading disagreement — both under the "Do
Nothing" list in the handoff).

────────────────────────────────────────────────────────────────

## 7. Files touched
```
modified: backend/sports_engine.py
modified: backend/services/game_context.py
modified: backend/services/magic_tier_policy.py
added:    backend/tests/test_block2a5_2_mlb_hitter_reachability.py
added:    BLOCK2A5_MLB_BASELINE_CORRECTED.md   (this report)
```

Return token: **`BLOCK2A5_MLB_BASELINE_CORRECTED`**
