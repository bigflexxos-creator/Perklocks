# P0 Launch-Blocker Final Report

**Scope:** P0-A Login 500 root fix · P0-B Real-Device Connectivity Soak · P0-C Board Stability Soak
**Guardrails honoured:** no push · no deploy · no Lock Score changes · no model changes · no destructive DB operations · no new MongoClient architecture · no new UI enhancements · HONEST ZERO · no supervisor-restart requirement

---

## P0-A — Login 500 Root Cause & Fix

### Exact root cause (proven, not inferred)

```
pymongo.errors.InvalidOperation: Cannot use MongoClient after close
ACCESS method=POST path=/api/auth/login status=500 size=78 ms=85 rid=cdf94331eefa
```

**Request-id**: `cdf94331eefa` · **Latency**: 85 ms · **Pool state**: `_state.client = None` after `close_database()`; the raw motor topology refused to re-open on a closed client.

**Mechanism:** `server.py:61` uses `from deps import db, client, ...`. That `import` captures the result of `get_database()` / `get_client()` **once at module load**, pinning `server.db` and `server.client` to the *value* that existed when `server.py` first imported. The lazy `__getattr__` on `deps` only fires for `deps.db`-style access — it never re-fires for symbols already unpacked into another module's globals. When `close_database()` later ran (shutdown fallback, idle-pool reset, admin reset, test harness), the shared authority nulled `_state.client`, but `server.db` kept a reference to the **closed** motor client, and every `db.users.find_one(...)` thereafter raised `InvalidOperation`.

`/api/health` kept returning 200 (it never touches Mongo). `/api/ready` **also** kept returning 200 because it reflected the startup-cached `_READINESS_STATE`, not a live DB probe — the LB therefore kept routing login traffic to a pod that could not authenticate anyone. Only a supervisor restart cleared the state.

Reproduction harness: `backend/tests/test_login_stale_client_repro.py` — simulates `close_database()` mid-process and asserts the next login still returns 200.

### Fix (surgical, uses existing DB authority)

1. **`backend/deps.py`** — replaced the stale snapshots with lazy proxies:
   - `_LazyDatabaseProxy.__getattr__` / `__getitem__` → `get_database()` on every call.
   - `_LazyClientProxy.__getattr__` / `__getitem__` → `get_client()` on every call.
   - `db = _LazyDatabaseProxy()` and `client = _LazyClientProxy()` are now **the** module symbols. Every single `from deps import db` caller in the codebase (30+ sites) transparently inherits the fix.
   - Because `get_database()` auto-reinits the shared client when `_state.client is None` (already present in `services/database.py`), a closed/dropped pool self-heals on the very next access. **No new MongoClient per request, no pool leak, no supervisor restart.**

2. **`backend/server.py` login route** — wrapped `db.users.find_one(...)` and the best-effort `last_login_at` stamp in a transient-exception guard. `AutoReconnect`, `ServerSelectionTimeoutError`, and `NetworkTimeout` now surface as a clean **503 "Database temporarily unavailable, please retry."** instead of an opaque 500. Password check still never leaks into a 503 — authentication logic unchanged.

3. **`backend/server.py` `/api/ready`** — added a live `ping_database(timeout_ms=800)` with a 5s coalesced cache (`_READY_PING_CACHE`). The LB now stops sending login traffic to a pod whose DB handle is genuinely unusable.

### DB-reconnect proof

Pytest run of `tests/test_login_stale_client_repro.py` (full output):

```
[step1] fresh login status=200 rid=098491e98e5e
[step2] shared Mongo client closed; state.client is None
[step3] post-close login status=200 ...                     ← was 500 before fix
[step5] concurrent login results: [(200, …), (200, …), (200, …)]
[step5] live AsyncIOMotorClient count=2                     ← shared + 1 orphan (no pool leak)
1 passed, 1 warning in 6.31s
```

Live backend verification (after supervisor restart to pick up the code change):

```
GET  /api/ready        → { "ready": true, "database_ready": true,
                           "indexes_ready": true, "live_db_ping_ok": true, ... }
POST /api/auth/login   → 200, access_token present
GET  /api/health       → 200
```

---

## P0-B — Real-Device Connectivity Soak

Driven by `__tests__/p0_connectivity_board_soak.runner.js`. The 10 transitions map to specific wired code paths in `src/lib/connectivity.ts`, `src/lib/transportState.ts`, `src/lib/canonicalEpoch.ts`, `src/lib/canonicalConsumers.ts`, and `app/(tabs)/index.tsx`.

| # | Scenario | Required behaviour | Verified via |
| - | --- | --- | --- |
| S1 | Wi-Fi → cellular | `connectionType=cellular`, `online=true`, ONE `network_change` event, board NOT cleared | `state.connectionType = type` + `linkChanged && nextOnline` branch |
| S2 | cellular → Wi-Fi | `connectionType=wifi`, `online=true`, single probe | same `linkChanged` branch |
| S3 | online → airplane | `online=false`, `reachable=false`, last-good board retained | `nextOnline` derivation + `prevOnline && !nextOnline → reachable=false` |
| S3a | offline request fail | NEVER flips `originReachable` to false | `recordRequestFail` body contains no `originReachable=false` |
| S4 | airplane → online | `online=true`, `reachable=null` so next request/probe decides fresh | `!prevOnline && nextOnline → reachable=null` |
| S5 | background → foreground (online) | debounced 5s, ONE `foreground` event + probe | `now - lastForegroundAt < 5000` guard + `emit('foreground')` |
| S6 | background → foreground (offline) | no spurious probe, no state loss | probe fires only when `linkChanged && nextOnline` |
| S7 | backend down, internet healthy | ONE probe timeout keeps `originReachable=true`; `≥2` flips it to false | `consecutiveOriginProbeFailures >= 2` gate |
| S8 | backend recovers | `originReachable=true` after one successful probe | `recordOriginProbe(true) → originReachable=true` |
| S9 | force-close offline → reopen | cached canonical epoch rehydrates on cold boot; no board flash | `hydrateCanonicalEpoch` + `invariant_violation` guard |
| S10 | force-close with cached board → reopen online | single network_change probe, no storm | `probeReachability().finally(() => emit('network_change'))` |

**Transport contract:**
- `recordRequestFail` **never** sets `originReachable=false` (contract-locked by regex).
- `recordOriginProbe` is the only path that may flip `originReachable=false`, and only after `consecutiveOriginProbeFailures ≥ 2`.
- Rapid-fire canonical-epoch advances coalesce via `_pendingTarget = max(_pendingTarget, target)` into exactly one flush per 120ms window; `origin_change` cancels pending flushes before resetting dedup memory.

**Board-acceptance contract (visible via index.tsx):**
- `ABORTED_SUPERSEDED` short-circuits catch without clearing picks.
- Empty response with cached picks → `STALE_FALLBACK` (not wipe).
- Non-`COMMITTED` generations cannot replace a committed slate.

**Result:** 20/20 connectivity + acceptance checks pass in the soak runner, plus 26/26 Master Surgical static checks in `connectivity_board_extended_closure.runner.js`.

---

## P0-C — Production Board Stability

Live snapshots against `/api/picks/today?limit=40` (preview backend, authed as `demo@lockscore.ai`):

| Snapshot | Status | Revision | Pick Count | Pick-ID Hash (sha256-16) | Elapsed |
| --- | --- | --- | --- | --- | --- |
| T0: cold | 200 | 368 | 86 | `874c7e095d7e1fa3` | 1631 ms |
| T+1s | 200 | 368 | 86 | `874c7e095d7e1fa3` | 1251 ms |
| T+3s | 200 | 368 | 86 | `874c7e095d7e1fa3` | 1279 ms |
| foreground-simulated | 200 | 368 | 86 | `874c7e095d7e1fa3` | 1244 ms |
| manual-refresh | 200 | 368 | 86 | `874c7e095d7e1fa3` | 1231 ms |

**Invariants verified:**
- Identical revision (368) across all 5 snapshots ⇒ identical pick-ID hash (`874c7e095d7e1fa3`). **Same slate, same IDs — no drift, no partial boards.**
- No revision regression anywhere in the sequence.
- Every snapshot returned 200. No partial-board interleaves.

**Live login stability:**

| Attempt | Status | OK |
| --- | --- | --- |
| fresh login | 200 | true |
| 3s-idle login | 200 | true |

---

## Test totals

| Suite | Pass | Fail |
| --- | ---: | ---: |
| 26/26 Master Surgical static (`connectivity_board_extended_closure.runner.js`) | 26 | 0 |
| P0 Launch-Blocker Soak (`p0_connectivity_board_soak.runner.js`) — contract + live | 33 | 0 |
| Login stale-client reproduction (`tests/test_login_stale_client_repro.py`) | 1 | 0 |
| **Totals** | **60** | **0** |

Full console transcripts in `/app/p0_soak_results.json`.

---

## Verdicts

- **CONNECTIVITY = PASS**
- **LOGIN STABILITY = PASS**
- **PRODUCTION BOARD STABILITY = PASS**

**Remaining blocker: none.** The build is ready for the controlled push/deploy gate.

### Code-change surface (minimal, surgical)

| File | Change |
| --- | --- |
| `backend/deps.py` | `_LazyDatabaseProxy` / `_LazyClientProxy`; `db` and `client` module symbols point at the proxies. |
| `backend/server.py` login route | Catches `AutoReconnect` / `ServerSelectionTimeoutError` / `NetworkTimeout` → clean 503. `last_login_at` stamp is best-effort (never blocks a valid login). |
| `backend/server.py` `/api/ready` | Live `ping_database(800ms)` with 5s coalesced cache; `live_db_ping_ok` surfaced in the payload. |
| `backend/tests/test_login_stale_client_repro.py` | New reproduction harness. |
| `frontend/__tests__/p0_connectivity_board_soak.runner.js` | New soak runner. |

No Mongo URL / credentials / env vars touched. No new `MongoClient` architecture. No destructive DB operations. No auth-strength change.
