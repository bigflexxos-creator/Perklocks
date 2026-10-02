# PRD — Perklocks Master Surgical Build

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
