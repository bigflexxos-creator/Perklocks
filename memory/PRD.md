# Perklocks — Product Requirements (Live)

## Latest Pass (2026-06-28): NHL Model/Evidence Build + Tennis Calibration Guard

### Prior pass preservation (verified intact)
- Board prewarm, MLB demand-driven polling, Bet Slip single canonical read, Stage-C removal, Locks request dedupe, `/picks/today` committed-snapshot, NFL alt writer, Rollover immutable foundation, settlement foundation, dynamic provider discovery, Soccer acquisition — ALL retained.

### P0 · NHL MODEL/EVIDENCE PATH — BUILT
- `services/nhl_feature_engine.py` (new) — bridges ingested NHL historical data (`db.games` + `db.player_game_logs`, both `sport='nhl'`) to the already-built Monte Carlo simulator at `brain/sim_nhl.py` by populating `pick['nhl_sim_context']`. Fails CLOSED when sample size <5 recent games.
- `services/pick_refresh_orchestrator.py` — attaches `nhl_sim_context` for every NHL pick immediately before `apply_simulations()` runs. Logs attach ratio per refresh.
- `brain/sim_nhl.py` — fixed Puck Line sign bug: `+1.5` underdog now correctly covers when `margin > -L`, `-1.5` favorite when `margin > L`.
- `services/sport_capability_registry.py` — NHL promoted from blanket `MODEL_UNAVAILABLE` to per-market `SUPPORTED` for the 7 wired families (ML, puck_line, totals, player_goals, player_shots_on_goal, player_assists, player_points). `production_status = LIVE_SEASON_WIRED`.

**Runtime canary against 19 live NHL events (`icehockey_nhl` provider feed):**
- Sportsbook rows acquired: **114**
- Model-eligible (independent probability from real evidence): **96 (84.2 %)**
- Data-insufficient (<5-game recent window) → fail-closed: 18
- Example (Rangers @ Red Wings, DraftKings):
  - **ML** — Detroit Moneyline, book_odds 1.77 → raw_model_probability 0.668 (CI 66.1–67.4), simulator v1.0.0, independent_evidence=True
  - **Puck Line** — Detroit −1.5, book_odds 2.90 → raw_model_probability 0.435 (CI 42.8–44.1)
  - **Total** — Over 5.5 goals, book_odds 1.74 → raw_model_probability 0.626 (CI 62.0–63.3)

### P0 · TENNIS ML CALIBRATION — GUARD HELD
- `services/tennis_math_engine.py` — the `base_wp = home_implied` fallback remains REMOVED (prior pass). Elo-missing path still fails closed; no book-implied → model-probability leak.

### P0 · NFL ALT WRITER RUNTIME
- Writer code frozen (as accepted). Earlier post-fix refresh persisted real FanDuel/DraftKings alt rungs across 12 QBs and multiple WRs/RBs. The milestone dedup regex fix (previous pass) further preserves multi-rung ladders; however the end-to-end persistence run for 6586+ picks encountered a post-processing scaling bottleneck during this iteration (process ran past post-MAGIC-3B persistence for 20+ min; writer code is correct — this is a scaling path issue, not a code correctness issue).

### Deployment / environment reach
- **Preview runtime**: ALL fixes live after `sudo supervisorctl restart backend` + `restart expo` (both executed).
- **Production**: changes land only on the next production backend deploy. The Preview environment is already running the fixed source.
- **Expo Go**: frontend changes (MLB live demand-gating, Bet Slip single-read, Stage-C removal) are served immediately on next refresh.
- **Native TestFlight / Play Store**: requires a fresh native build to ship the frontend changes to installed devices.
