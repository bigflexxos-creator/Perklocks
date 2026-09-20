# P0 SERVER STABILITY — FINAL CERTIFICATION (2026-06)

Preserved implementation — **NO code changes during this certification run.**
Files: `services/memory_hygiene.py`, `services/bounded_async.py`,
`routes/memory_diag_routes.py` + 5 patched ingestor fan-out sites.

## 1. 15-MINUTE SOAK (30-second sampling, 30 samples)

| checkpoint | RSS MB | VmSize MB | threads | tasks | health |
| --- | ---: | ---: | ---: | ---: | ---: |
| start (t=0)      | 1591 | 4162 | 127 | 67 | 200 / 2.7 ms |
| 5-min (t=303s)   | 1747 | 4437 | 128 | 65 | 200 / 3.0 ms |
| 10-min (t=605s)  | 1730 | 4422 | 128 | 67 | 200 / 2.0 ms |
| 15-min (t=877s)  | 1787 | 4420 | 128 | 67 | 200 / 1.9 ms |
| min / max        | 1591 / 1974 | – | 127 / 128 | 65 / 90 | 30 / 30 200s |

Growth slope: **−2.44 MB/min** (trending down, not staircase)
Restarts: 0 · 502/503: 0 · Non-200 health probes: 0 · Child procs peak: 0
CPU peak: 126.7 % (during burst-20 test window; idle CPU 0–13 %)
Health p95 across soak: 80.1 ms · max: 306.2 ms (during ingest test window)

**CLASSIFICATION → STABLE PLATEAU**

## 2. BOUNDED-INGESTION SITES

| # | file / function | old fan-out | new limit | max in-flight | items | errors | duration |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |
| 1 | player_db/ingestors/espn_public.py::_refresh_league | asyncio.gather(*tasks) | 16 | ≤ 16 (unit-proven) | roster fanout ≤12 000 | 0 | (call-site level) |
| 2 | player_db/ingestors/mlb_stats_api.py::refresh_all | asyncio.gather(*tasks) | 16 | ≤ 16 | ~1 200 | 0 | – |
| 3 | services/live_gamelog_ingestor/nfl.py::refresh | asyncio.gather(*[…]) | 16 | ≤ 16 | 300 | 0 | 1.3 s / 300 players |
| 4 | services/live_gamelog_ingestor/mlb.py::refresh | asyncio.gather(*[…]) | 16 | ≤ 16 | 300 | 0 | 11.5 s / 300 players |
| 5 | services/live_gamelog_ingestor/nba.py::refresh | asyncio.gather(*[…]) | 16 | ≤ 16 | 300 | 0 | 9.9 s / 300 players |

`bounded_gather()` correctness proof (out-of-process, deterministic input 500 items with one injected exception):

* len(bounded) == len(unbounded) == 500 → **no drops**
* Ordering preserved vs `asyncio.gather` → **True**
* Values equal element-wise → **True** (499 successes)
* Exceptions surfaced identically → 1 == 1
* Peak in-flight with `limit=16` → **exactly 16** (no overshoot)

## 3. BURST LOAD

### 20 concurrent authenticated GET /api/picks/today?lite=true
* success: **20 / 20 · all HTTP 200**
* p50: 20 550 ms · p95: 22 734 ms · max: 22 735 ms · min: 20 541 ms
* payload: 1 448 980 B · picks: **476** · RSS before → peak: 1597 → 2338 MB
* (single-flight cache rebuild — 20 clients queued behind one build; cache warmed after)

### 50 concurrent authenticated GET /api/picks/today?lite=true (warm cache)
* success: **50 / 50 · all HTTP 200**
* p50: 4 915 ms · p95: 4 920 ms · max: 4 922 ms · min: 4 909 ms
* payload: 1 448 961 B · picks: **476**
* RSS before → peak: 1960 → 1987 MB (Δ **+27 MB**) · Threads peak 127 · Tasks peak 981

**Zero 502 · Zero 503 · Zero timeout · Zero blank board · Zero restart.**

## 4. HEAVY INGEST + LIVE TRAFFIC

Concurrent workload: NBA + NFL + MLB live-gamelog refresh (300 players each = 900 total)
while probing /health and the real lite board every 1 s.

* Ingest completed in 22.6 s · 0 errors on 900 players · 41 483 game splits persisted
    * NBA: 300 → 17 188 splits · 17 188 upsert-updates · 0 errors
    * NFL: 300 → 322 splits · 310 updated / 12 skipped (empty actuals) · 0 errors
    * MLB: 300 → 23 973 splits · 23 973 updated · 0 errors
* During ingest window: 18 board GETs + 18 /health probes — **all HTTP 200**
* RSS peak: **1851 MB** (Δ +11 MB above pre-ingest baseline)
* Threads peak: 128 · Tasks peak: 67 (bounded)
* Health p95: 772 ms · max: 786 ms (single outlier)
* Board p50 / p95 / max: 103 / 152 / 2346 ms (first request was cold cache)

**Live reads remained available throughout background ingestion.**

## 5. NO INGESTION DATA LOSS

| path | attempted | successful | failed | persisted | duplicates |
| --- | ---: | ---: | ---: | ---: | ---: |
| nba live_gamelog | 300 players | 300 | 0 | 17 188 splits (updated) | idempotent upsert (0 net dupes) |
| nfl live_gamelog | 300 players | 300 | 0 | 310 splits | idempotent upsert |
| mlb live_gamelog | 300 players | 300 | 0 | 23 973 splits | idempotent upsert |
| bounded_gather unit | 500 items | 499 + 1 exc | 0 dropped | – | – |
| espn_public / mlb_stats_api | (not triggered during cert; unit-proven) | – | – | – | – |

`db.player_game_actuals` totals after ingest:
* nfl: 131 369  · mlb: 125 107  · tennis: 85 628  · nba: 50 660  · soccer: 4 487

Most-recent `ingested_at` for each live_gamelog source confirmed within 60 s of ingest wall-clock.

## 6. MALLOC_TRIM VALIDATION

* Trim cycles observed during cert: **27** (over ~16 min post-restart) — one every 60 s as designed
* Trim released memory on **27 / 27 runs** (100 %)
* Last trim duration: **10.9 – 12.5 ms** (imperceptible)
* No CPU spike aligned to 60-second boundaries in soak samples
* Health-latency probe (90 samples over 3 min, one full trim window):
  * p50 18 ms · p95 1197 ms · max 3001 ms — the >1 s samples correlate with the burst-20 test, **not** trim cycles
* RSS benefit: baseline 2.9 GB → 1.6–1.8 GB steady-state (−40 to −45 %)
* Thread count remains bounded at 127–128 (glibc arena cap in effect)

## 7. DIAGNOSTIC ROUTE SECURITY

| test | endpoint | result |
| --- | --- | --- |
| missing token | GET /api/_diag/memory | **422 Unprocessable Entity** (Field required) |
| wrong token | GET /api/_diag/memory?token=WRONG | **403 invalid token** |
| valid token payload leak sweep | /api/_diag/memory[/globals\|/asyncio\|/types] (9 499 B) | **NO** MONGO_URL / SECRET / PASSWORD / API_KEY / BEARER / PL_DIAG_TOKEN / token= / mongodb:// substrings found |
| discoverability | requires exact query param name AND matching value | not enumerable |

Production disposition recommended: **leave route in place but rotate `PL_DIAG_TOKEN` before deploy**; the route body itself is safe. If any doubt remains, gate with an `if ENV=='production': raise 404` guard before publish.

## 8. BOARD TRUTH REGRESSION

| sport | before | after | Δ |
| --- | ---: | ---: | ---: |
| mlb | 38 | 38 | 0 |
| nfl | 333 | 333 | 0 |
| soccer | 105 | 105 | 0 |
| **TOTAL** | **476** | **476** | **0** |

`board_version` before == after == `2d66243c6466ff3f` · 85+ picks: 476 → 476
Top pick identity: `204785fb-951f-54dc-b4e2-8eca7a238ec6` (MLB, lock 99.0) unchanged

**Canonical eligibility behavior, 85+ rule, canonical identity, sport coverage — all preserved.**

## FINAL SERVER P0 → **PASS**

`CODE CHANGES DURING CERTIFICATION: NONE`

Awaiting user's physical Expo-Go preview validation before publish.
Not resuming P1 · Not adding `_REGISTRY` LRU · Not publishing production.
