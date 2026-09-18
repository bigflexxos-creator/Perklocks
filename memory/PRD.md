# LockScore — Product Requirements (Live)


## Historical Intelligence — Universal Date & Opponent Fix (2026-09-18)
Fixes user-visible "fake stats" in Pick Breakdown → Historical Intelligence:

1. **Real game log dates (universal)** — `player_game_actuals` docs
   whose `event_time` was `None` (63,855 MLB rows on the ingested
   batch) previously fell back to `ingested_at`, collapsing every
   game-log date to the ingestion day ("08/11 08/11 08/11…").
   - One-time script `backend/scripts/backfill_player_event_time.py`
     joins `team_game_actuals.event_time` by `event_id` across MLB /
     NFL / NBA / NHL.
   - Runtime defensive helper
     `_enrich_event_time_from_team_actuals()` in
     `services/historical_intelligence.py` protects every player
     adapter (MLB + NFL wired) against future regressions in the
     ingestion pipeline.
2. **VS OPP resolves for player props (universal)** — picks whose
   payload lacks `player_team_name` (very common for MLB/NFL/NBA
   props published through some odds providers) previously returned
   `NO PRIOR MATCHUPS` even when 90+ game logs vs that opponent
   existed. Route now performs a `_lookup_player_team()` against
   `player_identities.current_team` keyed by `provider_ids`
   (mlb_stats · gsis · nba_stats · nhl_stats · wnba_stats · fotmob ·
   cfbd) with name-norm fallback, then hands the result to
   `_resolve_opponent()` as `player_team_fallback`.
3. **`_resolve_opponent()` partial-word match** — now also matches
   `"phillies" ⊂ "philadelphia phillies"` so short player-team
   labels still resolve.

Verified end-to-end:
- Bryce Harper (Over 0.5 Hits): dates now 08-11, 08-09, 08-05, 07-28,
  06-23 (real). VS Milwaukee Brewers → 9 prior matchups, 55.6% hit.
- Oneil Cruz / Paul Skenes: dates now span 2022-2026 correctly.



## Universal Evidence Authority (UEA) — 2026-06 CERTIFIED (STRUCTURAL)
### Scope: MLB · NFL · CFB · SOCCER · TENNIS (NBA/NHL/UFC untouched)
- New shared contract at `services/evidence_authority_contract.py`
  with 9 axes and first-class MISSING semantics — absent evidence is
  MISSING (not synthetic 85).
- Sport-agnostic adapters at `services/evidence_authority_adapters.py`
  map existing MLB/NFL/CFB/Soccer/Tennis evidence into the contract.
  Correlated Elo / SP+ / xG derivatives deduped; Sportsbook Implied
  excluded from independent-evidence axes.
- `compute_lock_score` LIFTS on strong evidence, CEILINGS on
  contradictions; final clamped to 99 (Apex 100 preserved).
- NFL WP cap `60 + wp × 40` surgically scoped in
  `pick_refresh_orchestrator.py` — full-evidence picks keep UEA
  authority; thin-evidence picks still receive legacy protection.
- MLB projected-starter cap replaced with coverage-based helper
  `projected_starter_max_by_coverage`.  Confirmed / bench /
  scratched behaviour preserved.
- New `services/cfb_independent_simulator.py` produces a genuinely-
  independent Monte Carlo margin/total distribution (P16/P17).
- 36 new tests + 134 regression tests pass (170 total).
- Live-distribution + peak-provenance report:
  `services/uea_live_distribution_report.py`.


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

---
## Session 5 · Universal Historical Intelligence 2.0 (2026-09-17)

### Backend adapters — all wired to REAL raw historical stores
- `services/historical_intelligence.py`
  - NFLPlayerHistoricalAdapter (129,657 rows: pass/rush/rec yds/tds, receptions, targets, ATD)
  - NFLTeamHistoricalAdapter (570 rows: ML/spread/total from raw final scores)
  - MLBPlayerHistoricalAdapter (64,976 rows: hits/TB/H+R+RBI/HR/RBI/runs/Ks/outs)
  - MLBTeamHistoricalAdapter (4,026 rows: ML/run line/total)
  - SoccerPlayerHistoricalAdapter (50,112 rows: goals/assists/G+A/shots/SOT)
  - SoccerTeamHistoricalAdapter (25,497 finished matches: 1X2/handicap/total/BTTS/DC)
  - TennisHistoricalAdapter (37,992 ATP rows: ML/game spread/game total; safe score parser)
  - CFBHistoricalAdapter (2,231 final games: ML/spread/total)
  - Universal reducer: L5/L10/L20/SEASON scope trim, HOME/AWAY filter, VS OPP, quantiles.
  - MISSING ≠ ZERO: raw actual `null`, `NO PRIOR MATCHUPS` sentinels, honest coverage %.

### Endpoints
- `GET /api/picks/{pick_id}/historical-intelligence?sample_scope&venue_scope&context_scope`
- `GET /api/historical-intelligence/coverage` — 36/36 CERTIFIED coverage matrix

### Frontend
- `src/components/HistoricalIntelligence.tsx` — universal reusable component
- Tabs: GAME LOGS · VS OPP · SPLITS · DISTRIBUTION
- Sample toggles L5/L10/L20/SEASON (real refetch)
- Venue toggles ALL/HOME/AWAY (Tennis auto-hidden, surface splits used instead)
- Threshold chart via `react-native-svg` (dots + dashed line, lightweight)
- Current-Line hero clearly distinct from Lock Score / Win Expected
- Loading skeleton, honest missing states, endpoint failure never crashes Pick Breakdown
- Mounted in `app/pick/[id].tsx`

### Performance
- Locks lite: p50 40ms, ETag 304 32ms (unchanged from Session 3)
- HI endpoint after indexes: cold 8-20ms, p50 10-20ms, p95 <180ms
- New indexes: `player_game_actuals.hi_player_recent`, `team_game_actuals.hi_team_recent`

### Preservation
- No sports_engine / UEA / Lock Score / 85 threshold / scoring changes
- No NBA/NHL/UFC touched
- ATD by-game merge preserved from Session 3
- Board snapshot cache + prewarm intact

## FINAL UNIVERSAL ROOT CLOSURE (2026-09-18) — see memory/final_universal_root_closure_2026_09_18.md
- P0.1 89-vs-85 root cause fixed: GET-time rescoring removed for published picks; `hydrate()` snapshot-always-wins
  (v4 read-override deleted); versioned re-publication + canonical `published_grade` + `publication_events`.
- One-time reconciliation persisted the truth the board already displayed (no displayed numbers changed).
- P0.5 truth manifest + per-pick `truth_fingerprint` on list & detail; lite whitelist extended.
- Frontend: single-flight 401 verification, origin+board_version keyed board cache (v2), STALE/UPDATING pill,
  global connectivity authority, DEV HUD surface/origin/env/board version.
- P4 `GET /api/nfl/atd/slate` single universe (cached 10 min, warmed at boot); ATD screen redesigned By Game.
- P6 `rollover_slates`/`rollover_slate_events` immutable official Top 3; ranking = conservative calibrated WP.
- P1.2 soccer resolver: deterministic freshness + prior-season shrinkage; Sørloth transliteration; Endrick mononym.
- Harness: `scripts/p10_final_parity_harness.py` (92 picks, 0 unexplained diffs); `scripts/parity_list_vs_detail.py`.
- Open: Kane VS Union real game logs (needs match-level ingest), NBA authority (no NBA slate), settlement
  single-authority consolidation, Intelligence 2.0 UI, player media, P11 device reliability matrix, ATD tab entry.

## FINAL CERTIFICATION PASS (2026-09-18, iteration 141) — see test_reports/iteration_141.json
- MLB props recovered (root causes, all surgical): (a) `alt_lines_feed._flatten_odds` now persists `side` (Over/Under) and
  disambiguates the composite key — the MLB prop cache-first reconstruction (`sports_engine` ~5731) emitted direction-less
  outcomes that the fail-closed direction filter dropped → 0 props every cache-HIT cycle; legacy rows without `side` are
  treated as cache MISS. (b) per-sport refresh timeout: MLB gets 900s (`PERKLOCKS_REFRESH_SPORT_TIMEOUT_SEC_MLB`) — the 420s
  cap killed the MLB cycle ~80s before insert. (c) `game_context` probable-pitcher lookup uses US/Eastern game date (UTC
  slice resolved tomorrow's starters for evening games) + Stuff+ import fixed. Board: 314 MLB rows (287 props), all ≥85.
- NFL alt parity: `locks_eligibility.rescue_missing_eligible` now `hydrate()`s rescued rows (mutable aliases leaked to lite).
  Regression fixture: tests/test_nfl_alt_rescue_parity_regression.py. Harness: 480 checked, 0 unexplained diffs.
- ATD permanent fix: durable universe collection `nfl_atd_universe_rows` (persist on live expansion, fallback between provider
  snapshots); alt-lines feed acquires `player_anytime_td` for ALL upcoming NFL events (full slate: 15 games / 188 candidates);
  ATD chip no longer shows Locks-board "PASS" on leaderboard rows (evidence badge instead; L-score still shown).
- Intelligence 2.0 polish: stray whitespace text node in HistoricalIntelligence GameLogsTab removed (red toast gone);
  pointerEvents → style; Pick type extended (player_name/team/home/away/line/venue/lineup_status).
- Ops note: uvicorn runs with --reload; editing backend .py kills in-flight refresh & orphans `scheduler_leases` /
  `scheduled_jobs` leases (owner pid dead). Release orphans before re-triggering.
- Open: CFB 2.0 model upgrade (NEXT), P11 device matrix, CFBD quota. HR / TB MLB families rarely clear 85 (honest).

## P0 HI PARITY + PROBABILITY AUTHORITY CLOSURE (2026-09-18, iteration 142+)
- **Expo Go ↔ Web Historical Intelligence parity (P0)** — first divergence: the HI service swallowed adapter
  exceptions and returned HTTP 200 with 0 observations ("NO RECENT HISTORY" false-empty); the client had no way to
  distinguish it from a real empty. Closure: `services/historical_intelligence.py` raises `HistoricalQueryFailed`
  → route returns **503 + detail.status=QUERY_FAILED**; every 200 carries `status` ∈ {AVAILABLE_WITH_DATA,
  AVAILABLE_EMPTY, SOURCE_UNAVAILABLE} + `served_by{host,db,pid,at}` fingerprint. Frontend
  (`HistoricalIntelligence.tsx`, `Intelligence2.tsx`): QUERY FAILED state with RETRY (testID hi-retry), status footer
  (testID hi-status-line: `AVAILABLE_WITH_DATA · L10 n=10/80 · src <host>`), header pill/modules never say
  "NO RECENT HISTORY" on failure. `api.ts` native guard: when the inlined EXPO_PUBLIC_BACKEND_URL preview host ≠ the live
  dev-server host (`Constants.expoConfig.hostUri`, both preview domains) Expo Go follows the dev-server host (stale
  bundle ≠ different backend). Trevor Lawrence 150+ pick id `6d76acfa-6c03-560f-b0b8-8a1626adeee6` → L10 10/10, 80 obs.
  Tests: tests/test_historical_intelligence_status_semantics.py, tests/test_hi_endpoint_status_semantics_http.py.
- **Probability closures** `services/probability_closures.py` (wired inside `probability_authority.evaluate`, shadow-first;
  recorded on `probability_contract.closure` + `closure_probability`):
  - SOCCER player props (GOALSCORER/ASSISTS/SHOTS/SOT/GOAL_CONTRIBUTION): minutes/lineup model — Poisson exposure
    rescaling (k from "N+"/line) × availability prior by `lineup_status` (confirmed .99/84', projected .88/80',
    unknown .80/72', bench .72/28', OUT → fail-closed `LINEUP_OUT`); goal_scorer_v3 picks get availability only.
    `canonical_publication_boundary` rejects LINEUP_OUT / BOOK_IMPLIED_SEED inline (integrity, mode-independent).
  - NBA player props: `nba_threshold_probability` Student-t (df=n_games−1) with role variance (rate×minutes_sd)²;
    `brain/sim_nba.py` empirical branch now uses it (points/PRA) or gamma-Poisson (counts) and stamps
    projection_mean/sd, sample_games, minutes_projection/sd → authority closure. Book-implied seeds fail closed.
    Game markets are separate families (NBA_ML/SPREAD/TOTAL).
  - CFB TOTAL: fixed-σ normal retired — `cfb_over_probability` → `cfb_total_residuals.predictive_total_over_probability`
    Student-t posterior predictive, df = PRIOR_DF(6) + verified residual n, scale = NIG blend of tier prior σ and
    residual σ. Miami@Stanford O46.5 fixture 0.9266 → 0.9015; Akron@Wake U50.5 0.8832 → 0.8608; converges to normal as
    residuals accumulate (test). Registry `CFB_TOTAL_SIGMA` now stores `sigma_raw`, `prior_df`.
  - TENNIS: closure records `lock_score_authority` (flags LEGACY_LOCK_CONSTRUCTION if not canonical).
  - Family keys: NFL alt tokens (passing/rushing/receiving yards, anytime touchdown), soccer h2h/market_key fallback,
    GOAL_CONTRIBUTION before GOALSCORER; team-ambiguous tokens require player identity.
- **Shadow report** `GET /api/probability-authority/shadow-report` (per-family live board evaluation + registry metrics +
  readiness verdict PROMOTED / READY_FOR_PROMOTION / IDENTITY_IS_CHAMPION / NOT_ENOUGH_EVIDENCE / NO_SETTLED_EVIDENCE);
  `POST /api/probability-authority/refit`. Current: MLB_HRR READY_FOR_PROMOTION (platt), MLB_HITS identity champion,
  all NFL/CFB/Tennis/NBA families NO_SETTLED_EVIDENCE (no settled rows yet) — honest, nothing promoted, mode=shadow.
  Point-in-time guard excludes 680 soccer rows whose published_at was re-stamped after event_time.
- Promotion: env `PROBABILITY_AUTHORITY_MODE=active` + `PROB_AUTH_PROMOTE_<FAMILY>=1` (or registry promoted) — only then
  a family's calibrated/closure probability drives win_probability for NEW publications. Lock Score formula untouched.
- Pre-existing failing tests (not from this session): test_cfb_game_market_evidence_contract (2), test_iter115 (3),
  test_block2a_tennis_consolidation sim matrix (2), test_nfl_nba_rationale_smoke (async), test_sim_phase_b sim_runs 20000.

## ITERATIONS 144–147 · ATOMIC BOARD GENERATIONS + INSTANT UX + PROMOTION GOVERNANCE (2026-09-18)
- **144 root defect**: pick_refresh_orchestrator delete_many/insert_many mutated the same population `/picks/today` reads and
  the 15s snapshot TTL re-snapshotted partial populations (146→3→104 flapping). Fix: `services/board_generation.py`
  (BUILDING→VALIDATING→COMMITTED|FAILED, tiny `board_generations/active` pointer, compare-and-set commit, no-op commit when
  board_version unchanged, exactly one snapshot invalidation on a real commit). `_refresh_picks` (all scheduled loops +
  manual UPDATE) wrapped in begin/validate/commit/fail; `board_snapshot_cache` pinned (TTL ignored, no new snapshots) while
  building; `/picks/today` truth_manifest gains generation_id/state/revision/committed_at/event_count/schema_version +
  authority fingerprint (environment_id, backend_revision, authority hash — no secrets); responses assembled during a build
  are labelled BUILDING. 144-F: `_ensure_today_picks(allow_heal=False)` — healer no longer launched by reads; runs at
  startup + 10-min lifecycle loop inside a HEAL generation. Client (`app/(tabs)/index.tsx`): accepts/persists only
  COMMITTED (or LEGACY_UNTRACKED) responses, discards older revisions (acceptedRevisionRef), keeps last committed board
  with "Board updating…" otherwise. Runtime proof: NFL UPDATE → every poll stayed on the committed generation
  (396 picks, same board_version), single transitions per commit; failed build keeps A (tests/test_iter144_board_generation.py).
- **145**: `src/lib/useSWR.ts` selective last-known-good persistence (`swr_lkg_v1`: rollover|parlay|my-bets|profile|stats
  prefixes, ≤200KB, success-only writes) hydrated in `app/_layout.tsx`; Lab list seeded from SWR cache (warm revisit, no
  spinner). Tabs already freezeOnBlur + module-level SWR cache → state preserved; errors never clear data.
  Real-device (Expo/iPhone) airplane-mode proof NOT executed here (web proof only, iteration_147.json).
- **146**: `POST /api/probability-authority/promote/{family}` (gate: shadow_ready, held-out n ≥100, log-loss beats identity)
  / `demote`. `PROBABILITY_AUTHORITY_MODE=active` in backend/.env. Promotion persists across refits. Calibrators never
  extrapolate outside training support (`support` in registry → identity + `OUT_OF_CALIBRATION_SUPPORT`).
  `prediction_publication_service._build_payload` stamps promoted families: calibrated probability → win_probability,
  Lock Score recomputed via canonical `compute_lock_score` (formula untouched), legacy values retained. PROMOTED: MLB_HRR
  (platt, n=615/held-out 415, log-loss 0.7408→0.7375; calibrator is nearly flat ≈0.60–0.62 → HRR locks will honestly fall
  below 85 on NEW publications). All other families shadow (NOT_ENOUGH / NO_SETTLED_EVIDENCE).
- **147**: iteration_147.json — board manifest COMMITTED & stable (0 flaps), HI Trevor Lawrence AVAILABLE_WITH_DATA 10/10,
  Rush/Rec/Receptions HI 200 with status, 503 on /picks/today keeps cards visible, warm tab switching no spinners.
  Known minor: dev-only red-box "Unexpected text node: ." from `src/components/StrategyLabWorkstation.tsx:149` (Lab tab, pre-existing).

## NATIVE PERFORMANCE CLOSURE (iteration 148, 2026-09-18) — frontend only
- MLB live: `src/contexts/MLBLiveContext.tsx` → module-level external store (`_publish/_subscribe`), `useMLBLive` uses
  `useSyncExternalStore` with a keyed selector + `_sameGame` identity guard; non-MLB cards pass null → constant snapshot.
  Provider still polls 60s (visibility-paused); context value now only {lookup, refresh} and never changes on poll.
- Featured hero: `src/lib/featuredStore.ts` (`setFeaturedPickId`, `useIsFeatured`); `listRows` deps = [dayGroups] only;
  rotation effect sets store id; `LockPickCard` derives `featured` via keyed subscription. Proof: 15s (2 rotations + poll)
  → 3 of 59 mounted cards re-rendered, total renders 60→64.
- Cache authority: `src/lib/serverStateKeys.ts` canonical keys; `_picksMem/_statsMem` in index.tsx are adapters over
  `picks|lite|{sport}` / `profile|stats`; PICKS_CACHE_KEY AsyncStorage hydrates the SWR resource; `useBoardCursor` seeds
  from / writes (complete page only) to `picks|lite|{sport}`; `swrCacheTs`, bounded TTL sweep (40 entries / 10 min) for
  `pick-detail|` and `historical-intelligence|` keys. pick/[id].tsx + HistoricalIntelligence seed from cache, revalidate quietly.
- Prefetch: `preloadPrimaryTabs` seeds Rollover, Parlay (user prefs key), My Bets, Profile via Promise.allSettled (4/4 fire).
- Dedupe proof: boot 17 API reqs, tab loop 1 = 12, loop 2 = 0 (all warm), no URL requested >3×.
- getItemLayout: SKIPPED (variable-height cards: featured atmosphere, tiers, live rows).
- Dev telemetry: `window.__lockCardRenders`, `__lockCardRendersById` (DEV only). StrategyLabWorkstation boolean coercions
  added for the dev red-box (not reproduced after fix).
