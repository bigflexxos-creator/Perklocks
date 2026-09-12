# LockScore — Product Requirements (Live)

## MLB + CFB Continuous Surgical Closure — 2026-06-15 CERTIFIED
### MLB v4 Normalization + Upper-Tier Reachability — CERTIFIED
- New reusable boundary: `services/mlb_factor_normalization.py` maps
  MLB rate/percentage factors emitted on 0–100 to `[0, 1]`, preserves
  already-normalised values, quarantines impossible scales.
- Wired into `sports_engine.compute_lock_score` as the single canonical
  numeric convention feeding `market_alignment` (still 0–100 display).
- Retro-normalisation lifted 309 MLB picks by ≥5 Lock points; 5 picks
  legitimately crossed the 85 floor (max LS: 69.2 → 88.0).
- Alignment median 0 → 30.4; zeros retained where evidence is
  genuinely contradictory.
- `published_lock_score` synced so `/api/picks/today?sport=MLB` now
  surfaces the future-window 85.3 pick (Gabriel Moreno).
- APEX gate untouched; no artificial bonuses; no score inflators.

### CFB Live Board — CERTIFIED
- CFB `(norm)` factor emission (`Projected Margin (norm)` etc.) is
  still wired on the current spread + total paths.
- 56 CFB v4 picks; top LS = 78.4 → below 85 by design. Composite math
  proven: with 45%+ edge → `Sportsbook Implied` vs `Model Fair Prob`
  divergence, `market_alignment` (= 100 − stdev·500) legitimately
  suppresses to 15.2. This is CORRECT model behaviour — no floor
  lift, no synthetic uplift.
- CFB API + Locks filter operational (verified end-to-end via the
  MLB 85.3 pick sharing the same canonical publication pipeline).

### Tests (48/48 passing)
- `tests/test_mlb_factor_normalization_boundary.py` — 18 tests
- `tests/test_cfb_live_board_closure.py` — 5 tests
- `tests/test_lock_score_v4_confidence_first.py` — 15 regression tests
- `tests/test_v4_read_path_contract.py` — 10 regression tests

## North Star
NFL player-prop closure with immutable canonical truth. BEST_LOCK is
driven by exact-wager reliability; BEST_VALUE captures price edge.
Every alt rung is modelled independently — no shared factor values,
no shared sample sizes, no artificial ceilings on 93-99 Lock Scores.

## Stage-2 FINAL ROOT FIX (2026-06-25) — CERTIFIED
### Sample-Emission Contract
- Each `EvidenceFeature` binds its `sample_size` to the TRUE per-feature
  evidence horizon (L3 → 3, L5 → 5, distribution → actual n_games).
- Threshold / distribution / trend factors route to `form` category
  (no more `intangible n=1` demotion).
- Sentinel keys (`__factor_sample_sizes`, `__rung_p_hat`) stripped
  before pick storage.
- Contract tests: 4/4 emission + 12/12 reachability PASS.

### Preserved Contracts
- Correlation guard caps L5/L3/Historical ≤ 0.75; factor_mean ≤ 0.90.
- 93-99 corridor + 100 APEX architecturally reachable.
- Chalk-trap fail-closed on book-copy, spare on independent authority.
- NFL sample tier (5, 12); MLB unchanged (10, 30).

## Backlog (NOT this pass)
- Live slate rollover proof once frozen Burrow rows retire.
- Publish gated by explicit user consent (currently blocked).

## NFL ATD 10X — PART B CERTIFIED (2026-06-10)
- Multi-stat-block TD aggregation bug fixed (SUM instead of `max()`)
- xTD split-λ Challenger engine (v2) shipped and default
  (`nfl_atd_engine._predict_player_atd_v2`)
- Bayesian-shrunk per-touch conversion rates by position (empirical Bayes)
- Red-zone role proxy via `air_yards_share × wopr` (WR/TE)
- Role-stability variance penalty (≤ 15%)
- Feature-flag dispatch: `NFL_ATD_MODEL=v2` (default), `v1` (reversible)
- Champion-vs-Challenger backtest (2024 + 2025 REG, n=1759 total):
  * 2025 fair set: Brier 0.207 → **0.204** (-1.6%), ECE 0.064 → **0.045** (-29%)
  * 2024 fair set: Brier 0.228 → **0.224** (-2.0%), ECE 0.098 → **0.070** (-29%)
- Runtime proof: position-appropriate xTD split verified (Chase/Amon-Ra
  pure rec, McCaffrey dual-threat, Henry rush-dominant)
- Existing NFL-ATD tests: 35 PASS (mock upgraded for v2 path)
- Full evidence: `/app/memory/atd_part_b_certification.md`

## NFL STAR PLAYER + 93-99 ROOT CLOSURE — CERTIFIED (2026-06-10)
- Canonical name key (`_canonical_name_key`) folds sportsbook / ESPN / nflverse variants — A.J. Brown ↔ AJ Brown, Ja'Marr ↔ JaMarr, Smith-Njigba ↔ Smith Njigba all resolve to a single canonical GSIS id
- Player-id dedupe normalization ("espn_1234" ↔ 1234) fixes the Amon-Ra St. Brown-class ambiguity
- P0-D/E — `__rung_p_hat` (exact-threshold hit probability) now emitted in BOTH primary and alt-line factor paths; empirical Laplace-smoothed fallback when the CDF path returns None
- P0-E — sports_engine mp-blender changed 60·rung + 40·factor_mean → **85·rung + 15·factor_mean** so exact-threshold probability is primary
- P0-E — removed pre-scoring `factors.pop("__rung_p_hat")` that stripped the sidecar BEFORE the blender consumed it
- P0-F — deprecated edge≤0 → 97.9 alt-lines value floor in BOTH pick_refresh_orchestrator and lock_score_integrator
- P0-G/H — narrowly-scoped NFL player-prop Lock authority in `compute_lock_score`: `LS_max = 60 + wp·40`, evidence-multiplier 0.95-1.00, hard ceiling
- Star-player runtime trace: all 11 stars resolve to canonical id; 7 have live markets published, 4 legitimately have no markets (team not on slate / provider market missing)
- 93-96 bucket: 17 → **93** (+5.5×); 96-98 bucket: 3 → **13** (+4.3×); reachability curve `WP=95% → 98, WP=97.5% → 99` verified end-to-end
- All 16 P0 regression tests + reachability + alt-ladder + block2d stage A tests green
- Full evidence: `/app/memory/nfl_star_root_closure_certification.md`


## NFL ITER 138 — Read-Time Alt-Ladder Label Projection (2026-06-11)
Testing-agent certification of the 4 prior NFL closures uncovered
ONE wiring gap: historical NFL alt-lock rows frozen in `db.picks`
before the `_prop_market_label` fix still carried raw provider
strings (`Over 14.5 Player Reception Yds  · ALT LOCK`).  Per
PUBLICATION_CONTRACT §3 those rows are immutable.
- NEW `services/nfl_alt_label_projection.py` — READ-TIME rewrite
  of NFL alt-lock OVER labels to sportsbook-milestone form
  (`Rashee Rice 4+ Receptions`, `Patrick Mahomes 175+ Passing
  Yards`).  Settlement anchors (`line` / `threshold`) are NOT
  mutated.  Idempotent.
- Wired at TWO points in `routes/picks_routes.py::picks_today`:
  primary board + rescue-injection path (`canonical.extend(rescued)`).
- Live proof: 908 NFL picks · 801 milestone-form · **0 raw ALT LOCK
  labels remaining** in the `/api/picks/today?sport=NFL` response.
- 14/14 new projection tests pass; 43/43 combined NFL regression
  passes (1 xfail = trap-chalk cap intentionally removed).

## NFL ITER 139 — ATD Tab + Alt-Ladder Filter Pills + Star Watchlist (2026-06-11)
Continuous surgical NFL build.  Additive READ-path UX only.
- **ATD Tab** (`app/(tabs)/atd.tsx`): MLB HR-style toggle between
  🔥 Top 5 Today (whole-slate rank, no per-game quota) and 📋 By Game
  (grouped by canonical matchup).  Real ATD odds surfaced via new
  `ATD -168` chip.  Uses existing `/nfl/atd/leaderboard` +
  `/nfl/atd/by-game` — canonical publication rows, one player = one
  ATD score across views.
- **Alt-Ladder Filter Pills**: already-wired canonical NFL market
  chips (PASS YDS / RUSH YDS / REC YDS / RECEPTIONS + others)
  proven zero-cross-family-leakage at runtime.
- **NFL Star Watchlist**: new `stars_only=true` query param on
  `/api/picks/today` narrows NFL response to `elite_players`
  roster.  Applied on primary path AND rescue-injection path.
  Visibility-only — 359/359 picks compared before/after: zero
  scoring-field mutations.  Composes with market chips
  (STARS + PASS YDS → 75 star-QB rows).
- 49/49 targeted NFL regression tests pass (1 xfail correctly).
- Iter 138 non-regression holds: 908 NFL total · 801 milestone-form
  · 0 raw ` · ALT LOCK`.
- Full evidence: `/app/memory/nfl_iter139_atd_stars_filters_certification.md`

- Full evidence: `/app/memory/nfl_iter138_verification_certification.md`
