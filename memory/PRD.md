# LockScore — Product Requirements (Live)

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
