# Phase 2 Final Report — Scheduler & API Optimization

**Status:** Phase 2 COMPLETE.  Ready for Phase 3.
**Predecessor:** Phase 1 (Immutable prediction pipeline + snapshot layer).
**Successor:** Phase 3 (Prediction quality — models, calibration, learning).

---

## Phase 2 milestones

| Phase | Deliverable | Status |
|---|---|---|
| **2α** | Scheduler & API audit (`PHASE2_BASELINE_REPORT.md`, `PHASE2_JOB_REGISTRY.md`, `PHASE2_API_CALL_INVENTORY.md`) | DONE |
| **2β** | JobCoordinator + ProviderBudget foundation + shadow instrumentation + hardened admin force-refresh + admin observability routes | DONE |
| **2γ** | OddsApiGateway cutover + distributed single-flight + tournament + bad-market registries + `run_immediately=True` removed + MLB pregame consolidation + normal-user refresh made DB-only + `_fetch_event_odds_individual` hard-removed | DONE |
| **2γ closeout** | Top-up accounting + `/sports` snapshot-scoped reuse + MLB late-night boot refresh routed through cold_start + Global refresh gated on `ODDS_GLOBAL_REFRESH_MODE=snapshot` (default) + corrected MLB savings math + `sports_engine._real_upstream_get` DELETED | DONE |
| **2δ** | Cache policy centralized + settlement scoping + BackgroundLifecycle (startup lease recovery + graceful shutdown) + admin `/health`, `/cache/policy`, `/settlement/scope`, `/lifecycle/status` endpoints | DONE |

## Cross-phase invariants preserved

- ✅ Phase 1 immutable prediction snapshots — verified by `test_iter117_phase1b` (13 tests) + `test_22_prediction_snapshots_are_not_mutated` in every subsequent test file.
- ✅ No prediction / scoring / market-selection / Magic Tier / Lock Score / H+R+RBI change.
- ✅ No UI / frontend response schema change (aside from additive `db_only:true` marker on `/picks/refresh` from Phase 2β).
- ✅ Every paid Odds API caller flows through `services.odds_api_gateway.OddsApiGateway`.

## Architecture at end of Phase 2

```
┌──────────────────────────────────────────────────────────────────────┐
│                     Every paid Odds API request                       │
│                                                                        │
│  Callers                       OddsApiGateway                Storage   │
│  ─────────                     ──────────────                ───────   │
│  scheduled snapshot loops ─┐                                          │
│  MLB pregame loop         ─┤                                          │
│  admin force-refresh      ─┼──► OddsApiGateway.fetch(...)             │
│  cold-start recovery      ─┤       │                                  │
│  sports_engine fallback   ─┘       ▼                                  │
│                              1. bad-market filter                     │
│                              2. build request_key                     │
│                              3. tournament registry                   │
│                              4. SingleFlight.acquire                  │
│                              5. ProviderBudget.reserve                │
│                              6. Circuit-breaker guard                 │
│                              7. httpx GET (ONLY here)                 │
│                              8. actual-cost from headers              │
│                              9. odds_cache._persist_cache_row         │
│                              10. ProviderBudget.top_up (if over)      │
│                              11. ProviderBudget.commit                │
│                              12. SingleFlight.complete                │
│                              13. odds_api_request_log write           │
│                              14. sports_engine CB state callback     │
│                                                                        │
│                                    ▲                                  │
│                                    │                                  │
│                     JobCoordinator (Mongo scheduled_jobs)             │
│                     BackgroundLifecycle (startup recovery + shutdown) │
│                                                                        │
└──────────────────────────────────────────────────────────────────────┘
```

## Observability surface (`/api/admin/ops/*`)

- `GET /jobs`, `/jobs/leases/active`, `/jobs/leases/expired`, `/jobs/executions`, `/jobs/registry`
- `GET /budget/status`, `/budget/daily`, `/budget/monthly`, `/budget/blocked`, `/budget/reservations/active`, `/budget/reconcile`
- `GET /shadow/decisions`, `/audit`
- `GET /cache/policy` (2δ)
- `GET /settlement/scope` (2δ)
- `GET /lifecycle/status` (2δ)
- `GET /health` (2δ — one-stop dashboard)
- `POST /jobs/leases/recover`, `/budget/reservations/sweep`

Every route requires `Depends(current_admin)`.  Raw lease tokens are NEVER returned — only SHA-256 hashes.

## Feature flags in production

| Env var | Default | Purpose |
|---|---|---|
| `ODDS_GATEWAY_ENABLED` | `true` | Toggle the gateway transport path. Budget + coordinator remain active in both modes. |
| `ODDS_GLOBAL_REFRESH_MODE` | `snapshot` | Snapshot mode disables the hourly global refresh; `legacy_hourly` is a temporary emergency rollback. |
| `ODDS_DAILY_CREDIT_LIMIT` | `3000` | Per-UTC-day cap. |
| `ODDS_MONTHLY_CREDIT_LIMIT` | `100000` | Per-month hard ceiling. |
| `ODDS_EMERGENCY_RESERVE` | `10000` | Emergency-only capacity carved out of the monthly cap. |

All variable names are documented in `backend/.env.example` (variable names only — no values committed).

## Metrics inputs

- **Phase 2α baseline:** 1,988 upstream/day, 3,270 credits/day, 55.4 % cache-hit, 617 dup/1 min, 98,100 monthly.
- **Phase 2γ 24-h measurement:** started `2026-08-06T14:22:38Z`, closes `2026-08-07T14:22:38Z`.
- **Final measurement script:** `backend/scripts/phase2gamma_24h_report.py`.
- **Final decision gate** (per user directive): measurement PASS → open Phase 3; measurement FAIL → reopen 2γ optimization, do NOT begin Phase 3.

## Rollback

See:
- `/app/PHASE2GAMMA_ROLLBACK.md` — matrix by symptom + feature-flag flips + hard branch rollback.
- `/app/PHASE2GAMMA_DECISION_GATE.md` — direct-fallback removal proof and post-window run instructions.
- `/app/PHASE2DELTA_DELIVERABLES.md` §10 — Phase 2δ-specific rollback.

## Confirmation

**Phase 2 is COMPLETE.**

- ✅ Scheduler + API optimization foundation, cutover, closeout, and hardening are all done.
- ✅ Every paid Odds API caller migrated to `OddsApiGateway`.
- ✅ All infrastructure operations (budget, leases, snapshots, cache, settlement scope, lifecycle) are observable via admin endpoints.
- ✅ Rolling deployment safe — startup recovery + atomic single-flight guarantee no duplicate paid work.
- ✅ 114 tests pass across Phase 1 + 2β + 2γ + 2δ.  No prediction, scoring, or market-selection code changed.

**Ready for Phase 3** — prediction-quality work (models, calibration, learning).  Do NOT start Phase 3 until the 24-h measurement window closes and the final Phase 2γ report shows daily credits ≤ 3,000 and monthly projection < 100,000.
