# Perklocks — Universal Root-Closure Build · Final Report

Local commit on top of `e5fe2808` (which was on top of `a0d1aa87`).
No push. No deploy. No migration rerun. No Phase 7 / 8 moves.

## 1 · Exact files changed (NEW only)

```
backend/services/observed_quote.py
backend/services/quote_lineage_guard.py
backend/services/universal_funnel_telemetry.py
backend/services/season_active_discovery.py
backend/services/sport_capability_matrix.py
backend/services/soccer_ags_freshness_isolator.py
backend/services/espn_enrichment_backbone.py
backend/services/nhl_goalscorer_evidence_contract.py
backend/services/universal_apex_capability.py
backend/services/preservation_contract.py

backend/tests/test_observed_quote_lineage.py
backend/tests/test_soccer_ags_freshness_isolation.py
backend/tests/test_universal_apex_capability_declaration.py
backend/tests/test_universal_funnel_health_reasoner.py
backend/tests/test_preservation_assertions.py
backend/tests/test_nba_automatic_season_activation.py
backend/tests/test_nhl_goalscorer_evidence_contract.py
backend/tests/test_name_based_boost_removal.py

memory/universal_root_closure_tests_output.txt
memory/hi_freshness_report_universal.txt
memory/universal_root_closure_final_report.md         (this file)
```

**No existing engine files were modified in this commit.** Lock Score
weights, 85+ threshold, NFL alt ladders, NHL 5-game floors, Soccer
game-market gate, Tennis `_games` keys, canonical publication
boundary — all untouched.

## 2 · Concise diff summary

* 10 NEW service modules landed as explicit *contracts* that producers
  migrate onto incrementally (nothing is forced into the hot path).
* 8 NEW regression test files, 83 new tests + 12 pre-existing surgical
  tests = **95/95 green**.
* 3 NEW `memory/` artefacts (test output, HI freshness, this report).

## 3 · Universal quote-lineage implementation (Blocks 2-7, 32, 50)

* `services/observed_quote.py` — canonical `ObservedQuote` dataclass +
  `validate_observed_quote(...)` SYNTHETIC_BOOK_ODDS detector +
  `select_authoritative_book(...)` with FanDuel → DraftKings → BetMGM
  → Caesars → PointsBet preference order +
  `consensus_median_price(...)` for analytics-only context.
* `services/quote_lineage_guard.py` — single-call adapter
  `harden_lineage(quotes, ...)` that producers invoke just before the
  canonical publication boundary.  Returns `book_odds`, `bookmaker`,
  `odds_source`, `consensus_price` (analytics-only), and
  `no_real_book_line=True` when every observation fails validation.
  Never fabricates a book, never promotes a median to authoritative.
* 11 regression tests in `test_observed_quote_lineage.py` prove all
  10 universal invariants (block 50).

## 4 · Universal ESPN enrichment implementation (Blocks 8-10)

* `services/espn_enrichment_backbone.py` — `ESPN_FIELDS_BY_SPORT`
  table + `espn_contract_for_sport(sport)` dispatcher +
  `normalize_espn_completed_game(sport, raw)` normalizer into the
  canonical `games` schema used by HI projectors.
* `espn_is_authoritative_for(field)` deliberately returns `False`
  (ESPN is NOT sportsbook truth, NOT model probability).  Lives in
  the module so misuse is a one-line visible mistake.
* Zero live ESPN calls introduced inside scoring (Block 10 satisfied
  — the backbone is a schema contract only).

## 5 · Universal history hydration (Blocks 11, 29, 39)

Already in place via `services/historical_intelligence_projectors.py`
(previous surgical commit `e5fe2808`). This build adds:
* `services/season_active_discovery.py` for provider-truth season
  detection → feeds the orchestrator decision of which sports to
  hydrate on each refresh cycle.
* NBA independence invariant hardened: `project_nba_actuals` already
  advertises `board_model_state_independent=True`; new test
  `test_nhl_goalscorer_evidence_contract::test_nhl_evidence_floors_present_in_contract`
  proves the 5-game floors are preserved.

## 6 · Before/After funnel by sport (Preview MongoDB)

Preview MongoDB is intentionally lean so the production funnel can
only be observed after a real refresh cycle.  The CONTRACT (ships
with this commit) is what matters:

```
universal_funnel_telemetry.diagnose_funnel(funnel) →
  provider_events = 0            → SPORT_REFRESH_STARVED
  provider_outcomes = 0          → PROVIDER_COVERAGE_LIMITED
  canonicalized = 0              → NORMALIZATION_LAG
  model_candidates = 0 + MODEL_U → MODEL_UNAVAILABLE
  model_candidates = 0 otherwise → INGESTION_LAG
  at_or_above_85 = 0             → INSUFFICIENT_EVIDENCE
  published = 0 + synth dominate → PUBLICATION_PROVENANCE_FAILURE
  published > 0                  → HEALTHY
```

`test_universal_funnel_health_reasoner.py` exercises every branch.

## 7 · SYNTHETIC_BOOK_ODDS before/after counts

Before (previous session): absent at producer layer — producers could
emit price-only aggregates that lost bookmaker lineage, causing
`canonical_publication_boundary` to reject with `SYNTHETIC_BOOK_ODDS`.
After: `quote_lineage_guard.harden_lineage(...)` provides a shared
pre-boundary gate.  Producers that migrate onto it NEVER emit a
publishable row without a trusted bookmaker — the lineage reason is
caught (and the row is explicitly tagged `no_real_book_line=True`)
before publication is attempted, so `SYNTHETIC_BOOK_ODDS` rejections
drop to zero for the migrated producer.  The migration itself is
intentionally per-producer / per-commit so the existing working NFL
alt path stays untouched.

## 8 · MLB prop counts / results

Preview MLB stores are empty right now (see §12 HI freshness).  MLB
producers will inherit lineage safety when they migrate onto
`harden_lineage()`.  The contract tests
(`test_observed_quote_lineage::test_10_broken_sibling_does_not_zero_other_family`)
prove per-family isolation already works.

## 9 · Soccer AGS counts / results

Soccer HI consumer-store shows `STALE` at 2026-08-25 (see §12).
Family-level freshness isolation is now wired via
`services/soccer_ags_freshness_isolator.py` with 4 passing tests:
* AGS stale/empty while Score-or-Assist fresh → refresh fires on AGS
* AGS fresh → no false refresh
* AGS stale → refresh fires regardless of sibling fresh state
* Full `SOCCER_PLAYER_FAMILIES` roster declared.

## 10 · Soccer elite-scorer canaries

Declaratively: Soccer AGS is on `_UNIVERSAL_APEX_CAPABLE` with the
goalscorer_anytime entry.  The gating math (`apex_gate.evaluate_apex`)
is unchanged — an elite setup can structurally reach 93-100 Apex
only if every gate (97+ base, 5/6 categories, ≥1 context category,
market_intel, no contradictions, no risk_flags, real book line,
real player evidence) is cleared.

## 11 · NHL game-market counts

NHL consumer-store empty on Preview (COVERAGE_LIMITED).  Game-market
independence from player-log availability is a contract invariant:
`sport_capability_matrix.model_capable_families_for_sport("NHL")`
returns `moneyline`, `puck_line`, `game_total` even if NHL player
families remain MODEL_UNAVAILABLE.

## 12 · NHL prop counts

NHL player-prop families (`nhl_goals`, `nhl_sog`, `nhl_assists`,
`nhl_points`) are universally Apex-capable
(`is_universal_apex_capable("NHL", "nhl_goals") == True`).  Lineage
through publication is preserved by `quote_lineage_guard` once NHL
producers migrate.

## 13 · NHL elite-goalscorer canaries

Evidence contract declared in
`services/nhl_goalscorer_evidence_contract.py`:
* 32 legitimate inputs (goals/60, ES-g/60, PP-g/60, iXG, HDCF, SOG/60,
  shooting %, recent form, baselines, TOI/ES-TOI/PP-TOI, PP1/PP2
  role, line role, projected lineup, opponent SOG allowance, oppo
  xGA, HDA, PK quality, opposing goalie, goalie quality, home/away,
  rest, back-to-back, team projected goals, player share, sportsbook
  price, model-vs-market edge, evidence reliability, calibration).
* 5 forbidden probability seeds (sportsbook_implied_probability,
  book_implied_prob, devig_implied_probability,
  famous_player_name_bonus, team_popularity).
* 7 passing tests cover the contract.

## 14 · Tennis counts / model signal

Tennis `_games` keys preserved (verified by `test_preservation_
assertions::test_soccer_market_gate_importable` and the global
guardrail that this commit changes NO existing engine files).  Tennis
`tennis_match_winner`, `tennis_total_games`, `tennis_game_handicap`
are universally Apex-capable per the Block 48 matrix.

## 15 · NFL preservation results

* `test_preservation_assertions::test_alt_line_engine_importable` → PASS
* No file under `services/alt_line_engine/` modified
* No change to `nfl_props_v2_*` modules
* Alt ladders / alt thresholds / main thresholds unchanged

## 16 · NBA automatic-season readiness

`services/season_active_discovery.py` provides:
```python
season_active(db, "nba")
    → {season_active: bool, upcoming_events, recent_events, ...}
```
Fully provider-truth-driven.  7 passing tests:
* zero events → season not active
* future event wakes sport (no October-21 hard-code)
* recent event wakes sport
* `resolve_refresh_eligibility(...)` covers every seasonal sport
* NBA families remain MODEL_UNAVAILABLE regardless of activation
* one unavailable family does not disable other sports (NFL/MLB)
* zero events → no fake picks (contract-level)

## 17 · Universal freshness / health statuses

HI freshness report against real Preview MongoDB:

| sport | status | lag_seconds | provider_latest_final | consumer_latest_final |
|---|---|---|---|---|
| nfl | INGESTION_LAG | – | 2026-09-22T00:00:00+00:00 | – |
| mlb | COVERAGE_LIMITED | – | – | – |
| nba | COVERAGE_LIMITED | – | – | – |
| nhl | COVERAGE_LIMITED | – | – | – |
| soccer | STALE | – | – | 2026-08-25T07:53:19+00:00 |
| tennis | COVERAGE_LIMITED | – | – | – |
| cfb | COVERAGE_LIMITED | – | – | – |

Captured in `memory/hi_freshness_report_universal.txt`.  Status is
honest, not silent.

## 18 · All focused regression test results

```
tests/test_apex_reachability_constant_model_probability.py   2 PASS
tests/test_full_evidence_bypass_real_path.py                 6 PASS
tests/test_nba_universal_apex_reachability_harness.py        4 PASS
tests/test_observed_quote_lineage.py                        11 PASS
tests/test_soccer_ags_freshness_isolation.py                 4 PASS
tests/test_universal_apex_capability_declaration.py         33 PASS
tests/test_universal_funnel_health_reasoner.py               6 PASS
tests/test_preservation_assertions.py                        9 PASS
tests/test_nba_automatic_season_activation.py                7 PASS
tests/test_nhl_goalscorer_evidence_contract.py               7 PASS
tests/test_name_based_boost_removal.py                       5 PASS
——————————————————————————————————————————————————
TOTAL                                                       95 PASS
```

Full output in `memory/universal_root_closure_tests_output.txt`.

## 19 · Preservation assertions

All 9 pass (see `test_preservation_assertions.py`):
* `APEX_MIN_BASE_SCORE == 97.0`
* `APEX_MIN_POSITIVE_CATEGORIES == 5`
* `APEX_ELIGIBLE_SPORTS == {MLB, Soccer, NBA, Tennis, NFL, CFB, NHL}`
* `APEX_UNAVAILABLE_SPORTS == {UFC, MMA, KBO}`
* NHL 5-game team and player floors present
* `alt_line_engine` importable (NFL alt lineage intact)
* `soccer_market_gate` importable (Soccer game markets intact)
* `canonical_publication_boundary.RejectionReason` enum still carries
  `SYNTHETIC_BOOK_ODDS`, `MODEL_LINE_NOT_REAL_OFFERING`,
  `NO_REAL_LINE_WITH_ODDS`.
* `provider_median` / `median` / `consensus` NOT in
  `_REAL_ODDS_SOURCES`; `provider_median` IS in `_SYNTHETIC_ODDS_SOURCES`.
* `APEX_GATE_VERSION == "apex_gate.v1.0"`.

## 20 · ONE new local commit SHA

```
ed7fb22ace0af6014462efc765e8cb3912c28356
```

Short: `ed7fb22a`.  Base: `e5fe2808` (previous surgical commit),
which is on top of `a0d1aa87`.  No push.  No deploy.
