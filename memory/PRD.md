# PerkLocks — Product Requirements

## MISSION
PerkLocks is a wager-analysis platform that surfaces edge-driven sports betting
picks ("Locks") for MLB, NFL, CFB, Tennis, and Soccer, with honestly-declared
capability states for NBA / NHL / UFC (provider wired, model deferred).

## HARDENED CONTRACTS (PERKLOCKS-MAIN 35 — PERMANENT ROOT CLOSURE CERTIFIED)

- **Zero mock data.** Real-time provider truth end-to-end.
- **Immutable canonical wager identity.** Once published, `PublishedPickContract`
  freezes selection / line / side / odds / publication_state. Every consumer
  (Locks, Pick Breakdown, Rollover, Parlay, My Bets, History, Analytics, Lab)
  reads the same contract.
- **Format-aware Tennis alt-total pricing.** ATP Grand Slam singles = BO5;
  WTA + regular tour = BO3. Exact-threshold pricing from a single
  empirical CDF per event. False-99% at 39.5 / 41.5 / 42.5 permanently
  closed.
- **Settlement Hard Gate.** SettlementCapabilityRegistry.is_gradeable() is the
  shared chokepoint. Missing actuals / not-final / identity-failure /
  unsupported → UNRESOLVED with reason, NEVER LOSS / zero / VOID.
- **Universal Market Classification.** UniversalMarketContract.is_alternate()
  is the single authoritative alt-vs-standard classifier for NFL / NBA / MLB
  / Tennis props. No manual `_alternate` bypass.
- **Real Alt-Line Authority.** Canonical publication boundary rejects
  `model_line=True` and any synthetic sportsbook source. Alt Magic ranks
  only real observed lines.
- **Canonical Lab Identity.** Strategy Lab consumes `canonical_market_family`
  before string heuristics. MLB + Other identity defect closed.
- **Dynamic Tennis Discovery.** `_discover_tennis_from_catalog` surfaces every
  provider-supported `tennis_*` key without a code release.
- **Deterministic Auth Tests.** In-memory rate-limit buckets can be reset by
  tests; production throttle strength unchanged.
- **Capability Alignment.** UniversalMarketContract and SportCapabilityRegistry
  agree on NBA / NHL / UFC state (MODEL_UNAVAILABLE — honest).
- **Factual "Why This Pick" Only.** No fabricated stats.
- **Pick Breakdown 2.0.** Read-only view surface over the immutable contract.
- **Same-Snapshot Parity.** All consumers deterministically produce identical
  wager identity for the same pick.

## FINAL CERTIFICATION MATRIX
60 (sport, canonical_market_family) rows registered — 33 ACTIVE, 25
MODEL_UNAVAILABLE (honest — provider wired, model deferred), 2 RESEARCH_ONLY
(first-goalscorer & first-TD kept as distinct canonical entries per product
requirement).

Persisted at: `/app/memory/perklocks_main_35_certification_matrix.json`

## MAIN 40 · SURGICAL CLOSURE ROUND 1 (2026-06)
- NFL Totals: distribution centered on model `expected_total`;
  book line is the threshold. `P(Over)+P(Under)+P(Push)=1`.
- History publication no longer erased by mutable `off_board`/`no_bet`.
- Provider failures → `unresolved` (first-class settlement result);
  14-day terminator emits unresolved instead of fabricated VOID.
- Soccer Draw No Bet grading wired in FotMob + ESPN settlers.
- Alt-Line Magic real-line hard gate — chips carry `bettable=True`
  only when a real sportsbook quote hydrates the row.

## MAIN 40 · SURGICAL CLOSURE ROUND 2 (2026-06 · FanDuel evidence)
- CFB alt-line ingestion wired: `SPORT_CONFIG["cfb"]` → americanfootball_ncaaf.
- Added missing NFL alt families: reception_yds, pass_tds, receptions.
- Alt-Line Magic reads normalized `live_alt_lines` keyed by
  `provider_event_id`/`canonical_event_id`, never internal pick id.
- Frozen identity priority in `SettlementService.settle_from_pick`.
- Soccer regulation-scope rollback for AET/PEN.
- Multi-book best-price policy (no relabeling).
- Game-market ranking is edge-driven only (no extreme-prob reward).
- Model-only chips filtered BEFORE ranking, not after.


## MAIN 41 · P0-A EXPO PARITY SOURCE-DEFECTS (2026-06)
Two confirmed source defects fixed:
1. **Native API silent fallback removed** (`frontend/src/lib/api.ts`)
   — `if (__DEV__) return PINNED_PREVIEW_URL` deleted from
   `resolveBaseUrl()`.  Native must resolve via
   `EXPO_PUBLIC_BACKEND_URL` or fail visibly.  Added parity log
   `[api] Native backend origin resolved: ${envUrl}`.
2. **Locks cache-bust key wired** (`frontend/src/lib/cachebust.ts`)
   — `locks_picks_cache_v1` added to `KNOWN_CACHE_KEYS`.
   `APP_DATA_VERSION` bumped to `20260906-main41-locks-cache-bust-v1`
   so every existing Expo Go device wipes the orphaned Locks cache
   on next launch (auth + version keys are preserved).

## MAIN 41 · NFL PROP PUBLICATION CLOSURE (2026-06 · End-to-end proven)

**ROOT DEFECT**: `_PROPS_PER_KEY_CAP` in `sports_engine.py` had no
entry for `americanfootball_nfl`, so NFL fell back to
`_DEFAULT_PROPS_PER_KEY = 3`.  On a Sunday slate carrying 14-16
NFL games, only 3 events ever received prop-acquisition — the
other 13+ events were silently starved BEFORE the model.

**RUNTIME EVIDENCE (NE @ SEA, event `8c94552d022acec4a0458d70c19d3da9`)**:
- Odds API returned rich prop payload: 8 bookmakers, FanDuel 11
  markets / DraftKings 14 markets / Fanatics 14 markets.
- `_extract_nfl_prop_candidates` yielded 820 candidates.
- `build_nfl_game_context` precomputed 77 (player × market)
  factor sets; 40 passed `MIN_FACTORS_NFL_PROP=3`.
- `_props_picks_from_event` emitted **24 NFL prop candidates,
  16 in Lock band 95-99, 8 in 90-94**.  Zero synthetic lines,
  zero fabricated odds.

**SURGICAL FIX**:
## MAIN 40 · SURGICAL CLOSURE ROUND 4 (2026-06 · Authority parity)
- CFB Lock/Probability parity traced end-to-end via hydrate boundary.
- Analytics converted to canonical accessor (Brier + Kelly).
- Fusion promotion policy = deterministic (accessor refuses non-authority).
- Historical intelligence proven live on Travis Kelce (60/60 games).
- Soccer settlement branches all executable (8/8).


- `sports_engine._PROPS_PER_KEY_CAP["americanfootball_nfl"] = 16`
- Same commit also adds:
  - `americanfootball_ncaaf: 8`  (typical CFB Locks-eligible slate)
  - `basketball_nba: 12`         (nightly full slate)
  - `icehockey_nhl: 14`          (nightly full slate)
- CFB prop-acquisition also now included (was in the ingestion
  wiring from Round 2 but not the acquisition scheduler).

Regression protection: `tests/test_main41_nfl_prop_slate_cap.py`
guards the cap + the standard prop-market list so the full FanDuel
family (passing/rushing/receiving standard + alternates + anytime
TD + first TD) remains reachable.

Total MAIN 40/41 coverage: **75/75 targeted pytest cases pass.**


CFB / Consumer parity closure with live-runtime evidence.

- **CFB parity traced end-to-end.** For the observed Notre Dame @
  Wisconsin CFB Total pick (id `7856b13c-4767-5807-9393-f8b6b2d8c980`):
    - Raw doc:  `win_probability=98.2`, `edge_percent=48.2`, `lock_score=98.0`
    - Canonical: `published_probability=0.7629`, `published_edge=26.29`, `published_lock_score=98.0`
    - Hydrated (what every consumer sees via `_canonicalize_lock_score → hydrate`):
      `win_probability=76.29`, `edge_percent=26.29`, `lock_score=98.0`.
    - **Parity is correct at the read boundary.**  The stale raw
      values on the DB doc are never displayed on the wire; they
      exist only as legacy artifacts of the shrinkage pipeline.
- **Analytics consumers converted to canonical accessor.**
  `analytics_routes.py` Brier + Kelly paths now consume
  `canonical_final_probability(pick)` and pull
  `published_probability` / `model_probability` on the read.
  Prevents raw pre-shrinkage 98.2 from inflating Brier vs the
## MAIN 40 · SURGICAL CLOSURE ROUND 3 (2026-06 · Live-runtime verified)
- Live CFB alt-line hydration: SMU/FSU has 206 alternate-total rows.
- NHL wired into universal player-history dispatcher.
- Dark contract fields populated (atomic_games, h2h_source_games,
  streak, days_since_last_game, vs_opponent_recent).
- Canonical Final Probability Authority helper created.
- Soccer settlement executability proven (8/8).


  actual 76.29 the user saw.
- **Fusion promotion authority is deterministic.** The canonical
  accessor refuses `fusion_probability` / `sim_probability` /
  `implied_probability` as authorities.  Fusion is a candidate-time
  enrichment layer only; once `published_probability` is stamped,
  every downstream consumer reads the frozen value.
- **Historical intelligence proven live.** Travis Kelce
  (`cpi=00-0030506`) via `get_player_history(NFL, ...)`:
    - `source=NORMALIZED`, `quality=HIGH`, `games=60/60`
    - `streak=HIT x 1`, `days_since_last_game=122`
    - `atomic_games` = 20 dated rows with opponent + home/away
    - `h2h_source_games (vs LAC)` = 5 rows
    - `season` aggregate: 19 games, hit_rate 47.4%, quantiles, variance
- **Soccer branches all executable.** BTTS + Double Chance +
  Win-or-Draw + Draw No Bet = 8/8 targeted grading cases pass.

Tests (this round): `test_main40_probability_consumer_parity.py`,
`test_main40_analytics_canonical_probability.py`.

Total MAIN 40 coverage: **71/71 targeted pytest cases pass.**


Live SMU/FSU acceptance path proven end-to-end:
- Forced refresh: 11,349 alt-line rows, 56 sports, 0 errors.
- CFB event `a570687d47661da4982cfd885cdb4a44` (SMU @ FSU) → 206
  alternate_totals rows across 61 thresholds (23.5→83.5); base 52.5
  present with real FanDuel Over -115 / Under -111.
- Magic bundle now surfaces 8 bettable chips, each with real book,
  real American price, real edge; DK best price on 45.5 Over -254
  wins over FanDuel via the new multi-book policy.

Round 3 patches:
- **NHL wired into universal player-history dispatcher.** Was
  advertising SPORT_NOT_SUPPORTED despite `player_history/nhl.py`
  adapter existing. Now routes through the same contract.
- **Dark contract fields populated (universal + MLB).** `atomic_games`,
  `h2h_source_games`, `streak`, `days_since_last_game`,
  `vs_opponent_recent` — all derived from existing loaded rows, no
  new provider calls. Populated in `_shared.populate_standard_evidence`
  (covers NFL/NBA/NHL/Soccer/Tennis/UFC) AND in the MLB-specific
  populator.
- **Canonical Final Probability Authority.** New helper
  `services/canonical_probability.py` provides
  `canonical_final_probability(pick)` — priority
  `model_probability → published_probability → win_probability`.
  Percentage inputs auto-normalise. `sim/implied/fusion` explicitly
  refused. Single source of truth for Locks / Pick Breakdown /
  Rollover / Parlay / Analytics.
- **Soccer settlement executability proven.** BTTS, Double Chance,
  Win-or-Draw, Draw No Bet — all branches grade correctly on real
  score inputs (8/8 targeted checks pass).

Tests (this round): `test_main40_history_dark_fields.py`,
`test_main40_canonical_probability_authority.py`,
`test_main40_soccer_settlement_executable.py`.

Total MAIN 40 coverage: **63/63 targeted pytest cases pass.**

Direct FanDuel runtime evidence for SMU vs FSU CFB Alt Total ladder
prompted a wiring closure pass — the "no alternate lines available"
symptom was NOT a legitimately empty market; it was a
retrieval/normalization defect.

- **CFB wired into alt-line ingestion.** `alt_lines_feed.SPORT_CONFIG`
  now includes `cfb → americanfootball_ncaaf` with alternate_totals +
  alternate_spreads + NFL-parallel player-alternate families.
- **NFL alt families expanded.** Added `player_reception_yds_alternate`,
  `player_pass_tds_alternate`, `player_receptions_alternate` (screenshot-
  proven markets that were silently missing).
- **Read from normalized store, not raw cache regex.** `_fetch_game_market_alt_lines`
  (game markets) and the player-market path now query `live_alt_lines`
  keyed by `provider_event_id` / `canonical_event_id`. Full observed
  ladder survives with sportsbook + observed_at provenance.
- **Internal pick id ≠ provider event id.** Explicit refusal —
  `provider_event_id == pick["id"]` returns empty rather than
  matching everything.
- **Frozen identity priority in settlement.** `SettlementService.settle_from_pick`
  now prefers `canonical_event_id → provider_event_id →
  fanduel_event_id → event_id → event` (display string last).
- **Soccer regulation-scope.** ESPN & FotMob settlers roll AET/PEN
  scores back to regulation via linescores / `scoreAtHalfTimePlusFT`.
  If regulation-time score unavailable → `None` → UNRESOLVED.
- **Multi-book price policy.** No more iteration-order "last book
  wins". For each (line, side) the engine now keeps ALL quotes and
  selects the best bettor-facing American price. Provenance
  (bookmaker) is always the winning quote's own book — never
  relabelled.
- **Game-market ranking: edge-driven only.** Removed the
  `base_edge = max(p-0.5, 0.5-p)` component that rewarded extreme
  probabilities regardless of value. Composite is now edge_boost +
  small market-presence bonus. Model-only chips filtered BEFORE
  ranking so they can never steal a top-N slot from real book chips.

Tests: `/app/backend/tests/test_main40_*.py` — 33 passing.

## MAIN 40 · BLOCKED (unchanged this round)
- Lab text-node crash — no runtime source-mapped stack available in
  this container; blind edits refused per user directive.
- CFB Lock-score/probability parity — no reproducible locator; skipped
  rather than opened a broad audit.
- Predictive P0 & Historical Intelligence Completion — architectural
  scope; would require refactoring per user's own "DO NOT REBUILD"
  rule. Deferred to a scoped follow-up with a specific defect locator.

Continuous surgical patch pass — smallest-safe-change per item, no
audits/redesigns.  Each closure is guarded by a targeted test in
`/app/backend/tests/test_main40_*.py`.

- **NFL Totals — Independent scoring distribution.** `sample_game_script`
  now centers on the model's `expected_total`, not the sportsbook line.
  Book line remains the O/U THRESHOLD. `P(Over)+P(Under)+P(Push)=1`.
- **History / Settlement Truth.** `PublishedResultsTruthService` no longer
  filters historical eligibility by mutable `off_board` / `no_bet` — a
  legitimately-published pick that later flips off-board still surfaces
  in History (spec §5). Publication evidence check moved BEFORE mutable
  exclusion flags.
- **Provider-missing → UNRESOLVED, not VOID.** `SettlementService`
  accepts `unresolved` as a first-class result; `settlement_engine`'s
  14-day terminator emits `unresolved` instead of fabricating VOID.
- **Soccer Draw No Bet.** Both FotMob and ESPN settlers now grade DNB
  (team wins → won, team loses → lost, DRAW → PUSH). Allow-list was
  already advertising DNB; grader was missing.
- **Alt-Line Magic Real-Line Hard Gate.** `AltLine.bettable = (source ==
  "market")`. Both game-market and player-market bundles filter out
  model-only chips — bettable chips must be backed by a real
  sportsbook quote. Empty bundle → client's existing empty state.
- **Lab crash (Text-node) BLOCKED.** Cannot repro runtime stack in
  this container per user directive; item left blocked without
  blind edits (would create Cabinets more risk than value).
