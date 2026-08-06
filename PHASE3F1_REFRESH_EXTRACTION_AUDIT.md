# Phase 3F-1 — Pick Refresh Extraction Audit

**Date:** 2026-08-06
**Scope:** Move `_refresh_picks` and its private helpers out of `server.py` into `services/pick_refresh_orchestrator.py`, wrap it with a typed request/result contract, and keep a thin compatibility wrapper in `server.py`.

---

## Pre-extraction inventory

### `_refresh_picks` callers
| File | Line | Context |
|---|---|---|
| `server.py:2974` | `_ensure_today_picks` comment reference | Documentation only |
| `server.py:3007` | `_ensure_today_picks` → `await _refresh_picks(today)` | Startup seed if slate is empty |
| `server.py:3950` | Rollover midnight loop → `await _refresh_picks(current_date)` | Every 5-min tick after UTC midnight |
| `server.py:4031` | Daily refresh loop → `await _refresh_picks(current_date)` | Hourly forced refresh |
| `server.py:4127` | MLB pregame → `await _refresh_picks(_today_str(), sport_filter="MLB")` | 5-min MLB refresh 12-24h UTC |
| `server.py:4172` | Tomorrow-slate MLB → `await _refresh_picks(tomorrow_str, sport_filter="MLB")` | Pre-midnight seed |
| `server.py:4770` | Admin refresh route thread → `return await _refresh_picks(_today_str(), sport_filter="MLB")` | Manual admin refresh |
| `routes/admin_routes.py:693-698` | `from server import _refresh_picks; await _refresh_picks(today_str)` | Admin manual refresh |
| `routes/admin_routes.py:767-768` | `from server import _refresh_picks; asyncio.create_task(_refresh_picks(today_str))` | Fire-and-forget admin refresh |

All 9 production callsites resolve to the compatibility wrapper. No callsite requires direct orchestrator access in Phase 3F-1.

### Test-only references
| File | Line | Nature |
|---|---|---|
| `tests/test_iter100_fusion_wiring.py:309` | Docstring reference | Guardrail — updated to search orchestrator |
| `tests/test_iter119_phase2b.py:560` | Guardrail — normal-user endpoint must NOT reference `_refresh_picks` | Still applies unchanged |
| `tests/test_iter123_phase2_final.py:94,136` | Docstring reference | Documentation only |
| `tests/test_lockscore_api.py:238` | HTTP-level admin refresh test | Unchanged (routes through admin route) |

### Private helpers called only by `_refresh_picks`
| Helper | Line (old) | Callers |
|---|---|---|
| `_dedupe_and_limit_goalscorers` | 1131 | Only `_refresh_picks` in this file. Comment reference in `elite_players.py:672` |
| `_cap_tennis_totals` | 1251 | Only `_refresh_picks` |
| `_reconcile_player_prop_contradictions` | 2515 | Only `_refresh_picks` + test_iter83 |
| `_ensure_csl_elite_picks` | 2758 | Only `_refresh_picks` |
| `_shadow_capture_gs_v2` | 2856 | Only `_refresh_picks` |

### Shared helpers used by `_refresh_picks` AND by other functions in `server.py`
| Helper | Also used by | Handling |
|---|---|---|
| `_prop_family_key` | `_reconcile_player_prop_contradictions` (moved) + `test_iter85` | Moved to orchestrator; re-exported from `server.py` |
| `_atomic_mark_no_bet` | `_reconcile_player_prop_contradictions` (moved) + `_enforce_no_bet_schema_invariant` (kept in server) + `test_iter88` | Moved to orchestrator; re-exported from `server.py`; `_enforce_no_bet_schema_invariant` imports from `server.py` (which re-exports from orchestrator) |

### Global state read/modified by `_refresh_picks`
- `db` — module-level `AsyncIOMotorDatabase`. Now imported from `deps` in the orchestrator, wrapped in a `_DBProxy` so test overrides via `server.db = <fresh_client>` continue to work.
- `logger` — module-level logger. Imported from `deps`.

### Global state used only inside `_refresh_picks` (module-level constants / imports)
- `Optional`, `uuid`, `datetime`, `timezone` — standard library, imported at top of orchestrator.
- `generate_all_picks` from `sports_engine` — imported at top of orchestrator.
- Every other symbol is imported lazily inside the pipeline body (e.g. `from tennis_extra import fetch_extra_tennis_picks`).

### Symbols other modules import from `server.py`
| Module | Symbol | Kind |
|---|---|---|
| `analytics.py` (comment only) | `_refresh_picks` | Docstring reference |
| `tennis_engine.py` (comment only) | `_refresh_picks` | Docstring reference |
| `services/mls_direct_inject.py` (comment only) | `_refresh_picks` | Docstring reference |
| `services/job_registry.py` | `"entrypoint": "server:_refresh_picks"` | String literal; wrapper preserves the symbol |
| `routes/admin_routes.py:693,767` | `from server import _refresh_picks` | Actual import; wrapper preserves signature |

### Circular-import risks
- `pick_refresh_orchestrator.py` imports from `deps`, `sports_engine`, and inline imports for stage collaborators.  It does NOT import `server`.
- `server.py` imports from `pick_refresh_orchestrator.py` at module load.
- `_DBProxy._resolve()` uses a LAZY `import server` inside its resolver method — this executes at call time, after `server` has finished importing.  No circular-import risk at bootstrap.

### Startup / scheduler / admin call chains
| Caller | Path | Concurrency guard | Unchanged? |
|---|---|---|---|
| Scheduler → `_daily_refresh_loop` (server.py) | `_refresh_picks` (wrapper) → orchestrator | JobCoordinator lease + ProviderBudget reservation (obtained in the scheduler tick BEFORE the orchestrator call) | ✅ |
| Scheduler → `_mlb_pregame_loop` | `_refresh_picks(sport_filter="MLB")` (wrapper) → orchestrator | Same | ✅ |
| Scheduler → `_rollover_refresh_loop` | `_refresh_picks(current_date)` (wrapper) → orchestrator | Same | ✅ |
| Admin → `POST /api/admin/refresh` | `_refresh_picks(today_str)` (wrapper) → orchestrator | Route-level admin auth + JobCoordinator lease | ✅ |
| Startup → `_ensure_today_picks` | `_refresh_picks(today)` (wrapper) → orchestrator | Single-shot with `is_seeding` guard | ✅ |

### Test monkeypatches
- `tests/test_iter83_*` patches `server._reconcile_player_prop_contradictions`, `server.db` — both still work post-3F-1 because we re-export the function and the `_DBProxy` honours `server.db` overrides.
- `tests/test_iter88_*` patches `server._atomic_mark_no_bet`, `server.db` — both still work for the same reasons.
- `tests/test_iter119_phase2b.py` asserts normal-user `/api/picks/refresh` body does NOT contain `_refresh_picks` — still passes because the refactor did not touch that route.

## Dependency classification
| Dependency | Classification | Notes |
|---|---|---|
| Shared `db` (Motor) | Shared database dependency | Provided via `_DBProxy` → Phase 3B shared owner or `server.db` test override |
| `sports_engine.generate_all_picks` | Injected function | Import-time, no cycle |
| `services.prediction_publication_service.PredictionPublicationService` | Injected service | Kept — publication ownership unchanged |
| `board_validator.validate_and_finalize` | Injected function | Kept — validation ownership unchanged |
| `services.pick_fusion_decorator.enrich_picks_bulk` | Injected function | Kept — enrichment ownership unchanged |
| `logger` | Configuration | From `deps` |
| `date_str: str`, `sport_filter: Optional[str]` | Input parameters | Unchanged |
| `caller`, `reason`, `job_name`, etc. | NEW input parameters | Added by request contract; wrapper supplies defaults for legacy callers |
| `JobCoordinator`, `ProviderBudget` | Deferred responsibility | NOT owned by orchestrator; caller must lease + reserve before invoking |
| `_prop_family_key`, `_atomic_mark_no_bet` | Compatibility dependency | Moved to orchestrator; re-exported from `server.py` for tests |
| `_enforce_no_bet_schema_invariant` | Compatibility dependency | Stays in `server.py`; imports moved helpers via re-export |

## Post-extraction structure
```
server.py
├── (~3,969 lines — down from 5,669)
├── existing routes, startup, background loops
├── from services.pick_refresh_orchestrator import (
│       PickRefreshOrchestrator, PickRefreshRequest, PickRefreshResult,
│       _dedupe_and_limit_goalscorers, _cap_tennis_totals,
│       _prop_family_key, _atomic_mark_no_bet,
│       _reconcile_player_prop_contradictions,
│       _ensure_csl_elite_picks, _shadow_capture_gs_v2,
│   )
├── async def _refresh_picks(date_str, sport_filter=None) -> int:   # THIN WRAPPER
│       orchestrator = PickRefreshOrchestrator()
│       result = await orchestrator.refresh(PickRefreshRequest(...))
│       return int(result.published_count or 0)
└── async def _enforce_no_bet_schema_invariant() -> dict: ...       # kept

services/pick_refresh_orchestrator.py
├── (~1,926 lines)
├── PickRefreshRequest, PickRefreshResult dataclasses
├── PickRefreshOrchestrator class with .refresh(request)
├── _DBProxy — late-binding db handle honouring server.db overrides
├── _dedupe_and_limit_goalscorers
├── _cap_tennis_totals
├── _refresh_picks — the ~1,000-line pipeline body (verbatim from server.py)
├── _pipeline_run — thin wrapper for the orchestrator to invoke
├── _prop_family_key, _atomic_mark_no_bet
├── _reconcile_player_prop_contradictions
├── _ensure_csl_elite_picks
└── _shadow_capture_gs_v2
```

## Behaviour-parity checklist
| Aspect | Preserved? | Verification |
|---|---|---|
| Exact pick-generation order | ✅ | `test_generation_stage_order_preserved` — 31 markers in fixed order |
| Sport filter | ✅ | `PickRefreshRequest.sport_filter` passed through unchanged |
| Validation order | ✅ | `test_validation_before_persistence` |
| Publication behaviour | ✅ | `test_publication_service_still_wired` |
| Snapshot behaviour | ✅ | Phase 1b regression suite passes |
| Board counts | ✅ | Result carries the pipeline's int return |
| Error/fallback behaviour | ✅ | Every try/except moved verbatim; result captures errors |
| Caller/reason metadata | ✅ | Added at contract layer; wrapper supplies defaults |
| Frontend response schema | ✅ | `/api/picks/today` returns 20 picks with identical keys |

No formulas, thresholds, ordering, or flag names were changed by this move.
