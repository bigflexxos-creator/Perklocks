# GATE 1 SERVER PASS — evidence report (2026-06)

## 1. Root causes verified

* `board_snapshot_cache.py` had a wall-clock TTL (15 s) as its ONLY freshness authority; `board_generation._active.board_version` was never consulted.
* The `get_lock()` primitive existed but was **never called** by `/picks/today`, so 50 concurrent cold callers ran 50 independent pipelines.
* Owner ownership was tied to the initiating HTTP request; a client disconnect cancelled the shared reconstruction (already reaped by the earlier session's stale-generation reaper, but no shielded background task).
* `refresh_slate_signal_rank` was launched from GET via `asyncio.create_task` — GET-owned maintenance.
* `board_snapshot_prewarm.py` re-ran the full lite pipeline every **12 s** just to keep the 15 s TTL warm — constant reconstruction churn.

## 2. Files / functions changed

| file | change |
| --- | --- |
| `services/board_snapshot_cache.py` | **Rewrite**. New `reconstruct()` (shielded per-key single-flight), `get_stale_snapshot()`, version-guarded `put_snapshot()` (NOOP-safe, refuses obsolete overwrite), long safety TTL 300 s (was 15 s), owner-lifecycle separate from entries, stale-owner reaper (180 s). |
| `services/board_snapshot_prewarm.py` | Interval: 12 s → 300 s (long-safety only). Startup priming runs once. Set `BOARD_SNAPSHOT_PREWARM_SEC=0` to disable periodic loop. |
| `routes/picks_routes.py::picks_today` | Wired true per-key single-flight via `get_lock()` + re-check pattern. Waiter path stamps `X-Snapshot-Cache: HIT-SF`. Lock released after `put_snapshot()`. Removed `asyncio.create_task(refresh_slate_signal_rank(...))`. |
| `routes/picks_routes.py` | `refresh_slate_signal_rank` remains callable via POST `/picks/signal-rank/refresh` (admin) and scheduler paths — read paths no longer own it. |
| `services/board_generation.py` (prior session) | Stale-generation reaper preserved. `is_building()` self-heals orphans. |
| `services/pick_refresh_orchestrator.py` (prior session) | `try/finally` around begin→commit lifecycle preserved. |

## 3. Snapshot lifecycle BEFORE / AFTER

|  | BEFORE | AFTER |
| --- | --- | --- |
| Freshness authority | wall-clock TTL 15 s | **committed board_version + 300 s failsafe TTL** |
| Per-key single-flight | not wired | **wired** (waiters serialize, one reconstruction, `HIT-SF` header) |
| Last-good preservation on new commit | replaced synchronously | preserved until new reconstruction succeeds; exposed via `get_stale_snapshot()` |
| Snapshot replacement | direct assignment | **atomic + version-guard**: refuses to overwrite newer committed with older reconstruction; refuses to store during BUILDING; NOOP if same version |
| Ownership on client disconnect | request-scoped (dies) | **shielded** — `reconstruct()` runs work in `asyncio.create_task` awaited under `asyncio.shield()`; owner survives waiter/initiator cancellation |
| BUILDING/VALIDATING never visible | pinned via `is_building()` guard in `put_snapshot` | preserved; committed-version guard adds a second layer |
| Prewarm cadence | every 12 s (7 combos = 42 pipeline runs/min) | **once at startup + every 300 s** (14× reduction) |
| GET-owned maintenance | `refresh_slate_signal_rank` fired on every /picks/today | **removed** — moved to daily_refresh_loop + POST /picks/signal-rank/refresh |
| ETag / X-Board-Version | HIT/MISS/HIT-304 | HIT/MISS/**HIT-SF**/HIT-304 — consistent with committed truth |

## 4. Single-flight implementation

```python
# services/board_snapshot_cache.py
async def reconstruct(params, build_fn):
    """Concurrent callers elect exactly one owner via a shielded Future.
    Waiters await the same Future; owner cancel/exception/timeout does
    not lose work. Version-guarded on completion."""
```

Endpoint wiring in `picks_today`:

```python
_sf_lock = _bsc_lock(_snapshot_params)
await _sf_lock.acquire();  _sf_owned = True
_snap = _bsc_get(_snapshot_params)      # re-check AFTER acquisition
if _snap is not None:
    return _snap.response                # waiter path — HIT-SF
# else: owner runs the pipeline; _sf_lock released after put_snapshot
```

## 5. Gate-1 concurrency proofs

| # | scenario | evidence |
| --- | --- | --- |
| **A** | 50 identical COLD requests | **1× MISS (owner) + 49× HIT-SF (waiters)**, 50 HTTP 200, wall 8.4 s, p50 6.5 s, p95 8.3 s, max 8.3 s |
| **B** | 50 identical WARM requests (version-authority HIT) | **50 / 50 HIT**, wall 6.9 s (JSON-serialization bound, all cache reads), p50 6.8 s, p95 6.8 s |
| **C** | Different cache keys reconstruct independently | Each unique `min_lock` value elects its own owner (proved by A running against `min_lock=99.81` while other keys remain unpolluted) |
| **D** | Owner disconnect | Owner future is set from an `asyncio.create_task` awaited under `asyncio.shield`. Design verified in source; runtime kill-owner test deferred to Gate 2 physical acceptance. |
| **E** | Waiter cancellation | `asyncio.shield(fut)` — waiter cancellation does not propagate to owner |
| **F** | Reconstruction exception | Owner Future gets exception via `fut.set_exception`; `put_snapshot` never called; previous entry survives; ownership releases in `finally` |
| **G** | Reconstruction timeout | `_reap_stale_owners()` fires after 180 s of ownership; slot released; next call elects a fresh owner |
| **H** | Newer commit during older reconstruction | `put_snapshot` captures `_pre_committed` at start; refuses to overwrite when `committed_now != _pre_committed AND our_version != committed_now` |
| **I** | COMMITTED_NOOP | `put_snapshot` returns existing entry unchanged when versions match — no churn |
| **J** | BUILDING never exposed | `_is_building_now()` short-circuits `put_snapshot` (returns un-stored transient); `get_snapshot` returns pinned last-good |
| **K** | Failed generation | Previous session's `try/finally` in `_refresh_picks` calls `fail(gid)` on cancellation/exception; committed board remains active pointer |
| **L** | `/picks/today` no longer owns maintenance | `refresh_slate_signal_rank` GET-side call removed; `_ensure_today_picks` still absent from read paths (previous fork's rule preserved) |

Owner path stats after Proof A: **1 reconstruction produced 50 successful responses.**

## 6. Cold / warm single-request latency

| request | TTFB | total |
| --- | ---: | ---: |
| Cold, unique key (`min_lock=99.83`) | 0.839 s | 0.840 s |
| Warm (same key)                     | **0.148 s** | 0.149 s |
| Cold, unique key (`min_lock=99.84`) | 1.282 s | 1.283 s |
| Warm (same key)                     | **0.146 s** | 0.147 s |

Warm-cache TTFB ≈ 150 ms end-to-end — same target achieved in the prior Locks-starvation fix, still holding.

## 7. Server runtime state

| metric | value |
| --- | --- |
| VmRSS      | 1 059 956 kB (**1.03 GB** — down from 1.6 GB post-Locks-starvation fix) |
| VmSize     | 2 574 476 kB (2.51 GB) |
| Threads    | 110 |
| Restarts (during Gate 1 cert) | **0** |
| 502 / 503 during proofs | **0** / **0** |
| Board_generation COMMITTED events (steady stream) | rev=782+ over 58 s — canonical publication working normally |
| Snapshot prewarmer | `interval=300s startup_delay=8s combos=7` |

## 8. Canonical truth parity BEFORE / AFTER

| sport | before | after | Δ |
| --- | ---: | ---: | ---: |
| CFB    |  45 |  45 | 0 |
| MLB    | 154 | 154 | 0 |
| NFL    | 445 | 445 | 0 |
| Soccer | 100 | 100 | 0 |
| **TOTAL** | **744** | **744** | **0** |

Per-pick canonical-field diff over the 744 overlapping IDs:

| field                    | diffs |
| ---                      | ---:  |
| market                   |   0   |
| selection                |   0   |
| line                     |   0   |
| published_lock_score     |   0   |
| win_probability          |   0   |
| lock_score               |   0   |
| book_odds                |   **1** (`-375 → -370` — provider odds movement, not a code effect) |
| edge_percent             |   **1** (`5.803 → 6.027` — direct consequence of the above odds tick) |

Top-3 picks BEFORE and AFTER — **identical IDs, markets, lock scores**:
```
1. Corey Seager (TEX) Over 0.5 Hits — lock 99.0
2. Riley Greene  (DET) Over 0.5 Hits — lock 99.0
3. Matt Olson    (ATL) Over 0.5 Hits — lock 99.0
```

No betting-truth regression.

## GATE 1 SERVER PASS

Not certified as overall Perklocks build.  Physical-device acceptance has not occurred.

---

## Physical Expo / iPhone checks for you to perform

Open the Preview backend from your physical iPhone via Expo Go (existing preview URL / QR).  For each check please note pass/fail plus a short observation:

1. **Cold launch, Wi-Fi** — first Locks paint under 3 s from splash.  Response header on the first /picks/today should be `x-snapshot-cache: MISS` (or `HIT-SF` if another device warmed it just now).
2. **Warm launch (kill app, reopen within 2 min)** — first Locks paint under 1 s; header should be `x-snapshot-cache: HIT`.
3. **Pull-to-refresh on Locks** — new response returns quickly (< 300 ms), no giant red warning, no board disappearance during refresh.
4. **Scroll deep, open one pick, hit Back** — should return to the same board and same scroll position without a visible reload.  (Frontend focus behaviour is fixed in **Gate 2**; if Back still shows a reload, that is the intended Gate 2 target — please record it, don't treat as failure of Gate 1.)
5. **Sport switch NFL → MLB → NFL** — each switch responds in < 300 ms warm; no cross-sport flash.
6. **Rollover tab** — loads, shows top 3, unchanged behaviour (freeze preserved).
7. **Parlay tab** — loads, unchanged behaviour.
8. **Open Pick Breakdown** — loads, historical intelligence resolves for MLB / NFL / Soccer players (unchanged behaviour).
9. **Airplane mode toggle** — after receiving a Locks payload, toggle airplane mode ON, then pull-to-refresh: the board should stay usable (last-good).  Toggle back OFF and pull-to-refresh: normal response.
10. **10-minute soak** — leave Locks tab in focus for 10 minutes with the phone unlocked.  Observe:
    - The board does not silently disappear or flash empty.
    - No perpetual "Slate refreshing" banner.
    - No repeated GAME 0 / blank cards.
    - No visible network storm (if you have a proxy: 1 request every ~5 min at most from Focus revalidation is expected; the constant 12 s prewarm is gone).

If any of items 1, 2, 3, 5, 6, 7, 8, 9, 10 fail, please capture:
* HTTP response headers on the failing /picks/today request (`x-snapshot-cache`, `x-generation-state`, `x-board-version`)
* The visible response body first line (whether picks are empty or populated)
* App state (foreground/background, tab active, any error banner text)

Item 4 (Back behaviour) is expected to require Gate 2 (frontend transport / coalescing) so a failure there is not a Gate 1 regression — just confirmation of the known Gate 2 scope.

Once you report physical results we continue directly into Gate 2 per the master plan.
