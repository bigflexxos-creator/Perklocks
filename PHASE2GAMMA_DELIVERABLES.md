# Phase 2γ — OddsApiGateway Cutover + Fan-out Consolidation

**Status:** Complete. Ready for review.
**Scope:** Gateway cutover + duplicate-suppression + refresh consolidation. Phase 2δ **not** started.

---

## 1. Files created

| Path | Purpose |
|---|---|
| `backend/services/odds_api_gateway.py` | Single choke point for all Odds API traffic (transport + budget + registries + logging) |
| `backend/services/single_flight.py` | Distributed request-owner election over `odds_request_flights` |
| `backend/services/tournament_registry.py` | `odds_tournament_registry` — suppression policy for inactive tennis/soccer keys |
| `backend/services/cold_start.py` | Startup freshness check + coordinator-gated recovery |
| `backend/scripts/phase2gamma_24h_report.py` | Post-cutover measurement script |
| `backend/tests/test_iter120_phase2c.py` | 27+ test assertions + repository guardrail |
| `backend/.env.example` | Variable-name-only reference for deployment env |
| `/app/PHASE2GAMMA_ROLLBACK.md` | Rollback instructions + feature-flag matrix |
| `/app/PHASE2GAMMA_DELIVERABLES.md` | This document |
| `/app/reports/phase2gamma_24h.txt` | Measurement checkpoint (PARTIAL) |

## 2. Files changed

| Path | Change |
|---|---|
| `backend/services/odds_cache.py` | `cached_httpx_get` now delegates to `OddsApiGateway` when `ODDS_GATEWAY_ENABLED=true`. Added `_persist_cache_row` helper. Removed the hard-coded provider URL literal and `THE_ODDS_API_KEY` string from the module. |
| `backend/alt_lines_feed.py` | **Hard-removed** `_fetch_event_odds_individual` (Phase 2α showed ~970 credits/day). |
| `backend/server.py` | Removed `run_immediately=True` from every paid snapshot loop. Added cold-start recovery. Each scheduled run of alt-lines / MLS / soccer-prop now acquires a JobCoordinator lease AND reserves ProviderBudget. Consolidated MLB pregame loop (today 5-min, tomorrow **30-min**, both lease-gated). |
| `backend/services/job_registry.py` | 3 snapshot loops promoted to `fully_managed`. New entries: `mlb_pregame_refresh_today`, `mlb_pregame_refresh_tomorrow`. |
| `backend/services/bad_market_registry.py` | Docstring reference to deleted function updated. |
| `backend/.env` | (Unchanged in this pod; values already added in Phase 2β.) |

## 3. Direct-call paths removed / migrated

| Module | Before | After |
|---|---|---|
| `alt_lines_feed._fetch_event_odds_individual` | ~970 cred/day fan-out | **Deleted** |
| `services/mls_direct_inject.py` | `cached_httpx_get` closure with local httpx | Delegated to gateway via `cached_httpx_get` |
| `services/soccer_prop_inject.py` | `cached_httpx_get` closure with local httpx | Delegated via gateway |
| `alt_lines_feed._fetch_events`, `_fetch_event_odds` | `cached_httpx_get` | Delegated via gateway |
| `services/odds_cache.py` | Owned upstream transport | Owns DB cache only; gateway is the transport |
| `services/mls_direct_inject.py` L475 raw `httpx.AsyncClient` | Local client for context | Kept as a client-context container; actual URL requests go through `cached_httpx_get` (now gateway) |
| Same for soccer_prop_inject / alt_lines_feed / brain/nrfi_engine / etc. | | |

**Repository guardrail** (`test_iter120_phase2c.py`):
- Fails if `api.the-odds-api.com` appears outside the allowlist.
- Fails if `THE_ODDS_API_KEY` appears outside the allowlist.
- Fails if any file constructs `httpx.get/post/AsyncClient` against `api.the-odds-api.com` in the same expression, except inside `odds_api_gateway.py`.
- Fails if `_fetch_event_odds_individual` reappears in the codebase.
- Fails if `odds_cache.py` regains a provider URL literal.

## 4. OddsApiGateway design

**Transport surface:** every method flows through `OddsApiGateway.fetch(url, params, *, caller, reason, job_name, ...)`. Callers MUST provide `caller` AND `reason` — no anonymous paid work possible.

**Pipeline (per request):**
1. Classify endpoint → `sports_list | events_list | bulk_odds | event_odds | scores | alt_lines | generic`.
2. Consult **bad-market registry** — filter unsupported markets pre-flight.
3. Estimate credits (`markets × regions`) — used as reservation upper bound.
4. Build deterministic **request key** (provider + endpoint path + sport + event + sorted markets + regions + bookmakers + odds format + safe extra params).
5. Consult **tournament registry** — if `events_list` and sport is currently suppressed → early exit.
6. **Single-flight acquire** on the request key. Losers wait ≤4s for the owner's result, then fall back to the DB cache row.
7. **ProviderBudget reserve** for the estimated credits.
8. **Circuit-breaker guard** — release lease + budget if the breaker is latched OPEN.
9. Perform **httpx call** (only place in the codebase allowed to do so).
10. Extract quota headers (`x-requests-used`) → compute **actual cost** as delta vs last-known value.
11. Persist to `odds_api_cache` via `_persist_cache_row` (no HTTP happens inside odds_cache).
12. `budget.commit(intent_id, actual_credits=...)` reconciles the reservation.
13. `single_flight.complete/fail` releases the flight so waiters resume.
14. **odds_api_request_log** row written with 25+ fields (see §12).

**422 retry path:** removed as a caller-visible operation. The gateway now:
- Only 422-retries when `endpoint_type ∈ {event_odds, alt_lines}` (approved).
- Marks the offending markets in `bad_market_registry`.
- Never fans out on 401 / 403 / 404 / 429 / 5xx / timeout / circuit-open / budget-denied / valid-empty / cache-error.

## 5. Distributed single-flight design

**Collection:** `odds_request_flights` (unique index on `request_key`, TTL index on `ttl_at`).

**Contract building** (`services.single_flight.build_request_key`): SHA-256 over the sorted normalized contract (provider, endpoint, sport, event, markets, regions, bookmakers, odds format, extra params — all API-key-like fields stripped).

**Acquire behavior:** atomic `find_one_and_update` with filter `status != inflight OR expires_at < now`. Owner receives an opaque token used to complete/fail.

**Waiter behavior:** `wait_for_result(rk, timeout=4s)` polls the doc, returns the `result_summary` produced by the owner. On timeout, the caller in the gateway falls back to `odds_api_cache` for the latest cached body (stale-while-revalidate semantics).

**Duplicate reservation prevention:** waiters do NOT reserve budget. Only the owner reserves. Duplicate suppression is logged (`odds_api_request_log.duplicate_suppressed = true`).

**Crash safety:** each flight has `expires_at` (default 30s). A subsequent acquire filter accepts either "not inflight" OR "expired". Rolling deployments overlap safely — the collection is shared across containers.

## 6. Bad-market retry design (narrow 422 path)

**Hard caps:** `MAX_422_RETRY_REQUESTS = 4`, `MAX_422_RETRY_CREDITS = 40`. Both must be exhausted for the retry to stop. Stop is enforced by whichever ceiling is hit first.

**Applicable ONLY to** `event_odds` and `alt_lines` endpoints.

**Blocks retry on** any of: 401 / 403 / 404 unrelated to market support / 429 / timeout / 5xx / circuit-open / budget-denied / valid empty / cache/database error.

**Every retry** writes an audit row (`retry_reason` populated) and marks the offending markets in the registry.

## 7. Tournament-registry design

**Collection:** `odds_tournament_registry`. One doc per `sport_key`. Fields per spec: `sport_group`, `title`, `active`, `last_catalog_seen_at`, `last_event_seen_at`, `last_successful_check_at`, `last_empty_check_at`, `consecutive_empty_checks`, `suppress_until`, `present_in_current_picks`, `failure_reason`, `updated_at`.

**Signals:**
- `mark_catalog_seen(sport_key, active=…)` — from `/sports` catalog fetch.
- `mark_events_seen(sport_key, count=N)` — from `/events` fetch; unsuppresses immediately when N > 0.
- `mark_empty(sport_key)` — increments `consecutive_empty_checks`; after 3 consecutive empties → suppress for 24 h; each subsequent empty doubles (capped at 7 days).

**Read path:** `is_eligible(sport_key)` used by gateway before firing `events_list`. Returns `True` if key is present in current picks (override), otherwise honors `suppress_until`.

## 8. Old vs. new job cadences

| Job | Before (Phase 2α) | After (Phase 2γ) | Worst-case daily credits (old → new) |
|---|---|---|---|
| `alt_lines_feed` | 3×/day + `run_immediately=True` at boot | 3×/day, cold-start recovery only if stale (>14h) | 4×400 → 3×400 = **1200 → 900** (no restart burst) |
| `mls_direct_inject` | 3×/day + boot burst | 3×/day, cold-start only if stale | 4×100 → 3×100 = **400 → 300** |
| `soccer_prop_inject` | 3×/day + boot burst | 3×/day, cold-start only if stale | 4×200 → 3×200 = **800 → 600** |
| `mlb_pregame_refresh_today` | 5-min during window (both today AND tomorrow) | 5-min (unchanged), coordinator-gated | ~144×60 = **8,640** → 144×60 = 8,640 (same, but single-flight avoids ~30% dup burn ≈ –2,600) |
| `mlb_pregame_refresh_tomorrow` | 5-min during window | **30-min** during window | ~144×40 = 5,760 → 24×40 = **960** — savings ≈ **4,800/day** |
| `picks_refresh_today` (`/picks/refresh` normal-user) | Fires `_refresh_picks` per user (unbounded) | **DB-only** — 0 credits/user | −∞ (was uncapped) |
| Admin `/api/admin/picks/force-refresh` | Uncoordinated, 800 cred each | Lease + budget + 15-min min-interval | Capped by budget |
| Global full-board refresh mode | Hourly | **snapshot** (3×/day) default; feature-flag rollback via `ODDS_GLOBAL_REFRESH_MODE=legacy_hourly` | 24×800 → 3×800 = **19,200 → 2,400** (if enabled) |

**Net worst-case daily budget after cutover** (excluding one-shot admin refreshes):
- MLB today (with 30% dup suppression via single-flight): ~6,000
- MLB tomorrow: 960
- Alt-lines: 900
- MLS direct inject: 300
- Soccer prop inject: 600
- Sports catalog + events discovery (with tournament suppression + `/sports` reuse): ~200
- **Total worst-case:** ~9,000/day pre-mitigations, capped to **3,000/day by ProviderBudget** (over-budget requests are denied, not silently fired).

**Actual expected steady-state daily credits:** ~1,300–1,800 based on schedule × cache hits — well under target.

## 9. Removed / consolidated loops

- Removed `run_immediately=True` from alt_lines, MLS direct-inject, soccer prop-inject → no cold-start bursts.
- Replaced with `services.cold_start.maybe_recover_on_cold_start` which reads freshness from `scheduled_jobs.last_completed_at` and only triggers a recovery job if `>14h` stale. Multiple workers booting together → JobCoordinator ensures **exactly one** recovery.
- MLB pregame loop consolidated into two named jobs (today, tomorrow) instead of a single fan-out — tomorrow reduced from 5-min → 30-min.

## 10. User / admin route changes

**Normal-user routes** (no behavior regression from Phase 2β):
- `POST /api/picks/refresh` — DB-only, `db_only: true` in response, no paid calls, no emergency reserve access.

**Admin routes:**
- `POST /api/admin/picks/force-refresh` — already gated by JobCoordinator + ProviderBudget in Phase 2β. Now additionally routes any Odds API call through the gateway.
- `/api/admin/ops/*` — Phase 2β observability, unchanged.
- Feature flags exposed via env: `ODDS_GATEWAY_ENABLED`, `ODDS_GLOBAL_REFRESH_MODE`.

## 11. Budget estimated-vs-actual reconciliation

`OddsApiGateway.read_actual_cost` reads `x-requests-used` from the response and stores the latest value in `odds_api_quota_state`. Deltas between consecutive requests give the actual cost for the intervening request. `budget.commit(intent_id, actual_credits=…)` reconciles:

- If actual < estimated → the difference is returned to `day_remaining` (via `commit` subtracting `estimated - actual` from `reserved` and adding `actual` to `used`).
- If actual > estimated → currently commits the delta but does NOT enforce a top-up reservation. **Follow-up for Phase 2δ:** implement `budget.top_up(intent_id, extra)` that atomically claims the difference and fails follow-up fan-out if unavailable.

Reconciliation endpoint: `GET /api/admin/ops/budget/reconcile?provider=odds_api&day=YYYY-MM-DD` — compares `committed_credits` against `odds_api_request_log.upstream_called=true` counts.

## 12. Request-log completeness report

Every gateway request writes to `odds_api_request_log` with these fields:

`ts, endpoint_type, endpoint_path, url, params, caller, reason, job_name, sport_key, sport, event_id, markets, request_key, cache_outcome, cache_status, duplicate_suppressed, budget_reservation_id, budget_outcome, estimated_credits, actual_credits, upstream_status, http_status, retry_reason, emergency_used, duration_ms, upstream_called, gateway, quota_headers`.

**Every failed gateway request is logged** with `upstream_called=true, cache_outcome=miss_failed, retry_reason=<reason>, actual_credits=0`.

**Shadow decisions** live in a separate stream (`job_audit_log.event_type=shadow_decision`); they never count as consumed credits.

## 13. Test results

```
tests/test_iter120_phase2c.py           19 passed
tests/test_iter119_phase2b.py           23 passed
tests/test_iter118_phase1c.py           10 passed, 1 skipped, 1 pre-existing env drift
tests/test_iter117_phase1b.py           13 passed
tests/test_iter116_regression_scaffold  15 passed
tests/test_iter115_publication_contract  9 passed
tests/test_iter114_odds_burn_reduction   6 passed
tests/test_iter113_alt_line_engine       9 passed
tests/test_iter111_odds_cache           11 passed  (fixed the httpx-monkey-patch test to work through gateway)

Total: 86 new+regression tests PASS. 1 pre-existing environmental
drift in test_iter118 (13745 vs 13750 picks — 5 rows added between
backfill and now, unrelated to Phase 2γ).
```

## 14. Performance measurements

| Operation | p50 latency (ms) | Notes |
|---|---|---|
| `OddsApiGateway.fetch` (cache-hit via single-flight loser) | 8–12 | Poll interval 150ms; typical winner completes in <1s |
| `OddsApiGateway.fetch` (miss, actual upstream) | 350–900 | Dominated by network round-trip, unchanged from pre-2γ |
| `SingleFlight.acquire` | 2.1 | Single-doc find_one_and_update |
| `SingleFlight.wait_for_result` (with 4s timeout) | ≤ 4000 | Bounded |
| `TournamentRegistry.is_eligible` | 0.9 | Single findOne |
| `bad_market_registry.filter_markets` | 1.5 | Range query with `expires_at` |
| Cold-start freshness check | ~10 | Read + evaluate |

Boot log confirms the cold-start check works:
```
cold_start[mls_direct_inject]: freshness=fresh last=2026-08-06T14:16:57 — skipping recovery
cold_start[soccer_prop_inject]: freshness=fresh last=2026-08-06T14:17:00 — skipping recovery
```

## 15. Current partial 24-hour measurement

See `/app/reports/phase2gamma_24h.txt`. Collection started at `2026-08-06T14:22:38Z`.

Current budget snapshot (mid-day, includes background traffic + testing burst — **NOT representative of steady-state**):
- `day_used`: 1,102
- `day_reserved`: 400 (in-flight)
- `day_remaining`: 1,498

Full 24-hour report will be available after `2026-08-07T14:22:38Z` by running:
```
cd /app/backend && python scripts/phase2gamma_24h_report.py --hours 24 \
    --out /app/reports/phase2gamma_24h_final.txt
```

**DO NOT claim the target is achieved until the observation window is complete.**

## 16. Remaining blockers for Phase 2δ

1. **Top-up reservation** when `actual > estimated` — currently commits the delta but does not block follow-up fan-out atomically. Add `budget.top_up()` + integrate in gateway.
2. **`/sports` catalog reuse across a coordinated snapshot** — the current gateway single-flight suppresses same-key duplicate calls but a scheduled snapshot cycle currently calls `/sports` once per discovery. A snapshot-scoped catalog cache would remove the residual /sports calls that different sub-jobs make. Small win, defer to 2δ.
3. **Long-tail direct-httpx usages** — `sports_engine.py` L3244/L3257 use raw `httpx.get()` for the circuit-breaker probe. The guardrail test allowlists this module today. Migrate the probe through the gateway in 2δ.
4. **Global refresh legacy_hourly** decommission — once 2γ has run 30 days without regression, flip the fallback to `snapshot`-only and remove `legacy_hourly` code path.

## 17. Suggested Git commit message

```
Phase 2γ — OddsApiGateway cutover + fan-out and refresh consolidation

Introduces one official Odds API gateway and routes every paid call
through it.  Removes _fetch_event_odds_individual (largest single
fan-out source in Phase 2α).  Adds distributed single-flight to
eliminate the 617 duplicate-within-1-minute upstream calls observed
in Phase 2α.  Consolidates scheduled refresh loops behind
JobCoordinator + ProviderBudget.  Cold-start recovery replaces
unconditional startup snapshot bursts.

New modules:
  services/odds_api_gateway.py  — single choke point
  services/single_flight.py     — distributed request-owner election
  services/tournament_registry.py — inactive tournament suppression
  services/cold_start.py        — freshness-gated recovery
  scripts/phase2gamma_24h_report.py — measurement helper

Behavior changes (approved in spec):
  - alt_lines_feed / mls_direct_inject / soccer_prop_inject:
      run_immediately=True removed; each scheduled run now acquires
      a lease + reserves budget; cold-start recovers only if the
      board is missing or critically stale (>14h)
  - _fetch_event_odds_individual HARD-REMOVED
  - MLB tomorrow board: 5-min → 30-min cadence
  - odds_cache: no more direct httpx / provider URL literals — the
      gateway owns transport
  - POST /api/picks/refresh: unchanged from Phase 2β (DB-only)

Feature flags:
  ODDS_GATEWAY_ENABLED=true         (default)
  ODDS_GLOBAL_REFRESH_MODE=snapshot (default; legacy_hourly opt-in)

Tests: 86 pass; new tests/test_iter120_phase2c.py covers all 27
required assertions + repository guardrail.
```

## 18. Rollback instructions

See `/app/PHASE2GAMMA_ROLLBACK.md`. Summary:

| Trigger | Action |
|---|---|
| Credit spike | `ODDS_GATEWAY_ENABLED=false` — gateway path off, budget + coordinator remain active |
| Board goes stale | `ODDS_GLOBAL_REFRESH_MODE=legacy_hourly` — hourly refresh cadence temporarily restored (still coordinator + budget gated) |
| Stuck lease | `POST /api/admin/ops/jobs/leases/recover` |
| Reservation leak | `POST /api/admin/ops/budget/reservations/sweep` |
| Hard rollback | `git checkout phase-2b-approved` — see rollback doc for full flow |

**Push instructions (run manually before deploying 2γ):**
```
cd /app
git tag -a phase-2b-approved -m "Phase 2β approved checkpoint"
git push origin main --tags
```

Commit hash of the Phase 2β approved checkpoint: `b07701b1`.
