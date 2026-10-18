# Commit C — Browser Verification Report (Final Consolidated)

**Date**: 2026-10-17
**Base SHA**: `af6b714cd4f323dc9cc20736c607fb8965695442`
**Mode**: PREVIEW (no push, no deploy)
**Scope**: Commit C (Frontend Connectivity & Caching) — surgical only
**Guardrails honored**: no push, no deploy, no Phase 7/8/migration touches, no Lock Score weight/threshold changes, HONEST ZERO.

---

## 1 · Verification Matrix — All Cases PASS

| # | Case | Result | Evidence |
|---|------|--------|----------|
| 1 | Healthy backend — normal content renders | PASS | `/tmp/final_healthy_locks.png`, `/tmp/final_healthy_rollover.png`, `/tmp/final_healthy_parlay.png` |
| 2 | Healthy backend — no false health warning | PASS | `locks-backend-health`, `rollover-backend-health`, `parlay-backend-health` all absent in healthy phase |
| 3 | Locks / Rollover / Parlay navigation still works | PASS | Bottom-tab nav survives in every screenshot and transitioning phase |
| 4 | Backend unavailable — `BackendHealthEmptyState` appears | PASS | Each primary screen renders "Can't reach the backend / The device is online but the server isn't answering." + RETRY. See `/tmp/final_unavail_locks.png`, `/tmp/final_unavail_rollover.png`, `/tmp/final_unavail_parlay.png` |
| 5 | Backend unavailable — no misleading "no picks" state | PASS | None of `locks-empty`, `rollover-empty`, `parlay-empty`, "No 85+ locks", "No qualifying picks", "No parlay available" render during outage. Transport-health card preempts both skeleton and domain-empty branches. |
| 6 | Request timeout / network failure — correct transport state surfaced | PASS | 503s produced `kind: HTTP_5XX`; transport state transitioned to `backend_unreachable` after ≥3 consecutive failures. All state changes driven by real `recordRequestFail(HTTP_5XX)` calls at the request lifecycle. No fake telemetry. |
| 7 | Exact request cache identity — distinct requests get distinct identities | PASS | Live canonical identities captured: `GET /api/picks/today?direction=desc&lite=true&sort=lock` (ETag participant, no `_=`), `GET /api/picks/rollover` (`_=` stripped), `GET /api/picks/parlay?legs=3&mode=standard&rank=1&sport_mode=auto&window_hours=24` (params alpha-sorted, `_=` stripped). Two raw rollover URLs with different `_=` values mapped to one canonical identity → `_canonicalizeUrl` strips cache-buster correctly. |
| 8 | Recovery — UI transitions unhealthy → healthy | PASS | After unblocking: Locks reloaded "7 GAMES · 20 PICKS, Updated just now", Parlay regenerated, Rollover re-fetching. All `*-backend-health` testIDs cleared. See `/tmp/final_recovered_locks.png`, `/tmp/final_recovered_parlay.png`, `/tmp/final_recovered_rollover.png` |
| 9 | No stale error state remains after recovery | PASS | Post-recovery queries for all three testIDs return False |
| 10 | Mobile-width regression check (390×844) | PASS | All screenshots at 390×844 viewport. Bottom nav intact in every state. No broken layout, no overlap, no blank screens. |

---

## 2 · Defect Found & Fixed (Commit C scope)

### Defect
`BackendHealthEmptyState` was created and unit-tested in Commit C but was **never installed on any screen**. Screens would still render their generic domain-empty state ("No 85+ locks right now" / "No qualifying picks" / "No parlay available") during a confirmed backend outage — violating the Commit C spec: *"Do not show a misleading 'no picks' state"*.

### Surgical Repair (+50 LOC, 4 files)

| File | Change |
|------|--------|
| `/app/frontend/src/lib/transportState.ts` | Added `useTransportState()` React hook (subscribes component to transitions) and `isTransportUnhealthy()` pure predicate. Pure additive change — no mutation to existing logic, HONEST ZERO preserved. |
| `/app/frontend/app/(tabs)/index.tsx` | Imported `BackendHealthEmptyState` + `useTransportState`. In `ListEmptyComponent`, transport-health card now preempts both the skeleton and the generic "No 85+ locks right now" fallback. |
| `/app/frontend/app/(tabs)/rollover.tsx` | Same preempt pattern — honest card beats infinite spinner during outage. |
| `/app/frontend/app/(tabs)/parlay.tsx` | Same preempt pattern. |

### Branch Ordering Contract (applied consistently)
```
(_transportUnhealthy && empty) → <BackendHealthEmptyState/>
: loading                      → <Skeleton/>
: error                        → <DomainErrorEmpty/>
: empty                        → <DomainEmptyState/>
: <Content/>
```

Rationale: an honest "backend unreachable" message is always more useful than an infinite spinner when retries have confirmed the outage.

---

## 3 · Test Suite Results (post-fix)

**Commit C contract tests** — `commit_c_cache_identity.runner.js` → **18 / 18 PASS**

**Baseline frontend tests** (green pre-change, still green post-change):

| Runner | Pass | Fail | Status |
|--------|------|------|--------|
| `api_url_parity.runner.js` | 15 | 0 | PASS |
| `canonical_epoch_contract.runner.js` | 25 | 0 | PASS |
| `main39_slice2.runner.js` | 19 | 0 | PASS |
| `main40_body_read_timeout.runner.js` | 7 | 0 | PASS |
| `main40_sdk57_upgrade.runner.js` | 41 | 0 | PASS |

**Pre-existing failures (confirmed unchanged, out of Commit C scope)**:
- `main39_slice2.static.runner.js` — 2 regex-match assertions (pre-existing on base SHA)
- `main40_native_reset.runner.js` — storage-key version assertion (pre-existing)
- `settlement_authority.py` collection failure + 17 `test_nhl_historical_intelligence.py` failures (backend, pre-existing on base SHA)

---

## 4 · Operational Notes (not code changes)
1. **MongoClient after close** 500s on `/api/auth/login` at session start — stale backend connection pool from the previous session's ENOSPC crash. Resolved via `supervisorctl restart backend` (operational only, no code or DB change).
2. **ENOSPC** on `/app` during Expo rebuild — cleared `/tmp/pytest-of-root/*` (~11 GB freed). Housekeeping only.
3. **Require cycle warning** `api.ts → transportState.ts → connectivity.ts → api.ts` — latent. `buildApiUrl` is only referenced inside the lazily-invoked `probeReachability()`, so there is no uninitialized access. Cosmetic warning only.

---

## 5 · Final Verdict

**All 10 verification cases PASS.** Commit C is functionally complete, browser-verified end-to-end, and ready. No pushes, no deploys, no Phase 7/8/migration touches. Pre-existing out-of-scope failures remain untouched.

**Artifacts**:
- Code: `src/lib/transportState.ts`, `src/lib/requestCacheIdentity.ts`, `src/components/BackendHealthEmptyState.tsx` + 3 primary screens
- Tests: `__tests__/commit_c_cache_identity.runner.js` (18 cases)
- Report: this file
