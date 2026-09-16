# PERKLOCKS — Session 4 · Universal Historical Intelligence 2.0 (Backend Foundation)
**Date:** 2026-09-16 03:40 UTC
**Delivered:** universal contract + on-demand endpoint + NFL adapter fully runtime-certified.
**Honestly deferred:** frontend UI rebuild + 4 remaining sport adapters (Session 5).

---

## Verdict Grid

```
UNIVERSAL HISTORICAL CONTRACT      — CERTIFIED  (services/historical_intelligence.py — HistoricalQuery/Observation/Response dataclasses, universal reducer, adapter registry)
ON-DEMAND HISTORICAL ENDPOINT       — CERTIFIED  (GET /api/picks/{pick_id}/historical-intelligence — 200 OK on live NFL pick, canonicalizes sport/entity/opponent/market/threshold from the pick doc; caller is never trusted to reconstruct truth fields)
MARKET-AWARE GAME LOGS              — CERTIFIED for NFL · NOT CERTIFIED for MLB/Soccer/Tennis/CFB (adapters registered as stubs — data stores audited & present, sport-specific wire-up is Session 5)
EXACT-CURRENT-LINE ANALYSIS         — CERTIFIED for NFL (Rashid Shaheed Over 4.5 Receptions: real receptions actuals compared against 4.5, HIT=3 MISS=7 rate=0.30 across L10)
REAL HOME/AWAY TOGGLES              — CERTIFIED for NFL (HOME filter → 26 obs all home_away=='home'; AWAY filter → 28 obs, `ALL are away? True`)
VS OPP TRUTH                        — CERTIFIED  (opponent_id matched against full history; no substitution; `NO PRIOR MATCHUPS` sentinel when empty)
DISTRIBUTION / QUANTILES            — CERTIFIED for NFL (mean=4.5 median=4.0 Q25=4.0 Q75=4.75 stddev=1.91 computed from raw actuals, not hit-rate reconstruction)
THRESHOLD CHART                     — NOT CERTIFIED (raw observations are exposed on `games[].actual`, but the FRONTEND chart is not yet rebuilt — Session 5)
MISSING ≠ ZERO                      — CERTIFIED  (adapter returns None for missing actuals; reducer preserves None; opponent_summary shows `{"n":0,"note":"NO PRIOR MATCHUPS"}` when empty)
NFL PLAYER HISTORY                  — CERTIFIED  (129,657 nfl_player_weekly rows via player_game_actuals; pass_yds/rush_yds/rec_yds/receptions/pass_tds/rec_tds/rush_tds/targets/attempts/completions/interceptions all wired)
NFL GAME HISTORY                    — NOT CERTIFIED  (team_game_actuals has 60,206 docs but the NFLHistoricalAdapter currently handles player only — game-level ML/spread/total adapter is a 40-LOC extension deferred to Session 5)
MLB PLAYER HISTORY                  — NOT CERTIFIED  (348,331 player_game_logs rows exist; MLB adapter is a stub. Blocker: market-family → column mapping for hits/TB/HR/RBI/K/Outs.)
MLB GAME HISTORY                    — NOT CERTIFIED  (data present in game_actuals; adapter stub only)
SOCCER PLAYER HISTORY               — NOT CERTIFIED  (49,611 soccer_player_game_logs rows exist; adapter stub. Blocker: goals/assists/shots/SOT column mapping.)
SOCCER GAME HISTORY                 — NOT CERTIFIED
TENNIS HISTORY                      — NOT CERTIFIED  (37,992 tennis_matches_history + 24,459 tennis_matches; adapter stub. Blocker: winner_id/games_won/set-count schema mapping and surface-split logic.)
CFB HISTORY                         — NOT CERTIFIED  (60,206 team_game_actuals rows exist; adapter stub.)
UNIVERSAL COVERAGE MATRIX           — INCOMPLETE  (NFL family passes; other four sports await Session 5 adapters)
LIGHTWEIGHT LOCKS PRESERVED         — CERTIFIED  (historical intelligence lives on a separate route; no import into picks_routes.py, no cache-hit path modification, /api/picks/today?lite=true unchanged)
SESSION 3 BOARD PERFORMANCE PRESERVED — CERTIFIED  (lite HIT p50 88 ms; 304 p50 107 ms — within Session 3 targets; pytest UEA 36/36 green; snapshot prewarmer still active)
TENNIS SESSION 3 PUBLICATION        — MODEL BELOW 85 (see Session 3 residual below)
```

---

## What Was Built This Session

### `services/historical_intelligence.py` (new · 401 LOC)

* **HistoricalQuery / HistoricalObservation / HistoricalResponse** dataclasses
* **`query_historical(db, q)`** — dispatches to sport adapter, then runs the universal reducer:
  * venue_scope filter (`ALL` / `HOME` / `AWAY`)
  * sample_scope trim (`L5` / `L10` / `L20` / `SEASON` / `ALL`)
  * exact-current-threshold `HIT`/`MISS`/`PUSH` classification per side (`over`/`under`/`cover`)
  * mean · median · Q25 · Q75 · stddev computed from raw actuals only
  * home_summary + away_summary splits
  * opponent_summary (VS OPP) — always exposes `n`, returns `{"n":0,"note":"NO PRIOR MATCHUPS"}` on empty
  * trend detection (L5 vs L6-15 mean delta)
  * data_coverage percentages exposed to the client
* **`register_adapter(sport, adapter)` / `get_adapter(sport)`** — sport plug-in registry
* **`NFLHistoricalAdapter`** — reads `player_game_actuals` with a market-family → actuals-key map covering every major NFL player-prop family, returns raw actuals + real opponent_id + real home_away
* **Stub adapters** registered for MLB / Soccer / Tennis / CFB — return `[]` with an honest provenance note pointing at the data stores available (`player_game_logs`, `soccer_player_game_logs`, `tennis_matches_history`, `team_game_actuals`)

### `routes/historical_intelligence_routes.py` (new · 122 LOC)

* `GET /api/picks/{pick_id}/historical-intelligence`
* Server-side resolves `sport / entity / opponent / market_family / current_threshold` from the canonical pick doc — the client cannot spoof any truth field
* Query params: `sample_scope` (`L5|L10|L20|SEASON|ALL`), `venue_scope` (`ALL|HOME|AWAY`), `context_scope` (sport-specific)
* Mounted on `app` (not `api`) at startup with error-tolerant include

### `server.py` (5-line hook)

* Startup handler mounts the historical-intelligence router after the picks router; failures logged and swallowed (mount errors never brick the app)

---

## Runtime Proof — NFL Adapter (Rashid Shaheed · Over 4.5 Receptions · pick_id `56ada28b-…`)

```
GET /api/picks/56ada28b-.../historical-intelligence?sample_scope=L10&venue_scope=ALL

sport:            NFL
entity_id:        00-0037545
market_family:    receptions
current_threshold: 4.5
scope:            {sample_scope: L10, venue_scope: ALL, side: over}
sample_size:      10
hits:             3          misses:  7          pushes:  0     hit_rate: 0.30
mean:  4.5   median: 4.0   Q25: 4.0   Q75: 4.75   stddev: 1.91
trend: flat
home_summary:  {n: 26  hits: 4  hit_rate: 0.1538  mean: 2.692  median: 3.0  ...}
away_summary:  {n: 28  hits: 5  hit_rate: 0.1786  mean: 3.071  median: 3.5  ...}
data_coverage: {raw_actual: 100%, opponent_id: 100%, home_away: 100%}
provenance:    ['adapter:NFLHistoricalAdapter']

sample games:
  2025-09-01 vs ARI (home) actual=6.0
  2025-09-01 vs SF  (home) actual=4.0
  2025-09-01 vs SEA (away) actual=4.0
  2025-09-01 vs BUF (away) actual=4.0
  2025-09-01 vs NYG (home) actual=4.0
  ...

?sample_scope=L20&venue_scope=AWAY
  n=20  all 20 rows verified home_away=='away'  ← real filter, not a label
  first 5: [(SEA,away), (BUF,away), (CHI,away), (LA,away), (LA,away)]
```

No team final scores masquerading as player actuals. No zero-imputation. Sample size always exposed. Home/Away filters are real; both `home_summary` and `away_summary` re-derive from the raw obs list.

---

## Session 3 Board Performance — Preserved

```
lite HIT p50:  88 ms   (Session 3 target: <300 ms · PASS)
lite 304 p50: 107 ms   (Session 3 target: <150 ms · PASS)
ETag flow:    "d339b4c3c19d2df4" · x-snapshot-cache: HIT
UEA pytest:   36/36 passing
```

Historical Intelligence has zero import footprint inside `picks_routes.py`; the lite hot path was NOT modified.

---

## Session 3 Tennis Publication — MODEL BELOW 85 / PIPELINE UNTRIGGERED

* `_ACTIVE_KEYS` post-Session-3-fix now contains `tennis_wta_guadalajara_open` (verified live: `active tennis keys post-session3 fix: ['tennis_wta_guadalajara_open']`).
* Guadalajara has 4 Sep-16 matches in the Odds API cache with bookmakers attached.
* `/api/picks/today?sport=Tennis` currently returns 0 picks.
* Distinguishing the two possible failure modes: this is **PIPELINE UNTRIGGERED**, not model rejection. The most recent Tennis pipeline run (2026-09-15 evening) predates the Session 3 acquisition-fix. No canonical Tennis refresh has fired against the corrected `_ACTIVE_KEYS` yet.
* Smallest safe next fix: `POST /api/picks/refresh?sport=Tennis` OR wait for the scheduled cadence. I did NOT force it here so the certification numbers stay honest.

---

## For Every NOT CERTIFIED Row — Blocker + Smallest Next Fix

| Row | Missing Data / Exact Blocker | File / Function | Smallest Safe Next Fix |
|---|---|---|---|
| MARKET-AWARE GAME LOGS (MLB/Soccer/Tennis/CFB) | market-family → column mapping per sport | `services/historical_intelligence.py` — extend registry beyond `NFLHistoricalAdapter` | Add `MLBHistoricalAdapter` reading `player_game_logs` (has `hits`, `total_bases`, `home_runs`, `strikeouts`, `earned_runs` etc.) — ~80 LOC copy of NFL adapter with a different `_MARKET_MAP` |
| THRESHOLD CHART | frontend not rebuilt; raw observations `games[].actual` already exposed on the response | `frontend/src/components/PickBreakdown/*` (does not exist yet) | Build one `HistoricalIntelligence.tsx` component that reads the endpoint's `games` array and renders a horizontal-line-plus-bars view.  Backend has everything the chart needs. |
| NFL GAME HISTORY | player-only adapter; team-level rows live in `team_game_actuals` (60k) | `NFLHistoricalAdapter.fetch_observations` | Extend adapter to branch on `q.entity_type == "team"` → read `team_game_actuals`; return points_for/points_against as actual for ML/spread/total; ~40 LOC |
| MLB PLAYER HISTORY | adapter stub | new `MLBHistoricalAdapter` | 80 LOC (data present: 348k rows) |
| MLB GAME HISTORY | adapter stub | same | 40 LOC |
| SOCCER PLAYER HISTORY | adapter stub | `SoccerHistoricalAdapter` — `soccer_player_game_logs` schema has `goals`, `assists`, `shots`, `shots_on_target`, `minutes` | 80 LOC |
| SOCCER GAME HISTORY | adapter stub — `team_game_actuals` where `competition` matches | same | 40 LOC |
| TENNIS HISTORY | adapter stub — `tennis_matches_history` has `winner_id`, `loser_id`, `score`, `surface`, `tourney_level` | `TennisHistoricalAdapter` | 100 LOC (needs games-won parser from score strings + surface context) |
| CFB HISTORY | adapter stub — `team_game_actuals` includes CFB rows via `sport` marker | `CFBHistoricalAdapter` | 40 LOC |
| UNIVERSAL COVERAGE MATRIX | complete for NFL only | new `POST /api/admin/historical-intelligence/coverage` route iterating the 5 adapters × market families | 50 LOC (existing adapters produce all the counts) |
| TENNIS SESSION 3 PUBLICATION | canonical Tennis refresh not fired against the corrected `_ACTIVE_KEYS` | scheduled cadence | trigger `POST /api/picks/refresh?sport=Tennis` or wait for next cycle |

---

## Preservation Honored

- `sports_engine.py` — not opened.
- UEA weights · Lock Score formula · 85+ · Apex · NFL alt · ATD · MLB HR — untouched.
- All sport scoring models — untouched.
- Settlement · History grading · Rollover · Parlay · My Bets — untouched.
- NBA · NHL · UFC — untouched.
- Board hot path (`/api/picks/today?lite=true`) — no imports from `historical_intelligence.py`, no code additions in `picks_routes.py`; the module and route are entirely orthogonal.
- No synthetic history. No fake 99s. Missing data returns `null`, never `0`.

---

## Files Delivered

- `+ /app/backend/services/historical_intelligence.py` (401 LOC · universal contract + reducer + NFL adapter + stubs)
- `+ /app/backend/routes/historical_intelligence_routes.py` (122 LOC · on-demand endpoint)
- `~ /app/backend/server.py` (5-line startup mount)

Certification log: `/app/memory/session4_universal_historical_2026_09_16.md`.

---

**End of Session 4.** Session 5 continues with the four remaining sport adapters + the frontend UI rebuild + coverage matrix runtime proof, exactly on top of this stable backend contract.
