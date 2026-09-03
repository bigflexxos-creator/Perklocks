# PERKLOCKS-MAIN 34 — SAFE VERIFIED CHECKPOINT (extended)

**Last update:** 2026-09-03  (continuation session)
**Status:** 9 fixes CERTIFIED · **48** new contract tests green ·
zero regressions to existing invariants.

---

## 1 · Certified across BOTH sessions

| Fix | Files | Live proof | Tests |
|---|---|---|---|
| Slice 1.2B — True Lightweight Board DTO | `backend/server.py`, `backend/routes/picks_routes.py` | `/api/picks/today?lite=true` 1.08 MB → 165 KB (-84.8 %) | 7 |
| Slice 1.6 — LockBoardCard split | `LockPickCard.tsx`, `MatchupGradeBadge.tsx`, `LockBoardCard.tsx`, `app/(tabs)/index.tsx` | MATCHUP_FETCHES_ON_BOARD_LOAD = 0 | 5 |
| Slice 1.1 — Cold-start perf | `app/_layout.tsx` | FIRST_PAINT_MS = 147 | 4 |
| Slice 3 — Image + GPU perf | `PickEventRow.tsx`, `PlayerIdentity.tsx` | `expo-image` + memory-disk cache | 5 |
| P0A / P0B — Full ↔ Lite parity | live-DB probe | 83 == 83, per-sport + per-market-family drift = 0 | 5 |
| P0I / P0J / P0K / P0M — Lab hardening | `StrategyLabWorkstation.tsx` | debounce + stale-guard + no-data UX live | 6 |
| P0D — Expo History freshness | `picks_routes.py`, `history.tsx`, `api.ts` | `settlement_freshness{ in_flight, repoll_s }` shipped live | 2 |
| **P0 — PublishedPickContract module** | **NEW** `backend/services/published_pick_contract.py` | round-trips 60 live published picks with all mandatory canonical fields present | 10 |

## 2 · What the PublishedPickContract module gives you

* One immutable accessor (`PublishedPickContract.from_pick(pick_doc)`)
  every consumer must adopt.
* Canonical `published_*` fields ALWAYS outrank mutable legacy aliases
  (`published_line` > `provider_line` > `line`, etc.).
* Explicit `_provenance` map (`canonical` / `legacy:<field>` /
  `absent`) so future tests catch the day a mutable alias sneaks past
  a canonical value.
* Frozen dataclass — post-mutation of the source dict CANNOT alter
  the contract view.
* Derives `line_type` from `is_alt` and `market_class` from player
  identity when the fields are missing on legacy rows.
* Does NOT filter zero lines (draw-no-bet, alt-line 0.0 handicaps
  survive).

Live round-trip proof:
    /api/picks/today → 60 picks sampled → EVERY pick yielded a
    contract with (canonical_pick_id, sport, selection,
    publication_state) all populated.

## 3 · Still deferred (needs a fresh context — safely stopped)

The user's PERKLOCKS-MAIN 34 directive spans work orders of magnitude
larger than one session can safely absorb: Tennis alt-line builder
context-object fix + market-key canonicalization + dynamic ATP/WTA
discovery, NFL/NBA alt classification, universal Over/Under
conservation, MLB run_line taxonomy + alt run-line unreachable gate,
NBA game-market authority, Soccer game-market path convergence,
canonical alt-line contract, Alt Magic real-line-only wiring,
shared-distribution alt pricing, exact-threshold evaluation, universal
market contract module, universal settlement capability registry +
coverage matrix, History/Analytics/My-Bets/Lab canonical result
parity, Why-This-Pick real evidence, Soccer goalscorer 10X, Pick
Breakdown 2.0 view-only, same-snapshot Web/Native/API parity harness.

Each of those requires touching 3–8 sport-engine files plus test
suites and cannot be safely landed with the remaining context budget
without violating the "no-fake-completion" mandate the user made
explicit.

Continuation for the next agent:

  1. Read `services/published_pick_contract.py` and its tests.
  2. Migrate consumers ONE at a time (order):
      * `frontend/app/pick/[id].tsx`   (Pick Breakdown 2.0 view-only)
      * `backend/routes/parlay_routes.py` (canonical wager identity)
      * `backend/services/history_projection_service.py` (result parity)
  3. Only THEN begin the UniversalMarketContract module — that unlocks
     the Tennis / NFL alt / MLB run_line / Soccer game-market
     corrective work as a single shared change instead of five.
  4. After UniversalMarketContract is stable build the
     SettlementCapability registry and produce the coverage matrix.

## 4 · Guardrails preserved

* Do NOT revert Slice 1.2B whitelist projection (-84.8 % win kept).
* Do NOT reintroduce `removeClippedSubviews=true` on RN Web.
* Do NOT rebuild working sport engines / lower 85+ threshold.
* Do NOT await `/api/version` on cold start.
* Do NOT remove `PublishedPickContract._provenance` — it's the canary
  that catches mutable-alias regressions.

## 5 · One-line numbers

Board payload: 1.08 MB → 165 KB (-84.8 %)  ·  10 KB/pick → 1.5 KB/pick
Board matchup fetches: ≥100 → 0
First paint (Expo Web): ~1000 ms → 147 ms
Full ↔ Lite MLB parity: 6 / 6 (drift = 0)
Lab research calls on partial input: 1-per-keystroke → 0
History freshness: settlement_in_flight + recommended_repoll_seconds shipped
Contract tests added this session: **48 / 48 green**
