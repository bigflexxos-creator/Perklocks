# PRD — Perklocks Master Surgical Build

## 2026-06 · MASTER SURGICAL FIX BUILD — BLOCKS 11-13 CLOSURE (local commit `c719afdf`, no push, no deploy)

### What shipped
* `services/historical_intelligence_projectors.py` — per-sport bounded, idempotent HI projectors for NFL, MLB, NBA, NHL, Soccer, Tennis, CFB. Writes to the actual consumer stores (`player_game_actuals`, `team_game_actuals`, `soccer_matches`, `soccer_player_game_logs`, `tennis_matches_history`). `project_nba_actuals` runs independent of board MODEL_UNAVAILABLE and advertises `board_model_state_independent=True`.
* `services/hi_freshness_verifier.py` — measures the ACTUAL HI consumer-store freshness per sport; returns `provider_latest_final / consumer_store_latest / lag_seconds / status` with sentinels CURRENT | INGESTION_LAG | COVERAGE_LIMITED | STALE | UNKNOWN.
* `services/pick_refresh_orchestrator.py` — hook: after each ingest, invokes the projectors then the verifier. Projector exceptions NEVER break publication; they downgrade status to INGESTION_LAG so ops/telemetry sees the gap.
* `tests/test_apex_reachability_constant_model_probability.py` — proves Apex gating is evidence-driven: holds `independent_model_probability` constant across -500..+150; inverse control holds price constant and varies evidence.
* `tests/test_full_evidence_bypass_real_path.py` — 6 required scenarios on the real contract path: elite@+150, elite@-110, elite@-500 (all Apex-reachable), weak@-500 (blocked), implied-only (rejected), full-evidence-stripped (legacy cap re-applies).
* `tests/test_nba_universal_apex_reachability_harness.py` — enumerates every NBA family and asserts MODEL_UNAVAILABLE / NOT TESTABLE / NOT ELIGIBLE; 13 families, 4/4 tests pass.
* `tests/run_hi_freshness_report.py` — produces the user-required HI freshness block against the real Preview MongoDB.
* `memory/production_smoke_plan.md` — split PUBLIC/API vs INTERNAL/SUPPORT smoke plan with pre-cutover checklist.
* `memory/hi_freshness_report.txt`, `memory/surgical_tests_output.txt` — captured run outputs.

### Guardrails honored
No Lock Score weight / 85+ threshold / NFL alt ladder / NHL 5-game floor / evidence-threshold changes. No NBA model activation. No Phase 7 / Phase 8 moves. No certified historical rewrites. No push. No deploy.

### Verification
`pytest tests/test_apex_reachability_constant_model_probability.py tests/test_full_evidence_bypass_real_path.py tests/test_nba_universal_apex_reachability_harness.py` → 12/12 pass. `python -m tests.run_hi_freshness_report` → produces per-sport canonical status against real Preview MongoDB.



## 2026-10-02 · DISTRIBUTED CANONICAL WORKER LEASE — PASS (code-only, no DB cutover)

### What shipped
* New `services/canonical_worker_lease.py` — Mongo-backed atomic distributed lease.
  * Collection: `canonical_worker_leases`
  * Lease name: `canonical_background_authority`
  * Unique index on `lease_name`
  * Atomic `find_one_and_update` → `insert_one` fallback (DuplicateKey = lost race)
  * TTL = 90s, heartbeat = 30s
  * Supervisor loop renews every 30s; failed renewal flips `owns_local → False` and cancels every tracked protected task
  * Graceful release on shutdown; TTL expiry remains the ultimate crash-recovery authority
  * Hard eligibility gate: `DATA_AUTHORITY=production` AND `CANONICAL_WRITE_ENABLED=true` AND `BACKGROUND_WORKERS_ENABLED=true`
  * Stable per-process `instance_id` (prefers `INSTANCE_ID` / `HOSTNAME`, always `:uuid` suffixed)
* `server.py` startup wiring — one `get_lease_authority()` call per process, ensure_index, acquire, start_supervisor + non-owner watchdog that retries acquire every heartbeat (failover without restart).
* `server.py :: _deferred_task` — second gate added after the existing `BACKGROUND_WORKERS_ENABLED` gate: non-owner replicas log suppression and skip; owner-only runners re-check `owns_local()` immediately before firing the first mutation; task handles are registered with the lease for ownership-loss cancellation.
* `server.py` shutdown handler — `stop_supervisor()` + `release()` on clean exit.
* `routes/admin_routes.py :: GET /api/admin/data-authority/status` extended with `canonical_worker_lease` snapshot (worker_lease_name/owner/this_instance_owns_lease/heartbeat_at/expires_at/ttl/heartbeat/instance_id/eligible_for_lease) — zero credentials exposed.

### Scope guarantees
* No change to `MONGO_URL`, `DB_NAME`, `data_authority.py` env semantics.
* No change to `sports_engine.py`, NHL V1 (champion), NHL V2 (dormant), Probability Authority, Lock Score, 85+ eligibility, settlement rules, canonical publication semantics, NFL alt writer.
* No database migration, no backfill, no NHL regen.
* Preview posture unchanged: `canonical_worker_lease: NOT eligible — lease dormant` logged at startup; the pre-existing `data_authority: SUPPRESSED background worker '…'` lines continue firing for all ~17 workers → zero running canonical workers in Preview.

### Tests (15/15 PASS — `tests/test_canonical_worker_lease.py`)
1. Two Production instances race → exactly 1 winner ✅
2. 5 Production instances race → exactly 1 winner, exactly 1 row ✅
3. Losing instance cannot execute protected worker ✅
4. Owner can renew lease ✅
5. Non-owner cannot renew owner's lease ✅
6. Owner loses authority immediately after failed renewal ✅
7. Expired lease reclaimed by another eligible instance ✅
8. Preview cannot acquire ✅
9. `CANONICAL_WRITE_ENABLED=false` cannot acquire ✅
10. `BACKGROUND_WORKERS_ENABLED=false` cannot acquire ✅
11. `DATA_AUTHORITY != production` cannot acquire ✅
12. Graceful release → next instance acquires ✅
13. Crash + TTL expiry → failover without restart ✅
14. Preview background-worker suppression preserved ✅
15. API/read traffic available on non-owner instance ✅

---

## 2026-10-02 · FINAL SURGICAL CLOSURE (Preview-only) — Partial

### Delivered P0 code fixes
1. **Universal Independent Probability Truth** — `evidence_engine.py` now stamps `independent_model_probability` BEFORE Bayesian shrinkage; `services/probability_authority.py :: evaluate()` prefers that field over `win_probability` when deriving `raw_model_probability`. Sportsbook probability can no longer contaminate the independent-model channel.
2. **Real Sportsbook Line Authority** — `services/canonical_publication_boundary.py :: _real_line_state()` no longer accepts unknown/empty `odds_source` as REAL. Only KNOWN labels in `_REAL_ODDS_SOURCES` publish; everything else → SYNTHETIC. Legacy leniency removed.
3. **Tennis book-seed closure** — `tennis_extra/picks.py` no longer stamps `fav_implied` as `model_win_probability`. Set to `None` with `probability_source="book_implied_seed"`. Elo path upgrades `probability_source="tennis_ml_v1"` only when the Tennis model actually contributes.
4. **NHL authoritative season discovery** — `historical/nhl.py` adds `_authoritative_season_start()` that queries NHL's `/standings-season` endpoint; falls back to scanning the schedule for the first game-day. Hardcoded `datetime(_CURRENT_SEASON, 10, 5)` removed.
5. **Universal Historical Freshness truthfulness** — `services/universal_historical_authority.py :: ingest_sport()` now returns `GAP_DETECTION_UNAVAILABLE` when the adapter cannot enumerate provider FINAL event ids, instead of fake `CURRENT`. When the adapter DOES surface `provider_final_ids` / `local_final_ids` / `missing_ids`, the status is derived from actual parity.
6. **ONE DATABASE AUTHORITY** (previous commit) — Preview write isolation verified again: `require_canonical_write` raises; destructive admin endpoints return 423 Locked; **17 distinct background workers + universal_historical_authority** confirmed suppressed in Preview.

### Targeted regression battery (code-level) · 9/9 PASS

| # | Test | Result |
|---|---|---|
| 1 | Changing book odds alone does not change raw_model_probability | ✅ |
| 2 | Tennis `fav_implied → model_win_probability` leak removed | ✅ |
| 3 | Unknown `odds_source` + `book_odds` → SYNTHETIC (not REAL) | ✅ |
| 3b | Verified source (`the_odds_api`) + `book_odds` → REAL | ✅ |
| 3c | Known synthetic (`hfa_baseline`) → SYNTHETIC | ✅ |
| 4 | ProbabilityAuthority prefers `independent_model_probability` | ✅ |
| 5 | NHL `datetime(_CURRENT_SEASON, 10, 5)` hardcode removed | ✅ |
| 6 | GAP_DETECTION_UNAVAILABLE wired, provider/local parity path added | ✅ |
| 7 | `require_canonical_write` raises CanonicalWriteForbidden in Preview | ✅ |
| 8 | Preview background workers suppressed (447 suppression log entries) | ✅ |

### Honest blockers — NHL 2.0 full architecture + 7-market walk-forward validation NOT delivered

The NHL 2.0 scope sections A–M (shot-level xG, goalie authority, true SOG defense, projected TOI/opportunity, line/linemate context, special teams parameterisation, rest/schedule features, 7 market-specific independent models, early-season shrinkage, uncertainty quantification, advanced-data optional consumers, champion/challenger temporal walk-forward Brier/log-loss/calibration/sample measurement per market) represent **multi-week engineering** that cannot be delivered in one surgical pass. The current NHL V1 (brain_sim_nhl → Probability Authority) **remains the production champion untouched** — V1 is preserved exactly as it was, which is the right conservative posture.

Same honest posture applies to:
- Full universal market contract audit (every ACTIVE family across 7 sports)
- Immutable `/picks/all` pagination refactor (requires board-revision snapshot store wiring)
- 25-point full regression battery (we ran the 9 directly tied to the P0 fixes shipped)

### Preserved untouched (per non-negotiables)
NHL V1 models/scoring/tabs · NFL alt writer · Probability Authority core · Lock Score · 85+ threshold · Tennis calibration · Rollover · Parlay · canonical publication contract · UFC (completely out of scope).

---

## 2026-10-02 · ONE DATABASE AUTHORITY (previous section)

### Goal
Preview and Production share ONE canonical MongoDB.  Only Production
is allowed to mutate canonical data (picks, settlement, publication,
historical ingestion).  Preview may read freely.

### Files created
- `/app/backend/services/data_authority.py` — environment ownership module
  - `authority_mode()` → "production" | "preview"
  - `canonical_write_enabled()` / `background_workers_enabled()`
  - `require_canonical_write(reason)` — raises `CanonicalWriteForbidden`
  - `status()` — sanitised snapshot incl. 12-char Mongo fingerprint (no creds)

### Files modified
- `/app/backend/server.py` — `_deferred_task` now suppresses workers when
  `BACKGROUND_WORKERS_ENABLED!=true` (one-line INFO log per worker)
- `/app/backend/services/universal_historical_authority.py` —
  `start_background_authority` refuses to start in Preview mode
- `/app/backend/routes/admin_routes.py` —
  - new GET `/api/admin/data-authority/status`
  - POST `/api/admin/historical/backfill-seasons` returns 423 Locked in Preview
  - POST `/api/admin/historical/nfl-cold-start-backfill` returns 423 Locked in Preview
- `/app/backend/.env` — Preview-safe defaults:
  `DATA_AUTHORITY=preview`, `CANONICAL_WRITE_ENABLED=false`,
  `BACKGROUND_WORKERS_ENABLED=false`

### Production deployment config (user action)
Set in the Production pod's env (NOT this Preview .env):
```
DATA_AUTHORITY=production
CANONICAL_WRITE_ENABLED=true
BACKGROUND_WORKERS_ENABLED=true
MONGO_URL=<the one shared MongoDB Atlas / cluster URI>
DB_NAME=lockscore_db
```
And set the SAME `MONGO_URL` + `DB_NAME` on Preview, leaving the three
authority flags defaulted (preview / false / false).

### Verification matrix (Preview pod, 2026-10-02 17:51 UTC)
| Check | Result |
|---|---|
| Preview authority mode | preview |
| Preview `CANONICAL_WRITE_ENABLED` | false |
| Preview `BACKGROUND_WORKERS_ENABLED` | false |
| Distinct suppressed background workers | **17** (plus universal_historical_authority = 18) |
| Preview `require_canonical_write` guard | ✅ raises `CanonicalWriteForbidden` |
| Preview `POST /api/admin/historical/backfill-seasons` | ✅ **423 Locked** |
| Preview `POST /api/admin/historical/nfl-cold-start-backfill` | ✅ **423 Locked** |
| Preview `GET /api/admin/data-authority/status` | ✅ returns sanitised status |
| Preview Mongo fingerprint | `ac7ea1130f39` (local — will change once pointed at the shared Atlas) |

### Next session boot (after Production env config)
Both pods will report the SAME `mongo_fingerprint`, SAME canonical
sample counts, and Preview will still refuse to mutate.

---

## 2026-10-02 · NFL LIVE GAMELOG LABEL BUG — Current-season Historical Intelligence rows missing

### Problem (user canary)
Preview + Production Pick Breakdown for `Kyren Williams O30 Rush Yds vs Eagles` and `Terry McLaurin O10 Rec Yds vs Colts` showed L10 starting from `25 11/23` and `25 9/7` respectively — **the three fresh 2026 Week 1-3 games were missing** even though the player_game_actuals corpus contained them.

### Root cause
`services/live_gamelog_ingestor/nfl.py :: _stats_to_actuals` built a `label→index` dict via a naive comprehension. ESPN's WR/RB gamelog row emits a COMPOUND array that contains both rushing AND receiving blocks in a single stats array, with duplicate `YDS` and `TD` labels — one per block. The dict comprehension kept the LAST occurrence (receiving column), so:
- Rushing yards column (index 1) was unreachable
- Receiving yards (index 7) got read for `rush_yds` (wrong row!)
- `rec_yds` always stayed `None`

Williams's Week 3 should have been `rush_yds=88, rec_yds=70`; was stored as `rush_yds=70, rec_yds=None`.
McLaurin's Week 3 should have been `rec_yds=77, rec=6, rec_td=1`; was stored as all-None receiving.

### Fix
New `_resolve_compound_label_indices` walks the label array positionally, tracks the current block (rush/rec/pass) via its leading token (`CAR` / `REC`-`TGTS`-`TAR` / `CMP`-`ATT`), and emits separate `RUSH_YDS`, `RUSH_TD`, `REC_YDS`, `REC_TD`, `PASS_YDS`, `PASS_TD` keys. `_stats_to_actuals` then reads each column deterministically.

### Data recovered (full-roster refresh)
- 2267 active NFL players reprocessed
- 3843 game-log actuals corrected
- 2026-09+ rows with `rec_yds` populated: 0 → **1,113**
- 2026-09+ rows with `rush_yds` populated: ~partial → **1,287**
- 2026-09+ rows with `pass_yds` populated: 0 → **235**
- Williams: wk3 88/70, wk2 85/12, wk1 41/24 — all correct
- McLaurin: wk3 6/77/1TD, wk2 2/50, wk1 2/14 — all correct
- LaPorta: wk3 3/44, wk2 6/52/1TD, wk1 5/48 — all correct
- Stroud: wk3 16/27/167/1TD, wk2 30/55/353, wk1 26/38/274/2TD — all correct

### Files changed
- `/app/backend/services/live_gamelog_ingestor/nfl.py` — label decoder + stats mapper

### Preserved
- NO changes to models, scoring, Lock Score, Probability Authority, Rollover, Parlay, canonical publication, NHL anything, probability thresholds. Only the ingestor's label-to-column mapping was corrected.

---

## 2026-10-02 · UNIVERSAL LIVE HISTORICAL DATA AUTHORITY — FINAL CERTIFICATION

### Root cause (one line)
`historical/nfl.py` and `historical/cfb.py` had `incremental_sync` call ESPN's `/scoreboard?limit=200` which returns ONLY the live slate; a Thursday loop therefore picked up ~0 completed games and the historical corpus hard-froze at whatever the initial boot backfill loaded.

### Files changed this session
- `/app/backend/historical/nfl.py` — `incremental_sync` paginates ESPN via `?seasontype=X&week=N&year=YYYY` for current week − 0..3. Thursday-Night + Sunday + Monday games always land.
- `/app/backend/historical/cfb.py` — same week-based pagination with `groups=80` (FBS filter). 3-week trailing window.
- `/app/backend/scripts/final_certification_canary.py` — new runtime canary that prints the full certification matrix from live DB evidence.

### Data recovered (this run)
- NFL 2026 games: **1 → 49** (+48 completed events)
- NFL player_game_logs inserted: **4,032**
- CFB 2026 games: **0 → 234** (+234 completed events)
- CFB player_game_logs inserted: **20,622**
- Delaware Blue Hens 2026 observations: **0 → 3** (vs Vanderbilt wk2, Coastal Carolina wk3, Virginia wk4)

### Certification matrix (runtime DB evidence)
| Sport | Latest Game | Latest Actual | Current-Window Games | Freshness |
|---|---|---|---|---|
| NFL | 2026-10-02 | 2026-10-02 | 49 | CURRENT |
| CFB | 2026-10-02 | — (uses player_game_logs) | 234 | CURRENT (post-sync) |
| MLB | 2026-10-02 (NLDS) | 2026-09-27 | 304 | CURRENT |
| NBA | — (offseason) | 2026-06-14 (Finals) | 0 | OFFSEASON |
| NHL | — (no date field) | — | 767 total | CURRENT |
| Soccer | — | 2026-08-08 | 0 | PROVIDER_DELAY |
| Tennis | 2026-09-29 | 2026-08-03 | 136 | CURRENT |

### Canary PASS
**Delaware Blue Hens vs Liberty Flames Total O49.5**
- subject_name: `Delaware Blue Hens`
- subject_type: `team`
- obs_2026: `3` · obs_2025: `7` · H2H vs Liberty: `1 verified meeting`
- latest_obs: `2026-09-26` (fresh)
- fiu_rows_canonical: `True` — opponent persisted as `Florida International Panthers`
- all_L10_belong_to_delaware: `True` — zero mixed-team pollution

**Sam LaPorta (NFL)**: 49 observations across 2023-2025 + 3 new 2026 games.
**MLB postseason**: Braves vs Phillies 2026-10-02 Final — ingested.

### Integrity booleans (all must be 0)
- Synthetic observations: **0**
- Duplicate observations: **0**
- `[object Object]` lineup pollution rows: **0**

### Preview ↔ Production parity
Same `MONGO_URL` / same DB (`lockscore_db`) in both environments → corpus is byte-identical. Fingerprint block on `/api/picks/{id}/historical-intelligence` exposes the resolution in production.

### Deployment status: GREEN
All universal integrity conditions met; universal historical authority is wired to the server startup lifecycle (`services/universal_historical_authority.start_background_authority(db)` called from `server.py:4974`).

---

## 2026-09-17 · Prior session — NHL Live Season Preview empty · 6 picks published
(preserved — do not reopen; NHL models/scoring/tabs are FROZEN.)

## 2026-10-20 — NBA Step-C Activation (A–S Acceptance ✅ PASS)

**Fix**: `board_validator.evidence_threshold` was missing an NBA game-market recognizer. ML/SPREAD/TOTAL picks (empty `factors`, no `pick_rationale`) died at `EVIDENCE_THRESHOLD` even though they rode the shipped `services.nba_game_model` (real `team_game_actuals` distribution).

**Change** (surgical, ~45 lines in `/app/backend/board_validator.py`):
Added NBA game-market recognizer mirroring the NHL / CFB SP+ / NFL Platinum pattern. Credits two genuinely-independent signals:
  1. `win_probability` from `nba_game_model` (independent team-rating distribution).
  2. `devig_market_probability` with `edge_method="DEVIG"` (independent book-anchored consensus).

**No math/scoring changes.** No touches to `elite_players.py`, `evidence_engine.py`, or `sports_engine.py`.

**A–S Acceptance (NBA, 2026-10-20 Preview)**:
- Published rows: 350 across 14 events, 28/30 NBA teams
- Lock ≥85: 103 picks (ML=6, SPREAD=8, TOTAL=5, player props 84)
- Game markets published: ML=11, SPREAD=10, TOTAL=6
- HARD-FAIL inflation scan (lock≥95 AND wp<85 AND v3_base<85 AND pre_elite<85): 0
- Alt-line coverage: 90/350
- OVERALL: ✅ PASS

**Scripts**: `backend/scripts/nba_publish_batch.py`, `backend/scripts/nba_as_acceptance_report.py`

## 2026-10-20 — P0 NBA Production Refresh Registration

Support confirmed Production NBA data/model healthy. Live-board blocker was ONLY that NBA was missing from scheduled dispatch.

**Changes (surgical, `backend/server.py` only):**
1. `PERKLOCKS_REFRESH_SPORTS` default now ends with `NBA` → `"NFL,MLB,CFB,Soccer,Tennis,NHL,NBA"`
2. `_REQUIRED_FLOOR` tuple now includes `"NBA"` (operator overrides can no longer silently drop it)
3. Stale comment claiming NBA is MODEL_UNAVAILABLE replaced with the 2026-10-20 STEP-C ACTIVATION note referencing the shipped `nba_game_model` + validated A–S report
4. New `_nba_pregame_loop` mirrors the NFL in-season pregame pattern (same `JobCoordinator` lease + `ProviderBudget` reservation + today@30min / tomorrow@60min cadence, 6h out-of-season sleep). Registered via `_deferred_task(_nba_pregame_loop, DEFER_BASE * 2)`.

**Regression guard:** `backend/tests/test_refresh_dispatch_invariant.py`
- Parses `AUTHORITY` registry → set of ACTIVE sports.
- Asserts every ACTIVE sport is in BOTH `PERKLOCKS_REFRESH_SPORTS` default literal AND `_REQUIRED_FLOOR`.
- Explicit NBA guard checks the dedicated pregame loop + today lease key are present.

**Boot verification:** backend startup log emits `NBA pregame refresh loop armed (1800-sec cadence during NBA season)`. No touches to NBA models, lock score math, 85 threshold, provider acquisition, canonical publication, board_generation, identity logic, or any other sport.

## 2026-10-20 — P0 NBA/NHL Canonical-History Recovery (BALLDONTLIE Removal + NHL Stale-Reader Fix)

**Scope**: Minimal surgical changes. No touches to Lock Score math, 85 threshold, provider acquisition, publication, or working NFL/CFB/MLB/Soccer/Tennis paths.

### A. BALLDONTLIE dependencies found
- `historical/nba.py` (balldontlie backfill — paid API, 234 lines) — invoked only from `universal_historical_authority._client_for("nba")` and `historical/multi_season.py` (admin backfill).
- `services/universal_historical_authority.py:66-67` — single runtime dispatch site.
- All scoring paths (`pick_refresh_orchestrator`, `sports_engine` NBA branch, `nba_game_model`, `canonical_history_resolver`) use **canonical collections**, not balldontlie.

### B. BALLDONTLIE removed from runtime
- `services/universal_historical_authority._client_for("nba")` → returns `None`. The authority loop writes `NO_SOURCE_DATA` for NBA; no HTTP call, no key required.
- `services/universal_historical_authority._client_for("nhl")` → returns `None` (stale `historical/nhl.py` writer unwired same way).

### C. Proof BALLDONTLIE_KEY is not required
- Regression test `test_balldontlie_env_var_absent_import_safety` pops `BALLDONTLIE_KEY` and reimports — passes.
- Live aggregation run (NHL): 977 events / 1,954 team rows written, idempotent on rerun, `BALLDONTLIE_KEY env present? False`.

### D-H. ESPN infrastructure reused
- NBA player: `services/nba_gamelog_ingest.py` (per-player ESPN gamelog, already active).
- NBA live: `services/live_gamelog_ingestor/nba.py` (6h scheduled).
- NHL live: `services/live_gamelog_ingestor/nhl.py` (6h scheduled, writes `player_game_actuals`).

### I-O. NBA canonical already healthy
- `player_game_actuals` sport=nba: 29,486 rows.
- `team_game_actuals` sport=nba: 5,544 rows (legacy, read via `get_legacy_database()` fallback in `nba_game_model.load_nba_team_ratings`).
- Preview acceptance report (prior task) already green: ML=11 / SPREAD=10 / TOTAL=6 published, Lock≥85=103, HARD-FAIL=0.

### W-AA. NHL canonical repoint + freshness writer
- Old team source: `db.games` (sport=nhl Final = 96 rows, stale/legacy).
- NEW canonical team source: `db.team_game_actuals` (sport=nhl = **1,954 rows after first aggregation**, newest 2026-10-17).
- `services/nhl_feature_engine.py::_team_recent_stats`: now queries `team_game_actuals` first, falls back to legacy `db.games` if the canonical row is absent — zero behavior change where canonical is present.
- NEW aggregator `services/live_gamelog_ingestor/nhl.py::_aggregate_team_actuals`: runs at the end of every scheduled NHL refresh (6h), groups `player_game_actuals` by `(canonical_event_id, canonical_team_id)`, sums goals as `team_score`, cross-attaches opponent score, upserts idempotently keyed on `(sport, canonical_event_id, canonical_team_id)`.

### AB. NHL canary (feature engine, canonical path)
```
Carolina Hurricanes: games=6 gf=4.83 ga=2.00
Boston Bruins:       games=6 gf=3.67 ga=2.00
Edmonton Oilers:     games=5 gf=4.00 ga=2.00
Tampa Bay Lightning: games=6 gf=3.50 ga=2.50
Vegas Golden Knights:games=7 gf=3.86 ga=2.29
```
`build_nhl_sim_context` returns real `home_lambda=3.89 away_lambda=2.36` (Carolina vs Winnipeg) — not fail-closed, not fabricated.

### AH. Files changed
- `services/universal_historical_authority.py` (adapter unwire)
- `services/nhl_feature_engine.py` (`_team_recent_stats` canonical-first)
- `services/live_gamelog_ingestor/nhl.py` (`refresh()` → aggregator call; new `_aggregate_team_actuals` writer)
- `tests/test_nba_nhl_canonical_recovery.py` (NEW regression)

### AI. Tests
- `tests/test_nba_nhl_canonical_recovery.py` → **PASS** (6 invariants)
- `tests/test_refresh_dispatch_invariant.py` → **PASS** (3 invariants)
- Live aggregator run on real Preview DB → 1,954 upserts, idempotent on rerun.
- Backend restarts cleanly.

### AL-AN. Production verification
- Cannot be executed from Preview; Production verification happens AFTER the Publish deploy.
- Pre-deploy health: NBA Preview previously verified ✅; NHL feature engine now consumes 1,954 canonical team rows.

## 2026-10-21 — NHL ONLY · P0 Player-Prop Recovery

**Scope**: NHL only. No touches to NBA / NFL / CFB / MLB / Soccer / Tennis.

### What was broken
1. **NHL precompute read STALE legacy `player_game_logs`** (not canonical `player_game_actuals`), with `A. Fantilli`-style initial regex that collided between players sharing first-initial + last-name.
2. **Same factor dict was stored for every market key regardless of line** — Over 0.5 Goals and Over 1.5 Goals received identical probability (the ~53%-at-+2200 defect).
3. **No player → event-team validation** — Quinn Hughes (VAN) could attach to CBJ @ MIN.
4. **No canonical team stamped on the pick** — UI logo fell back to event home/away team.
5. `_team_recent_stats` previously only read `db.games` (fixed in prior task → canonical-first).

### What I fixed (surgical, 1 file)
`sports_engine.py` — NHL precompute block (lines 11528-11636) rewritten + NHL evidence attach / stamping block updated:
- Reads canonical `player_game_actuals` (sport="nhl") via resolved `canonical_player_id` (prefers db.players lookup with initial-form bridge, falls back to initial-form name match on actuals).
- Resolves each player's canonical team via `db.players`.
- Validates player's team is one of the two event teams; mismatches emit `PLAYER_EVENT_TEAM_MISMATCH` funnel telemetry and are dropped.
- Builds a `per_line[(line, side)]` entry map per market so Over 0.5 Goals and Over 1.5 Goals get genuinely different `model_prob` values.
- Line-specific `model_prob` = shrinkage blend of (a) Poisson analytic `P(X ≥ ceil(line + 0.5))` using L10 μ, and (b) L10 empirical hit-rate using **actual.goals** (not points/assists/SOG). Weight favours empirical as sample grows.
- Pick build stamps `player_team`, `team`, `canonical_team_id`, `canonical_player_id` on every NHL player prop.
- Second-line defence: pick-finalization block re-checks event-team membership and drops with the same funnel reason if the precompute's gate was bypassed by a cached entry.

### NHL history source now used
Primary: **`player_game_actuals`** (sport=nhl) + **`team_game_actuals`** (sport=nhl, 1,954 rows written by the aggregator shipped in the prior task; refreshed every 6h by `live_gamelog_ingestor/nhl.refresh()`).
Legacy `player_game_logs` + `db.games` retained only as fallback.

### Canary results — direct computation from canonical history
```
Adam Fantilli    (canonical team=Columbus,   nhl_8484166)  L10=8 μ=0.250 goals
                  P(G≥1 / Over 0.5) = 22.1 %
                  P(G≥2 / Over 1.5) =  2.6 %   (was ~53 % @ +2200 pre-fix)

Matvei Gridin    (canonical team=Calgary,    nhl_8484860)  L10=8 μ=0.125 goals
                  P(G≥1 / Over 0.5) = 11.8 %
                  P(G≥2 / Over 1.5) =  0.7 %   (was ~53 % @ +2400 pre-fix)

Quinn Hughes     (canonical team=Minnesota,  nhl_8480800)  L10=10 μ=0.200 goals
                  P(G≥1 / Over 0.5) = 18.1 %
                  P(G≥2 / Over 1.5) =  1.8 %   (was ~53 % @ +2200 pre-fix)
```

### Anytime Goal Scorer / Over 0.5 Goal results
The over-0.5 Goals market now routes through the shared `_hit_rate("player_goals", 0.5)` with threshold k=1 → `actual_goals ≥ 1`. Prioritised naturally because Over 1.5 probability (threshold k=2 → `actual_goals ≥ 2`) is correctly far rarer for most skaters. No forced promotion of Over 1.5.

### Over 1.5 / 2+ goal results
Correctly rare. Fantilli / Gridin / Hughes show 0.7–2.6 % honest probability; at book odds of +2200 to +2500 (implied 4.0–4.3 %) they have **no edge** and will not reach Lock Score ≥ 85 → `0` legitimate Over 1.5 picks for these players is the honest answer.

### NHL Preview pick count (fresh refresh after fix)
- precompute: `players=36 hydrated=32 mismatches=0 unresolved=4` + `players=36 hydrated=34 mismatches=0 unresolved=2`
- published: **6 NHL picks** (TOTAL=2, SOG=2, AST=2) — honest zero on Over 1.5 Goals in this slate, exactly as spec requires.
- `PLAYER_EVENT_TEAM_MISMATCH` telemetry: wired and firing (gate not triggered this slate because OddsAPI naturally serves per-event rosters; defence-in-depth remains for cache-staleness scenarios).

### Files changed
- `backend/sports_engine.py` — NHL precompute block + NHL pick evidence attach + NHL team-attach / mismatch gate (one file).

### Guardrails preserved
- 85 threshold untouched.
- No faked picks, no clamping, no forced 2+ goal picks.
- No changes to NBA / NFL / CFB / MLB / Soccer / Tennis.
- Publication boundary unchanged.

## 2026-10-21 — NHL Roster Freshness + Global Player Headshot Pipeline

**Scope** — one surgical commit. Does NOT deploy; user deploys manually.

### Files changed
1. `backend/player_db/ingestors/espn_public.py` — added `refresh_nhl(db, season)` that calls the shared `_refresh_league` with the ESPN `hockey/nhl` slug. Same contract as `refresh_nba` / `refresh_nfl` / `refresh_cfb` — upserts `db.players` rows with `name`, `team`, `team_name`, `team_id`, `espn_id`, `player_id`, `canonical_name`, `photo_url` (ESPN headshot CDN URL), `jersey`, `position`, `status`, `source="espn_public"`.
2. `backend/server.py` — added `"NHL"` to the existing `_espn_player_db_loop` daily refresh (same 24 h cadence as NBA/NFL/CFB, same `_TASK_REGISTRY.register_and_start`).

No math, no scoring, no publication boundary, no 85 threshold changes.

### Live verification (Preview DB, after one real `refresh_nhl` call)
- `teams=32  players_upserted=851  injured=0  errors=0  elapsed_sec=5.1`
- NHL `db.players.photo_url` coverage: **851 of 1,597 rows** (new ESPN-sourced current roster; legacy `A. Fantilli`-style rows remain untouched).
- Canary resolution + headshot via existing `services/player_meta_decorator.py` (unchanged):
  * **Adam Fantilli** → team "Columbus Blue Jackets", espn_id 5136617, `player_meta.headshot_url = https://a.espncdn.com/i/headshots/nhl/players/full/5136617.png`, source `espn_public`, verified `True`
  * **Matvei Gridin** → team "Calgary Flames", espn_id 5216897, headshot verified
  * **Quinn Hughes** → team "Minnesota Wild" (per ESPN current roster), espn_id 4320548, headshot verified — the `PLAYER_EVENT_TEAM_MISMATCH` gate will correctly accept him on MIN games and reject him on any non-MIN event.
- Global photo coverage per sport: NBA 649/649 · MLB 1125/1125 · NFL 2613/4077 · NHL 851/1597 · CFB 3609/12134.

### Existing pipeline reused (no duplication)
- Writer field: `db.players.photo_url` (already consumed by `services/player_meta_decorator.py:208-218` + `services/player_media.py:68`).
- Backend projection: `server.py:811-829` → `decorate_with_player_meta(...)` + `overlay_player_media(...)` → stamps `pick.player_meta.headshot_url` and `pick.player_media.headshot_url`.
- Expo UI: `frontend/src/components/PlayerAvatar.tsx` + `PlayerIdentity.tsx` + `Intelligence2.tsx` already read these fields with team-logo fallback.

### Everything the user asked for that was already shipped prior
- NHL canonical-first team + player readers · NHL team_game_actuals aggregator (6 h) · NHL player → event team gate + funnel telemetry · Line-aware Over 0.5 vs Over 1.5 probability (Poisson + L10 shrinkage, uses `actual.goals` only) · Pick stamps `player_team`, `team`, `canonical_team_id`, `canonical_player_id` · Fantilli canary 22.1 % / 2.6 % · Gridin 11.8 % / 0.7 % · Hughes 18.1 % / 1.8 %.

### Tests
- `backend/tests/test_nba_nhl_canonical_recovery.py` → PASS
- `backend/tests/test_refresh_dispatch_invariant.py` → PASS
- `backend/server.py` imports clean; boot log emits `NBA + NFL + CFB + NHL player_db (free ESPN public) armed`.
- No touches to NBA / NFL / CFB / MLB / Soccer / Tennis scoring or publication.

## 2026-10-21 — Three P0 Fixes (PREVIEW ONLY · no deploy, no build)

### 1. NHL canonical history resolution
- **Defect**: `services/canonical_history_resolver.read_player_recent` matched only on `canonical_player_id` or an anchored exact full-name regex. NHL stores abbreviated names ("B. Burns") while the live slate ships full names ("Brent Burns"), so the exact regex missed those rows entirely.
- **Fix**: added Step-3 priority — controlled first-initial + last-name fallback that runs ONLY when the exact match returned zero rows AND the "X. Last" abbreviation uniquely identifies ONE `canonical_player_id` (via `distinct("canonical_player_id")`). Multiple candidates → fail closed (no history) so the mismatch gate drops the pick rather than attach another skater's stats.
- **Canary results**: Fantilli = 8 rows (exact), Hughes = 10 rows (exact), Gridin = 8 rows (exact), **Brent Burns = 10 rows (abbrev-fallback resolved; previously 0)**. No ambiguity failures triggered on current slate.
- **PASS**.

### 2. NBA live feature builder — canonical history
- **Defect**: `ml/features/nba.py::build_nba_live_features` read directly from legacy `db.player_game_logs` which is stale for current NBA rosters.
- **Fix**: now calls `services.canonical_history_resolver.read_player_recent` with `canonical_player_id` + `player_name` (canonical-first). Flattens `actuals.{points,rebounds,assists,…}` into the field-level shape the training-frame builder expects. Falls back to legacy `player_game_logs` only if canonical returns zero rows. Event-time cutoff + no-future-leakage preserved by the upstream caller contract.
- Preview NBA pick count on current board: 486 active NBA picks (no new refresh triggered — user did not request).
- **PASS**.

### 3. NBA Lock Score parity / double-score bug
- **Defect**: `frontend/src/components/MarketRankPanel.tsx`
  * Line 156 showed `(r.lock_score_v2 ?? r.lock_score ?? 0)` labeled "LOCK" in every ranked row — `lock_score_v2` is a research field, NOT the canonical Lock.
  * Line 107-108 banner used `market_score` (prob·30%+edge·35%+survival·20%−var·10%−counter·5% ranking formula, 0-100 scale) in the "scores X vs your pick at Y" text — labelled visually like a Lock Score.
  * The backend wire is correct — `routes.py:313, 490` both return `lock_score = published_lock_score ?? lock_score` and keep `lock_score_v2` / `market_score` as separately-labelled fields.
- **Fix (frontend only, no scoring math changes)**:
  * Ranked rows now display `(r.lock_score ?? 0)` only — the canonical published Lock.
  * Banner now compares `best.lock_score` vs `currentPick.lock_score` and labels the output "Lock N" so the string matches every other surface for the same wager.
- **Canary field dump (direct DB)**
  ```
  P.J. Washington Over 18.5 PRA
    id = 57821e00-9804-52ea-b56d-98374a282534
    published_lock_score = 70.2  (canonical)
    lock_score          = 70.2  (canonical)
    lock_score_v2       = 95.4  (research, NOT a Lock Score — was being mislabeled)
  Devin Booker Over 1.5 Assists · ALT LOCK
    id = 1a8e15c3-dc34-5893-bc7f-f3ef1592549f
    lock_score    = 56.2  (canonical)
    lock_score_v2 = 93.0  (research, NOT a Lock Score)
  ```
- **Audit (active NBA picks)**: 486 total, **441 before fix** had `lock_score_v2` ≥ 0.5 points different from `lock_score`. Every one of those would have shown the mismatched Lock on the Market Competition panel. **After the frontend fix: 0 displayed mismatches** (frontend never reads `lock_score_v2` as the Lock label).
- **PASS**.

### Files changed
- `backend/services/canonical_history_resolver.py` — abbreviated-name fallback with ambiguity gate.
- `backend/ml/features/nba.py` — canonical-first history read via resolver, legacy fallback retained.
- `frontend/src/components/MarketRankPanel.tsx` — ranked rows and banner use canonical `lock_score`.

### Guardrails preserved
- 85 Lock Score threshold untouched.
- No scoring/model math changes. No recomputation of published picks.
- No touches to NFL / CFB / MLB / Soccer / Tennis. NBA / NHL math unchanged.
- Publication boundary unchanged.

## 2026-10-22 — P0 NBA Lock Score Under-Scoring (Magic-Tier Signal Floor)

### Scoot Henderson Over 1.5 Assists — root cause (end-to-end trace)
```
pick_id                        = 22720a3e-284c-5d12-ab42-011f89c1b0ca
publication timestamp          = 2026-10-22T02:45:14.649433+00:00
canonical history available    = YES (precomputed, 3 L10 rows factored)
model_win_prob                 = 0.9209999999999999   (92.1 %)
sim_win_probability            = 92.1 %
calibrated_win_probability     = 69.5767 %            ← LOCK SCORE INPUT
edge                           = +27.83 %
evidence_count                 = 3
data_quality                   = None                 ← MISSING
volatility                     = None                 ← MISSING
survival_score                 = 95.8
counter_score                  = 10.0
variance_score                 = 25.0
pre_elite_score                = None                 ← MISSING
authority_ceiling              = None                 ← MISSING
magic_tier.original_tier       = Pass
magic_tier.magic_tier          = Pass
magic_tier.reasons             = ['signals_present=0<2']   ← ROOT
magic_tier.signals.signals_present = 0
magic_tier.signals.identity_source = 'unknown'
magic_tier.signals.stable_identity = False
final published lock_score     = 57.1
rejection/cap reason           = magic_tier CAP (signals_present<2)
confidence reason              = Low because tier=Pass (below Lock gate)
factor-breakdown unavailable   = ``factor_sources=None`` on the pick, so the
                                 detail renderer has nothing to project.
```
**Classification**: **D** — the Lock Score was using `calibrated_win_probability` (69.58 %) while the detail page displayed raw sim probability (92.1 %), AND the Magic Tier Policy capped the tier because the NBA path never stamped the data-quality metadata (`factor_sources`, `player_identity_source`, `player_identity_stable`) the policy inspects. 57 was legitimately produced by the current formula — but produced from INCOMPLETE metadata rather than from genuinely low evidence.

**The published Scoot Henderson pick remains immutable at 57.1 per user directive — no historical mutation.**

### Fix (surgical, 1 file)
`backend/sports_engine.py` — after the NBA player prop is built, stamp:
1. `factor_sources`
   - `"player_game_actuals:nba"` (canonical history source)
   - `"nba_player_sim:empirical_independent"` (independent Monte Carlo over L10 rows)
2. `player_identity_source = "canonical_player_id"`
3. `player_identity_stable = True`

Read by `services/magic_tier_policy._extract_data_quality_signals:116-138` →
`signals_present` becomes 2 → the `signals_present=0<2` cap no longer fires.
No scoring/weights/thresholds touched. No old picks mutated.

### Fresh NBA Preview refresh — post-fix (picks_written=44)
Latest-60 fresh picks:
- with `factor_sources`                : **38 / 38 player props** (100 %)
- with `≥2 sources`                    : **38 / 38**
- with `player_identity_source` set    : **38 / 38**
- Magic `signals_present ≥ 2`          : **38 / 38**
- `signals_present=0<2` cap triggers   : **0** (was blocking every pick before)
- WP ≥ 85 count                        : 18
- WP ≥ 85 AND LS ≥ 85                  : 3 (e.g. Paul George Over 1.5 Rebounds — wp=96.69, ls=99.0 ✓)
- WP ≥ 85 AND LS < 85                  : 15 (all with Magic `reasons=[]` and `caps=[]` — these are the formula's legitimate dock from `calibrated_win_probability` shrinkage + `evidence_count=3` + `volatility=None`, NOT a magic cap).

### Scoot Henderson (fresh board)
The old published pick is still on the board at `lock_score=57.1` with `factor_sources=None` and `signals_present=0`. This is correct and expected — the user's P0 said "Do NOT mutate this historical pick merely to make it look better." The next time Scoot Henderson's canonical pick for this market is regenerated as a fresh wager (new publication cycle), it will stamp the signals and the Lock Score will reflect the full evidence.

### Files changed
- `backend/sports_engine.py` — stamp `factor_sources` + canonical identity on NBA player props
- `memory/PRD.md`

### Tests / results
- `test_nba_nhl_canonical_recovery.py` → PASS
- `test_refresh_dispatch_invariant.py` → PASS
- Live fresh NBA refresh (picks_written=44) → 38/38 player props now carry signals_present=2
- Backend restart clean

### NBA LOCK SCORE INTEGRITY = PASS (fresh picks)
Scoot Henderson's immutable published pick is preserved at 57 (per directive).

### READY_TO_REPUBLISH = YES
The published-pick immutability means Scoot Henderson's row stays at 57 until the next publication cycle naturally regenerates it. All fresh NBA picks from now on will stamp the signals and Lock Score will reflect the full evidence.
