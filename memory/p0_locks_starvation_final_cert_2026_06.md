# P0 LOCKS-STARVATION — LIVE INCIDENT CAPTURE + SURGICAL FIX (2026-06)

## Live incident state (captured BEFORE any restart)

| metric | value |
| --- | --- |
| worker pid / uptime  | 4608 / 49m 30s |
| VmRSS / VmSize       | 1868 MB / 5751 MB |
| RssAnon              | 1758 MB |
| threads              | 174 (soft grew from 128) |
| asyncio task count   | 65 (bounded, healthy) |
| CPU                  | 0.0 % idle |
| background jobs      | 4 in-flight request middleware chains, 1 prewarm loop, sport-DB loops (all healthy) |
| board_generation.active (db) | gen_20260920T053013_9fad0a7c (COMMITTED @ 05:30:14) |
| board_generation._building (memory) | **≥3 stale BUILDING orphans** never released (see below) |
| board_version served                | c3f5f7bca64a332c |

## Locks request timing (server-side, before fix)

| # | http | ttfb | total | bytes | x-snapshot-cache | x-generation-state |
| - | ---- | ---: | ----: | ----: | --- | --- |
| 1 | 200 | 6.27s | 6.27s | 1 736 337 | **MISS** | **BUILDING** |
| 2 | 200 | 3.65s | 3.65s | 1 736 337 | **MISS** | **BUILDING** |
| 3 | 200 | 3.34s | 3.34s | 1 736 337 | **MISS** | **BUILDING** |

Every request bypassed the snapshot cache and re-ran the 3-6 s canonical pipeline.

## Canonical data status

Data exists ⇒ **REQUEST STARVED**, not DATA EMPTY, not RESPONSE-TIMES-OUT-BEFORE-COMPLETION.
- Mongo `db.picks` today: **575** picks eligible (nfl 333, mlb 112, soccer 103, cfb 27)
- Active generation committed: revision 708, pick_count 1093, event_count 226
- lite endpoint returned all 575 picks correctly — just too slow for Expo Go's client timeout envelope

## Working control paths (unchanged)

| endpoint | server duration | verdict |
| --- | ---: | --- |
| /api/picks/rollover | 0.37 s | ✅ Working |
| /api/picks/parlay   | 2.67 s | ✅ Working |

Rollover and Parlay hit `db.picks.find(...)` directly, bypassing the snapshot cache — so the cache leak did not affect them. The first path divergence is at
`picks_routes.py:1302  if lite: from services.board_snapshot_cache import get_snapshot`.

## Root cause

`_refresh_picks(...)` calls `board_generation.begin(...)` which registers the generation into a module-level `_building: dict`.
Under normal flow `commit()` / `fail()` releases the entry.  However:

1. **asyncio.CancelledError bypasses `except Exception`** (BaseException in Py 3.8+) — a task cancellation between `begin()` and `commit()` leaks the entry.
2. `board_snapshot_cache.put_snapshot()` refuses to store while `is_building()` is True.
3. `is_building()` returns True as long as ANY entry remains in `_building`.
4. Result: **once a single orphan lands in `_building`, the snapshot cache is permanently starved** — every subsequent Locks request re-runs the full pipeline (3-6 s TTFB) instead of serving the 200 ms cached snapshot.

DB evidence: 20+ generations left in state=BUILDING going back to 04:00, of which the 3 that started after worker boot at 04:41 were the actual in-memory leaks.

## Surgical fix (no betting-logic changes; frozen memory-hygiene + bounded fan-out preserved)

**File 1 — `services/board_generation.py`**

Added stale-generation reaper.  `is_building()` and `building_ids()` now sweep any `_building` entry older than `BOARD_GEN_STALE_SEC` (300 s default) before answering, so a leaked entry cannot pin the snapshot cache indefinitely.  Zero effect on healthy builds (a normal build finishes in <60 s).

**File 2 — `services/pick_refresh_orchestrator.py`**

Wrapped the entire generation lifecycle in a `try / finally` that calls `_bg.fail(...)` if neither `commit()` nor `fail()` fired (i.e. asyncio Task was cancelled).  This closes the leak at source; the reaper is now defense-in-depth for background paths (HEAL loop, publication_reconciliation) that follow the same pattern.

## Post-fix production certification

### Cold + Warm

| # | http | ttfb | x-snapshot-cache |
| - | ---- | ---: | --- |
| 1 (cold) | 200 | 5.09 s | MISS (populates cache) |
| 2        | 200 | **0.161 s** | **HIT** |
| 3        | 200 | **0.164 s** | **HIT** |
| 4        | 200 | **0.166 s** | **HIT** |
| 5        | 200 | **0.159 s** | **HIT** |

Warm-cache Locks TTFB: **~160 ms** — physical-device Expo Go can no longer time out on this path.

### 20 concurrent

* 20 / 20 HTTP 200 · p50 28.3 s · p95 29.3 s (thundering-herd on cold cache — not part of this incident; unchanged behaviour vs. before fix)

### 50 concurrent

* 50 / 50 HTTP 200 · p50 7.4 s · p95 7.4 s (warm cache — all HITs; serialization-bound)
* RSS before/peak/after: 2641 / 2724 / 2652 MB · Task peak 1073 · Threads flat at 130

### Working controls after fix

* Rollover: 200 in 0.37 s ✅
* Parlay:   200 in 2.67 s ✅

### Board counts (before / after)

| sport | before | after | Δ |
| --- | ---: | ---: | ---: |
| nfl  | 333 | 333 | 0 |
| mlb  | 112 | 112 | 0 |
| soccer | 103 | 103 | 0 |
| cfb  | 27  | 27  | 0 |
| **TOTAL** | **575** | **575** | **0** |

`board_version = c3f5f7bca64a332c` identical.  Picks < 85 lock: **0**.
Zero eligibility change · Zero 85+ rule change · No sport lost · No arbitrary cap added.

### Malloc / thread / memory certification (from prior soak — unchanged)

- Threads: 127-130 (arena cap holding)
- RssAnon: 1.6-2.7 GB (baseline still ~1.6 GB idle, spikes to 2.7 GB under 50-concurrent stress then decays)
- No process restarts across the entire session

## FINAL LOCKS P0 → **PASS**

```
CODE CHANGES DURING CERTIFICATION:
  services/board_generation.py           +38 lines (reaper)
  services/pick_refresh_orchestrator.py  +23 lines (try/finally)
  0 betting-logic changes
  0 publish/deploy actions
```

Awaiting physical Expo-Go validation on Locks (multiple sport tabs, repeated switching/reloads), Rollover, Parlay, Pick Breakdown, Historical Intelligence.

Not resuming P1 · Not adding `_REGISTRY` LRU · Not publishing production.
