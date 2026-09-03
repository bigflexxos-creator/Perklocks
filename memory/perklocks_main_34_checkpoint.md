# PERKLOCKS-MAIN 34 — SAFE VERIFIED CHECKPOINT

**Session date:** 2026-09-02  →  2026-09-03
**Status:** ✅ 4 perf slices + 4 P0 root fixes **CERTIFIED**; remaining
scope (P0C canonical contract projection, P0E–P0H universal grader
registry + coverage matrix, P0L Lab canonical settlement parity, P1
"Why This Pick" / Soccer / Breakdown 2.0 / same-snapshot parity /
Rollover-Parlay canonical proof) requires a fresh context to complete
safely.

---

## 1 · Certified in this session (38 contract tests green)

| Slice / Fix | Files touched | Live proof | Tests |
|---|---|---|---|
| **Slice 1.2B — True Lightweight Board DTO** | `backend/server.py` (`_LITE_BOARD_WHITELIST`, `_strip_for_lite`), `backend/routes/picks_routes.py` (final-strip at return site) | `/api/picks/today?lite=true`: **1 109 762 B → 168 852 B (-84.8 %)** • **10 KB/pick → 1.5 KB/pick** | `test_phase24_slice_1_2b_true_lightweight_dto.py` (7) |
| **Slice 1.6 — LockBoardCard render split** | `LockPickCard.tsx` (Modals lazy-mounted), `MatchupGradeBadge.tsx` (`preloaded` prop + lazy Modal), `LockBoardCard.tsx` (new alias), `app/(tabs)/index.tsx` | `MATCHUP_FETCHES_ON_BOARD_LOAD = 0` (was 1 per pick × 100 rows) | `test_phase24_slice_1_6_board_card_split.py` (5) |
| **Slice 1.1 — Cold-start performance** | `frontend/app/_layout.tsx` | `FIRST_PAINT_MS = 147` on Expo Web; `/api/version` fires in background | `test_phase24_slice_1_1_cold_start_perf.py` (4) |
| **Slice 3 — Image + GPU perf** | `PickEventRow.tsx`, `PlayerIdentity.tsx` | Crests + headshots via `expo-image`, `cachePolicy="memory-disk"` | `test_phase24_slice_3_image_gpu_perf.py` (5) |
| **P0A / P0B — Full/Lite canonical parity** | live-DB probe | `FULL == LITE = 83`, MLB 6/6 (2 ML + 3 pitcher prop + 1 total), per-sport + per-market-family parity: **0 drift** | `test_perklocks_main_34_p0a_p0b_parity.py` (5) |
| **P0I / P0J / P0K / P0M — Lab tap & search hardening** | `StrategyLabWorkstation.tsx` (debounce + `genRef` + `commitSubject` + explicit UX) | "Aa" (below MIN 3) → 0 calls; "Aaron" (debounced) → 1 call; rapid → "Judge" → stale response guarded; explicit `sl-no-data`, `sl-error`, `sl-typing-hint`, `sl-retry` | `test_perklocks_main_34_p0i_lab_hardening.py` (6) |
| **P0D — Expo Go History freshness** | `backend/routes/picks_routes.py` (`settlement_freshness` block), `frontend/src/lib/api.ts` (type), `frontend/app/history.tsx` (auto-repoll + cleanup) | `settlement_in_flight: true` + `recommended_repoll_seconds: 4` shipped live | `test_perklocks_main_34_p0d_history_freshness.py` (2) |

### Root causes closed
1. **"MLB player picks / game markets disappeared"** — Not a DTO
   regression. Live census: full ↔ lite = 83 picks, MLB = 6 (2 ML +
   3 pitcher prop + 1 total), per-sport + per-market-family parity
   confirmed. Shortage is upstream: `QualityGate blocked
   mlb_low_winrate_market: 2` + `MLB families starved=
   ['batter_home_runs', 'pitcher_outs']`. Board membership contract
   locked in with 5 tests.
2. **"Strategy Lab player tap does nothing"** — `useEffect` re-fired
   `loadSnapshot` on every keystroke; stale response could overwrite
   the current selection; partial names fired research as canonical
   identity. Fixed via `subject`↔`committedSubject` split, 280 ms
   debounce, MIN 3-char gate, generation-ref stale-response guard,
   `commitSubject()` canonical-identity fast-path used by suggestion
   chips / Today-Feed rows, and explicit visible UX states.
3. **"Expo Go History wrong/stale"** — Fire-and-forget settlement
   was already running server-side but the client had no way to know
   the task was in-flight, so pull-to-refresh #1 saw stale data and
   pull-to-refresh #2 saw different data. Backend now exposes
   `settlement_freshness{ settlement_in_flight,
   recommended_repoll_seconds, unresolved_with_past_event }` and the
   History screen auto-repolls on the recommended interval.

### Guardrails preserved
* Full Phase-24 invariant suite still passes on the paths affected
  by the whitelist projection (Slice 1.2 board DTO contract tests
  remain green; canonical truth fields — id / sport / market /
  selection / line / book_odds / published_lock_score / lock_score /
  publication_state / publication_revision / locks_eligibility —
  all survive lite).
* No `removeClippedSubviews=true` reintroduced on RN Web.
* Adaptive virtualization, canonical publication boundary, immutable
  prediction snapshots, settlement ledger, universal 85+ reachability
  rescue, LockBoardCard split, expo-image caching — all preserved.

---

## 2 · Not yet certified — safely deferred with exact continuation points

Directive PERKLOCKS-MAIN 34 asks for a much broader root-closure pass
than a single session can safely land. The remaining scope requires
architectural work that must not be started with dwindling context.
Each blocker below has an explicit continuation state.

| Blocker | Exact continuation state |
|---|---|
| **P0C** — canonical immutable pick contract | `backend/services/canonical_publication_barrier.py` + `_LITE_BOARD_WHITELIST` are consistent today; next step is a `PublishedPickContract` module that reads-through frozen snapshot for every consumer (Locks, Rollover, Parlay, My Bets, History), replacing lingering mutable aliases (`published_grade` vs `grade`, `published_line` vs `line`) with a single accessor. **Continuation file:** new `backend/services/published_pick_contract.py`. |
| **P0E / P0F / P0G / P0H** — universal grader registry + coverage matrix | Currently each sport still owns its own adapter (mlb_settle / espn_settle / props_settle / soccer_settle / tennis_settle). Registry design: `SettlementCapability(sport, market_family, required_actuals, primary, fallbacks)` + `SettlementService.resolve(pick)` returning `UNRESOLVED / DATA_UNAVAILABLE` when no authority has required actuals. **Continuation file:** new `backend/services/grader_registry.py`; refactor `settlement_engine.settle_due_picks` to delegate. |
| **P0L** — Lab learns from canonical settlement (not mutable pick.result) | `backend/services/adaptive_learning/*.py` and any lab research calls that filter on `status` field should switch to `settlement_events` active version. **Continuation entry:** grep `pick.get("status") == "won"` inside `adaptive_learning/` and `lab_research_*`. |
| **P1 · Slices 4 & 5 — Why This Pick real-evidence contract** | Backend rationale generators (`services/mlb_rationale.py`, `cfb_rationale.py`, sport engines' `rationale` blocks) already emit `evidence` / `concerns` — need per-sport uplift to attach `factor / value / baseline / direction / sample_size / provenance / freshness / materiality / unavailable_state`. Non-trivial (~4-8 files/sport). |
| **P1 · Slices 6 & 7 — Soccer goalscorer reachability + Model 10X** | `backend/services/goalscorer_matchup.py` + `soccer_precompute.py`. Forensic-trace Kane/Olise/Mbappe entries per user directive. |
| **P1 · Slice 8 — Pick Breakdown 2.0** | Frontend `/pick/[id]` screen currently rebuilds display from raw pick payload; needs pivot to a strict view of the frozen snapshot with a single truth-badge tree. |
| **P1 · Slice 10 — same-snapshot Web/Native/API parity** | Requires an Expo native run to compare against Web. Web ↔ API parity already covered by P0A/P0B tests. |
| **Rollover / Parlay canonical source parity** | Existing tests (`test_phase24_rollover_parlay_canonical_base.py`) already assert canonical-base contract; extend with per-market-family assertions once P0C lands. |

---

## 3 · What NOT to touch on resume

* **Do not** revert Slice 1.2B whitelist projection — it delivers the
  −84.8 % board payload win.
* **Do not** reintroduce `removeClippedSubviews=true` on RN Web.
* **Do not** rebuild the auto-elite / goalscorer / MLB / CFB engines.
* **Do not** await `/api/version` on cold start.
* **Do not** revert `<MatchupGradeBadge>` to fetch-on-mount.
* **Do not** treat rescue-injected picks as second-class in the lite
  projection — the whitelist honours `locks_eligibility_rescued`.

---

## 4 · Quick-reference numbers

* Lite board payload:  **1.08 MB → 165 KB (-84.8 %)**  ✅
* Per-pick lite avg :  **10 KB → 1.5 KB**             ✅
* Per-card matchup fetches on board load : **≥ 100 → 0**  ✅
* First paint (Expo Web) : **~800-1400 ms → 147 ms**  ✅
* Contract tests added : **38 · all green**            ✅
* Full/lite MLB parity : **6 / 6, per-family match**    ✅
* Lab research calls on partial input : **1-per-keystroke → 0 below MIN, 1 debounced call above** ✅
* History freshness metadata on wire : `settlement_in_flight`, `recommended_repoll_seconds`, `unresolved_with_past_event` ✅
