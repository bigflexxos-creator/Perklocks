# PERKLOCKS-MAIN 34 — SAFE VERIFIED CHECKPOINT (13 slices / 81 tests green)

**Session:** 2026-09-02 → 2026-09-03
**Status:** 13 fixes CERTIFIED · **81 contract tests green** · zero regressions to existing invariants.

---

## Certified in this run (in commit order)

| # | Slice / Fix | Files | Live proof | Tests |
|--:|---|---|---|--:|
| 1 | Slice 1.2B — True Lightweight Board DTO | `backend/server.py` (`_LITE_BOARD_WHITELIST`, `_slim_selection_v2`, `_cap_nested_blob`), `routes/picks_routes.py` (final-return strip) | `/api/picks/today?lite=true` 1.08 MB → 165 KB (-84.8 %); 10 KB/pick → 1.5 KB/pick | 7 |
| 2 | Slice 1.6 — LockBoardCard split | `LockPickCard.tsx`, `MatchupGradeBadge.tsx`, `LockBoardCard.tsx` (new), `app/(tabs)/index.tsx` | MATCHUP_FETCHES_ON_BOARD_LOAD = 0 (was ≥100) | 5 |
| 3 | Slice 1.1 — Cold-start perf | `app/_layout.tsx` | FIRST_PAINT_MS = 147 (Expo Web) | 4 |
| 4 | Slice 3 — Image + GPU perf | `PickEventRow.tsx`, `PlayerIdentity.tsx` | expo-image + `cachePolicy="memory-disk"` | 5 |
| 5 | P0A / P0B — full ↔ lite parity | live-DB probe | 83 == 83, per-sport + per-market-family drift = 0 | 5 |
| 6 | P0I/J/K/M — Lab tap + search hardening | `StrategyLabWorkstation.tsx` | debounce + gen guard + `commitSubject` + explicit UX; `sl-loading`/`sl-error`/`sl-retry`/`sl-typing-hint`/`sl-no-data` | 6 |
| 7 | P0D — Expo History settlement freshness | `routes/picks_routes.py`, `api.ts`, `history.tsx` | `settlement_in_flight`, `recommended_repoll_seconds`, `unresolved_with_past_event` shipped live | 2 |
| 8 | P0 · PublishedPickContract module | NEW `services/published_pick_contract.py` | round-trips 60 live published picks with every canonical key present | 10 |
| 9 | STEP 1 · consumer migration (Pick Breakdown) | `routes/picks_routes.py` (pick_detail) | `/api/picks/{id}` now ships `published_pick_contract` + `_provenance` | 4 |
| 10 | STEP 2 · UniversalMarketContract module | NEW `services/universal_market_contract.py` | one registry keyed by (sport, canonical family); Tennis alt-key + MLB run_line taxonomy resolved | 12 |
| 11 | STEP 3A · Tennis alt-builder context bug | `sports_engine._build_tennis_alt_picks` | `game.get(...)` → `event_payload.get(...)`; `NameError` now surfaces at ERROR level as `ALT_MODEL_PROGRAMMING_ERROR` (was silently `ALT_MODEL_SIGNAL_UNAVAILABLE`) | 4 |
| 12 | STEP 4 · Universal Over/Under conservation | `sports_engine.py` (two `kmath_neither_default_over` branches) | deterministic tiebreak by `hash(player_name) & 1`; no systematic Over bias | 3 |
| 13 | STEP 15 · SportMarketReachability classifier | NEW `services/sport_market_reachability.py` | classifies every zero-published to ONE of the 10 canonical reason codes (`NO_EVENTS`, `NO_REAL_MARKETS`, `NORMALIZATION_FAILURE`, `IDENTITY_FAILURE`, `MODEL_UNAVAILABLE`, `INTEGRITY_REJECTED`, `LEGITIMATELY_BELOW_85`, `PUBLICATION_FAILURE`, `API_FAILURE`, `FRONTEND_FAILURE`) — flags contradictions with UniversalMarketContract | 10 |

**Aggregate regression proof (final rerun this session):** 81/81 green. Zero pre-existing test broken.

---

## Guardrails preserved (must NOT be undone by next agent)

* Do NOT revert Slice 1.2B whitelist projection (−84.8 % win kept).
* Do NOT reintroduce `removeClippedSubviews=true` on RN Web.
* Do NOT rebuild working sport engines / lower 85+ threshold.
* Do NOT await `/api/version` on cold start.
* Do NOT remove `PublishedPickContract._provenance` — canary catches mutable-alias regressions.
* Do NOT re-add `game.get(...)` references inside `_build_tennis_alt_picks` (the audited STEP 3A regression class).
* Do NOT reintroduce `kmath_neither_default_over` bias — deterministic tiebreak preserves both sides equally.

---

## Remaining scope — safely stopped per user's stop-condition #4

The PERKLOCKS-MAIN 34 continuation directive still contains work orders that cannot be safely landed in the current context window without violating the "no fake completion" mandate:

* **STEP 1 (rest of consumers)** — migrate Parlay source, Rollover source, My Bets, History projection, Analytics, Lab references to read `published_pick_contract`. Pick Breakdown is done; the others follow the same pattern (import module, attach contract to response, add contract test).
* **STEP 3B/3C/3D/3E** — Tennis dynamic ATP/WTA discovery + standard game-market authority + board reachability funnel run. Requires touching `sports_engine.py` + `event_acquisition.py` + odds-provider adapters.
* **STEP 5** — NFL/NBA alternate prop classification migration into UniversalMarketContract (change every hardcoded `_ALT_PROP_MARKETS` set to call `is_alternate(sport, provider_key)`).
* **STEP 6/7** — MLB run_line / alt run-line unreachable-gate: reorder `apply_quality_gate` so authoritative model probability precedes any pre-model edge threshold.
* **STEP 8/9/10** — NBA authority resolution, Soccer game-market convergence, NHL/UFC honest-unavailable declarations.
* **STEP 11/12/13/14** — canonical `live_alt_lines` schema tightening, Alt Magic real-line-only wiring, shared-distribution alt pricing (ladder monotonicity), exact-threshold evaluation.
* **STEP 16-19** — SettlementCapability registry, provider fallback, coverage matrix, canonical result parity across History/Analytics/My Bets/Lab.
* **STEP 20** — Lab canonical player identity (backend `canonical_player_id` resolver).
* **STEP 21** — Why This Pick real-evidence contract with sport adapters.
* **STEP 22** — Soccer goalscorer forensic reachability traces.
* **STEP 23** — Pick Breakdown 2.0 view-only frontend refactor.
* **STEP 24** — Rollover/Parlay canonical parity extension.
* **STEP 25** — Same-snapshot Web/Expo/API release harness.

---

## Exact continuation state for the next agent

**Resume file/function:** `backend/routes/parlay_routes.py` — attach `PublishedPickContract.from_pick(pick).as_dict()` to every parlay-response pick, then add `tests/test_perklocks_main_34_step1b_parlay_migration.py` mirroring the pick-detail contract test.

**Then:** `backend/services/history_projection_service.py` — same pattern for History rows.

**Then:** open STEP 3B (Tennis dynamic tournament discovery) — path is `event_acquisition.acquire_tennis_events`.

**Do NOT** call any of the following certified without landing them:
* UniversalMarketContract full consumer migration
* SettlementCapability registry
* Why-This-Pick real-evidence adapters
* Same-snapshot Web/Expo/API release harness

---

## One-line numbers

* Lite board payload:  **1.08 MB → 165 KB (-84.8 %)**  ✅
* Per-pick lite avg :  **10 KB → 1.5 KB**              ✅
* Board matchup fetches on load: **≥100 → 0**           ✅
* First paint (Expo Web) : **~1000 ms → 147 ms**        ✅
* Full ↔ Lite MLB parity : **6/6, per-family drift 0**   ✅
* Lab research calls on partial input : **1-per-keystroke → 0**  ✅
* Contract tests this session : **81 / 81 green**       ✅
* Modules created : `published_pick_contract`, `universal_market_contract`, `sport_market_reachability`, `LockBoardCard.tsx` ✅
* Regressions to existing certified invariants : **0**   ✅
