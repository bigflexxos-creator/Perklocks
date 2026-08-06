# Phase 2 Job Registry — Pre-Change Inventory

**Snapshot taken:** 2026-08-06.  Every recurring / startup / admin-triggered job that runs in the backend process.

**Convention:** ✅ = already coordinated / low risk · ⚠️ = needs coordinator lease · ❌ = current uncontrolled path

## 1. Continuous asyncio loops (`asyncio.sleep(N)` inside `while True:`)

| Job name | File | Entrypoint | Cadence | run_immediately | Paid providers | Free providers | Max calls/exec | Distributed lease? | Duplication risk |
|---|---|---|---:|:-:|---|---|---:|:-:|:-:|
| `_daily_refresh_loop` | `server.py:3906` | `_daily_refresh_loop()` | ~60 min during game window | ❌ (delayed start) | The Odds API | ESPN, MLB Stats | ~50-80 credits | ❌ | ⚠️ |
| `_mlb_pregame_loop` | `server.py:3964` | `_mlb_pregame_loop()` | **5 min** | ❌ | The Odds API | MLB Stats | 20-60 credits | ❌ | ⚠️ |
| `_mlb_late_night_boot_refresh` | `server.py:4505` | via `_deferred_task` | once at boot | ✅ implicit | The Odds API | — | ~30 credits | ❌ | ⚠️ |
| `_settlement_loop` | `server.py:4184` | `_settlement_loop()` | ~60 s | — | (rare Odds API scores) | ESPN, MLB Stats, MLS API | 0-10 credits | ❌ | ⚠️ |
| `_grading_validator_loop` | `server.py` | — | 15 min | — | — | ESPN | 0 | ❌ | ⚠️ |
| `_stuck_pick_reaper` | `server.py` | — | 60 min | — | — | — | 0 | ❌ | ⚠️ |
| `_weekly_model_tuning_loop` | `server.py:4513` | `_weekly_model_tuning_loop` | 7 days | — | — | — | 0 | ❌ | low |
| `_line_observer_loop` | `server.py` | — | ~90 s pre-game | — | The Odds API (via cache) | — | ~20 credits/pass | ❌ | ⚠️ |
| `_closing_snapshotter` | `closing_line_snapshotter.py` | — | at kick-off | — | The Odds API | — | ~5 credits/event | ❌ | ⚠️ |
| `_steam_detector_loop` | `steam_detector.py` | — | 10 min pre-game | — | — | (reads cached lines) | 0 | ❌ | ⚠️ |
| `_espn_soccer_loop` | via `_deferred_task` | | ~90 min | — | — | ESPN | 0 | ❌ | ⚠️ |
| `soccer_pipeline_loop` | `soccer/pipeline.py` | | ~30 min | — | — | Football-Data, Understat | 0 | ❌ | ⚠️ |
| `soccer_backfill_loop` | | | ~24 h | — | — | Football-Data | 0 | ❌ | low |
| `uefa_espn_loop` | `uefa_espn_ingest.py` | | ~60 min | — | — | ESPN | 0 | ❌ | ⚠️ |
| `ufc_espn_loop` | `ufc_espn_ingest.py` | | ~60 min | — | — | ESPN | 0 | ❌ | ⚠️ |
| `hot_scorers_loop` | `soccer_hot_scorers.py` | | ~60 min | — | — | ESPN, Wiki | 0 | ❌ | ⚠️ |
| `_espn_meta_loop` | `server.py:4585` | | 6 h | — | — | ESPN | 0 | ❌ | low |
| `soccer_player_form_loop` | `soccer_player_form.py` | | ~30 min | — | — | Understat | 0 | ❌ | low |
| `lineup_verifier_loop` | `mlb_lineup.py` | | ~10 min | — | — | MLB Stats | 0 | ❌ | ⚠️ |
| `nrfi_yrfi_loop` | `brain/nrfi_engine.py` | | 90 min pregame / 3 h off | — | The Odds API | MLB Stats | ~100 credits/day | ❌ | ⚠️ |
| `_mlb_player_db_loop` | `server.py:4639` | | 24 h | — | — | MLB Stats | 0 | ❌ | low |
| `_espn_player_db_loop` | `server.py:4666` | | 24 h | — | — | ESPN | 0 | ❌ | low |
| `_mls_stats_loop` | `server.py:4696` | | 12 h | — | — | ESPN | 0 | ❌ | low |
| `_mls_matchup_loop` | `server.py:4720` | | 7 days | — | — | ESPN | 0 | ❌ | low |
| `_background_refresh` | `server.py:3017` | | one-shot @ startup | ✅ | — | — | 0 | ❌ | low |
| `_historical_props_loop` | `server.py:4466` | | 24 h | — | — | Football-Data | 0 | ❌ | low |

## 2. Scheduled snapshot loops (via `services/scheduled_snapshot.py`)

| Job | File | Cadence | `run_immediately` | Paid providers | Max calls/exec |
|---|---|---:|:-:|---|---:|
| `alt_lines_feed_snapshot` | `server.py:4745` | 12:00 / 18:00 / 23:00 UTC | ✅ **YES** | The Odds API | ~500-1,000 credits |
| `mls_direct_inject_snapshot` | `server.py:4753` | 12:00 / 18:00 / 23:00 UTC | ✅ **YES** | The Odds API | ~50 credits |
| `soccer_prop_inject_snapshot` | `server.py:4779` | 12:00 / 18:00 / 23:00 UTC | ✅ **YES** | The Odds API | ~100 credits |

**Every restart fires all 3 immediately → estimated ~1,500 credits per restart.** Phase 2γ target: replace `run_immediately=True` with "run only if snapshot missing OR beyond max staleness".

## 3. User-callable routes that trigger global work ❌

| Route | Method | File:line | Direct call | Auth |
|---|---|---|---|---|
| `/api/picks/force-refresh` | POST | `routes/picks_routes.py:2543` | `asyncio.create_task(_refresh_picks(_today_str()))` | ⚠️ user-callable |

**Highest-risk uncontrolled entrypoint.** Any user hitting this endpoint kicks off a full ~45s pipeline that pulls paid odds. Phase 2γ must route this through the coordinator + budget check.

## 4. Admin-callable routes that trigger global work ⚠️

| Route | Method | File:line |
|---|---|---|
| `/api/admin/picks/force-refresh` | POST | `routes/admin_routes.py:590` |
| `/api/admin/refresh-soccer-player-form` | POST | `admin_routes.py:81` |
| `/api/admin/backfill-tennis-elo` | POST | `admin_routes.py:167` |
| `/api/admin/historical/backfill` | POST | `admin_routes.py:191` |
| `/api/admin/historical/backfill-seasons` | POST | `admin_routes.py:260` |
| `/api/admin/rollover/backfill-tags` | POST | `admin_routes.py:668` |
| `/api/admin/scorer-backfill` | POST | `admin_routes.py:780` |
| `/api/admin/csl-espn-refresh` | POST | `admin_routes.py:882` |
| `/api/admin/services-nba-refresh` | POST | `admin_routes.py:929` |
| `/api/admin/services-nfl-refresh` | POST | `admin_routes.py:940` |
| `/api/admin/uefa-espn-refresh` | POST | `admin_routes.py:949` |
| `/api/admin/ufc-espn-refresh` | POST | `admin_routes.py:965` |
| `/api/admin/espn-team-meta-refresh` | POST | `admin_routes.py:975` |
| `/api/admin/espn-injury-refresh` | POST | `admin_routes.py:986` |
| `/api/admin/espn-form-refresh` | POST | `admin_routes.py:997` |
| `/api/admin/wiki-record-refresh` | POST | `admin_routes.py:1008` |
| `/api/admin/wiki-top-scorers-refresh` | POST | `admin_routes.py:1021` |
| `/api/admin/soccer-hot-scorers-refresh` | POST | `admin_routes.py:1033` |
| `/api/admin/services-soccer-refresh` | POST | `admin_routes.py:1119` |
| `/api/admin/services-cfb-refresh` | POST | `admin_routes.py:1132` |

**Every admin refresh currently:** fires immediately, no lease, no budget check, no idempotency. Phase 2γ target: route all through `JobCoordinator.enqueue()`.

## 5. Startup burst (all `run_immediately` + `_deferred_task` fires within first 10 min)

- **3 snapshot jobs fire immediately** (`run_immediately=True`): alt_lines + mls_direct + soccer_prop
- **26+ deferred tasks** queued via `_deferred_task` with `DEFER_BASE * N` seconds staggering
- Estimated startup credit burst: **~1,500-1,800 credits** every time the backend restarts

## 6. Process-local locks / in-memory state that should be distributed

- `services/odds_cache.py` — single-flight uses `asyncio.Lock` — **process-local only**. If we split into worker + API processes, both can duplicate.
- Alt-lines feed uses no cross-process coordination for the events discovery step
- No shared "next-eligible-at" or "last-completed-at" state anywhere in the codebase

**Phase 2β target:** move all of this into the `scheduled_jobs` Mongo collection.

## 7. Summary — Phase 2β/2γ enforcement targets

**Every row marked ❌ or ⚠️ must be brought under the coordinator by end of Phase 2γ.** Priorities:

1. **P0:** `/api/picks/force-refresh` (user-callable) — Phase 2γ must gate it.
2. **P0:** Kill `run_immediately=True` on the 3 snapshot jobs — Phase 2γ replaces with "run only if snapshot missing / critically stale".
3. **P0:** `_mlb_pregame_loop` 5-min cadence + today+tomorrow regeneration — Phase 2γ consolidates.
4. **P1:** All 20 `/api/admin/*-refresh` endpoints — Phase 2γ routes through coordinator.
5. **P1:** `nrfi_yrfi_loop` uses direct paid path — Phase 2γ routes through gateway.
6. **P2:** Every free-source loop (ESPN, MLB Stats, Understat, etc.) also needs a lease so worker-split doesn't duplicate work.
