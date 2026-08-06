# PHASE 4A — MODEL AUDIT

**Status:** Read-only audit (no code changes).
**Scope:** Every production prediction model wired into the live picks pipeline.
**Companion:** `PHASE4_MARKET_AUDIT.md`, `PHASE4_SIMULATOR_AUDIT.md`, `PHASE4_MAGIC_TIER_AUDIT.md`, `PHASE4_CALIBRATION_BASELINE.md`, `PHASE4_AUDIT_EXECUTIVE_SUMMARY.md`.

**Ground truth of what is currently live:** `backend/sports_engine.py` — `SPORT_KEYS` (lines 38-97), `PROP_MARKETS` (lines 2170-2243), and the pick-emission path `_props_picks_from_event` + `_picks_from_game` + `_build_mlb_alt_picks` + `_build_tennis_alt_picks`.

---

## 1. Sports currently live in the pipeline

| Sport | `SPORT_KEYS` entry | Live? | Notes |
|---|---|---|---|
| MLB | `baseball_mlb` | ✅ | Real feature engine wired for props (`services.mlb_feature_engine`). |
| NFL | `americanfootball_nfl` + `_preseason` | ✅ | Real feature engine wired via `ctx["nfl_precomputed"]`. |
| CFB | `americanfootball_ncaaf` | ⚠️ | Feature engine EXISTS (`services.cfb_feature_engine`) but is **NOT wired** to the sync emission path. Falls through to book-follow. See `sports_engine.py:3992-4014`. |
| NBA | `basketball_nba` | ⚠️ | Real feature engine **NOT wired**. Emits `factors = {"Book Implied Probability": mp}` — pure book-follow. See `sports_engine.py:4024-4030`. |
| NHL | *(absent)* | ❌ | **NOT SUPPORTED.** No entry in `SPORT_KEYS`. `historical/nhl.py` exists for ingest only. |
| Soccer | 30+ league keys | ✅ | Scorer engines (`goal_scorer_engine_v2`, `goalscorer_matchup`) real, but non-scorer soccer props fall to book-follow. |
| Tennis | 5 keys (`tennis_atp_*`, `tennis_wta_*`, `tennis_grand_slam`) | ✅ | `tennis_engine.apply_tennis_engine` — see **defect T-1** below (book-follow + hashed noise). |
| UFC | `mma_mixed_martial_arts` | ✅ | `mma_method_of_victory` prop supported; no `PROP_MARKETS` list entry for UFC beyond method. |
| WNBA | *(commented out)* | ❌ | Disabled — `# DISABLED — killing ROI (-31% Player Points)`. |
| KBO | *(commented out)* | ❌ | Disabled 2026-06-18. |

**Conclusion:** 6 sports are truly live for user-visible picks: MLB, NFL, CFB, NBA, Soccer, Tennis. UFC has one prop market. NHL is fully absent.

---

## 2. Per-sport model inventory

### 2.1 MLB

| Layer | Module | Type | Notes |
|---|---|---|---|
| Prop factor engine (pitcher K) | `services/mlb_feature_engine.py::build_mlb_pitcher_k_factors` | Deterministic feature-based | Uses statsapi season K%, opp team K%, park factors, weather, umpire. Min 3 real factors gated. |
| Prop factor engine (hitter H/HR/TB/HRR/RBI) | `services/mlb_feature_engine.py::build_mlb_hitter_factors` | Deterministic feature-based | Statcast, BvP, opp SP hand splits, lineup slot. Min 3 real factors gated. |
| K probability model | `services/mlb_k_probability.py::evaluate_k_pick` | **Poisson (real)** | Called from the pair-dedup + pick-gate paths. Emits or drops per `emit=True/False` + `edge_pp`. |
| Game moneyline | `services/mlb_feature_engine.py::build_mlb_ml_factors` | Deterministic feature-based | Elo, bullpen, recent form, park. Min 4 factors. |
| Game totals | `services/mlb_feature_engine.py::build_mlb_total_factors` | Deterministic feature-based | Park, weather, umpire, lineup R/L. Min 4 factors. |
| Alt run-line / team totals | `sports_engine._build_mlb_alt_picks` | Book-anchored + real factors recalibration | Seeds `mp = imp + 0.03` then overrides with factor mean when ≥3 real factors present (line 3093). |
| NRFI/YRFI | `brain/nrfi_engine.py` + `brain/nrfi_yrfi_model.py` | Independent model | 639+174 LOC. Read-only inspection of file headers. |
| BvP snapshot | `mlb_bvp.py` | Enrichment (not a model) | Feeds `sim_mlb` via `mlb_bvp.ba / hr_per_ab / rbi_per_ab / k_rate`. |
| Lineup / starter | `mlb_lineup.py`, `mlb_live.py` | Enrichment | Confirms starting pitchers, opposing hand, lineup slot. |
| Umpire K adjust | `services/mlb_umpire.py` | Enrichment | Feeds K factor engine. |
| Statcast usage | `services/mlb_statcast.py`, `services/mlb_stuff_plus.py` | Enrichment | Feeds hitter/pitcher engines. |
| Simulator (Monte Carlo) | `brain/sim_mlb.py::simulate_mlb_pick` | Distribution-based MC (20K runs) | See `PHASE4_SIMULATOR_AUDIT.md` — **not seeded**. |

**Verdict:** MLB is the most complete stack — real feature engines with hard `has_enough_real_data` gates on every hitter/pitcher prop path.

### 2.2 NFL

| Layer | Module | Type | Notes |
|---|---|---|---|
| Prop factor engine (all NFL prop markets) | `services/nfl_feature_engine.py::build_nfl_prop_factors` | Deterministic feature-based | Uses NFLverse historical data (2019-2025). Min 3 real factors gated by `has_enough_real_data_nfl`. |
| Prop pre-compute wiring | `sports_engine._props_picks_from_event` (async pre-loader → `ctx["nfl_precomputed"]`) | Cache + sync lookup | If precomputed missing → `_skip_pick`. |
| Anytime TD dedicated engine | `nfl_atd_engine.py` | Causal Poisson: `P(TD≥1) = 1 − exp(−λ)` with 5 layered factors | **NOT wired to live pick emission.** Only exposed via `routes/nfl_routes.py::nfl_atd_leaderboard`, `nfl_atd_predict`. |
| Safe-bets engine | `nfl_safe_engine.py` | Empirical CDF on rolling distributions with hard filters | **NOT wired to live pick emission.** Admin/on-demand only. |
| Game moneyline / spread / total | `nfl_game_engine.py` | Points-differential + logistic + Normal CDF | **NOT wired to live pick emission.** Live pipeline uses `sports_engine._picks_from_game`. |
| Simulator | *(none for NFL)* | — | `_SPORTS_WITH_SIM = {MLB, Soccer, NBA, Tennis}` — NFL is EXCLUDED. |

**Verdict:** Rich model catalogue but only the prop feature engine is on the emission path. `nfl_atd_engine`, `nfl_safe_engine`, `nfl_game_engine` are **underutilised**.

### 2.3 CFB

| Layer | Module | Type | Notes |
|---|---|---|---|
| Prop factor engine | `services/cfb_feature_engine.py` | Deterministic feature-based (returning production, SP+, transfer portal, career-vs-opp, SoS, L5) | **NOT wired to live pick emission.** Comment at `sports_engine.py:3992-4014` explicitly says wiring is pending Aug 15. |
| CFB pre-compute | `services/cfb_precompute.py` | Ingest job | Exists but never fed into `_ctx["cfb_precomputed"]` because CFB emission falls through to `factors = {"Book Implied Probability": mp}`. |
| CFB rationale | `services/cfb_rationale.py` | Descriptive | Generates "Why this pick" text — not a model. |
| Simulator | *(none for CFB)* | — | Not in `_SPORTS_WITH_SIM`. |

**Verdict:** CFB is book-follow only today. Feature engine exists but is dark. **Defect C-1** in the executive summary.

### 2.4 NBA

| Layer | Module | Type | Notes |
|---|---|---|---|
| Prop factor engine | *(none live)* | — | `sports_engine.py:4024-4030` — falls to `{"Book Implied Probability": mp}`. |
| Ingest | `services/nba_ingest.py`, `services/nba_gamelog_ingest.py` | Data ingest | Populates `player_game_logs` but not consumed by the emission path. |
| Rationale | `services/nba_rationale.py` | Descriptive | Not a probability model. |
| Simulator | `brain/sim_nba.py::simulate_nba_pick` | Distribution-based MC (20K runs, Poisson) | Uses `_factor(pick, "usage_rate", 50.0)` — anchored on Lock-Score factor keys. Reads its own inputs from the pick's `factors` dict — which for NBA is `{"Book Implied Probability": mp}`. **The sim reads mostly defaults.** |

**Verdict:** NBA has NO real production feature engine — book-follow with a Poisson sim that reads mostly hard-coded defaults. **Highest-risk sport for false confidence.** **Defect N-1**.

### 2.5 Soccer

| Layer | Module | Type | Notes |
|---|---|---|---|
| Anytime goal scorer / SoA / FGS | `goal_scorer_engine_v2.py` (640 LOC), `goalscorer_matchup.py` (1003 LOC) | Feature-based scorer model | Real. Includes minutes, starter status, xG, opp matchup. |
| Elite player boost | `elite_players.py` + inline hooks in `_props_picks_from_event` (lines 4067-4099) | Hard-coded whitelist with lock-score floor 88 | Curated list of Salah/Haaland/Mbappé/Kane/Messi/etc. |
| ESPN CSL / MLS scorer pick generator | `sports_engine._espn_csl_scorer_picks`, `_espn_mls_scorer_picks` | Alternate scorer path (real ESPN feeds) | ~200 LOC each. |
| Synthetic soccer scorer picks | `sports_engine._synthetic_soccer_scorer_picks` (line 5028) | ⚠️ Fallback synthesizer | Need to review below (see **defect S-1**). |
| MLS scorer gate | `services/mls_scorer_gate.py` | Whitelist filter | Blocks reserve players from surfacing over real starters. |
| Non-scorer props | *(none live)* | — | 1X2 / BTTS / totals / corners / cards / player shots use book-anchored `mp = imp + 0.03` with **no real feature engine**. |
| Simulator | `brain/sim_soccer.py`, `brain/sim_soccer_scorer.py` | Distribution-based MC (20K runs, Poisson λ) | See simulator audit. |

**Verdict:** Scorer path is real. **Non-scorer soccer markets (1X2, BTTS, totals, corners, cards, shots) are book-follow.**

### 2.6 Tennis

| Layer | Module | Type | Notes |
|---|---|---|---|
| Composite confidence | `tennis_engine.py::apply_tennis_engine` | Weighted book-implied + `_player_hash` variance | **DEFECT T-1**: `_player_hash(name)` uses MD5-of-name → deterministic pseudo-random 0-1 per player, used as identity baseline for surface/form/serve/motivation/matchup scores. Comment on line 199: *"Real stats will replace this when wired in"*. |
| Alt-line builder | `sports_engine._build_tennis_alt_picks` | Book-anchored + `_pick_sweet_spot_alts` | **Uses REAL sportsbook alt outcomes only** as of 2026-06-30 (`_synthesize_chalk_alt_totals` is dead code and no longer called — see `sports_engine.py:2810-2820`). |
| Tennis Elo | `services/tennis_calibration.py`, `services/tennis_elite_players.py` | Elo-style ratings ingest | Backfilled by `espn_settlement.backfill_tennis_elo`. |
| Feature engine | `services/tennis_feature_engine.py` (130 LOC) | Thin | Not aggressively wired. |
| Math engine | `services/tennis_math_engine.py` (208 LOC) | Serve/return-based match win probabilities | Wired for spread/game-total sim. |
| Simulator | `brain/sim_tennis.py::simulate_tennis_pick` | **True event simulation** (point-by-point via serve p) | See simulator audit. |

**Verdict:** Tennis publication layer is **book-follow with hashed variance**. Real Elo/serve% ratings exist in feature/math engines but their wiring to the visible composite score is thin.

### 2.7 UFC

| Layer | Module | Type | Notes |
|---|---|---|---|
| ATP/MMA method-of-victory | `sports_engine._props_picks_from_event` for `mma_method_of_victory` | Book-follow with min 18% implied floor | No real model. |
| Ingest | `ufc_espn_ingest.py` | Data ingest | Fight results only. |
| Elo/rating | *(none)* | — | UFC has NO rating system in code. |

**Verdict:** UFC method-of-victory is **book-follow only**. Very small market — 1 prop key.

---

## 3. Global concerns across all sports

### 3.1 Book-follow fallback breadth

Fully **real-model** paths on the live emission path:
- MLB pitcher K props
- MLB hitter props (Hits, TB, H+R+RBI, HR, RBI)
- MLB moneyline / spread / total (with `_ctx` game context)
- NFL prop markets (via NFL precompute)
- Soccer anytime scorer / SoA / FGS (via `goal_scorer_engine_v2`)

Fully **book-follow** on the live emission path (per `sports_engine.py:3992-4030`):
- CFB — all markets
- NBA — all markets
- Non-scorer soccer — 1X2, BTTS, totals, corners, cards, player shots
- Tennis (composite path — hashed variance + book implied)
- UFC — method of victory
- Non-MLB pitcher props (KBO leftover)

### 3.2 Composite mixing across model → seed → recalibration

Every non-MLB / non-NFL sport currently follows the pattern:
```python
mp = max(0.65, min(0.95, implied + <small bump>))
factors = {"Book Implied Probability": mp}
# then compute_lock_score(factors, win_prob=mp*100)
```
This produces a `lock_score` derived **entirely** from book implied probability. The lock score is then a deterministic monotonic function of book price — the model adds **no signal**.

### 3.3 Elite-player boosts

Soccer elite-player list (`elite_players.py`) applies:
- +10 % boost to every factor
- 88 lock floor
- 95 elite floor via `evidence_engine.py` career enrichment path

This is a **hard-coded lift** for a curated list of ~40-50 players. It is **not calibrated** to their per-market historical hit rate. Risk: elite bias inflates lock for markets where the elite player is priced accurately.

### 3.4 Data-quality gates

Per-sport enforced:
- MLB — `MIN_FACTORS_K_PROP=3`, `MIN_FACTORS_HITTER_PROP=3`, `MIN_FACTORS_ML=4`, `MIN_FACTORS_TOTAL=4`.
- NFL — `MIN_FACTORS_NFL_PROP=3`.

Per-sport NOT enforced (silent book-follow):
- NBA, CFB, non-scorer Soccer, Tennis (composite path), UFC — **no min-factor gate**. A pick emits with a single "Book Implied Probability" factor. **Highest false-confidence risk.**

---

## 4. Model comparison — most-complicated ≠ best

For MLB pitcher K props, TWO models overlap:
1. `services.mlb_feature_engine.build_mlb_pitcher_k_factors` — deterministic feature-based, feeds Lock-Score.
2. `services.mlb_k_probability.evaluate_k_pick` — Poisson probability model with explicit `emit`/`edge_pp`.

Both fire in `_props_picks_from_event`: model 1 builds Lock-Score factors, model 2 gates emission (`if not _k_eval.get("emit"): _skip_pick = True`) and OVERRIDES `mp` with `_k_eval["model_prob"]`. **Two models, one wins mp, the other wins lock inputs.** Ensemble by construction. Recommended check in Phase 4B: compare `_k_eval.model_prob` vs Lock-Score-derived `mp` for out-of-sample ROI.

For soccer scorer, two engines overlap:
1. `goal_scorer_engine_v2` — factor-based per-player scorer probability.
2. `goalscorer_matchup` — opponent-adjusted scorer probability.

Wiring order is not obvious from static reading; runtime tracing needed to confirm which produces the emitted `mp`.

For MLB game markets, THREE models overlap: `services.mlb_feature_engine.build_mlb_ml_factors` (production emission), `brain/sim_mlb` (as anchor floor via `sim_runner.apply_simulations`), and (dormant) `brain/nrfi_engine`. Simulator floors are ONE-DIRECTIONAL (lift only) — see `PHASE4_SIMULATOR_AUDIT.md` §2.

---

## 5. Reproducibility

Every prop path uses `random.random()` or an unseeded `random.Random()`:
- `brain/simulator.py:35`: `_RNG = random.Random()` — no seed.
- `brain/sim_mlb.py:64,73`: `random.random()` — global RNG, no seed.
- `brain/sim_nba.py:53-66`: `_poisson` uses global RNG.
- `brain/sim_tennis.py`: `random.random()` in game/set/match loops.
- `brain/sim_soccer.py`: `_poisson` global RNG.
- `sports_engine._build_mlb_alt_picks:2914-2915`: **uses a deterministic seed** derived from `hash(f"MLB-alt-{home}-{away}-{date_str}")` — this is the ONE path that IS reproducible.

**Verdict:** every Monte Carlo simulator is **non-reproducible across runs**. Every refresh may produce different `sim_win_probability` values for the same input pick. This is a test/regression blocker.

---

## 6. Historical performance tracking

- `services/lock_score_performance.py`, `services/pvt_backtest.py`, `services/backtest_framework.py` — backtest scaffolding present.
- `learning_engine.py`, `learning_system_v2.py`, `learning_buckets.py` — learning loops feeding recalibration.
- `lock_calibration.py` — isotonic regression from historical settled picks with 100-pick auto-refit trigger.
- `parlay_history.py` legacy (still there for old rows, per Phase 3G).
- `user_bets` — Phase 3G canonical wager ledger (Phase 4 does not need to touch it).

**Observation:** the calibration loop treats all sport/market combinations as a **single pooled distribution**. Any per-sport / per-market calibration would need a segmented curve. See `PHASE4_CALIBRATION_BASELINE.md` for the recommended split axes.

---

## 7. Files audited (Model layer)

- `sports_engine.py` — full read of prop path (lines 2170–4230) and market map.
- `brain/simulator.py`, `brain/sim_runner.py`, `brain/sim_mlb.py` (full read).
- `brain/sim_nba.py`, `brain/sim_soccer.py`, `brain/sim_soccer_scorer.py`, `brain/sim_tennis.py` (signature-level).
- `services/mlb_feature_engine.py` (signatures + `has_enough_real_data`).
- `services/nfl_feature_engine.py`, `services/cfb_feature_engine.py` (signatures + wiring status).
- `services/mlb_k_probability.py` (via cross-references).
- `services/alt_line_engine/ranker.py` (full read — admin-only).
- `services/discovery/magic_finder.py` (full read — Magic Tier aggregator).
- `nfl_atd_engine.py`, `nfl_game_engine.py`, `nfl_safe_engine.py` (docstrings + wiring).
- `tennis_engine.py` (partial — enough to identify defect T-1).
- `settlement_engine.py` (settlement mapping — full read of `settle_pick`).
- `parlay_history.py`, `prop_settlement.py`, `espn_settlement.py`, `kbo_settlement.py` (signatures + settlement invariants).

**Not read** (deferred to Phase 4B if needed): full body of `brain/nrfi_engine.py`, deep body of `goal_scorer_engine_v2.py` (640 LOC), `goalscorer_matchup.py` (1003 LOC), full body of `services/mlb_hitter_intel.py` (909 LOC), full body of `services/mlb_hr_intel.py` (893 LOC). Signature-level review suggests these are legitimate real-data engines; full byte-level review deferred.
