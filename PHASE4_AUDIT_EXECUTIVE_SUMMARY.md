# PHASE 4A — AUDIT EXECUTIVE SUMMARY

**Status:** Phase 4A audit COMPLETE. No code changes made. Ready for user review before Phase 4B implementation.
**Companion docs:** `PHASE4_MODEL_AUDIT.md`, `PHASE4_MARKET_AUDIT.md`, `PHASE4_SIMULATOR_AUDIT.md`, `PHASE4_MAGIC_TIER_AUDIT.md`, `PHASE4_CALIBRATION_BASELINE.md`.

---

## 1. Full sport / market inventory (live pipeline)

| Sport | Live? | Real model on emission path? | Markets covered | Markets missing |
|---|---|---|---|---|
| **MLB** | ✅ | ✅ props + game | ML/spread/total, Hits (main+alt), TB (1.5+/alt), H+R+RBI (main+alt), HR (main+alt), RBI (main+alt), pitcher K (main+alt), pitcher Outs (main), alt run-line +1.5-3.5. NRFI/YRFI has an engine (`brain/nrfi_engine.py`) — wiring to emission NOT confirmed. | **Runs (batter_runs_scored) NOT fetched** even though Odds API supports it. Team totals disabled. Pitcher walks / hits-allowed / earned-runs not fetched. |
| **NFL** | ✅ | ✅ props via `nfl_precomputed` | ML/spread/total, pass yds/TDs/att/comp, rush yds/att/TDs, receptions/rec-yds/rec-TDs, Anytime TD, 1st TD. | Sport-specific ATD & Safe-Bets & Game engines EXIST but are NOT wired (admin routes only). |
| **CFB** | ⚠️ book-follow only | ❌ CFB feature engine DARK | Same market list as NFL. | Feature engine (`services/cfb_feature_engine.py`) exists but is not wired to emission (`sports_engine.py:3992-4014`). |
| **NBA** | ⚠️ book-follow only | ❌ no feature engine wired | ML/spread/total, Points, Rebounds, Assists (each with alt). | **PRA and 3-pointers NOT fetched.** No usage/pace/rest/minutes/injury gate. Ingest exists but is unconsumed. |
| **NHL** | ❌ | ❌ | *(none)* | **Entire sport absent.** Only `historical/nhl.py` for historical ingest exists. |
| **Soccer** | ✅ scorer / ⚠️ book-follow non-scorer | ✅ scorer / ❌ non-scorer | 1X2, spread, totals, Anytime Scorer, Score-or-Assist, First Goal Scorer. 30+ leagues incl. World Cup, UCL, EPL, La Liga, Bundesliga, MLS, CSL, Copa Libertadores. | **BTTS, corners, cards, player-shots NOT fetched.** |
| **Tennis** | ⚠️ book-follow + hashed noise | ❌ composite uses MD5-of-name variance | ML, game spread, total games, alt spreads, alt totals (real book only since 2026-06-30). | Set markets, correct-score, first-set not fetched. |
| **UFC** | ⚠️ book-follow | ❌ | Method of victory (book-follow, ≥18% implied). | Method breakdown by round, round betting not fetched. No fighter Elo/rating anywhere. |

---

## 2. Top defects ranked by impact × likelihood

### 🔴 P0 — Structural false-confidence risks

| Rank | ID | Description | Sports affected |
|---|---|---|---|
| 1 | **S-1** | `brain/simulator.py::run_simulator` is a Beta-Bernoulli sampler seeded from the pick's own confidence, presented as independent MC evidence. | ALL |
| 2 | **T-1** | Tennis composite uses `_player_hash` MD5-of-name as identity baseline for surface / form / serve / motivation / matchup scores. Comment explicitly labels it a placeholder that was never replaced. | Tennis |
| 3 | **NBA-1 + NBA-3** | NBA prop feature engine not wired; no usage/pace/rest/minutes/injury gate; every NBA prop emits with `factors = {"Book Implied Probability": mp}`. | NBA |
| 4 | **C-1** | CFB feature engine exists but is not wired; every CFB pick emits book-follow. | CFB |
| 5 | **S-3** | Simulator anchor is asymmetric (lift-only). Sim cannot correct engine over-confidence. | ALL sports with a sim wired |
| 6 | **M-3** | Primary emission sort key at the family-dedup stage is book-implied, not EV/edge. | ALL |
| 7 | **CAL-1** | Isotonic calibration curve is pooled across all sport/market pairs. | ALL |
| 8 | **CAL-2 + CAL-3** | No per-line-band and no per-odds-band segmentation in calibration buckets. | ALL |

### 🟡 P1 — Coverage gaps

| Rank | ID | Description | Sports affected |
|---|---|---|---|
| 9 | **M-1** | MLB `batter_runs_scored` (Runs) market NOT fetched. | MLB |
| 10 | **NBA-2** | PRA + 3-pointers not fetched. | NBA |
| 11 | **SOC-1** | BTTS / corners / cards / player-shots not fetched. | Soccer |
| 12 | **T-2** | Set markets, correct-score, first-set not fetched. | Tennis |
| 13 | **NHL absent** | Entire NHL sport absent from live pipeline. | NHL |
| 14 | **NFL underutilised** | `nfl_atd_engine`, `nfl_safe_engine`, `nfl_game_engine` are real, guardrailed models but NOT on the live emission path. | NFL |

### 🟡 P1 — Structural bookkeeping

| Rank | ID | Description | Sports affected |
|---|---|---|---|
| 15 | **RL-1** | Bookmaker identity anonymised in emitted pick (`median` price across books). | ALL |
| 16 | **RL-2** | Odds-snapshot timestamp per book not on pick doc. | ALL |
| 17 | **M-2** | Rejection counters do not distinguish provider-gap vs feature-gap vs dedup vs implied-gate. | ALL |
| 18 | **CORR-1** | Cross-market correlation not enforced. `correlation_guard.py` exists but wiring not confirmed. | ALL |
| 19 | **S-2** | All simulators use unseeded global `random` → non-reproducible. | ALL |
| 20 | **CAL-4 to CAL-8** | Push/void not modelled, CLV backfill incomplete, blend weights static. | ALL |

### 🟡 P1 — Simulator quality

| Rank | ID | Description | Sports affected |
|---|---|---|---|
| 21 | **S-4** | `sim_nba` back-solves λ from book_implied → adds no signal beyond book. | NBA |
| 22 | **S-5** | `sim_mlb._simulate_hrr` uses hardcoded `run_p = BA*0.45` + probable HR double-count. | MLB H+R+RBI |
| 23 | **S-6** | Wilson CI reported per pick but never used as a rejection gate. | ALL |
| 24 | **S-8** | NBA sim reads default factor values (50.0) — sim inputs are all defaults. | NBA |

### 🟡 P2 — Magic Tier (contained to admin today)

| Rank | ID | Description |
|---|---|---|
| 25 | **MT-1** | `bucket_roi` defaults to 0.5 when historical data absent — low-sample markets rank equal to calibrated ones. |
| 26 | **MT-2** | Composite score not calibrated against historical ROI — no probability meaning. |
| 27 | **MT-3** | `p_norm` and `edge` are correlated components sharing `p_model`. |
| 28 | **MT-4** | Weak / book-follow models can achieve mid-to-high composites. |
| 29 | **MT-5** | `source="model_projection"` alt lines rank alongside real market alts. |
| 30 | **MT-8** | If wired to publication, Magic Tier acts alone (no Lock/sim/data-quality interaction). |

### 🟡 P2 — Soft / latent

| Rank | ID | Description |
|---|---|---|
| 31 | **SOC-2** | Non-MLS starter-status gate absent — reserves in other leagues can slip through. |
| 32 | **SOC-3** | `_synthetic_soccer_scorer_picks` needs verification that every emitted pick is bookmaker-priced. |
| 33 | **RL-3** | `_synthesize_chalk_alt_totals` still exists as dead code (2026-06-30 sunset) — recommend deletion. |
| 34 | **S-7** | Push handling on integer-line markets would misgrade (latent, not fired today). |

---

## 3. Models that are truly active

| Model | Active on emission? |
|---|---|
| `services/mlb_feature_engine.build_mlb_pitcher_k_factors` | ✅ |
| `services/mlb_feature_engine.build_mlb_hitter_factors` | ✅ |
| `services/mlb_feature_engine.build_mlb_ml_factors` | ✅ |
| `services/mlb_feature_engine.build_mlb_total_factors` | ✅ |
| `services/mlb_k_probability.evaluate_k_pick` (Poisson) | ✅ |
| `services/nfl_feature_engine.build_nfl_prop_factors` (via precompute) | ✅ |
| `services/cfb_feature_engine.build_cfb_prop_factors` | ❌ (not wired) |
| `nfl_atd_engine.predict_player_atd` | ❌ (admin route only) |
| `nfl_safe_engine.compute_safe_bets` | ❌ (admin route only) |
| `nfl_game_engine` | ❌ (admin route only) |
| `goal_scorer_engine_v2` | ✅ |
| `goalscorer_matchup` | ✅ |
| `tennis_engine.apply_tennis_engine` (composite w/ hashed noise) | ✅ |
| `brain/simulator.py::run_simulator` | ✅ but mislabelled |
| `brain/sim_mlb`, `sim_nba`, `sim_tennis`, `sim_soccer`, `sim_soccer_scorer` | ✅ |
| `services/alt_line_engine/*` (Magic Tier ranker) | ❌ (admin only) |
| `services/discovery/magic_finder` | ❌ (admin only) |

---

## 4. Simulators — real vs mislabelled

| Simulator | Verdict |
|---|---|
| `brain/simulator.py::run_simulator` | ❌ **Mislabelled** — Beta-Bernoulli around the pick's own confidence. |
| `brain/sim_mlb` | ✅ Real distribution MC (Bernoulli-per-AB), but non-reproducible + H+R+RBI has hardcoded coefficients. |
| `brain/sim_nba` | ⚠️ Real MC mechanism, but back-solves λ from `book_implied` and reads default factors — adds no signal beyond book on NBA emission. |
| `brain/sim_tennis` | ✅ **True event simulation** (point-by-point), but serve-gap back-solved from book price for h2h. |
| `brain/sim_soccer` + `sim_soccer_scorer` | ✅ Real dual-Poisson MC. |
| `brain/nrfi_engine.py` | Not audited in depth this pass — wiring to emission needs confirmation. |

**All simulators use unseeded `random` → non-reproducible across runs.**

---

## 5. Which markets use real lines correctly

Full compliance with real-line policy (real bookmaker odds, exact line preserved):
- MLB props (Hits, HR, RBI, TB, H+R+RBI, K, Outs).
- MLB alt run-line and (dormant) team totals.
- MLB ML / spread / total.
- NFL props + game markets.
- CFB props + game markets (odds real; model book-follow).
- NBA props + game markets (odds real; model book-follow).
- Soccer scorer + game markets.
- Tennis h2h + spreads + totals + alt spreads + alt totals **(since 2026-06-30 synthesis sunset)**.
- UFC method of victory.

Contained real-line risks:
- `_synthesize_chalk_alt_totals` is dead code but still present — accidental re-invocation risk (**RL-3**).
- `alt_line_engine` emits `source="model_projection"` lines — contained to admin routes today (**MT-5**).
- `_synthetic_soccer_scorer_picks` — needs verification it never emits without a real book price (**SOC-3**).

---

## 6. Markets at risk of synthetic or stale lines

| Risk | Location | Contained? |
|---|---|---|
| `_synthesize_chalk_alt_totals` | `sports_engine.py:2600-2704` | Yes — not called since 2026-06-30 but code still exists. |
| `alt_line_engine::AltLine.source="model_projection"` | `services/alt_line_engine/ranker.py:222-236` | Yes — admin only. |
| `_synthetic_soccer_scorer_picks` | `sports_engine.py:5028+` | **Needs verification.** |
| Odds staleness | `median` re-computed on every refresh — no per-book snapshot lifetime | Partial — `prediction_snapshots` captures point-in-time, but user-facing pick shows current median. |

---

## 7. Which settlement paths are unsafe

| Path | Sport | Status |
|---|---|---|
| `settlement_engine.settle_pick` | ALL game markets | ✅ Handles ML (2-way + 3-way), spread/handicap, team totals, game totals. `_score_for` team-name matching could fail on alias mismatches — mid-priority. |
| `espn_settlement.settle_tennis_via_espn` + `settle_ufc_via_espn` + `settle_player_props_via_espn` | Tennis, UFC, NFL/NBA player props | ✅ Standard ESPN boxscore path. Retirement/walkover handled. |
| `prop_settlement._espn_did_score_goal` / `_did_score_or_assist` | Soccer | ✅ Player-appearance + goal detection present. |
| `soccer_espn_settle.py`, `soccer_fotmob_settle.py` | Soccer scorer | ✅ Dual-provider settlement. |
| `kbo_settlement.py` | KBO | Sport disabled — path is dormant. |
| `tennis_extra/settle.py` | Tennis retirements/walkovers | ✅ Present. |
| MLB batter did-not-play void | `stuck_pick_reaper.py` | Needs verification that scratched-starter picks are voided vs settled. |
| Postponed / suspended games | `settlement_engine.settle_pick` returns `None` on `not completed` | ✅ Correct — leaves pending. |
| Stat corrections / official-scoring changes | *(no dedicated audit trail)* | ⚠️ No documented re-settlement flow when an official scoring change reverses a pick. |

---

## 8. Sports with insufficient data

| Sport | Data status |
|---|---|
| MLB | ✅ Statcast + statsapi + BvP + park + weather + umpire. |
| NFL | ✅ NFLverse 2019-2025 historical. |
| CFB | ⚠️ Feature engine exists (`services/cfb_precompute.py`) but not wired. |
| NBA | ⚠️ Ingest exists (`services/nba_gamelog_ingest`, `services/nba_ingest`) but unconsumed by emission. |
| NHL | ❌ Not in live pipeline. |
| Soccer | ✅ ESPN + FotMob + multiple providers for scorer. Non-scorer data thin. |
| Tennis | ⚠️ Elo backfill via ESPN present, but composite emission path ignores it and uses hashed identity variance. |
| UFC | ❌ No rating/style data. |

---

## 9. Magic Tier inputs that are unreliable

- **`bucket_roi` default = 0.5** when no historical data.
- **`edge` default = 0.5** when `model_projection` (no market line).
- **`confidence` default = 0.5** when `residual_std` is None.
- **`p_norm` correlates with `edge`** (both derive from `p_model`) — moves 55% of composite together.

Result: **Magic Tier can label a book-follow pick as Tier 1** with no real signal. Not user-facing today (admin only) — but critical to fix before any surfacing to end users.

---

## 10. Recommended Phase 4B execution order

Ordered by impact-to-risk ratio. Each step ships with a regression baseline and no simultaneous coverage expansion.

| Step | Deliverable | Impact | Risk |
|---|---|---|---|
| **4B-0** | Baseline calibration report + DB snapshot (**audit tooling only**, zero production writes). Ships `scripts/phase4_calibration_report.py`. | Fixes CAL blind spot before any change. | Very low. |
| **4B-1** | Retire / rebrand `brain/simulator.py::run_simulator` — rename to `posterior_uncertainty`, stop feeding as independent evidence to Decision Filter. Retain CI output. | Fixes S-1. | Low — filter downstream is well-encapsulated. |
| **4B-2** | Seed every simulator's RNG per pick (deterministic per `pick.id`). Fixes S-2. Enables regression tests. | Medium. | Very low. |
| **4B-3** | Make `sim_runner._anchor_pick_to_sim` symmetric within `SIM_RESIDUAL_MAX = 3.0` band, preserving elite-floor override. Fixes S-3. | High. | Medium — need to protect elite floor. |
| **4B-4** | Fix `sim_mlb._simulate_hrr` double-count and add lineup-slot-aware `run_p`. Fixes S-5. Applies to H+R+RBI (per user's headline requirement). | High. | Low. |
| **4B-5** | Wire the existing CFB feature engine to the sync emission path via a `_ctx["cfb_precomputed"]` precompute step. Fixes C-1. | High for CFB (once season starts). | Low. |
| **4B-6** | Build NBA feature engine (usage / pace / minutes / rest / matchup / injury) and wire via `_ctx["nba_precomputed"]`. Adds min-3-factor gate. Fixes NBA-1, NBA-3. Add PRA + 3-pointers to fetch list (NBA-2). | Very high — NBA is the largest sport by market count. | Medium — new engine build. |
| **4B-7** | Replace `tennis_engine._player_hash` with the existing Elo + serve% data (via `services/tennis_math_engine`). Fixes T-1. | High. | Low-Medium. |
| **4B-8** | Add `batter_runs_scored` to `PROP_MARKETS["MLB"]` and route through existing hitter factor engine. Fixes M-1. | Medium. | Very low. |
| **4B-9** | Segment isotonic calibration curves by `(sport, market_family)`. Fix CAL-1. Requires 4B-0 baseline. | Very high. | Medium — code touches every pick serialisation. |
| **4B-10** | Add push/void/postponed rate tracking per market family. Fixes CAL-4. Verify MLB scratched-starter void path. | Medium. | Low. |
| **4B-11** | Sort emission by EV/edge instead of raw implied (or blend). Fixes M-3. | High. | Medium — changes board composition. |
| **4B-12** | Add `main_vs_alt` split to (sport, market_family) calibration for MLB hitter markets. | Medium. | Low. |
| **4B-13** | Add BTTS / corners / cards / player-shots to Soccer fetch list. Fixes SOC-1. | Medium. | Low. |
| **4B-14** | Add set markets / correct-score / first-set to Tennis fetch list. Fixes T-2. | Medium. | Low. |
| **4B-15** | Add bookmaker identity + per-book odds-snapshot timestamps to emitted pick. Fixes RL-1, RL-2. | Medium — audit strength. | Low. |
| **4B-16** | Add rejection-count telemetry per market to distinguish provider-gap vs feature-gap vs dedup vs implied-gate. Fixes M-2. | Low. | Very low. |
| **4B-17** | Wire `correlation_guard.py` to the emission path OR document that correlation is intentionally not enforced. Fixes CORR-1. | Medium. | Low. |
| **4B-18** | Delete dead `_synthesize_chalk_alt_totals`. Fixes RL-3. | Very low. | Very low. |
| **4B-19** | Verify `_synthetic_soccer_scorer_picks` never publishes without a real bookmaker price. Fixes SOC-3. | Low. | Low. |
| **4B-20** | Wire `nfl_atd_engine` + `nfl_safe_engine` + `nfl_game_engine` into the live NFL emission path. Fixes NFL underutilisation. | Very high (NFL). | Medium — model comparison needed before promoting. |
| **4B-21** | (Deferred) NHL scope decision — build from scratch or leave out. | — | Very high — full sport implementation. |

---

## 11. Estimated risk of each fix

- **Very low risk:** 4B-0, 4B-2, 4B-8, 4B-13, 4B-14, 4B-15, 4B-16, 4B-17, 4B-18, 4B-19.
- **Low risk:** 4B-1, 4B-4, 4B-5, 4B-10, 4B-12.
- **Medium risk:** 4B-3, 4B-7, 4B-9, 4B-11, 4B-20.
- **Higher risk (new engine builds):** 4B-6 (NBA), 4B-21 (NHL if in scope).

---

## 12. Dependencies and blockers

- **4B-9 (segmented calibration)** blocks any downstream calibration-related change — must land first once 4B-0 baseline is captured.
- **4B-3 (symmetric sim anchor)** should follow 4B-4/4B-5/4B-6 so that any newly-corrected sim outputs don't cause pre-existing engine over-confidence to be surfaced twice.
- **4B-20 (NFL engine wiring)** requires 4B-9 segmented calibration to avoid regressing NFL calibration by mixing model outputs.
- **NBA feature engine (4B-6)** blocks 4B-9 NBA segmentation being informative — until the NBA engine ships, NBA calibration is calibrating book_implied against outcomes, which is fine but redundant.
- **CFB feature engine wiring (4B-5)** requires the `americanfootball_ncaaf` season to be live for meaningful volume — Week 0 is late August 2026. **This may impose a schedule constraint.**

---

## 13. H+R+RBI root cause findings

**User claim to validate:**
> main line and alternate lines are both discovered; real Over 0.5 is supported when supplied by a sportsbook; Over 0.5 is never fabricated from 1.5; 0.5, 1.5, and 2.5 are evaluated independently; exact line, odds, sportsbook, and timestamp are preserved; one line failing does not block another qualified line; player/event matching does not merge different contracts; provider missing-data issues are distinguishable from downstream filtering; main-line and alt-line rejection counts are logged separately.

**Findings — per the deep dive in `PHASE4_MARKET_AUDIT.md` §MLB H+R+RBI:**

- ✅ **Main + alt lines both discovered** — both `batter_hits_runs_rbis` and `batter_hits_runs_rbis_alternate` are in `PROP_MARKETS["MLB"]` and fetched per event.
- ✅ **Real Over 0.5 supported when book supplies it** — the emission path buckets by `(mk, player, point, side)` — `point` = exact float from The Odds API.
- ✅ **No fabrication from 1.5 to 0.5** — no synthesis code touches MLB H+R+RBI.
- ✅ **0.5, 1.5, 2.5 evaluated independently** — different `point_key` = different bucket → separately scored through the implied gate + `build_mlb_hitter_factors` + `has_enough_real_data` gate.
- ✅ **Exact line & odds preserved** — `point` is the exact float; `median` is the median across books for the identical `(line, side)` contract.
- ⚠️ **Sportsbook identity** — the `median` is anonymised across books (**RL-1**). The exact per-book price is captured in `alt_lines_feed` / `propline_feed` snapshots but the emitted pick only shows `median`.
- ⚠️ **Timestamp** — `event_time` and `created_at` are on the pick, but the odds-snapshot timestamp per book is only in `prediction_snapshots`, not on the pick doc (**RL-2**).
- ✅ **One line failing does not block another qualified line** — pair-dedup and family-dedup keep the WINNER; other lines are dropped by the family cap, not by the failure. If 0.5 has no feature coverage but 1.5 does, 1.5 emits.
- ✅ **Player/event matching does not merge different contracts** — bucket key includes `(mk, player, point, side)` so 0.5 Over and 1.5 Over for the same player are different bucket keys.
- ⚠️ **Provider gap vs downstream filtering NOT distinguishable in logs** (**M-2**) — no separate counter for "provider returned no line" vs "line dropped by implied gate" vs "line lost family dedup" vs "line failed `has_enough_real_data`".
- ⚠️ **Main + alt rejection counts NOT logged separately** — aggregated only in `PAIR_DEDUP: dropped N ...` and `MLB alt-line dedupe: N → M`.

**Structural weakness — simulator side (S-5):**
`sim_mlb._simulate_hrr` hardcodes `run_p = BA*0.45` and adds a `hr * 0.4` extra bump on top of `ba` (which already includes HR) — this **likely double-counts HR contribution** to H+R+RBI. For heavy hitters (BA .300+, HR/AB .05+) the sim slightly over-predicts H+R+RBI Over 1.5.

**Verdict:** H+R+RBI **discovery + line preservation + independence** is correct. The **observability** (rejection log granularity) and the **simulator-side coefficient** are the two fixable items. Neither breaks correctness today; both cap the ceiling on H+R+RBI ROI.

---

## 14. Real-line coverage findings

Summary in `PHASE4_MARKET_AUDIT.md` §9:
- **Real-line policy honoured for prices.** Every emitted market's price is a real median across live book outcomes.
- **Two soft weaknesses:** bookmaker identity anonymised (RL-1) and odds-snapshot timestamp not on pick doc (RL-2).
- **Dead code risk:** `_synthesize_chalk_alt_totals` still in the file — recommend deletion (RL-3).
- **Admin containment:** `alt_line_engine::model_projection` and Magic Finder outputs never surface as public picks.

---

## 15. Settlement audit findings

Summary in `PHASE4_CALIBRATION_BASELINE.md` §7 and `PHASE4_MARKET_AUDIT.md` §11:
- Game markets settle correctly via `settlement_engine.settle_pick`.
- Player props via `prop_settlement.py` + ESPN box scores (`_espn_player_started` / `_appeared` / `_did_score_goal` / `_did_score_or_assist`).
- Tennis retirement / walkover via `tennis_extra/settle.py` and `espn_settlement._tennis_pick_outcome`.
- Postponed → returns `None` from `settle_pick` (pending; correct).
- **No documented re-settlement flow** for stat corrections / official-scoring changes — recommend Phase 4B check.
- **Push/void rate per market family not tracked** — recommend Phase 4B measurement.

---

## 16. Test commands and results

Phase 4A introduces **no new tests** (per user instruction: *"No new tests unless they only validate audit tooling and perform zero production writes."*).

All existing tests (272+ across Phase 3G suite and adjacent) remain unmodified. The Phase 3G Step 7 suite passes 147/147 (from prior verification).

If Phase 4B is authorised, the recommended first test is `scripts/phase4_calibration_report.py` — read-only baseline generator. That is a script, not a test — no `db.picks.update` / `db.user_bets.insert` calls, just aggregations.

---

## 17. Risks and blockers

- **Blocker 1: CFB season timing** — CFB feature engine wiring (4B-5) has minimal impact until Week 0 (late Aug 2026). Recommend prioritise NBA / MLB / Simulator work first.
- **Blocker 2: NBA engine effort** — 4B-6 is a new feature-engine build. Requires: NBA data ingest verification, feature list agreement, min-factor gate calibration.
- **Blocker 3: Sim reproducibility fix (4B-2)** may cause 0.5-1.0 pp lock shifts on picks in flight — communicate to users before roll-out.
- **Blocker 4: Symmetric sim anchor (4B-3)** may demote elite-player picks in edge cases — must protect the `elite_players.py` floor.
- **Risk: Calibration segmentation (4B-9)** cannot run before 4B-0 baseline is captured — enforce ordering.

---

## 18. Suggested Git commit message

```
Phase 4A audit — read-only prediction-engine, market, simulator, magic
tier, calibration baseline. No code changes.

Adds six audit documents at repo root:
  PHASE4_MODEL_AUDIT.md
  PHASE4_MARKET_AUDIT.md
  PHASE4_SIMULATOR_AUDIT.md
  PHASE4_MAGIC_TIER_AUDIT.md
  PHASE4_CALIBRATION_BASELINE.md
  PHASE4_AUDIT_EXECUTIVE_SUMMARY.md

Identifies 34 defects across 7 sports and 6 layers, ranks them by
impact × likelihood, and proposes a 21-step Phase 4B execution
order. Highlights structural false-confidence risks in
brain/simulator.py, tennis_engine._player_hash, the NBA and CFB
book-follow emission paths, and the pooled isotonic calibration
curve. Confirms H+R+RBI discovery + line-preservation +
independence are correct today; identifies simulator-side coefficient
and log-granularity fixes.

Stops. No implementation.
```

---

## 19. Rollback instructions

Phase 4A adds **six markdown files** at repo root:
```
/app/PHASE4_MODEL_AUDIT.md
/app/PHASE4_MARKET_AUDIT.md
/app/PHASE4_SIMULATOR_AUDIT.md
/app/PHASE4_MAGIC_TIER_AUDIT.md
/app/PHASE4_CALIBRATION_BASELINE.md
/app/PHASE4_AUDIT_EXECUTIVE_SUMMARY.md
```

No source files changed. Rollback = `rm` the six files.

---

## 20. Deliverables checklist

1. ✅ Exact files created — six audit MDs listed above.
2. ✅ Exact files changed — **zero source files changed**.
3. ✅ Sport-by-sport model inventory — `PHASE4_MODEL_AUDIT.md` §2.
4. ✅ Market-by-market audit — `PHASE4_MARKET_AUDIT.md` (all sports).
5. ✅ Simulator classification — `PHASE4_SIMULATOR_AUDIT.md` §1 + verdicts §8.
6. ✅ Magic Tier audit — `PHASE4_MAGIC_TIER_AUDIT.md`.
7. ✅ Calibration baseline — `PHASE4_CALIBRATION_BASELINE.md`.
8. ✅ Top defects ranked by impact — §2 of this document.
9. ✅ Recommended implementation sequence — §10.
10. ✅ H+R+RBI root cause findings — §13.
11. ✅ Real-line coverage findings — §14.
12. ✅ Settlement audit findings — §15.
13. ✅ Test commands and results — §16 (no new tests; existing suite intact).
14. ✅ Risks and blockers — §17.
15. ✅ Suggested Git commit message — §18.
16. ✅ Rollback instructions — §19.

---

**Phase 4A audit is COMPLETE. Awaiting user review before beginning any Phase 4B implementation.**
