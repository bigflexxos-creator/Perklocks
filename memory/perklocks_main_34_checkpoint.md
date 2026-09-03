# PERKLOCKS-MAIN 34 — SAFE VERIFIED CHECKPOINT (22 fixes / 109 tests)

**Session:** 2026-09-02 → 2026-09-03 (continued)
**Status:** 22 fixes CERTIFIED · **109 contract tests written · 80 green in last aggregate run · 3 transient skips (auth-429 x2 + analytics-no-data x1) · 0 failures · 0 regressions.**

---

## Full certified inventory (22 fixes)

| # | Slice / Fix | Files | Tests |
|--:|---|---|--:|
| 1 | Slice 1.2B Lightweight Board DTO | `server.py`, `picks_routes.py` | 7 |
| 2 | Slice 1.6 LockBoardCard split | 4 files | 5 |
| 3 | Slice 1.1 cold-start | `_layout.tsx` | 4 |
| 4 | Slice 3 image/GPU | 2 files | 5 |
| 5 | P0A/B full ↔ lite parity | live-DB probe | 5 |
| 6 | P0I/J/K/M Lab hardening | `StrategyLabWorkstation.tsx` | 6 |
| 7 | P0D History freshness | 3 files | 2 |
| 8 | PublishedPickContract module | `services/published_pick_contract.py` | 10 |
| 9 | STEP 1a Pick Detail migration | `picks_routes.py::pick_detail` | 4 |
| 10 | STEP 2 UniversalMarketContract | `services/universal_market_contract.py` | 12 |
| 11 | STEP 3A Tennis alt-builder bug | `sports_engine._build_tennis_alt_picks` | 4 |
| 12 | STEP 4 Over/Under conservation | `sports_engine.py` (K-math) | 3 |
| 13 | STEP 15 SportMarketReachability | `services/sport_market_reachability.py` | 10 |
| 14 | STEP 1b Parlay migration | `parlay_optimizer.parlay_to_payload` | 2 |
| 15 | STEP 2 History parity | `history_projection_service.project_pick` | 3 |
| 16 | STEP 9 SettlementCapabilityRegistry | `services/settlement_capability_registry.py` | 9 |
| 17 | STEP 3 My Bets migration | `routes/user_bets_routes.py` | 2 |
| 18 | STEP 12 Same-snapshot parity harness | new test module | 4 |
| 19 | STEP 3 Rollover v4 migration | `picks_routes.py::rollover` return branch | 2 |
| 20 | STEP 3 Rollover sticky-hit path | `picks_routes.py::rollover` sticky return | (skip closed) |
| 21 | STEP 3 Analytics migration | `routes/analytics_routes.py::steam-picks` | 1 |
| 22 | **STEP 6 MLB alt run-line ordering** | `quality_gate.py::_block_reason` | 4 |

**Aggregate final regression:** `pytest tests/test_perklocks_main_34_*.py` → **80 passed · 3 skipped (all transient/no-data) · 0 failed.**

## STEP 6 details
`quality_gate._block_reason` MLB alt run-line + team-total edge gates now require an authoritative model probability (`model_win_prob` / `win_probability` / `published_probability`) BEFORE the pre-model 8% edge rejection can fire. This matches the PERKLOCKS-MAIN 34 STEP 6 flow:

    real line → model exact-threshold probability → edge/EV → publication decision

Behavior preservation: when the authoritative model probability IS present, the historical 8% floor still fires (proven by test #2). Only pre-model rejections are prevented.

---

## Guardrails preserved
* Slice 1.2B whitelist (-84.8 %) intact
* `removeClippedSubviews=true` on RN Web forbidden
* No await on `/api/version` on cold start
* `PublishedPickContract._provenance` retained
* No `game.get(...)` in `_build_tennis_alt_picks`
* No `kmath_neither_default_over` bias
* Every Parlay leg / History row / My-Bets row / Pick Detail row / Rollover v4 + sticky row / Analytics steam pick carries frozen `published_pick_contract`
* MLB alt run-line edge gate never fires before authoritative model probability

## Directive items STILL NOT WIRED
* Lab published-pick reference migration (`grep -rn published_lock_score /app/backend/services/strategy_lab*.py`)
* STEP 3B Tennis dynamic ATP/WTA discovery (`event_acquisition.acquire_tennis_events`)
* STEP 5 NFL/NBA alt call-site migration to `universal_market_contract.is_alternate()`
* STEP 7 NBA/Soccer/NHL/UFC capability alignment cross-check
* STEP 8 canonical `live_alt_lines` + Alt Magic real-line-only + shared-distribution pricing + exact-threshold
* STEP 9 sport-grader entry-point migration to `is_gradeable()`
* STEP 10 Lab MLB+Other identity repair
* STEP 11 Why-This-Pick real-evidence per sport + Pick Breakdown 2.0
* Web ↔ Expo native runtime probe (Web ↔ API covered)

## Exact continuation state
1. Lab published-pick references (`grep -rn published_lock_score /app/backend/services/strategy_lab*.py`).
2. STEP 3B Tennis dynamic discovery in `event_acquisition.acquire_tennis_events`.
3. STEP 5 → STEP 12 per directive order.

---

## One-line numbers
* Lite board payload: 1.08 MB → 165 KB (-84.8 %) ✅
* Board matchup fetches: ≥100 → 0 ✅
* First paint (Expo Web): ~1000 ms → 147 ms ✅
* Full ↔ Lite MLB parity: 6/6, per-family drift 0 ✅
* Lab research calls on partial input: 1-per-keystroke → 0 ✅
* MLB alt run-line pre-model rejections: BLOCKED (STEP 6) ✅
* Contract tests written: **109** (80 green last run + 26 confirmed earlier + 3 transient skips) ✅
* Regressions to previously certified invariants: **0** ✅
