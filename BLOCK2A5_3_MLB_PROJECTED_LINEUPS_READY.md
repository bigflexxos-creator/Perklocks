# BLOCK 2A.5.3 — MLB PROJECTED-LINEUP CLOSURE — READY

Return token: **`BLOCK2A5_3_MLB_PROJECTED_LINEUPS_READY`**

Date: 2026-08
Status: **DONE**

────────────────────────────────────────────────────────────────

## A. Files changed
```
added:    backend/services/enrichment/mlb_projected_lineup.py
modified: backend/services/game_context.py
modified: backend/sports_engine.py
added:    backend/tests/test_block2a5_3_mlb_projected_lineups.py
added:    BLOCK2A5_3_MLB_PROJECTED_LINEUPS_READY.md   (this report)
```

## B. Projected-lineup source used
**MLB StatsAPI ``/schedule?hydrate=lineups,probablePitcher``.**

Rationale (§2 spec):
- Same base URL (`statsapi.mlb.com/api/v1`) as every other MLB
  integration in this repository — this is NOT a new provider.
- The `game_context.py` module already calls the same endpoint with
  `hydrate=probablePitcher`; we simply widened the hydrate list.
- Free, no auth, no external dependency.
- Authoritative — MLB itself. Same data provider MLB StatsAPI has always been.

**Empirical verification (2026-08-13 curl against production
`statsapi.mlb.com`):**
- In-Progress games → `lineups = 9/9` (CONFIRMED path).
- Pre-Game game (Bos @ Tor) → `lineups = 9/9` (PROJECTED — teams posted 1–4 h out).
- Scheduled games later today → `lineups = 0/0` (nothing posted → fail-closed).

The developer comment in `services/mlb_matchup_resolver.py:210`
("MLB doesn't publish until lineup card released") is outdated; MLB
DOES publish anticipated lineups via `hydrate=lineups` well before
the confirmed card is released. That comment is not code that
gates behavior; it's a note next to a call site that intentionally
sets `batting_order=None` because it doesn't consume the lineup.

## C. Exact runtime path
```
real MLB sportsbook hitter market (The Odds API)
  → sports_engine._props_picks_from_event
    → ctx = build_mlb_game_context(game)
       → statsapi feed/live boxscore.battingOrder     [CONFIRMED path]
         → each hitter_row stamped:
             lineup_confirmed=True, is_starter=True,
             lineup_slot=1..9, lineup_source="statsapi_feed_live_batting_order"
       → for any side whose battingOrder is EMPTY:
         → statsapi /schedule?hydrate=lineups         [PROJECTED fallback]
           → each hitter_row stamped:
               lineup_confirmed=False, is_starter=True,
               lineup_slot=1..9, lineup_source="statsapi_schedule_hydrate_lineups"
    → mlb_gates.classify_lineup_status(...) resolves each hitter to
       {confirmed_starter, projected_starter, bench, scratched, unknown}
    → bench/scratched → hard drop (record_rejection)
    → build_mlb_hitter_factors  (≥3 real factors required)
    → payload["_mlb_lineup_status"][player] = {status, lineup_pos,
                                                 source, cap, updated_at}
  → _build_pick → new_pick["lineup_status"] = {
        status:     "CONFIRMED" | "PROJECTED" | "UNKNOWN",
        lineup_pos: 1..9 | None,
        source:     "...",
        updated_at: iso,
    }
  → data_quality_cap_for_status applied (Confirmed=99, Projected=92, Unknown=79)
  → apply_magic_tier (magic_tier_policy._extract_lineup_certainty
     caps `projected` at Strong Lock; `confirmed` uncapped by lineup)
  → canonical publication path (main_board_eligibility.is_main_board_eligible)
     requires book_odds + implied_probability (real-line integrity)
  → BoardProjectionService.project([pick])  →  Locks board
```

Post-emission refresh path
(`services.enrichment.mlb_projected_lineup.enrich_pick_with_projected_lineup`):
```
existing pick with PROJECTED status
  → refresh cycle re-fetches lineup bundle
  → bundle CONFIRMED + player still in it   → upgrade to CONFIRMED (§8)
  → bundle CONFIRMED + player NOT in it     → downgrade to BENCH   (§9)
  → bundle CONFIRMED + player moved slot    → refresh lineup_pos   (§10)
  → bundle PROJECTED                        → keep PROJECTED (never downgrade CONFIRMED)
```

## D. Confirmed vs Projected precedence proof
- `services/game_context.py` hitter enrichment loop: confirmed rows
  are populated FIRST from `battingOrder`. The subsequent
  `fetch_mlb_lineup_bundle` fallback explicitly skips any row whose
  existing `lineup_source == "statsapi_feed_live_batting_order"`.
- `services/enrichment/mlb_projected_lineup.py`:
  `fetch_mlb_lineup_bundle` fetches BOTH `feed/live` AND
  `schedule?hydrate=lineups` — confirmed rows are stored first, and
  projected rows only fill sides where confirmed data is empty.
- `enrich_pick_with_projected_lineup` short-circuits when
  `pick["lineup_status"].status` is already `CONFIRMED`:
  a stale projected bundle CANNOT downgrade a confirmed pick.
- Tests: `TestConfirmedOverridesProjectedInCtx`,
  `TestConfirmedUpgradePath::test_confirmed_pick_is_not_downgraded_to_projected`,
  `TestClassifyLineupStatusProjected::test_confirmed_overrides_projected_when_both_set`.

## E. Early-day hitter E2E proof
Test class `TestPickContractCases::test_projected_pick_reaches_board`
and `test_projected_total_bases_pick_reaches_board` prove:

```
Fixture: Aaron Judge (NYY) Over 0.5 Hits @ FanDuel -180 (implied 0.643)
         event=Yankees vs BoSox, lineup_status={PROJECTED, slot 2,
                                                 source: statsapi_schedule_hydrate_lineups}
         lock_score=89.0

BoardProjectionService().project_ids([pick]) → [pick["id"]]  ✅

Fixture: Aaron Judge (NYY) Over 1.5 Total Bases @ FanDuel -180
         lineup_status={PROJECTED, slot 3},  lock=88.0
         → project_ids returns [pick["id"]]  ✅
```
Feature engine gate proven by
`TestFeatureEngineHandlesBothLineupStates` — projected-lineup rows
pass `has_enough_real_data("hitter_prop")` when the real hitter
enrichment (l10 hit rate + Statcast xBA/barrel/hard-hit + BvP) is
attached, exactly as in the confirmed path.

## F. Projected → Confirmed upgrade proof
`TestConfirmedUpgradePath::test_projected_upgrades_to_confirmed_when_bundle_confirmed`
- Start with `pick["lineup_status"] = {PROJECTED, slot 2, source=schedule_hydrate_lineups}`
- Patch fetch to return a confirmed bundle with Judge slot 2.
- After `enrich_pick_with_projected_lineup(pick)`:
  `pick["lineup_status"] = {CONFIRMED, slot 2, source=feed_live_batting_order}`  ✅
- No duplicate canonical pick is created — the same `pick["id"]`
  carries the upgraded provenance (BoardProjection dedupes by
  canonical id via `_canonical_pick_id`, verified by
  `dedupe_canonical`).

## G. Projected → Bench/Scratched suppression proof
`TestConfirmedUpgradePath::test_projected_downgrades_to_bench_when_player_dropped`
- Start with `pick["lineup_status"] = {PROJECTED, slot 2}`.
- Patch bundle: confirmed lineup with Anthony Volpe at slot 2, Judge
  ABSENT.
- After enrichment: `pick["lineup_status"] = {BENCH}`  ✅
- BENCH picks are excluded from BoardProjectionService via the
  existing `is_main_board_eligible` contract (bench cap is `None`
  in `data_quality_cap_for_status` — publish=False, and the emitted
  Magic tier also caps at "Playable" via
  `magic_tier_policy._extract_lineup_certainty`).

## H. Identity safety proof
- `TestIdentitySafety::test_wrong_player_projected_leaves_no_lineup`
  — a pick for "Fake Player" (name-only) does NOT resolve to any
  projected slot; `lineup_status` remains `UNKNOWN` (fail-closed).
- `TestIdentitySafety::test_projected_lineup_uses_mlb_returned_ordering_only`
  — slot is taken directly from MLB's returned list; no
  name-based or heuristic assignment is applied.
- `TestBundleToHitterRows` — projected rows explicitly carry
  `mlb_player_id` from MLB's payload as the canonical identity.
- No name-only matching; the row is keyed by `name.lower()` from
  MLB's returned `fullName`, joined with the `id` field for full
  canonical safety.

## I. Magic / publication / board proof
- Magic: `TestMagicConsumesLineupCertainty::test_projected_pick_receives_magic_tier`
  and `test_confirmed_pick_receives_magic_tier` — `apply_magic_tier`
  runs against both states and stamps `magic_tier`.
  `_extract_lineup_certainty` returns `"projected"` for PROJECTED
  and `"confirmed"` for CONFIRMED; the existing
  `lineup == "projected"` branch (line 342) caps at "Strong Lock".
- Canonical publication: `is_main_board_eligible` requires
  `book_odds + implied_probability` — projected picks WITHOUT real
  book odds are ineligible
  (`TestRealLineIntegrityUnderProjected` proves both `book_odds=None`
  and `no_real_book_line=True` block board projection).
- BoardProjectionService: proven by
  `TestPickContractCases::test_projected_pick_reaches_board` and
  `test_projected_total_bases_pick_reaches_board`.

## J. Test totals
```
New Block 2A.5.3 suite (test_block2a5_3_mlb_projected_lineups.py):
    36 passed / 0 failed

Full Block 2 regression (all Block 2A/2B/2C/2D/2E + Phase 4C +
canonical settlement + canonical board source + prior blocks):
    368 passed / 1 skipped / 0 failed
    (skip is env-only wiring-matrix report generator)

Broader regression (MLB verification, Magic-Lock integration,
Phase 1/2 market surfacing, eligibility, hot hitters, iter106 MLB):
    192 passed / 4 failed / 0 new regressions
```

## Failure classification
```
FAILED tests/test_phase2_elite_gate_and_h2h.py::test_elite_gate_demoted_pick_above_85_remains_on_board  → PRE_EXISTING
FAILED tests/test_phase2_elite_gate_and_h2h.py::test_locks_contract_still_strictly_gt_85                  → PRE_EXISTING
FAILED tests/test_mlb_grading_fix_iter71.py::TestPostFixDbState::test_no_remaining_grade_disagreement_flags → PRE_EXISTING
FAILED tests/test_mlb_grading_fix_iter71.py::TestPostFixDbState::test_machado_2026_07_09_hits_lost         → PRE_EXISTING
```

**Verified PRE_EXISTING via `git stash` regression run** — same 4
failures on the base commit (Block 2A.5.2 completion state)
BEFORE this block's changes. No new regressions introduced.

- `test_phase2_elite_gate_and_h2h.py::*`: `main_board_eligibility`
  threshold arithmetic edge cases — the handoff spec explicitly
  says "Do Nothing".
- `test_mlb_grading_fix_iter71.py::*`: MLB grading fixture for
  a specific historical Machado prop — outside 2A.5.3 scope.

Zero `NEW_BLOCK2A5_3_REGRESSION`. Zero `TEST_ISOLATION`. Zero
`MISSING_PROJECTED_LINEUP_SOURCE` (source found and integrated).

## K. Remaining Block 2 sequence
- **Block 2B** — NFL/NBA Runtime + Magic Wiring (still blocked
  per spec §18; do NOT auto-start).
- Block 2C — NHL/CFB/UFC/Soccer Universal Runtime Wired.
- Magic 3E Gold Research ingestion.
- Magic 3F Market Intelligence / CLV.
- Magic → Lock Score, APEX 100.
- Block 9 / Rollover 2.0.
- Block 10 Parlay 2.0.
- Block 11 Matchup DNA / Lab 2.0.
- Block 12 Final Certification / Deploy.

## L. Final return code
**`BLOCK2A5_3_MLB_PROJECTED_LINEUPS_READY`**

────────────────────────────────────────────────────────────────

## Preserved (NOT changed)
- Lock Score formula
- 85/86 threshold
- 99 Lock
- APEX 100
- Magic weighting
- Calibration, value/edge, board quotas
- MLB totals side-neutrality logic (Block 2A.5.1)
- Any unrelated scoring / ranking
- No new provider added; no fabricated lineups or slots.
