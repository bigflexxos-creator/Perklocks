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

## MAIN 40 · SURGICAL CLOSURE (2026-06)
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
