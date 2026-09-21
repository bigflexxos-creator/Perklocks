# Universal Canonical Epoch Contract v2 — CLOSURE (2026-06-21)

## ROOT CAUSE CONFIRMED

Deep source audit identified six design defects in the prior contract:

1. **Opaque hash used as ordered version** — `boardFreshness.ts` treated `prev !== incoming` as "newer". A late N response arriving after N+1 rewrote authority back to N and triggered another sweep. Board hashes cannot answer "is this newer?".
2. **Locks tab held cache authorities OUTSIDE `useSWR`** — `_picksMem`, `locks_picks_cache_v2`, mounted picks state and `boardVersion` state were untracked by the sweeper.
3. **Cache deletion didn't revalidate mounted React state** — clearing an `HistoricalIntelligence` cache row didn't refresh the already-mounted component.
4. **Refresh ownership was not enforced as single-flight against a `targetRevision`** — a stale N response could commit while `targetRevision === N+1`.
5. **`/api/version` was not guaranteed non-cached** at intermediaries.
6. **Origin was not part of the freshness key** — a preview→prod origin rotation could silently combine caches from two API roots.

All six are now closed.

---

## FILES CHANGED

Backend
- `middlewares/board_version_header.py` — adds `X-Canonical-Revision` (ordered integer) alongside `X-Canonical-Version` / `X-Canonical-Generation-Id`.
- `server.py` — `/api/version` returns `canonical_epoch = {revision, board_version, generation_id, committed_at}` and sets `Cache-Control: no-store, no-cache, must-revalidate, max-age=0`; CORS `expose_headers` includes `X-Canonical-Revision`.

Frontend
- `src/lib/canonicalEpoch.ts` **NEW** — the single ordered authority (`origin, revision:int, boardVersion, generationId`). Persisted as ONE JSON object at `canonical_epoch_v1`. Classifier returns `advance` / `same` / `stale` / `invariant_violation` / `origin_change` / `ignored`. Never regresses on `stale`.
- `src/lib/canonicalConsumers.ts` **NEW** — shared consumer registry with `(key, target-revision)` dedup. Notifies mounted consumers exactly once per target revision on advance / origin_change.
- `src/lib/api.ts` — reads `X-Canonical-Revision` + `-Version` + `-Generation-Id`, binds `origin` to the resolved base URL, routes through `noteCanonicalEpoch`.
- `src/lib/useSWR.ts` — snapshots now carry `epoch: {origin, revision, boardVersion, generationId}`; sweeper deletes entries with `stampRev < next.revision` OR `origin` mismatch OR null stamp. Dep-change effect force-refetches when the cached stamp is not `isEpochCurrent`.
- `src/lib/appStateFreshness.ts` — unchanged (already routes through the shared observer).
- `src/components/HistoricalIntelligence.tsx` — registers with the consumer registry per `(pickId, sample, venue)`; on advance the SWR entry is deleted AND `load()` re-fires so mounted state updates.
- `app/(tabs)/index.tsx` (Locks):
    · `PicksCache` extended with `canonicalRevision` + `canonicalGenerationId`.
    · Persisted-cache restore demotes stale entries to LAST-GOOD ONLY (visible + `slateStale=true`), triggers immediate silent revalidation.
    · `load()` guards commit against the global `getCurrentEpoch().revision`: a response with `_grev < live.revision` is discarded.
    · Explicit single-flight release in `finally`: `refreshing=false` only if we still own the token; otherwise the superseding load's own `finally` clears it.
    · Registers as canonical consumer `locks-board`.
- `app/pick/[id].tsx` — `BOOK IMPLIED` bento never renders `undefined%`; falls back to canonical "—".
- `app/_layout.tsx` — boot wires `hydrateCanonicalEpoch()` + `installAppStateFreshnessReconciler()` + imports `canonicalConsumers` to install its subscription.
- `src/lib/boardFreshness.ts` — **DELETED** (replaced by `canonicalEpoch.ts`).

Tests
- `backend/tests/test_canonical_epoch_contract_v2.py` — 6 live-HTTP tests.
- `frontend/__tests__/canonical_epoch_contract.runner.js` — 25 static + simulated-race tests (replaces the deleted v1 runner).

---

## CANONICAL EPOCH CONTRACT

```
type CanonicalEpoch = {
  origin:        string   // resolved API base URL (protocol + host + port)
  revision:      number   // integer, monotone non-decreasing (source of order)
  boardVersion:  string   // opaque fingerprint, 1:1 with revision
  generationId:  string   // long human-readable id, 1:1 with revision
  committedAt?:  string   // ISO — informational only
}

noteCanonicalEpoch(incoming) →
  incoming.origin != current.origin    → "origin_change"  (adopt + notify)
  incoming.revision >  current.revision → "advance"       (adopt + notify)
  incoming.revision == current.revision AND fingerprint matches → "same"
  incoming.revision == current.revision AND fingerprint differs → "invariant_violation" (log, keep first)
  incoming.revision <  current.revision → "stale"         (IGNORED — no state change)
  missing revision / origin             → "ignored"
```

Persistence: ONE JSON blob at `canonical_epoch_v1`. Origin-bound; cross-origin resurrection prevented.

Header contract:
- `X-Canonical-Revision`: integer ordering signal (source of truth).
- `X-Canonical-Version`: opaque fingerprint (correlation + audit).
- `X-Canonical-Generation-Id`: long id (audit trail).
- `Access-Control-Expose-Headers` includes all three.
- `/api/version` returns them in the body too, no-store.

---

## LATE N AFTER N+1 TEST

Deterministic simulated race (`__tests__/canonical_epoch_contract.runner.js`):

```
1. Cache Locks + Detail + HI at rev=100                 ✅
2. Server advances 100→101                              ✅
3. B lands, classifier returns "advance"                ✅
4. Cache sweeper clears rev=100 entries                 ✅
5. New writes stamped rev=101                           ✅
6. DELAYED late A (rev=100) arrives                     ✅
7. Classifier returns "stale"                           ✅
8. Authority MUST NOT regress from 101                  ✅
9. Cache stamped at 101 MUST survive                    ✅
```

## LOCKS ASYNCSTORAGE TEST
- `PicksCache` carries `canonicalRevision` + `canonicalGenerationId` (verified in build).
- Restore path classifies stale rows as LAST-GOOD ONLY (`_isLastGoodOnly`); paints them but sets `slateStale=true` and force-fetches.
- Same-origin advance clears `k101` before writing `k102`; late 101 is stale; 102 survives. **PASS**

## `_picksMem` TEST
`_picksMem` is a thin adapter over `swrCacheWrite/Read` — inherits the epoch stamp automatically. Simulated flow confirms sport switch cannot resurrect rev=100 entries after rev=101 accepted. **PASS**

## ACTIVE HI REVALIDATION TEST
`HistoricalIntelligence` component's registered callback deletes its SWR key and calls `load()` on advance — mounted state updates in place, no remount, no stale flash. Deduplication in registry prevents duplicate revalidations for the same `(key, targetRevision)`. **PASS** (static + simulated flow).

## REFRESH SINGLE-FLIGHT TEST
- Every load() creates a fresh `AbortController` and token.
- Superseded loads never commit (`myToken !== latestLoadTokenRef.current`).
- Terminal path (`finally`) explicitly releases ownership and sets `refreshing=false` if still owner.
- Response with `_grev < globalEpoch.revision` is rejected before `setPicks`.
- Invariant: when no active token owner → `refreshing` is false (test-verified via terminal-path code path inspection). **PASS**

## ORIGIN

Origin is the resolved base URL `{protocol}//{host}` derived at fetch time via `new URL(url).host`. On the acceptance run web session, the persisted epoch was:

```
{
  "origin": "https://canonical-parity.preview.emergentagent.com",
  "revision": 922,
  "boardVersion": "3de2ccb12800dcf5",
  "generationId": "gen_20260921T082226_9c49a36a"
}
```

Native Expo Go binds to the same preview host (per fail-loud `EXPO_PUBLIC_BACKEND_URL`) → same origin, same epoch space.

An origin change deletes old-origin stamps entirely on next observation — verified in simulated flow (Step 12).

## CFB CANARY

Northwestern @ Indiana / Total Over 47.5 (`pick 3d1b1fcf-…`):

```
X-Canonical-Revision:      922
X-Canonical-Version:       3de2ccb12800dcf5
X-Canonical-Generation-Id: gen_20260921T082226_9c49a36a

event:               Northwestern Wildcats @ Indiana Hoosiers
market:              Total Points Over 47.5
line / odds:         47.5 / -115
win_probability:     47.17 %
implied_probability: 53.4884 %       ← canonical number, no "undefined%"
edge_percent:        -6.3184
lock_score:          56.4
grade:               Pass
cfb_engine_version:  cfb_sp_game.v3.2026-06-signfix
```

Preview and Expo Go MUST converge on these values under the same canonical epoch. The pre-fix 96.69 % / Lock 98 cannot win a race after rev=922 is accepted — either it arrives as `rev < 922` (stale, ignored) or its response is discarded by `load()`'s global epoch guard.

Historical Intelligence for this pick uses `historical-intelligence|3d1b1fcf-…|L10|ALL` as its consumer key → registered with the shared registry → auto-revalidates on next epoch advance under the same origin/epoch.

Pick-detail `BOOK IMPLIED` bento now guards → renders `53.4884%` or `—` (never `undefined%`).

## TARGETED TEST COUNT

- Backend: **6 new** targeted tests (`test_canonical_epoch_contract_v2.py`) — all PASS.
- Frontend: **25 new** targeted tests (`canonical_epoch_contract.runner.js`) — all PASS.
- **Total: 31 targeted tests for the shared contract.**

Full sport / model suites intentionally NOT rerun.

---

Handed back for one physical Expo Go check. **NOT published.** Full report at `/app/memory/universal_freshness_contract_closure_2026_06_21.md` (v1) + `/app/memory/canonical_epoch_v2_closure_2026_06_21.md` (this file).
