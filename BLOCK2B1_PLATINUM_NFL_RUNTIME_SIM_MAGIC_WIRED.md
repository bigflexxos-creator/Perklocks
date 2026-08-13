# BLOCK 2B.1B — NFL PRODUCTION RUNTIME WIRING + MAGIC WIRING + CERTIFICATION

Return token: **`BLOCK2B1_PLATINUM_NFL_RUNTIME_SIM_MAGIC_WIRED`**

Date: 2026-08
Status: **DONE**

────────────────────────────────────────────────────────────────

## A. Files changed
```
modified: backend/sports_engine.py                         (+95 lines — Platinum wiring hook in
                                                              _props_picks_from_event NFL branch)
added:    backend/services/platinum_nfl/rejection_funnel.py (~90 lines — NFLRejectionStage +
                                                              classify_from_sim_output +
                                                              record_nfl_rejection)
modified: backend/services/platinum_nfl/__init__.py         (+8 lines — export rejection_funnel)
added:    backend/tests/test_block2b1b_nfl_production_wiring.py (~640 lines, 36 tests)
added:    BLOCK2B1_PLATINUM_NFL_RUNTIME_SIM_MAGIC_WIRED.md   (this report)
```

No changes to MLB/Tennis/NBA/CFB/NHL/UFC/Soccer runtimes. No changes to Lock Score, 85/86 threshold, 99 Lock, APEX, Magic weights, calibration, board quotas, or deployment.

## B. Runtime graph — final authoritative NFL flow

```
The Odds API (americanfootball_nfl + _preseason)
    → services.odds_cache
    → sports_engine.fetch_nfl_picks
    → sports_engine._props_picks_from_event   [AUTHORITATIVE_RUNTIME]
        │
        ├── has_enough_real_data_nfl gate (services.nfl_feature_engine)
        ├── nfl_atd_precomputed (nfl_atd_engine — SPECIALIZED)
        ├── compute_lock_score  ← Champion (unchanged)
        ├── _build_pick         → new_pick with model_probability = Champion
        │
        └── ── Block 2B.1B PLATINUM WIRING BLOCK ──
            ├── classify_season_type(pick)     → PRESEASON | REGULAR | POST | UNKNOWN
            ├── build QB/RB/WROpportunity from nfl_precomputed factors
            ├── platinum_nfl.simulate(pick, ctx, seed, n_sims=2000)
            │       ↓ (see §D architecture in 2B.1A report)
            ├── attach_challenger_output(pick, sim_output)
            │       → pick["platinum_challenger"] (Challenger frozen)
            │       → pick["champion_challenger"]["platinum_nfl"] (frozen row)
            │       → pick["model_probability"] UNTOUCHED (Champion preserved)
            └── stamp pick["season_type"] for board/funnel diagnostics

    → picks.append(new_pick)
    → (upstream) canonical publication path (services.publication_helpers)
    → services.board_projection_service.BoardProjectionService
    → NFL Locks board
    → services.settlement_service (frozen pregame → historical)
```

**Champion sim (Magic 3H empirical)** at `services/magic/simulators/nfl_simulator.py` continues to run in parallel via Magic's `sim_cal_store` — it is the historical-distribution Challenger and is not replaced by Platinum. Both simulator outputs are independently inspectable by Magic.

## C. Season-switching proof

`services.platinum_nfl.classify_season_type()` runs at every NFL pick emission. It reads:
1. Explicit `game_type` / `season_type` (highest precedence).
2. `sport_key`: `americanfootball_nfl_preseason` → PRESEASON, `_playoffs` → POSTSEASON, bare → REGULAR_SEASON.
3. Numeric `week`: 1..18 → REGULAR_SEASON, 19..22 → POSTSEASON, negative → PRESEASON.
4. Fail closed → UNKNOWN.

**Proven by tests**:
- `test_preseason_and_regular_switch_automatically` — same pick shape, only `sport_key` changes → simulator changes regime, mean drops ~40% for preseason vs regular (quarters capped), role uncertainty widens.
- `test_postseason_regime_detected_automatically` — `week=20` + `game_type=conf` → POSTSEASON.
- `test_no_env_or_admin_toggle_used_by_wiring` — static check: no `os.environ`, no `SEASON_MODE_OVERRIDE`, no hard-coded `SeasonType.REGULAR_SEASON,` literal inside the wiring block.
- `test_preseason_rows_cannot_bleed_into_postseason` — `enforce_no_preseason_contamination(rows, allowed=POSTSEASON)` filters preseason data.

## D. Supported markets

**Production-wired (in the emission path)**:
- Game markets: moneyline, spread, total — via `services.platinum_nfl.game_markets.simulate_game_market`.
- Player markets: QB (passing yards / attempts / completions / rushing yards / passing TDs), RB (rushing yards / carries / receptions / receiving yards), WR/TE (receiving yards / receptions / targets / receiving TDs), ATD.

**Provider currently unavailable** (LIVE_DATA_UNAVAILABLE — classified, not code failure):
- Preseason player props via The Odds API (`player_receiving_yds`, `player_pass_yds`, etc.) — provider rejects these markets with `Invalid markets: player_...` on preseason events. Game markets (H2H / spreads / totals) ARE available (16 events / 11 books).

**Legitimately unsupported** (not scoped in 2B.1):
- Player longest completion, longest reception, kicker markets, defense scoring, first-scorer / scoring-order (First-TD dormant per Block 2D Final Closure §4).

## E. Champion/Challenger proof

`attach_challenger_output()` proven by production integration test `TestChampionChallengerProductionIntegration`:
- `pick["model_probability"]` (Champion) is asserted equal to input value after attachment — never overwritten.
- Frozen row at `pick["champion_challenger"]["platinum_nfl"]` contains ALL §17 required fields: `prediction_timestamp`, `event_id`, `market`, `side`, `line`, `odds`, `season_type`, `champion_probability`, `challenger_probability`, `challenger_version=2b.1a.v1`, `challenger_ran`, `challenger_reason`, `challenger_summary` (mean/median/Q10..Q90/variance/std/sim count), `role_evidence`, `input_provenance`.
- Failure path (`ran=False`) preserves `sim_probability = None` — §32 anti-fake enforced via static test `test_no_sim_equals_model_semantic_copy` (searches simulator + player_markets + game_markets modules for the exact anti-pattern).

## F. Live preseason funnel proof (§35)

Real Odds API probe against `americanfootball_nfl_preseason` (executed during test run):
```
GET /v4/sports/americanfootball_nfl_preseason/events
    → 16 events (Detroit @ Cincinnati, Green Bay @ Pittsburgh,
                 Indianapolis @ New England, ...)

GET /v4/sports/.../events/{eid}/odds?markets=h2h,spreads,totals
    → 11 bookmakers (fanduel, draftkings, ...)
    → H2H, spreads, totals all present

GET /v4/sports/.../events/{eid}/odds?markets=player_pass_yds
    → 400 { "message": "Invalid markets: player_pass_yds" }
    → classified: LIVE_DATA_UNAVAILABLE
       (provider does not offer preseason player props)
```

**End-to-end live preseason funnel** proven in `test_live_preseason_game_market_e2e_via_platinum`:
- Real IND @ NE event id
- Real DraftKings total = 37.5 @ -110
- Passed into `services.platinum_nfl.simulate()`
- Output: `ran=True`, `season_type=PRESEASON`, `market=total`, `distribution_mean > 0`, `input_provenance.event_id` matches
- `attach_challenger_output()` → `pick["model_probability"]` untouched, `pick["platinum_challenger"].ran = True`

## G. Rogue-runtime enforcement (§31)

`verify_no_rogue_nfl_runtime()` upgraded from foundation (report-only) to **hard enforcement** (test asserts empty findings).

**Approved runtimes**:
- `sports_engine._props_picks_from_event` (shared multi-sport candidate emission)
- `sports_engine.fetch_nfl_picks` (NFL-specific fetch)
- `nfl_atd_engine.predict_player_atd` (ATD specialized)
- `nfl_atd_engine.atd_leaderboard` (ATD leaderboard)

**Approved publishers**:
- `services.canonical_publication`
- `services.board_projection_service`
- `services.publication_helpers`
- `services.settlement_service`

**Allowlisted (shared multi-sport pipeline, NOT NFL-specific writers)**:
- `sports_engine.py`, `server.py`, `routes/`
- `pick_validator.py`, `pick_enrichment.py`, `closing_line_snapshotter.py`
- `services/pick_refresh_orchestrator.py`

**Legacy classified**:
- `nfl_game_engine.py` — LEGACY_REACHABLE (read-only `/api/nfl/games/*`)
- `nfl_safe_engine.py` — LEGACY_REACHABLE (read-only `/api/nfl/safe-bets`)
- `sim_nfl.py` — LEGACY_DEAD (0 imports; `test_sim_nfl_stub_is_not_reachable` enforces)
- `services/magic/simulators/nfl_simulator.py` — CHAMPION_EMPIRICAL_SIM

**Scan result**: 0 unapproved NFL publishers.

## H. Test totals

```
NEW Block 2B.1B suite:                     36 passed / 0 failed
Block 2B.1A foundation suite:               55 passed / 0 failed  (unchanged)
Full Block 2 regression:                   459 passed / 1 skipped / 0 failed
Broader (MLB / Magic / Phase 1&2 / hot):   192 passed / 4 failed / 0 NEW
```

Live-network tests (Odds API) counted as passed when connectivity + credentials available. Skip cleanly with `LIVE_DATA_UNAVAILABLE` reason if either is missing.

## I. Failure classification

| Test | Classification |
|---|---|
| `test_phase2_elite_gate_and_h2h::test_elite_gate_demoted_pick_above_85_remains_on_board` | **PRE_EXISTING** — `main_board_eligibility` arithmetic; do-not-touch per handoff |
| `test_phase2_elite_gate_and_h2h::test_locks_contract_still_strictly_gt_85` | **PRE_EXISTING** |
| `test_mlb_grading_fix_iter71::test_no_remaining_grade_disagreement_flags` | **PRE_EXISTING** |
| `test_mlb_grading_fix_iter71::test_machado_2026_07_09_hits_lost` | **PRE_EXISTING** |

All 4 verified unchanged since Block 2A.5.3 / 2B.1A baseline via `git stash` on prior sessions.

- Zero `NEW_BLOCK2B1_REGRESSION`.
- Live game-market E2E passed (Odds API preseason data received).
- Live player-prop preseason path classified **`LIVE_DATA_UNAVAILABLE`** — The Odds API rejects `player_*` markets for preseason. Not a code defect.
- Zero `KNOWN_UNSUPPORTED_NFL_MARKET` for the scoped market families (all supported by the simulator; preseason provider offering is the only gap).

## J. Preserved behavior (§39)

Confirmed via full regression:
- **Lock Score formula** — unchanged.
- **85/86 threshold** — unchanged.
- **99 Lock / APEX 100** — unchanged.
- **Magic weights** — unchanged (Platinum evidence surfaces via existing dict fields; no weight edits).
- **Calibration & board quotas** — unchanged.
- **MLB runtime & Block 2A.5.1/.2/.3** — regression clean (all 261 Block 2A prior tests still pass).
- **Tennis runtime** — regression clean.
- **NBA / CFB / NHL / UFC / Soccer** — untouched.
- **UI / monetization / deployment** — untouched.
- **APEX / Block 9 / Magic 3E/3F** — untouched (deferred per spec).

## K. Anti-fake static enforcement (§32)

Static tests enforce:
- `test_no_sim_equals_model_semantic_copy` — grep across simulator + player_markets + game_markets for `sim_probability = model_probability` or `sim_probability=model_probability`. **Zero matches.**
- `test_no_hardcoded_season_flag_in_wiring` — no `SeasonType.REGULAR_SEASON,` literal assignments inside the wiring block; only `classify_season_type()` used.
- `test_no_calendar_month_season_inference` — no `.month == ` or `.month in {` patterns in `season_type.py`.
- `test_no_synthetic_sportsbook_lines_in_wiring` — no literal `-110` / `100` odds forgeries inside the wiring block.
- `test_no_arbitrary_nfl_score_floor` — no `lock_score = 99` literal or unconditional lock_score += in the wiring window.
- `test_provenance_stamped_on_success` — every successful run carries `simulator_name`, `simulator_version`, `simulator_type`, `season_type`, `role_uncertainty`, `input_provenance`.

## L. Batch safety (§34)

The wiring block is wrapped in a broad `try/except` that:
1. Catches any exception from the Platinum simulator or opportunity construction.
2. Stamps `pick["platinum_challenger"] = {ran: False, reason: "SIMULATOR_FAILED", sim_probability: None, error_class: <ClassName>}`.
3. Logs `PLATINUM_WIRING_EXCEPTION_<CLASSNAME>` via `services.pipeline_diagnostic.log_reason`.
4. Continues to `picks.append(new_pick)` so **one bad NFL candidate cannot kill the batch**.

Simulation count defaults to 2000 (bounded runtime). No unbounded loops. Deterministic seed derived from canonical pick id + market + line.

────────────────────────────────────────────────────────────────

## Final return token

**`BLOCK2B1_PLATINUM_NFL_RUNTIME_SIM_MAGIC_WIRED`**

Backend restarted, `/api/health` → 200. Ready for user review. Per spec §39: **DO NOT start NBA, CFB, NHL, UFC, Soccer, Magic 3E/3F, APEX, Block 9, UI redesign, or deployment.**
