# BLOCK 2B.1A — PLATINUM NFL SIMULATOR FOUNDATION — READY

Return token: **`BLOCK2B1A_PLATINUM_NFL_SIM_FOUNDATION_READY`**

Date: 2026-08
Status: **DONE** — Foundation subphase per user's Option-B refinement.

────────────────────────────────────────────────────────────────

## A. Files added / changed
```
added:  backend/services/platinum_nfl/__init__.py            (~85 lines)
added:  backend/services/platinum_nfl/season_type.py         (~210 lines)
added:  backend/services/platinum_nfl/football_core.py       (~305 lines)
added:  backend/services/platinum_nfl/opportunity.py         (~220 lines)
added:  backend/services/platinum_nfl/game_markets.py        (~185 lines)
added:  backend/services/platinum_nfl/player_markets.py      (~245 lines)
added:  backend/services/platinum_nfl/simulator.py           (~275 lines)
added:  backend/services/platinum_nfl/rogue_guard.py         (~155 lines)
added:  backend/tests/test_block2b1a_platinum_nfl.py         (~700 lines, 55 tests)
added:  BLOCK2B1A_PLATINUM_NFL_SIM_FOUNDATION_READY.md       (this report)
```

No production runtime files (`sports_engine.py`, `services/nfl_feature_engine.py`, `nfl_atd_engine.py`, `nfl_safe_engine.py`, `nfl_game_engine.py`) were modified in this subphase — per the spec: **"Do NOT wire the new simulator into the production NFL candidate path yet."**

## B. NFL runtime inventory (from §2 mandate)

| Module | Path | Classification |
|---|---|---|
| Shared multi-sport candidate emission | `sports_engine._props_picks_from_event`, `sports_engine.fetch_nfl_picks` | **AUTHORITATIVE_RUNTIME** — the ONE NFL candidate generator. Uses `services.nfl_feature_engine.build_nfl_game_context` for enrichment. |
| NFL feature engine | `services/nfl_feature_engine.py` (`build_nfl_game_context`, `has_enough_real_data_nfl`, `precompute_nfl_prop_factors`) | **AUTHORITATIVE_HELPER** — consumed by `sports_engine.py:4933,5636`. |
| NFL ATD engine | `nfl_atd_engine.py` (`predict_player_atd`, `atd_leaderboard`) | **SPECIALIZED_SEPARATE_ENGINE** — per §3, kept as authoritative for ATD only. Consumed via `/api/nfl/atd/*` + `services/nfl_feature_engine.py:374`. |
| NFL game engine | `nfl_game_engine.py` (`predict_game`, `safe_alt_locks`, `team_strength_leaderboard`) | **LEGACY_REACHABLE** — consumed by `/api/nfl/games/*` HTTP routes (read-only). Does NOT write picks. |
| NFL safe-bets engine | `nfl_safe_engine.py` (`compute_safe_bets`) | **LEGACY_REACHABLE** — read-only `/api/nfl/safe-bets`. Reads player logs, computes alt-line locks. No canonical write. |
| NFL data ingest | `services/nfl_data_ingest.py`, `services/nfl_ingest.py`, `services/nfl_nflfastr.py` | **AUTHORITATIVE_HELPER** — feeds `player_game_actuals`, `games`. |
| NFL matchup intelligence | `services/nfl_matchup_intelligence.py` | **AUTHORITATIVE_HELPER** — consumed via `services.prediction_fusion_engine` + `services.pick_matchup_wiring`. |
| NFL opponent defense | `services/nfl_opp_defense.py` | **AUTHORITATIVE_HELPER**. |
| NFL rationale | `services/nfl_rationale.py` | **AUTHORITATIVE_HELPER** — consumed via `services.sport_rationale`. |
| NFL usage / nflfastR | `services/nfl_nflfastr.py`, `services/nfl_features.py` | **AUTHORITATIVE_HELPER**. |
| Existing NFL simulator (Magic 3H empirical) | `services/magic/simulators/nfl_simulator.py` | **AUTHORITATIVE_HELPER (Champion sim)** — reads `player_game_actuals`, produces market-appropriate empirical distributions. Wired to Magic via `sim_cal_store`. |
| Stub NFL simulator | `sim_nfl.py` (root) | **LEGACY_DEAD** — stub returning `{ran: False, reason: "nfl_simulator_pending_implementation"}`. NOT wired anywhere (0 imports). Superseded by the Platinum causal simulator in 2B.1B. |
| Platinum NFL causal simulator (NEW) | `services/platinum_nfl/*` | **CHALLENGER** — genuine causal-chain simulator built this session. NOT wired to runtime in 2B.1A (foundation only). |

Rogue-runtime scan result (Block 2B.1A §34): **0 unapproved NFL board writers found** after the shared multi-sport pipeline is allowlisted. Full enforcement in 2B.1B.

## C. Platinum simulator architecture (§4, §5, §6, §15)

```
                    ┌─────────────────────────────────┐
                    │  Season type (auto)             │
                    │  PRESEASON/REG/POST/UNKNOWN     │
                    └─────────────────┬───────────────┘
                                      ↓
                    ┌─────────────────────────────────┐
                    │  expected_possessions           │  ← pace + season baseline
                    └─────────────────┬───────────────┘
                                      ↓
                    ┌─────────────────────────────────┐
                    │  expected_plays                 │  ← plays / possession
                    └─────────────────┬───────────────┘
                                      ↓
                    ┌─────────────────────────────────┐
                    │  sample_game_script (Monte)     │  ← margin/total → pass rate
                    └─────────────────┬───────────────┘
                                      ↓
                    ┌─────────────────────────────────┐
                    │  Opportunity                    │  ← QB/RB/WR role scaling
                    │  QBOpportunity, RBOpp, WROpp    │
                    │  + apply_preseason_regime()     │
                    └─────────────────┬───────────────┘
                                      ↓
                    ┌─────────────────────────────────┐
                    │  Efficiency draws               │  ← ypa, ypc, catch_rate...
                    └─────────────────┬───────────────┘
                                      ↓
                    ┌─────────────────────────────────┐
                    │  Correlated outcomes            │  ← QB att ↔ WR targets
                    └─────────────────┬───────────────┘
                                      ↓
                    ┌─────────────────────────────────┐
                    │  Distribution + exact-line prob │  ← lognormal / Poisson / NegBin
                    │  mean, med, Q10/25/75/90, std   │
                    │  P(over), P(under), P(push)     │
                    └─────────────────────────────────┘
```

**Distribution samplers** (all in `football_core.py`):
- `sample_lognormal` — yards families
- `sample_poisson` — count families (TDs, INTs)
- `sample_negative_binomial` — receptions / TD-per-game (over-dispersed)
- `ShrinkageEstimator` — empirical-Bayes with league prior; small samples shrink toward league mean

**Correlation** (§8): QB attempts, WR targets and RB carries within a SINGLE call use the same game-script draws — a trailing-team script produces correlated high QB attempts + high WR targets + low RB carries.

## D. Season-mode proof (§10)

`classify_season_type(event_metadata)` — precedence:
1. Explicit `season_type` / `game_type` field (handles `PRE`, `REG`, `POST`, `WC/DIV/CONF/SB`, `Super Bowl`, `playoff`, `playoffs`).
2. `sport_key` string (`americanfootball_nfl_preseason`, `_playoffs`, bare `_nfl` → REGULAR_SEASON).
3. Numeric `week`: 1..18 → REGULAR_SEASON, 19..22 → POSTSEASON, <0 → PRESEASON.
4. UNKNOWN → fail-closed. **No calendar-month inference**.

No environment variables, no admin toggles, no code edits per week. `simulate()` returns `{ran: False, reason: "SEASON_TYPE_UNKNOWN"}` when season cannot be resolved (§10 fail-closed).

Preseason isolation (§9, §12) via `SeasonTaggedRow` + `enforce_no_preseason_contamination()`:
- Every stored row must carry an explicit `season_type` tag.
- Untagged rows are **dropped** (fail-closed) — cannot silently mix into calibration.
- Preseason rows filtered from regular-season calibration.

## E. Market proof — game markets + QB/RB/WR/TE/TD (§6, §7)

| Market family | Adapter | Distribution | Exact-line prob |
|---|---|---|---|
| Moneyline | `simulate_game_market` | Margin ~ Normal(exp_margin, σ) | P(home wins), P(away wins) |
| Spread | `simulate_game_market` | Same margin draws | P(cover \| side) |
| Total | `simulate_game_market` | Total ~ Normal(total_line, σ_total) | P(over), P(under) |
| QB passing yards | `player_markets` | Lognormal on att·ypa | Yes |
| QB attempts | `player_markets` | Direct integer count | Yes |
| QB completions | `player_markets` | Binomial(att, comp_rate) | Yes |
| QB rushing yards | `player_markets` | Lognormal on rush_att·rush_ypc | Yes |
| QB passing TDs | `player_markets` | NegBin(att·0.052, 0.35) | Yes |
| RB rushing yards | `player_markets` | Lognormal on carries·ypc | Yes |
| RB carries | `player_markets` | Direct count | Yes |
| RB receiving yards | `player_markets` | Lognormal on receptions·ypr | Yes |
| RB receptions | `player_markets` | NegBin(targets·catch, 0.35) | Yes |
| WR/TE receiving yards | `player_markets` | Lognormal on receptions·ypr | Yes |
| WR/TE receptions | `player_markets` | NegBin(targets·catch, 0.28) | Yes |
| WR/TE targets | `player_markets` | Direct count | Yes |
| ATD (any TD) | `player_markets` | Bernoulli(1 - e^-λ) via red-zone role | Yes |

Neutrality (§17):
- `p_over` / `p_under` computed identically from the same samples — no Over/Under preference.
- Spread neutrality proven: with `expected_margin=0`, Home -0.5 and Away +0.5 produce probabilities within 4 pp of each other (test `test_spread_neutrality_favorite_and_dog`).
- No +money penalty, no favorite safety floor.

## F. Champion / Challenger frozen provenance (§20, §22)

`attach_challenger_output(pick, sim_output)`:
- Champion (`pick["model_probability"]`) is **NEVER** overwritten.
- Challenger output stamped under `pick["platinum_challenger"]`.
- Frozen row stored under `pick["champion_challenger"]["platinum_nfl"]` with:
  - `prediction_timestamp` (ISO 8601 UTC — time-aware truth §22)
  - `event_id`, `market`, `side`, `line`, `odds`, `season_type`
  - `champion_probability`, `challenger_probability`, `challenger_version="2b.1a.v1"`
  - `challenger_ran`, `challenger_reason`
  - `challenger_summary` (mean, median, Q10/25/75/90, variance, std, sim count)
  - `role_evidence`, `input_provenance`
- `sim_probability` set **only** when Challenger ran successfully — §32 failure contract enforced.

## G. Simulator failure contract (§32)

Every failure path returns:
```python
{
    "ran": False,
    "reason": "SIMULATOR_UNAVAILABLE" | "SIMULATOR_FAILED" | "WRONG_SPORT" |
              "SEASON_TYPE_UNKNOWN" | "MISSING_EXPECTED_MARGIN" |
              "MISSING_TOTAL_LINE" | "UNSUPPORTED_MARKET" |
              "MISSING_OPPORTUNITY" | ...
    "sim_probability": None,
    ...
}
```
Never `sim_probability = model_probability`. Static-source anti-pattern check enforced via test (`test_no_agreement_faking`).

## H. Rogue-runtime guard foundation (§34)

`services.platinum_nfl.rogue_guard.verify_no_rogue_nfl_runtime()` returns findings from a codebase scan for pick / canonical_picks direct writes in NFL-touching files.
- Approved publishers: `canonical_publication`, `board_projection_service`, `publication_helpers`, `settlement_service`.
- Approved runtimes: `sports_engine._props_picks_from_event`, `sports_engine.fetch_nfl_picks`, `nfl_atd_engine.predict_player_atd`, `nfl_atd_engine.atd_leaderboard`.
- Allowlisted: shared multi-sport pipeline (`pick_refresh_orchestrator`, `pick_validator`, `pick_enrichment`, `closing_line_snapshotter`), `sports_engine`, `server`, `routes/`.

Current scan result: **0 unapproved NFL publishers**.

## I. Test totals

```
New Block 2B.1A suite (test_block2b1a_platinum_nfl.py):    55 passed / 0 failed
Full Block 2 regression (all Block 2A/2B/2C/2D/2E +
    Phase 4C + canonical settlement + canonical board):   423 passed / 1 skipped / 0 failed
Broader (MLB/Magic/Phase1/2 + iter106 + hot_hitters):     192 passed / 4 failed / 0 NEW
```

**Failure classification:**
- `test_phase2_elite_gate_and_h2h::test_elite_gate_demoted_pick_above_85_remains_on_board` → **PRE_EXISTING** (`main_board_eligibility` arithmetic; do-not-touch per handoff)
- `test_phase2_elite_gate_and_h2h::test_locks_contract_still_strictly_gt_85` → **PRE_EXISTING**
- `test_mlb_grading_fix_iter71::test_no_remaining_grade_disagreement_flags` → **PRE_EXISTING**
- `test_mlb_grading_fix_iter71::test_machado_2026_07_09_hits_lost` → **PRE_EXISTING**

Zero `NEW_BLOCK2B1_REGRESSION`. Zero `LIVE_DATA_UNAVAILABLE` (no live-data tests in 2B.1A — deferred to 2B.1B). Zero `KNOWN_UNSUPPORTED_NFL_MARKET` (all in-scope markets implemented).

## J. Preserved (spec §36, §37)

- **Lock Score formula** — unchanged.
- **85/86 threshold** — unchanged.
- **99 Lock / APEX 100** — unchanged.
- **Magic weights** — unchanged.
- **Calibration promotion** — unchanged.
- **Board quotas** — unchanged.
- **MLB / Tennis** — untouched.
- **No NBA / CFB / NHL / UFC / Soccer** — not implemented.
- **No deployment**.

## K. Deferred to 2B.1B (production runtime wiring)

Per spec §39, the following runtime arrows are the scope of 2B.1B — NOT proven in 2B.1A:

- real NFL data → authoritative NFL runtime → NFL model
- Platinum NFL simulator called from `sports_engine.py` hot path
- simulator output attached to actual candidate at emit time
- Magic **CALLED_AND_CONSUMED** on simulator evidence
- canonical candidate → canonical publication → BoardProjectionService → NFL Locks
- **Live preseason funnel** (event counts, ingestion, board eligibility)
- Preseason E2E fixture (Aug/Wk-0 preseason game)
- Week 1 REGULAR_SEASON auto-switch proof (fixture toggling `sport_key`)
- Postseason context proof
- Game-market E2E from real Odds API pick
- QB E2E (real passing-yards prop → simulator → Magic → board)
- RB / WR/TE / TD E2E
- Final rogue-runtime enforcement (asserting zero-findings under production load)

## L. Final return code

**`BLOCK2B1A_PLATINUM_NFL_SIM_FOUNDATION_READY`**

────────────────────────────────────────────────────────────────

Awaiting user go-signal for **Block 2B.1B — production runtime wiring + certification**. Per §37: do NOT start NBA, CFB, NHL, UFC, Soccer. No deployment.
