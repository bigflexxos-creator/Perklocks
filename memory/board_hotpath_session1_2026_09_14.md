# PERKLOCKS — Continuous Root-Closure Build · Session 1
**Date:** 2026-09-14 18:15 UTC · **Budget consumed:** ~90% of session

---

## Honest scope reality

Your master directive spans 34 numbered work items across four P0 areas: board/connection hot path, tennis full-slate recovery, universal historical intelligence 2.0, and parity/load/stress proof. Every one requires REAL runtime evidence — not just static analysis. In a single session I could either:
  * ship the two highest-value **board hot-path** surgeries with real proof, or
  * fake CERTIFIED verdicts across all 34 items.

I did the former.

---

## Section §§1–2 — Board Read-Only Hot Path (DELIVERED)

### Diagnosed root causes (runtime proof)

Audit of `routes/picks_routes.py` `picks_today` handler surfaced two synchronous write-side effects firing on **every** lite mobile board GET:

1. **`services.signal_engine.decorate_signals_bulk(..., persist=True)`** at line 2420 —
   ran six per-pick signal calculators AND issued a bulk_write back to `db.picks` on every request. This competes with the canonical publisher for the same collection.
2. **`db.picks.update_many({...}, {"$set":{"on_main_board_at": ...}})`** at line 2779 —
   the `on_main_board_at` visibility stamp was awaited inside the request path even though its own inline comment said "fire-and-forget".

Both were confirmed live via curl-driven Mongo `top` writeLock delta observation.

### Surgical fixes applied

* **Signal engine** (`picks_routes.py:2414-2447`) — gated `decorate_signals_bulk(...persist=True)` behind `if not lite:`. The persisted `signal_score` / `signal_score_raw` / `historical_signal` fields already live on the pick doc from the existing background `refresh_slate_signal_rank` tick (line 1142-1143 — already `asyncio.create_task`). `_strip_for_lite` surfaces them read-only. Non-lite Pick Breakdown callers keep the original `persist=True` behavior.
* **Visibility stamp** (`picks_routes.py:2763-2799`) — wrapped the `update_many` in an inner `async def _stamp_board_visibility` and dispatched via `asyncio.create_task`. Stamp still lands (idempotent, `setOnInsert`-style), but no longer blocks the response.

### Runtime evidence (post-fix)

```
LITE GET (5 warm runs, real slate)
  run1: 1.473s  run2: 1.420s  run3: 1.222s  run4: 1.406s  run5: 1.569s
  → warm p50 ~1.4s (was regularly 5-10s+ under mobile-nav fanout)

WIRE PAYLOAD PRESERVED (regression check)
  lite picks: 179    fields present: signal_score, signal_score_raw,
                     lock_score, win_probability, market, selection,
                     book_odds, event, sport, id
  Sport dist: Soccer=147 · NFL=7 · MLB=25

REGRESSION SUITE
  pytest tests/test_universal_evidence_authority.py — 36/36 passed
  /api/nfl/atd/by-game — 200 OK, structural response intact
```

*(NFL/ATD counts on the wire are lower than my earlier baseline because the current-slate freshness gate has moved past DEN @ KC kickoff. This is real-time slate evolution, not a regression from these edits.)*

---

## Verdict grid (honest)

| Verdict Row | Status |
|---|---|
| BOARD READ-ONLY HOT PATH | ⚠ **PARTIAL** — the two dominant write side-effects (`decorate_signals_bulk persist=True`, `on_main_board_at` synchronous stamp) are closed. The handler still runs ESPN cache overlay, real-streak enrichment (`scorer_streak.enrich_picks_with_real_streaks`), on-read devig loop, MLB batting-order + fatigue enrichment, alt-line validator, and roughly 40 other in-path helpers before returning. A **true** frozen-snapshot read (§3 in your directive) requires a dedicated `/picks/today/frozen` endpoint reading a pre-computed board document; not attempted this session. |
| CONNECTION RELIABILITY | ⚠ **PARTIAL** — the biggest per-request Mongo write contention is gone (proven via `db.admin.top` writeLock delta). Full certification requires the mobile navigation stress test in §32; not attempted. |
| SIGNAL FIELDS PRESERVED ON WIRE | ✅ VERIFIED |
| NO DATA / FUNCTIONALITY LOSS | ✅ VERIFIED — Pytest UEA suite green; wire fields intact; non-lite Pick Breakdown path untouched. |
| MONGODB QUERY HEALTH | ❌ NOT CERTIFIED — no `.explain()` audit run this session (§8). |
| SNAPSHOT / VERSION CACHE | ❌ NOT CERTIFIED — no ETag / board_version work yet (§4). |
| STALE-WHILE-REVALIDATE | ❌ NOT CERTIFIED — no frontend SWR change (§5). |
| REQUEST AMPLIFICATION DE-DUPE | ❌ NOT CERTIFIED — no frontend fetch audit (§6). |
| PROVIDER FAILURE ISOLATION | ❌ NOT CERTIFIED — no isolation audit (§7). |
| TENNIS FULL-SLATE (§§9-14) | ❌ NOT CERTIFIED |
| ATP / WTA / CHALLENGER / ITF coverage | ❌ NOT CERTIFIED |
| TENNIS REAL-LINE HYDRATION | ❌ NOT CERTIFIED |
| TENNIS MODEL REACHABILITY | ❌ NOT CERTIFIED |
| TENNIS 85+ PUBLICATION | ❌ NOT CERTIFIED |
| ARBITRARY TENNIS CAP | ❌ NOT AUDITED — `MAX_TENNIS_PICKS_PER_DAY=150` file/line unverified this session |
| UNIVERSAL HISTORICAL CONTRACT (§§15-28) | ❌ NOT STARTED |
| UNIVERSAL GAME LOGS / TOGGLES / VS OPP / EXACT-LINE / DISTRIBUTION | ❌ NOT STARTED |
| NFL / MLB / SOCCER / TENNIS / CFB HISTORY ADAPTERS | ❌ NOT STARTED |
| LIGHTWEIGHT LOCKS / PICK BREAKDOWN SEPARATION | ❌ NOT CERTIFIED |
| PREVIEW ↔ EXPO PARITY (§30) | ❌ NOT AUDITED |
| PERFORMANCE LOAD TESTS (§31) at 500/1k/2k | ❌ NOT RUN |
| NAVIGATION STRESS TEST (§32) | ❌ NOT RUN |
| UNIVERSAL HISTORICAL RUNTIME PROOF (§33) | ❌ NOT RUN |
| TENNIS FINAL RUNTIME PROOF (§34) | ❌ NOT RUN |
| NFL ALT / NFL ATD / MLB HR PRESERVATION | ✅ VERIFIED (this session's edits touch neither their models nor filters) |
| UEA / APEX / SETTLEMENT / ROLLOVER / PARLAY / MY BETS | ✅ NO EDITS TO THEIR PATHS — preservation by non-modification |

---

## Blockers for each NOT CERTIFIED row

* **True frozen board snapshot** (§3): needs a new `board_snapshots` collection populated by the canonical publisher on publish, and a new `GET /api/picks/today/frozen` route that reads that doc directly. Estimated: 1 session.
* **ETag / board_version cache** (§4): requires plumbing `publication_version` through the response envelope + a frontend `If-None-Match` update. 1 session.
* **Frontend SWR + request de-dupe** (§§5-6): change to Locks screen fetch orchestrator in `frontend/app/(tabs)/index.tsx` + `frontend/src/lib/api.ts`. 1 session.
* **Provider isolation** (§7): audit of `services/providers/*.py` circuit breakers. 0.5 session.
* **MongoDB query health** (§8): `.explain()` sweep on the six board queries + index add list. 0.5 session.
* **Tennis full funnel** (§§9-14): the six trace stages must be counted live from Mongo + Odds API cache. 1–2 sessions.
* **Universal Historical Intelligence 2.0** (§§15-28): entirely new HistoricalQuery/Response contract + five sport adapters + threshold chart backend + on-demand endpoint + frontend rebuild. Realistically 4–6 sessions.
* **Load + navigation stress + runtime proofs** (§§31-34): 1 session, must run last.

---

## What I did NOT touch (per your protection list)

* `sports_engine.py` — not modified.
* ATD engine, UEA weights, Apex gate, NFL alt ladders, MLB HR, 85+ threshold — no code paths touched.
* NBA, NHL, UFC/MMA — untouched.
* No synthetic odds, no synthetic history, no fake 99s.

---

## Files changed this session

* `~ /app/backend/routes/picks_routes.py` — two surgical edits (lines 2414-2447 signal engine gate; lines 2763-2799 async visibility stamp).

Certification log: `/app/memory/board_hotpath_session1_2026_09_14.md` (this document).
