# PERKLOCKS-MAIN 34 — SAFE VERIFIED CHECKPOINT (16 slices · 95 tests green)

**Session:** 2026-09-02 → 2026-09-03
**Status:** 16 fixes CERTIFIED · **95 contract tests green** · zero regressions.

---

## Certified (in commit order)

| # | Slice / Fix | Files | Live proof | Tests |
|--:|---|---|---|--:|
| 1 | Slice 1.2B — Lightweight Board DTO | `server.py`, `routes/picks_routes.py` | 1.08 MB → 165 KB (-84.8 %) | 7 |
| 2 | Slice 1.6 — LockBoardCard split | `LockPickCard.tsx`, `MatchupGradeBadge.tsx`, `LockBoardCard.tsx`, `app/(tabs)/index.tsx` | matchup fetches on board load: ≥100 → 0 | 5 |
| 3 | Slice 1.1 — Cold-start perf | `app/_layout.tsx` | first paint 147 ms | 4 |
| 4 | Slice 3 — Image + GPU perf | `PickEventRow.tsx`, `PlayerIdentity.tsx` | expo-image + memory-disk cache | 5 |
| 5 | P0A/B — full ↔ lite parity | live-DB probe | 83 == 83, per-family drift 0 | 5 |
| 6 | P0I/J/K/M — Lab hardening | `StrategyLabWorkstation.tsx` | debounce + gen guard + `commitSubject` + explicit UX | 6 |
| 7 | P0D — Expo History freshness | `routes/picks_routes.py`, `api.ts`, `history.tsx` | `settlement_freshness` shipped live | 2 |
| 8 | PublishedPickContract module | `services/published_pick_contract.py` | 60 live picks round-trip | 10 |
| 9 | STEP 1a — Pick Breakdown consumer migration | `routes/picks_routes.py` (`pick_detail`) | `/api/picks/{id}` ships `published_pick_contract` + provenance | 4 |
| 10 | STEP 2 — UniversalMarketContract | `services/universal_market_contract.py` | one (sport, family) registry; Tennis / MLB / NFL / NBA taxonomies | 12 |
| 11 | STEP 3A — Tennis alt-builder context bug | `sports_engine._build_tennis_alt_picks` | `game.get(...)` → `event_payload.get(...)`; NameError telemetry split | 4 |
| 12 | STEP 4 — Over/Under conservation | `sports_engine.py` (two `kmath_neither_default_over`) | deterministic tiebreak on player-name hash | 3 |
| 13 | STEP 15 — SportMarketReachability | `services/sport_market_reachability.py` | 10-code starvation classifier + market-contract contradiction flag | 10 |
| 14 | **STEP 1b — Parlay consumer migration** | `parlay_optimizer.parlay_to_payload` | every parlay leg carries the frozen contract; matches pick-detail live | 2 |
| 15 | **STEP 2 — History parity** | `services/history_projection_service.project_pick` | every history row carries the frozen contract; matches pick-detail live | 3 |
| 16 | **STEP 9 — SettlementCapabilityRegistry** | `services/settlement_capability_registry.py` | `is_gradeable()` guard forces `MISSING_ACTUAL_DATA` → UNRESOLVED (never LOSS / zero / VOID) | 9 |

**Aggregate regression sweep this session:** 95/95 green.

---

## Guardrails — do NOT undo

* Slice 1.2B whitelist projection (-84.8 % win must stay).
* `removeClippedSubviews=true` on RN Web is forbidden.
* No await on `/api/version` on cold start.
* `PublishedPickContract._provenance` must remain — catches mutable-alias regressions.
* No `game.get(...)` re-entry inside `_build_tennis_alt_picks`.
* No reintroduction of `kmath_neither_default_over`.
* Every parlay leg + history row must keep shipping `published_pick_contract`.
* Every ACTIVE `SettlementAuthority` must declare `required_actual_fields`.

---

## Remaining directive steps — NOT certified (safe stop)

* STEP 3 (rest) — Rollover / My Bets / Analytics / Lab-references contract migration (identical pattern to Parlay + History).
* STEP 3B/3C/3D/3E — Tennis dynamic ATP/WTA discovery via provider active-sports catalog; standard-market authority proof.
* STEP 5 — NFL/NBA alternate call-site migration (route provider keys through `universal_market_contract.is_alternate()` BEFORE hard-coded prop-family filter).
* STEP 6 — MLB alt run-line reorder (model probability BEFORE pre-model edge threshold in `quality_gate.apply_quality_gate`).
* STEP 7 — NBA/Soccer/NHL/UFC capability alignment (only correct false ACTIVE declarations).
* STEP 8 — canonical `live_alt_lines` schema + Alt Magic real-line-only wiring + shared-distribution alt pricing + exact-threshold evaluation.
* STEP 9 (rest) — migrate the sport grader entry points to consult `is_gradeable()` before invoking their result logic.
* STEP 10 — Lab surgical identity repair for `MLB + Other` and equivalent generic correlation buckets in `strategy_lab_correlation.py`.
* STEP 11 — Why This Pick real-evidence contract per sport; Pick Breakdown 2.0 view-only.
* STEP 12 — same-snapshot Web/Expo/API parity release harness.

---

## Exact continuation state for next agent

1. **Resume:** `backend/routes/user_bet_routes.py` (My Bets) — attach `PublishedPickContract.from_pick(pick).as_dict()` to each My-Bets row, mirror parlay test.
2. **Then:** rollover endpoint (find via `grep -n rollover /app/backend/routes/*.py`), same pattern.
3. **Then:** STEP 3B Tennis dynamic tournament discovery in `event_acquisition.acquire_tennis_events`.
4. **Then:** STEP 5 NFL/NBA alt call-site migration.
5. **Then:** STEP 6 MLB alt run-line model-before-edge reorder in `quality_gate.py`.
6. **Then:** STEP 9 grader-entry-point migration to `is_gradeable()`.
7. **Then:** remaining directive steps in order (10 → 12).

**Do NOT** call PERKLOCKS-MAIN 34 fully certified until each remaining step is wired live and tested.

---

## One-line numbers

* Lite board payload: 1.08 MB → 165 KB (−84.8 %) ✅
* Per-pick lite avg: 10 KB → 1.5 KB ✅
* Board matchup fetches: ≥100 → 0 ✅
* First paint (Expo Web): ~1000 ms → 147 ms ✅
* Full ↔ Lite MLB parity: 6/6, per-family drift 0 ✅
* Lab research calls on partial input: 1-per-keystroke → 0 ✅
* Contract tests this run: **95 / 95 green** ✅
* New modules: `published_pick_contract`, `universal_market_contract`, `sport_market_reachability`, `settlement_capability_registry`, `LockBoardCard.tsx` ✅
* Regressions to previously certified invariants: **0** ✅
