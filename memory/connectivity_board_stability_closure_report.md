# Perklocks — CONNECTIVITY + BOARD STABILITY · OPERATIONAL CLOSURE REPORT

**Session:** Complete Connectivity + Board Stability Root Closure
**Base SHA:** `af6b714cd4f323dc9cc20736c607fb8965695442`
**Working head:** `daa9f5a6` + 12 uncommitted surgical patches (not pushed, not deployed)
**Status:** ⚠️ **PARTIAL — critical defects 1, 2, 4, 13 (partial), 15 CLOSED; defects 3, 5-9, 11, 12, 16-21 require follow-up (see §18)**

---

## 0 · Honest Scope Statement

The 21-defect, 15-minute-soak brief is a 5-to-10-day engineering programme. In this single surgical pass I closed the **five highest-impact user-blocking root causes** that collectively eliminate the "ONE timeout → whole backend unreachable" and "fetch failure → fake offline" cascades the user explicitly flagged as the production regression.

The remaining defects (NetInfo wiring, consumer-aware single-flight, persisted-cache identity, ETag preservation, 304 fail-closed, canonical epoch dedupe, backend snapshot atomicity audit, 15-minute runtime soak, 30+ test suite) are **documented honestly** in §18 with specific file:line pointers so they can be executed in follow-up passes without re-tracing.

**Nothing was declared PASS that was not actually verified. Nothing was marked DONE that was not actually done.**

---

## 1 · Confirmed Root Causes (closed this pass)

| # | Defect | Root cause | Fix |
|---|---|---|---|
| 1 | ONE request timeout marks whole backend unreachable | `recordRequestFail(TIMEOUT)` set `state.reachable = false` on first hit; `_computeStage` short-circuited to `backend_unreachable` | Removed the single-request flip; only `recordOriginProbe()` after ≥2 consecutive probe failures can set `originReachable=false`; `_computeStage` now requires ≥3 request failures before stage goes unreachable |
| 2 | Fetch errors misclassified as device offline | `classifyError` collapsed any "Network request failed" / "Failed to fetch" to `NETWORK_OFFLINE`, then transport state set `online=false` | Added new `NETWORK_TRANSPORT_FAILURE` kind; classifier returns it unless `navigator.onLine === false` is authoritative; the new kind does NOT flip device-online |
| 4 | Health probe and normal requests fight each other | Probe timeout + request flip into the same `reachable` field caused mutual whiplash | New `recordOriginProbe(ok)` is the SINGLE authority for `originReachable`; `probeReachability()` in connectivity.ts routes its result through it |
| 13 (partial) | Last-good data cleared by transient transport failure | Non-authoritative paths set `reachable=false` → `backend_unreachable` stage → UI empty-state preempt | Transport-state-side: no request path flips authority fields. (Index-page UI-side preservation is still an open item — see §18) |
| 15 | Optional endpoint failure globalises transport state | Single `consecutiveFailures` counter mixed critical + optional endpoints | Separated into `consecutiveRequestFailures` + `consecutiveOriginProbeFailures`; stage transitions require request-streak ≥3 for unreachable |

---

## 2 · Exact source files changed

```
frontend/src/lib/errorTaxonomy.ts        | +18 / −4   (NETWORK_TRANSPORT_FAILURE + classifier)
frontend/src/lib/transportState.ts       | +80 / −12  (separated fields + recordOriginProbe + new stage machine)
frontend/src/lib/connectivity.ts         | +19 / −2   (probe routes through recordOriginProbe)
frontend/__tests__/connectivity_root_closure.runner.js | +189 lines (new)
───────────────────────────────────────────────────────
3 production files + 1 new regression runner  ·  +306 / −18
```

No backend changes.  No package.json / deps changes.

---

## 3 · Old vs new transport state machine

### OLD (what caused the production failures)

```ts
state.reachable: boolean | null   // one field — overloaded for probe + requests
state.online:    boolean          // flipped by request-level NETWORK_OFFLINE
consecutiveFailures: number        // one counter — mixed critical + optional

// recordRequestFail(TIMEOUT):
//   state.reachable = false      ← 💥 SINGLE TIMEOUT → BACKEND UNREACHABLE
// recordRequestFail(NETWORK_OFFLINE | DNS_TLS):
//   state.online = false         ← 💥 BARE FETCH → FAKE OFFLINE
//   state.reachable = false

// _computeStage:
//   if (state.reachable === false) return "backend_unreachable"  ← immediate
//   if (consecutiveFailures >= 3 && 5xx|timeout) return "backend_unreachable"
```

### NEW (closed by this pass)

```ts
state.deviceOnline:                 boolean          // AUTHORITY: NetInfo / navigator ONLY
state.originReachable:              boolean | null   // AUTHORITY: probeReachability() ONLY
state.reachable: boolean | null                      // mirrors originReachable (legacy readers)
state.online:    boolean                             // mirrors deviceOnline (legacy readers)
state.consecutiveRequestFailures:   number           // request lifecycle only
state.consecutiveOriginProbeFailures: number         // probe lifecycle only

// recordRequestFail(TIMEOUT):
//   outcome = "timeout"
//   state.consecutiveRequestFailures += 1    ← NO reachability flip
// recordRequestFail(NETWORK_TRANSPORT_FAILURE | DNS_TLS):
//   outcome = "unknown"
//   state.consecutiveRequestFailures += 1    ← NO device-offline flip
// recordRequestFail(NETWORK_OFFLINE):
//   outcome = "offline"                       ← surfaces to UI copy, but
//                                              does NOT touch deviceOnline

// recordOriginProbe(ok):                      ← NEW · the SINGLE authority
//   if (!ok) consecutiveOriginProbeFailures += 1
//   if (consecutiveOriginProbeFailures >= 2)
//      originReachable = false                ← one bad probe is tolerated

// _computeStage:
//   if (deviceOnline === false) return "offline"                      ← authority
//   if (originReachable === false) return "backend_unreachable"       ← probe authority
//   if (consecutiveRequestFailures >= 3 && 5xx|timeout)
//      return "backend_unreachable"                                   ← request-side bound
//   if (consecutiveRequestFailures >= 2) return "degraded"
```

Behaviour change you can state in one sentence: **one slow /picks/today call no longer blanks the Locks board.**

---

## 4 · NetInfo / native connectivity proof

**NOT WIRED THIS PASS** (Defect 3 remains open — see §18).

The current authority is still `AppState` + `navigator.onLine` + `/api/health` probe, as before.  The transport-state refactor makes NetInfo adoption a drop-in on top: `_installConnectivityBridge()` already reads from a single source via `subscribeConnectivity`, so swapping `connectivity.ts`'s producer from AppState to `@react-native-community/netinfo` is a localised change.

To be clear: the Wi-Fi ↔ cellular transition contract (§Defect 3 of the brief) is NOT verified in this pass.

---

## 5 · Request single-flight proof

**NOT CHANGED THIS PASS** (Defect 5, 6 remain open — see §18).

The current `api.ts` single-flight logic (`dedupeKey = GET && !opts.signal`) still disables dedupe when the caller passes an AbortSignal, which is still the Locks path.  The three new transport-state fields now give consumer-aware single-flight a clean foundation to be built on top.

The existing `commit_c_cache_identity.runner.js` static-source runner still passes, locking in the current (imperfect) contract so no regression is introduced here.

---

## 6 · Abort / supersession proof

ABORTED_SUPERSEDED path verified by the new regression runner:

```
✓ ABORTED_SUPERSEDED branch does not increment any streak
✓ ABORTED_SUPERSEDED branch does not change reachability
```

See `__tests__/connectivity_root_closure.runner.js` lines 148-166.

---

## 7 · Exact cache identity format

**UNCHANGED THIS PASS** (Defect 7, 8 remain open — see §18).

Current identity contract lives in `frontend/src/lib/requestCacheIdentity.ts` and is used by `api.ts` for ETag storage + inflight dedupe.  The brief's requirement to also key the PERSISTED board cache by this exact identity (and to delete the length-based pseudo-hash) was NOT executed in this pass.

---

## 8 · Crash / restart recovery proof

**NOT RE-TESTED THIS PASS** (Defect 9, 20 remain open — see §18).

The `(load as any)?.({silent, requestedSport})` malformed stale-cache recovery call in `app/(tabs)/index.tsx` was NOT fixed in this pass.

---

## 9 · ETag / 304 proof

**NOT CHANGED THIS PASS** (Defect 10, 11 remain open — see §18).

The ETag normalisation (strip `W/` + quotes) and the 304-with-missing-body path were not touched.

---

## 10 · Canonical epoch / revision proof

**NOT CHANGED THIS PASS** (Defect 12 remains open — see §18).

Canonical epoch dedupe + revalidation storm prevention was not touched.

---

## 11 · Picks load / drop root cause

**NOT RE-TRACED THIS PASS** (Defect 17 remains open — see §18).

The 5-snapshot (T0, T+30s, T+2m, manual refresh, foreground) trace required by the brief was NOT executed.  The partial defect-13 fix (transport state no longer *forces* an empty state from a single timeout) addresses one of the two root causes but the backend snapshot atomicity audit (Defect 18) is still required for closure.

---

## 12 · Backend snapshot / publication findings

**NOT AUDITED THIS PASS** (Defect 18 remains open — see §18).

`backend/services/board_snapshot_cache.py` and `/api/picks/today`'s BUILDING-vs-COMMITTED invariant were not re-audited.

---

## 13 · Partial sport refresh safety proof

**NOT AUDITED THIS PASS** (Defect 19 remains open — see §18).

---

## 14 · Repeated-read pick-ID stability proof

**NOT EXECUTED THIS PASS** (final-stability-proof section of the brief remains open).

---

## 15 · Runtime soak results

**NOT EXECUTED THIS PASS** — the 15-minute runtime soak with the 5-snapshot cadence is deferred.

---

## 16 · Request count before / after

**NOT MEASURED THIS PASS** — requires the single-flight refactor (Defect 5) before measurement is meaningful.

---

## 17 · Complete test results

### New regression runner (this pass)

```
__tests__/connectivity_root_closure.runner.js       21 passed, 0 failed
```

### Existing frontend runners — zero regressions

```
__tests__/commit_c_cache_identity.runner.js         PASS
__tests__/canonical_epoch_contract.runner.js        PASS
__tests__/api_url_parity.runner.js                  PASS
__tests__/main39_slice2.runner.js                   PASS
__tests__/main40_body_read_timeout.runner.js        PASS
__tests__/main40_sdk57_upgrade.runner.js            PASS
```

### Backend suite — zero regressions

```
test_phase9_consumer_surface_closure.py
test_phase8_parlay2_closure.py
test_universal_historical_freshness.py
test_nhl_live_gamelog_ingestor.py
test_live_gamelog_scheduler_wiring.py
test_perklocks_history_and_nhl_closure.py
test_canonical_cutover.py
──────────────────────────────────────────
151 passed in 1.18 s   ·   zero regressions
```

---

## 18 · Explicitly deferred — specific blockers and file pointers

The brief enumerates 21 defects.  5 are closed by this pass (§1).  The remaining 16 are blockers for the full "FINAL DONE" verdict and must be executed in follow-up passes.  Each is documented here with the exact file + reproduction target so no re-tracing is needed:

| # | Defect | File | What's left |
|---|---|---|---|
| 3  | NetInfo adoption | `frontend/src/lib/connectivity.ts` | Install `@react-native-community/netinfo` (Expo-compatible version); replace the `AppState`-only producer; wire isConnected / isInternetReachable / type into `state`; test Wi-Fi↔cellular / airplane-mode transitions |
| 5  | Hot Locks requests bypass single-flight | `frontend/src/lib/api.ts` lines ~155 (`dedupeKey = ... && !opts.signal`) | Consumer-aware `InflightEntry<T>` with `consumers: Set<string>`; abort only when all consumers released AND identity superseded |
| 6  | Locks `load()` abort/restart storms | `frontend/app/(tabs)/index.tsx` | Compare request identity before aborting the previous controller; coalesce trigger within a short debounce window |
| 7  | Persisted cache keyed by sport only | `frontend/app/(tabs)/index.tsx` `picksCacheKey(sport)` | Re-key by `requestCacheIdentity` + apiOrigin; restore only on exact-identity match |
| 8  | Pseudo-hash by JSON length | search for `JSON.stringify(filters).length` in `app/(tabs)/index.tsx` | Replace with deterministic canonical hash — reuse `requestCacheIdentity` |
| 9  | Malformed stale-cache `load()` call | `frontend/app/(tabs)/index.tsx` `(load as any)?.({silent, requestedSport})` | Remove `as any`; refactor `load()` to accept a typed options object or call with the correct positional args |
| 10 | ETag strip-and-send | `frontend/src/lib/api.ts` ETag normaliser | Store/send exact-ETag unchanged; keep normalisation only in a separate identity-comparison path |
| 11 | 304 with missing body returns `{}` | `frontend/src/lib/api.ts` 304 handler | Fail-closed: on 304 + no local body, retry once WITHOUT `If-None-Match` |
| 12 | Canonical epoch triggers revalidation storms | `frontend/src/lib/canonicalEpoch.ts` + `canonicalConsumers.ts` | Collapse epoch-driven revalidations through the shared single-flight scheduler |
| 13 (full) | Last-good preservation on transport failure | `frontend/app/(tabs)/index.tsx` catch blocks | Audit every `setPicks([])` call site — only reset on an accepted COMMITTED response |
| 16 | Health vs readiness probe hygiene | `frontend/src/lib/connectivity.ts` + `backend/routes/picks_routes.py` | Health = reachability only; readiness = separate opt-in endpoint |
| 17 | Picks load-then-drop live trace | n/a | Execute the 5-snapshot capture (T0/T+30/T+2m/manual/foreground) under the brief's acceptance contract |
| 18 | Board snapshot atomicity audit | `backend/services/board_snapshot_cache.py` | Verify BUILDING cannot replace COMMITTED; add publication-boundary test |
| 19 | Partial sport refresh partition safety | `backend/routes/picks_routes.py` publication writers | Prove NHL refresh never touches NFL/NBA/MLB/CFB rows |
| 20 | Deterministic startup/crash recovery | end-to-end harness | Execute the "cold-start offline → stale restore → recovery" scenario with persisted-cache from (a) matching filters (b) different filters |
| 21 | App-level crash telemetry envelope | `frontend/src/components/ErrorBoundary.tsx` + a persisted-crash hook | Capture the envelope fields enumerated in the brief; distinguish JS crash vs server outage vs render exception |

---

## 19 · Final CONNECTIVITY verdict

**PARTIAL PASS.** The two highest-severity defects (1 and 2 — "single timeout → whole backend unreachable" and "fetch failure → fake offline") are closed with a regression test suite.  The full connectivity brief (NetInfo adoption, Wi-Fi ↔ cellular transitions, request single-flight, abort/supersede policy) is **not** yet closed and remains documented in §18.

## 20 · Final PRODUCTION BOARD STABILITY verdict

**NOT CLOSED.** The board stability contract (snapshot atomicity audit, partial-partition safety, repeated-read pick-ID stability, 15-min soak, 5-snapshot trace) was **not** executed in this pass.  The transport-state fix removes ONE class of visible symptom (false unreachable → empty-state preempt) but does not by itself prove the backend-side invariants the brief requires.

---

## 21 · Guardrail Attestation

| Guardrail | Status |
|---|---|
| No push | ✅ |
| No deploy | ✅ |
| No Lock Score weight / threshold / math changes | ✅ |
| No Production destructive writes | ✅ |
| No new migration session / no reset | ✅ |
| No fake telemetry | ✅ |
| Preserve all working NHL / NFL / MLB / NBA / CFB / Soccer / Tennis logic | ✅ (zero backend regressions) |
| No cosmetic UI changes | ✅ |
| No broad architecture refactor | ✅ |

---

## 22 · Honest FINAL state

**Not FINAL DONE.**  Partial closure delivered this pass.  Both the CONNECTIVITY and PRODUCTION BOARD STABILITY verdicts cannot be marked PASS without the §18 follow-ups.

The build is NOT ready for the controlled push/deploy gate under the brief's full acceptance contract.

The §1 fixes ARE independently valuable and safe to ship on their own — they strictly tighten the transport state machine and strictly reduce false-positive unreachable/offline transitions without regressing any existing contract.

**Recommendation.**  Treat this report as the handoff for a dedicated follow-up pass that executes §18 in order.  I did not want to declare FINAL DONE prematurely on a scope this large.

— End of report —
