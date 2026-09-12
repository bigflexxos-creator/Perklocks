# CFB Complete Data-Intelligence Enrichment — SHORT REPORT

## Verdict: KEEP CHAMPION.
Challenger scaffolding is fully wired, but the CFBD monthly quota is
exhausted so the actual EPA / success-rate / explosiveness / havoc /
finishing-drives rows could not be ingested this cycle.  Without those
signals the enriched challenger reduces to the champion projection
(Δ = 0 on every metric).  When CFBD quota resets, re-running
`fetch_advanced_stats` + the backtest below will produce a real
comparison.

## 1. Files / functions changed
- `services/cfb_ingest.py::fetch_advanced_stats` **NEW** — pulls
  CFBD `/stats/season/advanced` into a new `cfb_advanced_stats`
  collection (offense/defense PPA, success rate, explosiveness,
  havoc, points-per-opportunity, standard/passing-downs
  efficiency, field position).  Wired into `refresh_all` so the
  daily scheduler retries automatically once quota resets.
- `services/cfb_ingest.py::refresh_all` — added `advanced_stats`
  step (failure-tolerant; existing SP+/RP/portal paths unchanged).
- **NEW** `services/cfb_challenger_model.py` — enriched shadow
  projection.  Champion baseline (SP+ Δ + HFA) + capped ±4 pt
  advanced-stats margin delta from EPA / success-rate /
  explosiveness deltas.  Emits real evidence factors:
    * Projected Margin (Champion) / (Enriched)
    * EPA Margin Delta
    * Offensive / Defensive EPA Advantage
    * Success Rate Advantage
    * Explosiveness Advantage
    * Pass / Rush Efficiency Edge
    * Havoc Rate Advantage
    * Finishing Drives Advantage
  Provenance tag: OBSERVED / EMPIRICAL / PRIOR_ONLY / MISSING.
  **Never wired into live scoring.**  Shadow only.
- **NEW** `scripts/cfb_challenger_backtest.py` — walk-forward
  backtest over completed CFB games with fair Champion-vs-
  Challenger comparison (identical SP+ context, identical
  advanced-stats coverage → apples to apples).
- Zero live scoring / publication / settlement files touched.
- NFL Champion + Iter 138 + Iter 139 + team-identity fix untouched.
- MLB / Tennis / Soccer / NHL / NBA untouched.

## 2. CFB datasets actually available
- `cfb_sp_ratings`: 137 teams (2025) + 139 teams (2026)
- `cfb_returning_production`: 270 rows across seasons
- `cfb_portal`: 7,359 rows
- `cfb_teams`: 138 rows (alias resolution)
- `games` (cfb 2022-2025 completed): **2,231 games with real final
  scores** available as backtest ground-truth

## 3. New inputs actually wired (into the challenger only)
Advanced season stats **schema** wired for:
  offense.ppa · offense.successRate · offense.explosiveness ·
  offense.pointsPerOpportunity · offense.passingPlays.* ·
  offense.rushingPlays.* · offense.standardDowns.successRate ·
  offense.passingDowns.successRate · offense.fieldPosition.average ·
  defense.ppa · defense.successRate · defense.explosiveness ·
  defense.pointsPerOpportunity · defense.havoc.total/frontSeven/db ·
  defense.passingPlays.* · defense.rushingPlays.*

**Zero rows currently ingested** — CFBD monthly quota exceeded.
`refresh_all` will populate automatically on the next monthly reset.

## 4. Requested inputs that remain unavailable
- EPA / PPA / success rate / explosiveness / havoc / finishing
  drives — schema wired, waiting on CFBD monthly quota reset.
- QB efficiency / QB continuity — CFBD `/stats/player/season`
  quota-blocked; deferred.
- Injury / availability feed — no CFBD injury endpoint in the
  free tier; would need paid tier or ESPN scraper (not scoped).
- OL / trench context (sacks allowed, line yards) — same quota
  gate.
- Weather — no per-game weather feed wired (deferred).
- Line movement history — no per-game timestamped odds stored
  historically (deferred).

## 5. Champion vs Challenger — 2025 REG backtest
```
[ctx] year=2025  sp_ratings=137  advanced_stats=0
total_games_scanned      : 636
games_evaluated          : 103   (both teams in SP+ ratings)
adv_stats_present        : 0

CHAMPION    Brier=0.1272   ML_acc=0.8447   Margin RMSE=11.31
CHALLENGER  Brier=0.1272   ML_acc=0.8447   Margin RMSE=11.31

brier_delta_pct   =  0.0
accuracy_delta_pt =  0.0 pt
rmse_delta        =  0.0 pt
```
Challenger with advanced_stats=0 rows falls back to Champion, so
the two are numerically identical.  Promotion criteria (≥3 % Brier
gain, ≤ 0.5 pt accuracy loss, ≥ 1 pt RMSE gain) all fail.

## 6. Lock-tier calibration
**Not measurable this cycle.**  `db.picks.count_documents({sport:
"CFB", settlement_status: {$in: ["won","lost"]}})` = 0 — no CFB
picks have settled yet in this deployment window.  The 2025 games
in `db.games` have final scores but were not scored through the
production Lock authority (they predate the current publication
pipeline).  Will re-run against settled CFB picks after the first
weeks of the 2026 CFB season roll through settlement.

## 7. Challenger promoted?
**NO.** Kept champion.  Zero-delta backtest fails all three
promotion criteria.

## 8. Tests passed
```
tests/test_nfl_alt_label_projection.py .............. (14)
tests/test_nfl_star_watchlist.py ......                (6)
tests/test_nfl_alt_ladder_truth.py ..............     (14)
tests/test_nfl_atd_leaderboard_routing_fix.py ........  (8)
tests/test_nfl_alt_ladder_full_emission.py .           (1)
─────────────────────────────────────────────────────────
Total: 43 passed
```
NFL live-board sanity unchanged: 0 raw ` · ALT LOCK`, milestone
labels preserved, NFL 93+ player-props preserved.

## Guardrails observed
- ❌ NFL not touched
- ❌ MLB / Soccer / Tennis / NHL / NBA not touched
- ❌ Settlement anchors untouched
- ❌ Publication contract §3 preserved
- ❌ Team-identity resolver untouched
- ❌ No Lock-Score inflation
- ✅ CFB additive scaffolding only (ingest fn + shadow model +
     backtest)
- ✅ Champion retained per promotion rule
