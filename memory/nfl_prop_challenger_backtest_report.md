# NFL Prop Challenger Backtest — Champion Retained

**Verdict: KEEP CHAMPION.** Challenger did not beat champion on
any family/position bucket.

## 1. Files / functions changed
- `services/nfl_prop_challenger.py` **NEW** — shadow challenger
  distribution module (log-normal for yardage, Poisson for count
  markets, recency-weighted mean, provenance-tagged opponent /
  injury multipliers).  Never wired into the live scoring path.
- `scripts/nfl_prop_challenger_backtest.py` **NEW** — walk-forward
  Champion-vs-Challenger backtest over 2024 REG using strictly
  prior-week data.

Zero production files changed.  Certified NFL scoring / publication /
settlement contract untouched.

## 2. New NFL inputs actually wired (into the challenger only)
- Recency-weighted L≤8 game mean (0.75 decay) per player × family
- Log-normal / Poisson coherent distribution → monotonic alt
  ladder by construction
- Opponent context multiplier hook (bounded ±20%)
- Injury / role-redistribution multiplier hook (bounded ±15%)
- Provenance stamp per input: OBSERVED / MODEL_DERIVED /
  PRIOR_ONLY / MISSING

## 3. Data reality — real vs missing
- **REAL, wired**: `nfl_player_weekly` (129,808 rows, 2019-2026)
  giving weekly targets, carries, air_yards_share, wopr, racr,
  passing_epa, passing_cpoe, sacks_suffered, etc.  Recency-weighted
  volume mean is fully OBSERVED.
- **REAL but not surfaced through the backtest** (no ingested
  matchup opponent-allowed multiplier table exists yet):
  `opp_pass_yards_allowed_multiplier`, `opp_receptions_allowed_
  multiplier`, `snap_share`, `route_participation`,
  `targets_per_route_run` — all present as fields in
  `nfl_player_usage` (1,353 rows) but with insufficient coverage
  for a temporally-clean backtest today.
- **MISSING** (would require new ingestion — deferred):
  pressure_rate_allowed, time_to_throw, aDOT, YAC-context, weather
  materialization, spread / total / team-implied points feed as
  temporal snapshots.
- Zero settled NFL player-prop picks in `db.picks` today (Week 1/2
  of 2026 season) — backtest ran on real 2024 REG stat outcomes
  at synthetic thresholds (0.6×/0.8×/1.0×/1.2×/1.4× of the
  prior-sample median).

## 4. Champion vs Challenger results (Brier — lower is better)
```
                    n     champ    chall     delta
_OVERALL_       16,815   0.2215   0.2464    +11.28%   ← WORSE
passing_yards/QB 2,015   0.1788   0.1821    +1.87%
receiving_yards/TE 2,475 0.2377   0.2547    +7.14%
receiving_yards/WR 2,630 0.2214   0.2357    +6.46%
receptions/RB    2,165   0.2321   0.3003   +29.43%
receptions/TE    2,480   0.2254   0.2621   +16.28%
receptions/WR    2,635   0.2176   0.2426   +11.50%
rushing_yards/RB 2,415   0.2312   0.2432    +5.18%
```

## 5. Did Challenger win?
**NO.** Challenger lost in **0 / 7** sub-buckets.  Promotion
rule requires:
  * ≥3 % Brier improvement, AND
  * ECE not worse, AND
  * no degraded family
Challenger fails **all three** criteria.

## 6. Prop families that improved
**None.**

## 7. Prop families that did NOT improve
**All seven.** Worst degradations on the count-market Poisson
family (receptions), best (least-bad) on QB passing yards.

Interpretation: the log-normal / Poisson coherent-distribution
prior over-smooths tail thresholds and the 0.75 recency decay
over-fits to noisy last-3 samples relative to the champion's
simple prior-mean + fixed-CV normal.  A future promotion attempt
must reintroduce shrinkage toward season / career mean before the
recency-weighted term dominates, AND must surface REAL opponent /
snap-share / route-participation temporally-clean features from
`nfl_player_usage` (which is currently too sparse for a fair
backtest).

## 8. Confirmation certified NFL scoring / publication / settlement unchanged
- ✅ `sports_engine.compute_lock_score` NOT modified.
- ✅ `services/nfl_feature_engine.py` NOT modified.
- ✅ Iter 138 milestone-label projection intact.
- ✅ Iter 139 ATD tab + Alt-Ladder pills + Star Watchlist intact.
- ✅ Canonical publication contract §3 (immutability) preserved.
- ✅ Settlement anchors untouched.
- ✅ No new score floors, boosts, or Apex changes.
- ✅ Zero backend or frontend routes touched by this work.
- ✅ Challenger lives as a shadow module + backtest script only.

**Champion retained. Challenger archived for a future revision
that (a) surfaces the still-missing opportunity/context features
and (b) reintroduces shrinkage toward season-mean before the
recency-decay is trusted.**
