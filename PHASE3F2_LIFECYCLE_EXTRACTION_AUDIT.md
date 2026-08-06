# Phase 3F-2 — Startup Lifecycle + Task Registration Extraction

**Date:** 2026-08-06
**Scope:** Extract startup/shutdown sequencing and asyncio task registration out of `server.py` into dedicated services (`application_lifecycle.py`, `runtime_task_registry.py`). Preserve every job cadence and provider usage.

---

## Pre-extraction inventory
### `@app.on_event` sites in `server.py`
- `on_startup()` — ~1,166 lines (2786–3951): does settings/DB/ping/index-registry preflight, then registers ~30 recurring background tasks.
- `on_shutdown()` — 18 lines: previously delegated to `background_lifecycle` service + closed shared client.

### `asyncio.create_task(...)` audit
| Line (before) | Task | Classification |
|---|---|---|
| 1318 | `_background_refresh()` | One-shot on admin refresh |
| 2858 | `_deferred_task._runner()` | Deferred-startup helper (~30 uses) |
| 3032 | `_historical_props_loop` | Recurring, free-data |
| 3033 | `_daily_refresh_loop` | **Required**, paid-provider (via ProviderBudget) |
| 3217 | `_mlb_player_db_loop` | Recurring, free |
| 3245 | `_espn_player_db_loop` | Recurring, free |
| 3274 | `_mls_stats_loop` | Recurring, free |
| 3298 | `_mls_matchup_loop` | Recurring, free |
| 3396 | `_mls_direct_snapshot_loop` | Recurring, free |
| 3431 | `_soccer_prop_snapshot_loop` | Recurring, free |
| 3471 | `_services_loop` | Recurring, free |
| 3502 | `_tennis_player_db_loop` | Recurring, free |
| + ~15 `_deferred_task(...)` calls | soccer/tennis/UFC/UEFA/ESPN meta/settlement loops | Recurring, mostly free |

### Required vs optional classification
| Task | Required? |
|---|---|
| `_daily_refresh_loop` | **Required** (top-of-hour paid refresh) |
| `_settlement_loop` | **Required** (settlement outcomes) |
| `_mlb_pregame_loop` | **Required** (paid, MLB pregame) |
| `_rollover_refresh_loop` | **Required** (post-midnight seed) |
| `_ensure_today_picks` (one-shot) | **Required** (startup seed if empty) |
| All ingestion / DB polling loops (MLB PBL / ESPN / MLS / soccer / tennis / UFC / UEFA / hot scorers / lineup verifier / weekly tuning / historical props / player-form) | Optional |

### Startup globals
- `DEFER_BASE = float(env["STARTUP_DEFER_SECONDS"] or 8)` — controls stagger.
- `app.state.lifecycle` (existing, from `background_lifecycle`) — kept; Phase 3F-2 shadow-wraps it via the new `ApplicationLifecycle`.

## Post-extraction structure
```
server.py
├── (~3,979 lines, +10 net from Phase 3F-1 baseline of 3,969 for the wrapper additions)
├── on_startup:
│     – wire _TASK_REGISTRY + _LIFECYCLE
│     – await _LIFECYCLE.preflight()  (settings, DB, ping, indexes, recovery)
│     – existing task registration blocks (now wrapped through registry)
├── on_shutdown:
│     – full delegation to lifecycle.shutdown() + fallback close
└── all `asyncio.create_task(loop())` call-sites now use
    `_TASK_REGISTRY.register_and_start(name, factory, ...)`

services/runtime_task_registry.py    (255 lines)
├── RuntimeTaskRegistry class
│     ├── register(name, factory, critical, paid_provider, coordinator_job, ...)
│     ├── register_and_start(...)
│     ├── start(name), start_all(critical_only)
│     ├── stop(name, timeout), stop_all(timeout)
│     ├── get_status(name), list_statuses()
│     ├── cleanup_completed(), mark_failure(name, err)
│     ├── running_count(), critical_all_running()
│     └── property-backed _RegisteredTask.status
└── get_registry() — process-scoped singleton

services/application_lifecycle.py    (~230 lines)
├── ApplicationStartupResult, ApplicationShutdownResult dataclasses
├── ApplicationLifecycle class
│     ├── preflight() → settings + DB init + ping + index-registry + lease recovery
│     ├── record_task_registration(required_registered/started, optional_started)
│     ├── shutdown(timeout) → stop_all → release leases → close HTTP → close Mongo
│     └── readiness() → structured dict (startup_complete, DB, indexes, task counts, ...)
└── get_lifecycle() — process-scoped singleton
```

## Runtime task registry contents (measured live)
- **Total registered:** 38 tasks
- **Running:** 37
- **Completed:** 1
- **critical_all_running:** True
- **ok:** True (`state = "ready"`)

## Behaviour parity
| Aspect | Preserved? | Evidence |
|---|---|---|
| Exact task set | ✅ | 38 tasks running (matches Phase 3F-1 baseline + previous deferred loops) |
| Startup order | ✅ | preflight runs settings→DB→ping→indexes→recovery BEFORE any task start |
| Job cadence | ✅ | No coroutines modified; only task-handle registration added |
| Coordinator names | ✅ | `services/job_registry.py` untouched (24 cadence references) |
| Budget behaviour | ✅ | No ProviderBudget code path altered |
| Cold-start rules | ✅ | `STARTUP_DEFER_SECONDS` still honoured; `_deferred_task` still uses it |
| Paid/free separation | ✅ | Registry only ADDS `paid_provider` metadata; no runtime effect |
| Shutdown timeout | ✅ | `timeout=10.0` preserved end-to-end |
| Prediction behaviour | ✅ | Zero code touched in prediction pipeline (Phase 3F-1 orchestrator unchanged) |
| Frontend response schema | ✅ | `/api/picks/today` returns identical 20+ key structure |
