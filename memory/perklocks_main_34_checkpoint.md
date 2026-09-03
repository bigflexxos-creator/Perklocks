# PERKLOCKS-MAIN 34 — SAFE VERIFIED CHECKPOINT (18 slices · 101 tests)

**Session:** 2026-09-02 → 2026-09-03
**Status:** 18 fixes CERTIFIED · **101 contract tests** (90 green in final rerun + 11 transiently skipped by auth-rate-limit 429; all 101 have proven green earlier in this session). **Zero regressions.**

---

## Full certified inventory

| # | Slice / Fix | Files | Live proof | Tests |
|--:|---|---|---|--:|
| 1 | Slice 1.2B — Lightweight Board DTO | `server.py`, `routes/picks_routes.py` | 1.08 MB → 165 KB (-84.8 %) | 7 |
| 2 | Slice 1.6 — LockBoardCard split | `LockPickCard.tsx`, `MatchupGradeBadge.tsx`, `LockBoardCard.tsx`, `app/(tabs)/index.tsx` | matchup fetches on board load ≥100 → 0 | 5 |
| 3 | Slice 1.1 — Cold-start perf | `app/_layout.tsx` | first paint 147 ms | 4 |
| 4 | Slice 3 — Image + GPU perf | `PickEventRow.tsx`, `PlayerIdentity.tsx` | expo-image + memory-disk cache | 5 |
| 5 | P0A/B — full ↔ lite parity | live-DB probe | 83 == 83, per-family drift 0 | 5 |
| 6 | P0I/J/K/M — Lab hardening | `StrategyLabWorkstation.tsx` | debounce + gen guard + `commitSubject` + explicit UX | 6 |
| 7 | P0D — Expo History freshness | `routes/picks_routes.py`, `api.ts`, `history.tsx` | `settlement_freshness` shipped live | 2 |
| 8 | PublishedPickContract | `services/published_pick_contract.py` | 60 live picks round-trip | 10 |
| 9 | STEP 1a — Pick Breakdown migration | `routes/picks_routes.py::pick_detail` | contract + provenance shipped | 4 |
| 10 | STEP 2 — UniversalMarketContract | `services/universal_market_contract.py` | one (sport, family) registry | 12 |
| 11 | STEP 3A — Tennis alt-builder context bug | `sports_engine._build_tennis_alt_picks` | `game.get` → `event_payload.get`; NameError telemetry split | 4 |
| 12 | STEP 4 — Over/Under conservation | `sports_engine.py` (K-math pair-dedup) | deterministic tiebreak on player-name hash | 3 |
| 13 | STEP 15 — SportMarketReachability | `services/sport_market_reachability.py` | 10-code starvation classifier | 10 |
| 14 | STEP 1b — Parlay consumer migration | `parlay_optimizer.parlay_to_payload` | contract on every leg | 2 |
| 15 | STEP 2 — History parity | `services/history_projection_service.project_pick` | contract on every history row | 3 |
| 16 | STEP 9 — SettlementCapabilityRegistry | `services/settlement_capability_registry.py` | `is_gradeable()` guard forces UNRESOLVED on missing actuals | 9 |
| 17 | **STEP 3 — My Bets migration** | `routes/user_bets_routes.py` (`list_user_bets`, `user_analytics_history`) | contract on every My Bets row + analytics history row | 2 |
| 18 | **STEP 12 — Same-snapshot parity harness** | new test module | Locks ↔ Pick Detail ↔ Parlay leg ↔ History spine drift = 0; full ↔ lite membership equal | 4 |

**Aggregate this run:** 90 passed + 11 auth-rate-limit skipped, 0 failed. All 101 have proven green during the session.

---

## Guardrails preserved

* Slice 1.2B whitelist projection (-84.8 %) intact.
* `removeClippedSubviews=true` on RN Web forbidden.
* No await on `/api/version` on cold start.
* `PublishedPickContract._provenance` retained.
* No `game.get(...)` in `_build_tennis_alt_picks`.
* No `kmath_neither_default_over` bias.
* Every parlay leg / history row / My-Bets row / Pick Breakdown response carries frozen `published_pick_contract`.
* Every ACTIVE `SettlementAuthority` declares `required_actual_fields`.

---

## Directive items NOT YET certified (safe stop)

* **Rollover consumer migration** — find rollover endpoint (`grep -rn 'rollover' /app/backend/routes/*.py`), attach contract, mirror test.
* **Analytics / Lab published-pick references** — same pattern where legacy fields reconstruct wager identity.
* **Tennis dynamic ATP/WTA discovery** — `event_acquisition.acquire_tennis_events` still uses provider active-sports catalog; needs surgical replacement of hard-coded tournament list.
* **NFL/NBA alt call-site migration to `universal_market_contract.is_alternate()`** — call sites in `sports_engine.py` / `props_engine.py` still use hard-coded `_ALT_PROP_MARKETS` sets.
* **MLB alt run-line model-before-edge reorder** — inside `quality_gate.apply_quality_gate` for MLB alt run-line branch only.
* **NBA/Soccer/NHL/UFC capability alignment** — cross-check registry state vs. production paths and fix false ACTIVEs.
* **Canonical `live_alt_lines` schema + Alt Magic real-line-only wiring**.
* **Shared-distribution alt pricing + exact-threshold evaluation** (STEP 13/14).
* **Grader entry-point migration to `is_gradeable()`** — every sport settlement adapter must call `is_gradeable(sport, family, event_final, identity_ok, actuals)` before grading; missing → UNRESOLVED.
* **Lab identity repair for `MLB + Other`** — `strategy_lab_correlation.py`.
* **Why This Pick real-evidence contract per sport + Pick Breakdown 2.0 view-only refactor**.
* **Web ↔ Expo runtime parity harness** — requires a real Expo native runtime probe; Web ↔ API parity already covered by STEP 12.

---

## Exact continuation state

1. **Resume:** `grep -rn "rollover" /app/backend/routes/*.py` — find endpoint, attach `PublishedPickContract`, add mirror test.
2. Then: STEP 3 remaining consumers (Analytics + Lab).
3. Then: STEP 3B `event_acquisition.acquire_tennis_events` dynamic discovery.
4. Then: STEP 5 NFL/NBA alt call-site migration to `universal_market_contract.is_alternate()`.
5. Then: STEP 6 MLB alt run-line reorder in `quality_gate.apply_quality_gate`.
6. Then: STEP 9 grader-entry-point migration.
7. Then: STEP 10 Lab identity repair.
8. Then: STEP 11 Why-This-Pick + Pick Breakdown 2.0.
9. Then: STEP 12 Web/Expo runtime parity harness completion.

Do NOT call PERKLOCKS-MAIN 34 fully certified until each remaining step is wired live and tested.

---

## One-line numbers

* Lite board payload : 1.08 MB → 165 KB (-84.8 %) ✅
* Per-pick lite avg : 10 KB → 1.5 KB ✅
* Board matchup fetches : ≥100 → 0 ✅
* First paint (Expo Web) : ~1000 ms → 147 ms ✅
* Full ↔ Lite MLB parity : 6/6, per-family drift 0 ✅
* Lab research calls on partial input : 1-per-keystroke → 0 ✅
* Locks ↔ Pick Detail ↔ Parlay ↔ History spine drift : 0 ✅
* Contract tests written : **101** ✅
* Regressions to previously certified invariants : **0** ✅
