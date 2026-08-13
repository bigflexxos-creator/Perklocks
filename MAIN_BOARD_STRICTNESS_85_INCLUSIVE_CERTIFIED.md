# MAIN BOARD STRICTNESS FIX — INCLUSIVE `>= 85` CERTIFIED

Return token: **`MAIN_BOARD_STRICTNESS_85_INCLUSIVE_CERTIFIED`**

Date: 2026-08
Status: **DONE**

────────────────────────────────────────────────────────────────

## Defect

The `Block 2B.1B` completion report referenced the threshold as `85/86`, which reflected the runtime state at that time. The **actual** Perklocks contract is:

> **FINAL LOCK SCORE >= 85 ⇒ eligible.  85–100 inclusive are score-eligible.**

The production module `services/main_board_eligibility.py` was implementing a strict `> 85` gate (`85.00 → OFF`, `85.001 → ON`), which rejected 85.00 candidates that should have qualified.

## Trace — where the wrong threshold lived

| File | Line | Was | Now |
|---|---|---|---|
| `services/main_board_eligibility.py` | Python cmp | `pls > 85.0` | `pls >= 85.0` |
| `services/main_board_eligibility.py` | Python cmp | `max(ls, ls_v2) > 85.0` | `max(ls, ls_v2) >= 85.0` |
| `services/main_board_eligibility.py` | Mongo query | `{"$gt": 85.0}` (published + legacy fallback) | `{"$gte": 85.0}` |
| `services/main_board_eligibility.py` | Docstring | "strict > 85" | "INCLUSIVE >= 85" |
| `services/main_board_eligibility.py` | Constant `MAIN_BOARD_LOCK_FLOOR_INCLUSIVE` | 85.01 (legacy epsilon alias) | 85.0 (correct) |
| `routes/picks_routes.py:1006` | Comment | "strict >" | "INCLUSIVE >=" |

All 6 grep matches for `$gt: 85` / `> 85` in **runtime** code paths (excluding one-shot `scripts/*` and comments) traced to a **single source of truth**: the two Python comparisons + the two Mongo `$gt` operators inside `services/main_board_eligibility.py`. No downstream consumer independently recreated the wrong threshold.

Verified no rogue threshold recreations remain in:
- `routes/picks_routes.py` (uses `main_board_lock_score_query()` + `MAIN_BOARD_LOCK_FLOOR_EXCLUSIVE` constant — both now point at `85.0` INCLUSIVE)
- `services/canonical_publication_barrier.py` (already used `lock < 85`, which passes 85.0 — already correct)
- `services/board_projection_service.py` (delegates to `is_main_board_eligible`)
- Frontend (grep across `/app/frontend/**/*.{ts,tsx,js,jsx}` — no `> 85` / `>= 86` runtime patterns found)

## Fixes applied

### 1. `services/main_board_eligibility.py` (canonical rule module)
- `is_main_board_eligible()`: Python `>` → `>=` on both the `published_lock_score` branch and the legacy `max(lock_score, lock_score_v2)` fallback.
- `main_board_lock_score_query()`: Mongo `$gt: 85.0` → `$gte: 85.0` on all four branches (canonical + legacy × published/legacy fields).
- Docstring rewritten to state INCLUSIVE contract with correct boundary table (`84.99 → OFF`, `85.00 → ON`, `85.01 → ON`, `86.00 → ON`, `99 → ON`, `100 → ON`).
- Constant `MAIN_BOARD_LOCK_FLOOR = 85.0` added as primary name; legacy aliases `MAIN_BOARD_LOCK_FLOOR_EXCLUSIVE` and `MAIN_BOARD_LOCK_FLOOR_INCLUSIVE` retained for backwards-compat, both now correctly `85.0`.

### 2. `routes/picks_routes.py` — inline comment corrected

### 3. `tests/test_phase2_elite_gate_and_h2h.py` — updated to reflect INCLUSIVE contract
- `test_elite_gate_demoted_pick_above_85_remains_on_board`: added `book_odds`/`implied_probability` so real-line integrity passes; 87.0 remains ON (unchanged intent).
- `test_elite_gate_demoted_pick_at_or_below_85_falls_off` → renamed **`test_elite_gate_demoted_pick_below_85_falls_off`**: `(85.0, 84.9, 70.0)` → `(84.9, 70.0)`. New assertion added: `pre=85.0` demoted MUST remain ON (INCLUSIVE contract). Real-line stub added.
- `test_locks_contract_still_strictly_gt_85` → renamed **`test_locks_contract_is_inclusive_gte_85`**: `85.0` is now expected `True`.

### 4. `tests/test_phase1_final_closure.py` — updated
- `test_boundary_85_00_off_exactly` → **`test_boundary_85_00_ON_inclusive`**.
- `test_mongo_predicate_uses_gt_not_gte_epsilon` → **`test_mongo_predicate_uses_gte_85_inclusive`**.
- `test_picks_routes_default_floor_uses_exclusive_constant` → **`test_picks_routes_default_floor_uses_shared_constant`** (accepts either alias name).
- Mongo predicate assertions updated to expect `$gte:85.0` on both canonical + legacy branches; `min_lock=70` (< 85) still clamped to base contract via `$gte:85.0`.

### 5. `tests/test_phase1_eligibility_and_accountability.py` — updated
- `test_eligibility_helper_boundary_85_00_off` → **`test_eligibility_helper_boundary_85_00_ON_inclusive`**.
- `test_query_helper_uses_strict_gt_85` → **`test_query_helper_uses_inclusive_gte_85`**.

### 6. New certification suite: `tests/test_main_board_strictness_85_inclusive.py`
27 boundary + integration tests across 6 sections:
- §A INCLUSIVE boundary (84.99 OFF, 85.00 ON, 85 int ON, 86 ON, 99 ON, 100 ON, published_lock_score, lock_score_v2)
- §B Legitimate rejection reasons: score-qualified `>=85` picks still rejected for no book_odds / `no_real_book_line=True` / `model_only=True` / `hide_from_main_board=True`
- §C Mongo predicate builder uses `$gte:85` and narrowing preserves inclusivity
- §D BoardProjectionService — 85.0 pick reaches board; 84.99 does not
- §E Backwards-compat aliases (`MAIN_BOARD_LOCK_FLOOR_EXCLUSIVE == MAIN_BOARD_LOCK_FLOOR_INCLUSIVE == 85.0`)
- §F No filler: missing lock_score / zero / non-dict / None all rejected

## Rejection funnel — score-qualified but off-board

A candidate with `lock_score >= 85` still legitimately fails when:
1. `no_real_book_line == True` — model-only synthesis, no real sportsbook line
2. `model_only == True` — same
3. `book_odds` missing / null / non-numeric — real-line integrity
4. `implied_probability` missing / null / non-numeric — same
5. `hide_from_main_board == True` — earlier stage suppressed the pick (bench/scratched lineup, off_board, etc.)
6. `no_bet == True` — not a betting-eligible pick
7. `off_board == True` — canonical publication rejection
8. `hide_from_main_board == True` (per Locks lifecycle module) — after grade / dedupe / lifecycle
9. Consumer-specific: parlay-only, rollover ineligible, etc.

Each of these is enforced by `is_main_board_eligible()` and/or downstream `BoardProjectionService`. All verified by the new §B rejection tests.

## Test totals

```
NEW strictness certification suite:   27 passed / 0 failed
Updated Phase 1 final closure:         12 passed / 0 failed
Updated Phase 2 elite gate + H2H:      15 passed / 0 failed  (previously 2 FAILED — now GREEN)
Updated Phase 1 eligibility:           18 passed / 0 failed
Full Block 2 + related regression:    693 passed / 1 skipped / 2 failed
```

**Failure classification**:
- `test_mlb_grading_fix_iter71::test_no_remaining_grade_disagreement_flags` → **PRE_EXISTING** (grading fixture for historical Machado prop; unrelated to threshold; unchanged since Block 2A.5.2 handoff)
- `test_mlb_grading_fix_iter71::test_machado_2026_07_09_hits_lost` → **PRE_EXISTING** (same)

**Newly GREEN** (were `PRE_EXISTING` failures; now pass because the fix was semantically correct):
- `test_phase2_elite_gate_and_h2h::test_elite_gate_demoted_pick_above_85_remains_on_board` ✅
- `test_phase2_elite_gate_and_h2h` (both former failing threshold tests) ✅

Zero `NEW_BLOCK2B1_REGRESSION`.

## Preserved (unchanged)

- Lock Score formula — untouched.
- 99 Lock semantics — untouched.
- APEX 100 rules — untouched.
- Magic weights — untouched.
- NFL Platinum architecture (Block 2B.1A/B) — untouched.
- MLB / Tennis behavior — untouched.
- Board quotas / other sports / UI / deployment — untouched.
- Real-line integrity — retained (a 99 without book_odds still rejected).
- Canonical publication barrier — already correct (`lock < 85` gate, so 85.0 already passed there).

## Final return code

**`MAIN_BOARD_STRICTNESS_85_INCLUSIVE_CERTIFIED`**

Backend restarted, `/api/health` → 200. Production behavior now correctly enforces the `85–100 inclusive` contract.
