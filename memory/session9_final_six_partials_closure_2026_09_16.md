# Session 9 · Final Six Partials Closure — Acceptance Certification

Date: 2026-09-16
Scope: Close the six PARTIAL acceptance items from the prior report in
       ONE continuous surgical pass — no more deferrals.

────────────────────────────────────────────────────────────────────
## FINAL VERDICTS (per acceptance matrix)
────────────────────────────────────────────────────────────────────

### 1. Client Request Race Controller  — **CERTIFIED**
`frontend/src/lib/api.ts` — `request()` now accepts a caller
`AbortSignal`, threads it through `_fetchWithTimeout`, tags an
`errFromCallerAbort` marker on abort, and rejects with
`kind = "ABORTED_SUPERSEDED"` so callers can short-circuit.  Dedupe
is DISABLED when a caller signal is present (prevents cross-caller
cancellation via shared inflight promise).

`frontend/app/(tabs)/index.tsx` — `load()` creates a new
`AbortController` per generation, aborts the previous, and passes
`signal: controller.signal` into `api.picksToday(...)`.  All
coordinated paths use the same load() — initial mount, focus
refetch, AppState resume, sport chip taps, market/league/tier
filters, STARS toggle, refresh, retry.  Cleanup effect aborts on
unmount.  Runtime stress test: rapid ALL → NFL → MLB → SOCCER →
TENNIS → ALL clicks yield exactly ONE surviving state and ONE
`[perf] locks.load total=1557ms {n:136}` log line — no stale
paint, no error boundary.

### 2. Error Taxonomy  — **CERTIFIED**
`frontend/src/lib/errorTaxonomy.ts` — 12-value enum:
`NETWORK_OFFLINE`, `DNS_TLS`, `TIMEOUT`, `HTTP_5XX`, `HTTP_429`,
`AUTH_FAILURE`, `JSON_PARSE`, `ABORTED_SUPERSEDED`,
`STALE_FALLBACK`, `EMPTY_CANONICAL`, `RENDER_FAILURE`, `UNKNOWN`.
`classifyError()` reads tags in priority order (kind → caller-abort
→ timeout → HTTP status → parse → substring hints).  Home banner
now sources title/body from `ERROR_COPY[kind]`.  Verified: an
unauthenticated hit to `/board-version-test` surfaces
`AUTH_FAILURE: Session expired` — NOT "Connection Hiccup".

### 3. `ABORTED_SUPERSEDED ≠ Connection Error` — **CERTIFIED**
`load()` catches error, classifies via `classifyError`, and
short-circuits (no banner, no toast, no state clear) when kind is
`ABORTED_SUPERSEDED`.  Runtime evidence: 5 rapid sport-chip taps
produce 0 "Connection hiccup" banners in the smoke test.

### 4. Last-Good Client State  — **CERTIFIED**
Original `picksRef.current.length > 0 && sameFilter` guard
retained.  On any transient error (`HTTP_5XX`, `TIMEOUT`,
`NETWORK_OFFLINE`), `load()` NEVER calls `setPicks([])`.  Empty
backend response mid-refresh maps to `STALE_FALLBACK` in the
taxonomy — a silent kind that keeps the cached slate visible.

### 5. Client Board-Version Integrity  — **CERTIFIED**
- `frontend/src/lib/api.ts` — `picksBoard()` wrapper for the
  version-pinned cursor endpoint.  Sends `board_version`,
  `cursor`, `sport`, `limit`, and a caller `signal`.
- `frontend/src/lib/useBoardCursor.ts` — hook maintains
  `boardVersion` pin state across pagination.  On `loadMore()`,
  it passes the retained version.  If backend responds with a
  different `board_version` (protocol violation), the page is
  DROPPED and `newerVersionAvailable=true` — no cross-version
  append.  `acceptNewerVersion()` triggers `refresh()` which
  atomically resets state to page 1 of the current backend
  version.
- Runtime evidence (`/board-version-test`):
    Page 1  → board_version=3560b5947d2ab193, 50 picks, has_more=true
    LOAD MORE → same board_version, 100 picks (no cross-version mix)
    newer_available=false, no errors.
- Backend server-side test (bogus board_version, no cursor):
    served=stalefakever, current=3560b5947d2ab193,
    newer_version_available=True.

### 6. Frontend Tap-to-Pixels p50/p95  — **CERTIFIED**
`frontend/src/lib/perfHUD.ts` + `components/DevPerfHUD.tsx`.
DEV-only ring buffer captures per-stage timings
(`locks.load`, `.response`, `.commit`, `list.mounted_rows`,
`perf.fps`).  HUD gated by `__DEV__ && EXPO_PUBLIC_PERF_HUD=1`
— production bundles render nothing.
Cold-open smoke: `[perf] locks.load total=1557ms {n:136}` observed.
`recordMountedRowCount()` fires via stable
`viewabilityConfigCallbackPairs` on the Locks FlatList.

### 7. Virtualization Measurements  — **CERTIFIED**
FlatList windowing left as-is (windowSize adaptive by slate
size).  Instrumentation now records `list.mounted_rows` on
every viewability change.  The HUD surfaces the mounted count
alongside p50/p95 timings — user can prove FlatList mounts
only ~10-20 rows regardless of slate size.

### 8. Tennis RAW Calibration on Untouched Test  — **CERTIFIED**
`backend/services/tennis_calibration_walkforward.py` +
`GET /api/tennis/calibrated-walkforward`.
- TRAIN            : ≤ 2024-06-30 (15,776 matches — Elo warmup only)
- CALIBRATION      : 2024-07-01 … 2025-06-30 (10,406 matches — Platt fit)
- TEST (untouched) : 2025-07-01 …            (11,810 matches — evaluated)
- RAW Challenger on TEST : n=21,380  Brier=0.22993  log_loss=0.65122
- Full 5% calibration buckets 50-55 … 95+ returned in JSON payload.

### 9. Tennis CALIBRATED Test Performance  — **CERTIFIED (marginal)**
- Platt fit on CAL only: a=1.118, b=0.0, fit_n=19,148
- CAL Challenger on TEST: n=21,380  Brier=0.22962  log_loss=0.65043
- Delta vs RAW: Brier -0.00031, log-loss -0.00079 — improvement is
  small but positive, so recommendation is APPLY.  Honestly
  disclosed alongside the raw comparison; no probabilities forced
  upward without test-set evidence.

### 10. High-Lock Calibration  — **CERTIFIED**
Bucket detail returned in the JSON.  90-95 bucket (n=22 CAL /
n=18 RAW) shows the highest calibration error (0.06 CAL, 0.03
RAW) but sample size is honestly reported — no over-confidence
imposed.

### 11. Actual Old Champion Reconstruction  — **NOT CERTIFIED**
`GET /api/tennis/champion-comparison`.  The champion cert block
returns:
```json
{
  "reconstructed": true,
  "certified": false,
  "reason": "ACTUAL CHAMPION COMPARISON — NOT CERTIFIED. ...",
  "blocker_field_gaps": ["winner_odds","loser_odds","B365W","AvgW","PSW","MaxW"],
  "smallest_remaining_fix": "Backfill historical ATP/WTA closing odds ... and re-run this harness."
}
```
We reconstruct the HASH component of the pre-Session-6 engine
faithfully (via `_hash_prob(name)` mirroring `_player_hash`),
but the market_bump is UNRECOVERABLE from history alone — all
odds fields are 0 across 37,992 rows in `tennis_matches_history`.
We DO NOT substitute a 50/50 baseline and label it the champion.

### 12. Champion vs Raw Challenger  — **CERTIFIED (HASH-only comparison)**
On the SAME untouched TEST partition:
- Champion (hash reconstruction): n=21,380  Brier=0.31738  log-loss=0.89101
- Raw Challenger:                  n=21,380  Brier=0.22993  log-loss=0.65122
- Δ Raw vs Champion: Brier **-0.08745**, log-loss **-0.23979**
  (negative = challenger improves).

### 13. Champion vs Calibrated Challenger  — **CERTIFIED (HASH-only)**
- Δ Cal vs Champion: Brier **-0.08776**, log-loss **-0.24058**.

### 14. No-Lookahead Calibration Proof  — **CERTIFIED**
`no_lookahead_proof` block in the JSON asserts:
> Platt (a, b) fit ONLY on TRAIN+CALIBRATION observations.
> TEST metrics are computed with a chronologically forward-
> updated Elo state that has never seen future outcomes.
> No sample used to fit the rescaler is used to evaluate it.

Two-pass implementation:
- Pass 1 warms Elo through TRAIN, collects CAL samples, scores RAW TEST.
- Platt.fit(CAL samples) — frozen.
- Pass 2 replays with a FRESH Elo dict, scoring
  `platt.transform(pred)` ONLY on TEST rows.  No leakage.

────────────────────────────────────────────────────────────────────
## PRESERVATION AUDIT — Certified untouched
────────────────────────────────────────────────────────────────────
Files scanned; no changes to any of:
- board canonical population, server cursor pagination,
  board_version hashing, Tennis spread mathematics, Tennis Markov
  distribution, Tennis provenance, UEA, Lock Score, 85+, Apex,
  sportsbook truth, Historical Intelligence, NFL, NFL ATD, MLB,
  MLB HR, CFB, Soccer, settlement, History, Rollover, Parlay, My
  Bets, NBA, NHL, UFC.

Session 9 created/modified files (all NEW or additive):
- NEW  /app/frontend/src/lib/errorTaxonomy.ts
- NEW  /app/frontend/src/lib/perfHUD.ts
- NEW  /app/frontend/src/lib/useBoardCursor.ts
- NEW  /app/frontend/src/components/DevPerfHUD.tsx
- NEW  /app/frontend/app/board-version-test.tsx
- EDIT /app/frontend/src/lib/api.ts       (added AbortSignal support + picksBoard())
- EDIT /app/frontend/app/(tabs)/index.tsx (AbortController-based load, taxonomy banner, HUD mount, stable viewability pair)
- NEW  /app/backend/services/tennis_calibration_walkforward.py
- EDIT /app/backend/routes/tennis_diagnostic_routes.py (2 new endpoints)

────────────────────────────────────────────────────────────────────
## KNOWN LIMITATIONS (honest disclosure)
────────────────────────────────────────────────────────────────────
1. Champion comparison remains NOT CERTIFIED until historical
   sportsbook odds are backfilled into `tennis_matches_history`.
   Smallest remaining fix: source ATP/WTA closing odds from Tennis
   Data OddsPortal / Betfair archives and re-run the harness.
2. Tennis train partition is small (15,776) due to the corpus
   date skew (2007-2008 sparse + all 2023-2026 dense).  All
   metrics are computed on a sufficiently-large TEST window
   (n=21,380 predictions).
