# Universal Preview ↔ Expo Go Canonical Freshness Contract — CLOSURE (2026-06-21)

**Scope**: One shared client freshness contract used across every canonical
surface, every sport, every screen. NOT another CFB fix. Preserves offline
last-good behaviour, cannot dead-end the user, self-heals on network reconnect.

**Not published to production.** Handed back for one physical Expo Go check.

---

## ROOT CAUSE

Server-side canonical fingerprints (`board_version`, `generation_id`, `ETag`,
`served_by`) were mature and centred on `/api/picks/today`. Only Locks board
benefitted:

- Client `useSWR` cache was keyed purely by `id` / query-params, TTL 15 s / 30 s.
- Pick-detail + Historical-Intelligence + rollover + parlay + my-bets + lab +
  analytics **did NOT participate in ETag**; no server fingerprint reached
  those cache rows.
- `AsyncStorage` LKG snapshot had no board-version stamp, so a cold-boot with
  stale rows had no way to know a rescore had happened.
- The Locks-tab-only AppState foreground reconciler never reached devices that
  stayed on any other screen.

Net effect: after a canonical rescore/republish the Preview (web) session
picked up the new numbers on next fetch, but an Expo Go device that never
revisited the Locks tab kept serving stale data indefinitely.

---

## SHARED FILES CHANGED

### Backend
| File | Purpose |
|---|---|
| `middlewares/__init__.py` | new package |
| `middlewares/board_version_header.py` | new — stamps `X-Canonical-Version` + `X-Canonical-Generation-Id` on every canonical GET response; throttled cross-process resync of `board_generation._active` every 8 s |
| `server.py` | registers `BoardVersionHeaderMiddleware`; adds `X-Canonical-Version` (and friends) to CORS `expose_headers` so browsers can read them |
| `scripts/maintenance/cfb_signfix_v3_rescore.py` | at completion bumps `updated_at`/`last_seen_at` on rescored rows and calls `board_generation.begin/commit` — makes the fingerprint advance after any in-place mutation |

### Frontend
| File | Purpose |
|---|---|
| `src/lib/boardFreshness.ts` | new — observer that owns `_current` version, persists to AsyncStorage, publishes `noteBoardVersion` (returns `first`/`same`/`new`/`ignored`), `subscribeBoardVersion`, `getCurrentBoardVersion`, `hydrateBoardVersion` |
| `src/lib/appStateFreshness.ts` | new — installs one AppState listener that debounces a `/api/version` ping on every background→foreground transition; response flows through the observer via the shared transport hook |
| `src/lib/api.ts` | reads `X-Canonical-Version` inside `_fetchWithTimeout`, pushes to `noteBoardVersion` on EVERY response body regardless of endpoint / sport |
| `src/lib/useSWR.ts` | `Snapshot<T>` carries `bv?: string | null`; write path stamps entries with `getCurrentBoardVersion()`; module install subscribes to advance events and sweeps every stamped entry that no longer matches the new version; dep-change effect additionally force-refetches when the cached entry's stamp is behind current |
| `app/_layout.tsx` | at boot hydrates the freshness observer and installs the AppState reconciler |

### Tests
| File | Purpose |
|---|---|
| `backend/tests/test_universal_canonical_freshness_contract.py` | 6 live-HTTP tests — header present on every canonical surface, uniform across surfaces at rest, matches `board_generation.active()`, body unchanged, denied on auth paths, N→N+1 stable when idempotent |
| `frontend/__tests__/universal_canonical_freshness.runner.js` | 14 static + simulated-flow tests — observer contract shape, transport wiring, useSWR sweeper contract, layout wiring, and a full N→N+1 simulated advance flow proving stale entries are swept and post-advance writes carry the NEW stamp |

---

## VERSION / GENERATION INVALIDATION MECHANISM

```
┌────────────────────────────────────────────────────────────────────┐
│                        BACKEND (single source)                      │
│                                                                     │
│   services/board_generation._active["board_version"] ← monotonic    │
│         ▲                                                           │
│  commit() on any real rescore / republish                           │
│         │                                                           │
│  ┌──────┴──────────────────────────────────────────────┐            │
│  │ BoardVersionHeaderMiddleware                        │            │
│  │   · resync from Mongo every 8 s (cross-process)     │            │
│  │   · stamps X-Canonical-Version on every GET to      │            │
│  │     /api/picks/*, /api/rollover/*, /api/parlay/*,   │            │
│  │     /api/my-bets/*, /api/version, /api/lab/*,       │            │
│  │     /api/analytics/*, /api/me/*, /api/pinned/*,     │            │
│  │     /api/historical-intelligence/*                  │            │
│  └────────────────────────┬────────────────────────────┘            │
└───────────────────────────┼────────────────────────────────────────┘
                            │
                            ▼    CORS Access-Control-Expose-Headers
                            │    includes X-Canonical-Version so
                            │    browsers can read it too.
                            ▼
┌────────────────────────────────────────────────────────────────────┐
│                    FRONTEND (web + Expo Go share)                   │
│                                                                     │
│   api.ts::_fetchWithTimeout on EVERY response:                      │
│         noteBoardVersion(res.headers['X-Canonical-Version'])        │
│                        │                                            │
│                        ▼                                            │
│   boardFreshness._current  ← "new" ← "same" ← "first" ← "ignored"   │
│      · storage.setItem("board_version_v1", v)  (AsyncStorage)       │
│      · fires subscribers ONLY on advance ("new")                    │
│                        │                                            │
│                        ▼                                            │
│   useSWR sweeper: cache.delete(entry) for every entry whose         │
│      bv stamp !== next version                                      │
│                        │                                            │
│                        ▼                                            │
│   Next screen render on any cached key detects stampBehind and      │
│   forces silent refetch                                             │
└────────────────────────────────────────────────────────────────────┘
```

**Advance semantics**:
- `first`: seed the observer (no sweep) — protects a cold boot from erroneously discarding brand-new fetches.
- `same`: no-op (bandwidth-free).
- `new`: sweep every stamped entry that lags the new version. Automatic detail / HI / rollover / parlay / lab / analytics refetch on next read.
- `ignored`: empty header / non-canonical / auth path — no state change.

---

## OFFLINE LAST-GOOD BEHAVIOUR

- `AsyncStorage` LKG snapshot (`swr_lkg_v1`) continues to seed the cache at cold boot exactly as before — warm tabs still paint instantly with previous content.
- The freshness observer runs entirely on the RESPONSE side. If the network is unreachable, `noteBoardVersion` never fires, `_current` stays at the persisted last-good value, and cached rows serve as-is.
- The **moment** connectivity returns and any API call lands, the response header carries the fresh `X-Canonical-Version`. If it differs from `_current` the sweeper runs — cached rows go, next fetch replaces them.
- Nothing is dead-ended offline: the pick screens keep displaying the last-good data; only the **hidden** stamp state changes when the version turns out to be behind. The user sees a smooth re-populate as soon as they're online.

Persisted observer state is a single 16-char hex string (`board_version_v1`) — trivial to write / read, cannot get corrupted enough to brick the app.

---

## N → N+1 TEST RESULT

Contract suite: `backend/tests/test_universal_canonical_freshness_contract.py`

```
tests/test_universal_canonical_freshness_contract.py::test_canonical_version_header_present_on_every_surface PASSED
tests/test_universal_canonical_freshness_contract.py::test_canonical_version_matches_board_generation_active PASSED
tests/test_universal_canonical_freshness_contract.py::test_canonical_version_is_uniform_across_surfaces_at_a_moment PASSED
tests/test_universal_canonical_freshness_contract.py::test_canonical_version_advances_on_commit_N_to_N_plus_1 PASSED
tests/test_universal_canonical_freshness_contract.py::test_middleware_never_blocks_response_body PASSED
tests/test_universal_canonical_freshness_contract.py::test_canonical_version_not_emitted_on_auth_paths PASSED
6 passed in 13.11s
```

Live runtime probe (`/tmp/universal_freshness_n_to_n1_proof.json`) confirmed the header on 6 distinct canonical surfaces (Version, MLB detail, NFL detail, CFB detail, MLB HI, NFL HI) is **uniform** at rest. After a real advance triggered by the CFB signfix rescore commit tail, every one of the 6 surfaces reported the SAME new value — Preview and Expo Go converge on identical truth.

Client-side simulated N→N+1 flow (`__tests__/universal_canonical_freshness.runner.js`): **14/14 PASS**

- first version is 'first' and does NOT trigger a sweep
- advance N → N+1 sweeps every cache entry with an older stamp
- same version is 'same' and preserves the cache
- empty note is 'ignored' — offline / no-header responses no-op
- post-advance writes carry the NEW stamp, not the old one

---

## PREVIEW ↔ EXPO RESULT

Native (Expo Go, iOS / Android):
- RN `fetch` returns `X-Canonical-Version` directly — no CORS gate.
- Shared `api.ts` transport hook fires `noteBoardVersion` on every response.
- `AsyncStorage` stamp + sweep works identically.

Web (Preview browser):
- Backend CORS now includes `expose_headers = ["X-Canonical-Version", ...]`.
- Confirmed at runtime: after login, `localStorage.getItem("board_version_v1")` returns the current fingerprint (`"6ab4b577d773451c"` in the acceptance run).

Both surfaces use the SAME shared code path (`src/lib/api.ts` + `src/lib/boardFreshness.ts` + `src/lib/useSWR.ts`). No sport-specific fork, no per-endpoint patch, no manual clear-cache button.

---

## TARGETED TEST COUNT

- **Backend contract**: 6 new (`test_universal_canonical_freshness_contract.py`)
- **Frontend contract**: 14 new (`__tests__/universal_canonical_freshness.runner.js`)
- **Prior P0 suite (regression control)**: 25 pass (freshness signfix + Soccer HI + HI status semantics + sign-flip regression)
- **Existing runners**: `__tests__/main40_body_read_timeout.runner.js` 7/7 pass (regression protection over the mutated `_fetchWithTimeout`)

**Total new tests targeted at the shared contract only: 20 (backend 6 + frontend 14).**

Full existing sport / model suites were NOT rerun — per directive.

---

## GUARDRAILS HONOURED

- No sport / model changes.
- No sport-specific invalidation hack.
- No manual AsyncStorage clear.
- No breaking of ETag / 304 fast path.
- No breaking of cursor pinning (per-endpoint `X-Board-Version` untouched).
- No production publish.
- No NHL / UFC / MLB history / NFL / Soccer / Tennis scoring changes.
- Offline last-good behaviour preserved.
- Cannot dead-end the user.

Handed back for one physical Expo Go check.
