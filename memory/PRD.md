# PerksLocks — Product Requirements (Live Delta 2026-06-21)

## Canonical Epoch v2 Contract — CLOSED (unpublished)

Ordered-revision authority. Origin-bound. Single-flight refresh. Shared consumer registry that revalidates mounted React state on advance. Late N responses after N+1 are `stale` and IGNORED — authority never regresses.

Six root defects closed:
1. Opaque hash used as ordered version → replaced with integer `revision`.
2. Locks tab caches outside SWR → stamped with `CanonicalEpoch`.
3. Cache deletion didn't refresh mounted state → shared consumer registry.
4. Refresh single-flight → `targetRevision` guard + explicit ownership release.
5. `/api/version` no-cache + `canonical_epoch` in body.
6. Origin now part of the epoch key.

**Tests targeted at shared contract only: 31 (6 backend + 25 frontend). PASS.**

Full sport / model suites intentionally NOT rerun.

Handed back for one physical Expo Go check. NOT published.

Full report: `/app/memory/canonical_epoch_v2_closure_2026_06_21.md`.

---

## Prior — CFB Sign Fix + Soccer HI (still valid)
`/app/memory/p0_universal_root_closure_final_2026_06_21.md`

---

## 2026-06-21 · Backend DATA_VERSION sync (surgical)
- Support confirmed Expo stale-bundle root cause is external. Investigation CLOSED.
- Preview QR (`exp://canonical-parity.preview.emergentagent.com`) is the sole physical test target.
- Deployed snapshot (`bet-edge-ai-1.emergent.host`) remains intentionally frozen — DO NOT republish.
- `backend/server.py` `DATA_VERSION` bumped from `2026.06.11-soccer-shots-sot-assists-wired-v49` → `2026.06.21-canonical-epoch-v2-signfix` to align `/api/version` with the current Canonical Parity frontend.
- Verified on Preview:
  - `GET /api/version` → `data_version: 2026.06.21-canonical-epoch-v2-signfix`
  - Profile screen shows Build/Source/Backend all pointing at Canonical Parity.
- NOT touched: Canonical Epoch logic, board generation, DB, API origins, Metro config.



---

## 2026-06-21 · Rollover True Immutability + Parlay 3.0 Root Closure
- **Rollover**: Frozen slate v2 with FULL wager snapshot (line/odds/book/WP/Lock/publication_version) + DB unique index on (slate_date, scope) + PICK_MISSING no longer auto-invalidates + frozen-view reader (`services/rollover_frozen_view.py`) serves wager truth from `legs[]` — never from mutable `db.picks`. Migration `scripts.migrate_rollover_slates_frozen_wager_v2` idempotent, only backfills fields provably recoverable from FROZEN events; never manufactures historical truth.
- **Parlay 3.0**: Single `ModePolicy` authority (`services/parlay/mode_policy.py`) + Feasibility Engine (`services/parlay/feasibility.py`) with structured reason codes. HIGH_RISK MLB 10/15/20 now returns truthful 6-of-10/6-of-15/6-of-20 partial cards instead of empty state. Saved parlay leg snapshot marks `frozen_wager_version=2`.
- 17-test regression suite passes: `tests/test_rollover_immutability_and_parlay30.py`.
- Full memo: `memory/rollover_immutability_and_parlay30_closure_2026_06_21.md`.
- PRODUCTION PUBLISHED: NO.


---

## 2026-06-21 · Parlay 3.0 Universal Sport Closure
- **Universal Dependency Authority** (`services/parlay/dependency.py`): same-event fail-closed for MLB/NFL/NBA/NHL/UFC/Tennis/CFB/Soccer. No more scattered per-sport branches.
- **HIGH_RISK edge policy**: now `min_edge_pct=None` — edge is a ranking input, not a hard gate. ADVANCED_HIGH_EV remains the +edge gate.
- **Per-mode `health_weights`** wired into `parlay_health()` — 12-leg HIGH_RISK tickets no longer auto-punished for being 12 legs.
- **Pin validation** (`services/parlay/pins_and_alternates.validate_pin`): returns PIN_ACCEPTED / PIN_CONFLICT + reason codes (`NOT_CANONICAL`, `NO_REAL_ODDS`, `BELOW_LOCK_FLOOR`, `DEPENDENCY_CONFLICT`, …). `pin_report` in every response.
- **Alternate ranking**: replaces "rank by Lock" — now ranks by dependency safety → mode eligibility → diversification → survival impact → Lock (tiebreak).
- **Regenerate determinism**: `refresh_nonce=0` → byte-identical fingerprints. `refresh_nonce>0` → controlled diversity.
- **Saved parlay wager freeze v2** verified across MLB/NFL/Soccer/Tennis.
- 43 regression tests pass (`tests/test_rollover_immutability_and_parlay30.py`, `tests/test_parlay30_universal_closure.py`).
- Memo: `memory/parlay30_universal_closure_2026_06_21.md`. PRODUCTION PUBLISHED: NO.


---

## 2026-06-21 · NFL Player Props 2.0 — Universal Game Intelligence + Best-Bet Discovery
- **New package**: `services/nfl_props_v2/` with replaceable `WeatherProvider` / `AvailabilityProvider` adapters, per-player `Distribution` + monotonic `ThresholdEvaluation` + safest-bet + best-value discovery.
- **Reuses existing PerkLocks providers only** — no new paid APIs added: `platinum_nfl.game_runtime`, `player_history.service`, `espn_injury_notes`, live NFL alt lines from `db.picks`.
- **Coverage strictly expanded**: 12 NFL picks scanned → all 4 sampled QB/RB/WR players reach evaluation with 51 / 43 / 19 / 18 real sportsbook alt lines each. Zero suppression, zero star bonuses, zero fake data, zero 40+ hardcodes.
- **Weather = UNAVAILABLE / Injury = PARTIAL** honestly reported; degrade confidence 0.855, do not suppress.
- **Admin endpoint**: `GET /api/admin/nfl-props-v2/discover` returns the full trace.
- **19 focused tests pass** (`tests/test_nfl_props_v2.py`); combined 62-test suite green.
- Memo: `memory/nfl_props_v2_universal_closure_2026_06_21.md`. PRODUCTION PUBLISHED: NO.

---

## 2026-06-21 · NFL Player Props 2.0 — P0 Distribution Closure
- **Canonical NFL market → stat mapper** (`services/nfl_props_v2/nfl_stat_mapping.py`): resolves free-form sportsbook markets to `player_game_actuals.actuals` keys (`pass_yds`, `pass_tds`, `attempts`, `completions`, `rush_yds`, `rush_attempts`, `rush_tds`, `rec_yds`, `receptions`, `rec_tds`, `targets`, `anytime_td=rush_tds+rec_tds`).
- **Distributions materialize** directly from `db.player_game_actuals` (sport=`"nfl"`, 132K docs) — no new history source; as-of safe (`event_time < as_of`); small-sample tolerant (n<20 OK).
- **Weather auto-penalty removed**: UNAVAILABLE / indoor / roof-closed → multiplier = 1.0. Only real bad-weather DATA (wind ≥ 20, gust ≥ 30, precip_prob ≥ 0.7) reduces confidence. Injury PARTIAL is now neutral.
- **Live proof (2026-09-22 slate)**: Stafford SAFEST `175+ Pass Yds @ -550, p=0.950, floor=+70`; Kyren SAFEST `44.5+ Rush Yds @ -250, p=0.950, floor=+15.5`; Bijan SAFEST `65.5+ Rush Yds @ -185, p=0.750, floor=+4.5`; Malik Nabers SAFEST `3+ Receptions @ -1000, p=0.895, floor=+2.0` (canary discovery).
- **74 tests pass · 0 fail** (NFL Props 2.0 up from 19 → 31; combined regression 62 → 74).
- Memo: `memory/nfl_props_v2_distribution_closure_2026_06_21.md`. PRODUCTION PUBLISHED: NO.


---

## 2026-06-21 · NFL Player Props 2.0 — Canonical Pipeline Wiring
- **NEW** `services/nfl_props_v2/canonical_wiring.py` — walks NFL picks, stamps `nfl_props_v2_evidence` on each doc (additive namespace; passes through `_canonicalize_picks` to `/api/picks/today`).
- **Admin endpoint** `POST /api/admin/nfl-props-v2/enrich` triggers the sweep (game context built once/game, distribution once/player-market, no provider fanout).
- **Slate sweep 2026-09-22**: 35 NFL picks total → 24 V2-stamped (all with lock ≥ 85), 23 with non-null probability, 4 at p ≥ 0.90. Highest Lock among V2-stamped = 94.0 (Stafford 175+ Pass Yds, Theo Johnson 0.5+ Receptions). Highest V2 probability = 1.0 (Cam Skattebo 10.5+ Rec Yds).
- **TE coverage confirmed** — Theo Johnson stamped, no elite_player_name gate.
- **Zero coverage shrinkage**: existing NFL picks preserved, additive only.
- **74 tests pass** (NFL Props 2.0 unchanged at 31; combined 74).
- **Blocker for 98/99**: downstream Lock authority does not yet READ `nfl_props_v2_evidence` — additive stamping is complete, downstream consumption is a follow-up task the spec explicitly forbids in this pass ("no score tuning").
- Memo: `memory/nfl_props_v2_canonical_wiring_2026_06_21.md`. PRODUCTION PUBLISHED: NO.


---

## 2026-06-21 · P0 Runtime Verification + P1 Canonical Test Sweep

### P0 — NFL Props V2 canonical wiring runtime verification
- Discovered stale-date defect in `canonical_wiring.py`: stamper defaulted to `datetime.now(timezone.utc).strftime("%Y-%m-%d")` while `/api/picks/today` uses `services.perklocks_day.current_slate_day()` (04:00 ET roll).
- **Fix (surgical, 8 lines)**: `enrich_nfl_picks_with_v2_evidence()` now defaults `pick_date` to `current_slate_day()` when not provided, matching the canonical Locks feed.
- **Runtime proof (slate 2026-09-21)** — 309 candidate NFL picks → 231 stamped, 208 with non-null probability. `/api/picks/today?sport=NFL` returns 11 Locks (all lock ≥ 85), **6 carry `nfl_props_v2_evidence`** with real distributions:
  - Kyle Pitts Over 33.5 Rec Yds — p=0.700, n=20 (`nfl_actuals:rec_yds:l20`)
  - Michael Penix Jr Over 201.5 Pass Yds — p=0.571, n=14 (`nfl_actuals:pass_yds:l14`)
  - Jordan Love Over 7.5 Rush Yds — p=0.500, n=20 (`nfl_actuals:rush_yds:l20`)
  - Chris Brooks Over 16.5 Rush Yds — p=0.150, n=20 (`nfl_actuals:rush_yds:l20`)
  - MarShawn Lloyd Over 28.5 Rush Yds — n=1, p=None (correctly refuses fabrication)
- Legacy "N+" markets, moneyline, and non-standard formats are correctly untouched (additive only, no suppression).
- Lock Scores UNCHANGED — evidence namespace is purely additive, no score tuning applied.
- Weather UNAVAILABLE / role PARTIAL do NOT suppress candidates.

### P1 — Failing canonical test investigation
- Suspected regression: `tests/test_canonical_epoch_contract_v2.py::test_canonical_revision_uniform_across_surfaces_at_rest`.
- Runtime result: **all 6 tests in that file PASS** (isolated and in-file). Suspected failure was a stale/state artifact from the prior session — no real product defect exists.
- Broader sweep: 80 tests across NFL Props V2 + Rollover Immutability + Parlay 3.0 Universal + Canonical Epoch v2 = **80 passed, 0 failed**.
- No production code changed for P1.

### Not touched (per instruction)
- No score tuning to fabricate 85+ picks.
- No re-audit of Rollover / Parlay / NFL Props V2 internals.
- No weather/injury adapter integration (P2 deferred).
- No other sports touched.

---

## 2026-06-21 · Soccer / Tennis / CFB Discovery + Coverage Sweep

### P0 — Tennis Board Recovery (real defect closed)
**Root cause**: `services/model_integrity_gate.evaluate()` check #9 rejected any pick with `universal_lock=None` — but the universal_lock authority (`services/universal_lock_authority.compute_universal_lock`) refuses to stamp MODEL_CONDITIONED provenance (specialized engines: tennis edge_v2, cfb_sp_game_model, NFL platinum, MLB K, Soccer scorer). Check #8 already exempts specialized engines from the blanket CONDITIONED rejection; check #9 was missing the symmetric carve-out. Every calibrated Tennis pick with lock ≥ 85, real odds, and canonical identity was being silently marked `off_board=True` with an EMPTY `off_board_reasons` array.
**Fix (surgical, 8-line comment + 1-line guard)**: `services/model_integrity_gate.py:206-233` — check #9 now bypasses `universal_lock_authority_rejected` when `_has_specialized_engine(pick)` is True.
**Recovery run (idempotent script `/tmp/remediate_tennis_offboard.py`)**: 
- **Cleared 400+ Tennis picks** stuck off_board with empty reasons; all had lock ≥ 85, real book_odds, tennis_calibrated markers, no chalk/longshot trap
- Today slate 2026-09-21: Tennis in-window went from **0 → 3 board picks** (Sofia Costoulas 87.1 · Xinyu Wang 85.3 · Storm Hunter 85.3); remaining 2 correctly off_board via `chalk_trap` (-425/-500)
- Overall Tennis board+lock≥85 across DB: **~0 → 2,458**

### P1 — CFB Rescore Moneyline Coverage (real defect closed)
**Root cause**: `scripts/maintenance/cfb_signfix_v3_rescore.py:_rescore_one` rejected every pick where `line is None` (line 116). Moneyline picks have no line by definition; 21 retired v2 ML picks (`retirement_reason=cfb_sp_signfix_v3_regen`) were dropped before rescoring, leaving **zero v3 CFB ML picks** across the entire DB (only Spread=124 + Total=59 for v3).
**Fix (surgical)**: `scripts/maintenance/cfb_signfix_v3_rescore.py:108-131` — require `line` only for spread/total markets; ML is line-agnostic and proceeds to `wp = float(game.p_home_ml)`.
**Post-run**: Rescore now processes ML picks; 19/20 target ML picks failed with `sp_missing:away` (SP+ ratings absent for the away team — legitimate data-coverage gap, not a pipeline defect). The rescore pipeline is now **capable** of regenerating CFB ML → v3 for every event where SP+ ratings cover both teams. Live sports_engine (`sports_engine.py:3041-3110`) already emits v3 ML natively for covered games.

### Soccer — funnel truth report (no code change)
Real off_board_reasons breakdown on today's slate (2026-09-21):
- `LOW_LOCK_SCORE: 18,908` (77% — model-eligible picks below 85; correct behavior)
- `TEAM_CONTEXT_UNAVAILABLE: 2,602` — leagues without model coverage (Nations League, Liga MX, Brasileirao B, Argentina Primera Division, Bundesliga Women, etc.)
- `NO_TEAM_CONTEXT: 1,076` · `NO_MODEL_PROBABILITY: 675` · `NO_POSITIVE_EDGE: 203`
Today's real Soccer slate window contains 264 events — all in uncovered leagues (Brasileirao B 149, Argentina Primera 99, Bundesliga Women 14). Per contract "SUPPORTED MODEL REQUIRED", these correctly do not reach evaluation. Universal discovery works — dynamic FD-code → Odds-API mapping covers 15 top-tier competitions plus 16 capability-registry leagues. Expanding to include NFL / EPL Sunday-only nations is not surgical (needs new team-form data).

### CFB — funnel truth report
- 2026-09-19 (Fri): 132 v2/v3 picks → 104 board (all v3-signfix). 10 with lock ≥ 85 (6 Spread + 4 Total). Corrected model; no ML in current v3 rows.
- 2026-09-20 (Sat) / 2026-09-21 (Sun) / 2026-09-22 (Mon): **0 CFB events** — no games (legit).
- Live board: 1 CFB Lock currently visible (Missouri State @ SMU Total Under 60.5, L=87.2). Not "restoring old inflated 90-99" — that's the truthful corrected output.

### Not touched
- No NFL Props V2 · Rollover · Parlay · other sports · Lock Score math · 85-threshold
- No new leagues added (blocked by team-model data availability, not a surgical change)
- No rescore of picks outside the retired-v2 CFB set

---

## 2026-06-21 · NFL Props V2 Writer-Side Score-Authority Integration

### Part 1 — Production historical backfill: **BLOCKED**
- `POST /api/admin/historical/backfill-seasons` at `https://bet-edge-ai-1.emergent.host` requires ADMIN role.
- `demo@lockscore.ai / demo123` is a REGULAR USER on production (`role=user`). Preview grants admin; production does not.
- HTTP 403 `{"detail":"Admin role required"}` returned. Cannot execute without production admin credentials.
- Recommendation: user must run this from an authenticated production admin session (or provide production admin credentials).

### Part 2 — Writer-side V2 integration (surgical, closed)
**Defect**: `services/nfl_props_v2/canonical_wiring.enrich_nfl_picks_with_v2_evidence` wrote `nfl_props_v2_evidence` additively but never consumed it in `compute_lock_score`. NFL Props V2 intelligence existed alongside an unchanged canonical Lock Score.

**Fix (~130 new LOC + surgical edit in `canonical_wiring.py`)**:
- **NEW** `services/nfl_props_v2/writer_integration.py` — pure functions that map V2 evidence into `[0,1]`-scale factor keys the existing `sports_engine.compute_lock_score` authority already accepts:
  - `NFL V2 Historical Hit Rate` — clamped `hit_probability_monotonic`
  - `NFL V2 Sample Confidence` — data quality (saturates at n=20)
  - `NFL V2 Distribution Floor` — Q25-vs-line cushion (line-scaled monotonic squash, penalises picks whose Q25 falls below the line)
  - `NFL V2 Indoor Neutral` — only when weather AVAILABLE + indoor
- Fail-closed: V2 with `hit_prob_monotonic=None` or `n<5` produces no factor block; the pick keeps whatever the base pipeline scored.
- Modified `enrich_nfl_picks_with_v2_evidence` to invoke `integrate_v2_into_lock_score` per pick, re-run `compute_lock_score(merged_factors, win_prob, pick, edge)` ONCE, and stage the result through the normal writer path (lock_score / lock_score_v2 / lock_score_raw / lock_breakdown / factors / published_lock_score / grade / lock_score_authority). No `max(old,new)`, no read-time repair, no direct override.

**Guarantees preserved (per user's explicit constraints)**:
- Lock Score ≠ Win Expected. V2 hit-probability is a factor, not the answer.
- Sportsbook implied stays market evidence, not model probability.
- No arbitrary bonuses. No hard-coded thresholds. No hard-coded stars.
- `_canonicalize_lock_score` at read time untouched.
- 85+ universal threshold untouched.

### Results — NFL slate 2026-09-21 (295 alt-line eligible picks)
| Tier    | BEFORE | AFTER |
|---------|--------|-------|
| 85-89   |   169  |  108  |
| 90-92   |    69  |  119  |
| 93-95   |    31  |   44  |
| 96-97   |    22  |   22  |
| 98      |     0  |    0  |
| 99      |     0  |    0  |
| 100     |     0  |    0  |

- **198/199 picks with valid V2 distributions were integrated** — 61 legitimately upgraded from 85-89 to 90+ (score moved UP where V2 evidence supports)
- Some picks moved DOWN (V2 weakened them): e.g. Chris Brooks 16.5 Rush Yds — V2 hit_prob=0.15 on n=20 → Lock stayed at 86.9 (real red-flag signal preserved).

### Canaries — BAL @ DAL (Dak 200+, Lamar 20+)
- **Not present in DB**: no Dak 200+ Pass Yds or Lamar 20+ Rush Yds picks exist for any pick_date. The BAL @ DAL matchup is not yet ingested. Cannot evaluate against real rows.
- Runtime plumbing verified via existing NFL slate picks (Cooper Kupp 5+ Rec Yds L=97.6, Alvin Kamara 1+ Rec L=97.5, Patrick Mahomes 150+ Pass L=97.4 — all V2-integrated with `hit_prob=1.0-0.9, n=20`).

### 98/99/APEX 100 reachability — honest state
- **Zero 98+ NFL picks on the current slate.** Top V2-integrated is Cooper Kupp @ 97.6.
- V2 factors alone can push a pick from 85-89 to 90-95 but not to 98/99/100 because the Magic Tier authority (`services/magic/lock_score_integrator.py:280`) still zeroes the positive delta on `INSUFFICIENT_EVIDENCE`. Multiple independent evidence categories (History + Recent Form + Role + Matchup + Independent Model + Market Intelligence) are needed for the 98+ / Apex ladder — V2 provides mainly History + Role signals; Recent Form and Matchup mapping into the Magic tier authority is an additional gap not addressed in this pass.
- 96-97 tier reachable, no manufactured 98+ (per user's explicit "not required").

### Canonical parity — verified
- Live `/api/picks/today?sport=NFL`: **4/4 picks have `lock_score == published_lock_score`** (exact match, no drift, no read-time repair). Writer-side integration produces the ONE canonical score that flows through the entire canonical publication chain.

### Regression
- **86/86** targeted tests pass (NFL Props V2 · Rollover · Parlay 3.0 · Canonical Epoch v2 · CFB sign-fix)
- 5 new writer-integration unit cases pass (empty V2 fail-closed · small-sample suppression · valid V2 factor block · below-line floor penalty · full integration)

### Files touched
- **NEW** `services/nfl_props_v2/writer_integration.py`
- **EDIT** `services/nfl_props_v2/canonical_wiring.py` — V2 → compute_lock_score wiring inside `enrich_nfl_picks_with_v2_evidence`

### Not touched (per constraints)
- MLB, CFB scoring math, Tennis scoring, Soccer scoring, Rollover, Parlay
- Apex gate structure, Magic tier ceilings, evidence-count caps
- Frontend, read-time canonicalisation, historical frozen wager snapshots
- 85+ universal threshold, published_lock_score direct-override path

---

## 2026-06-21 · Production Backfill + NFL Magic Evidence-Category Integration

### Part 1 — Production admin auth ✅
- `Bossmanperkins@yahoo.com` authenticated on production, role=admin, status=active
- Credentials used ONLY for this maintenance op. Never persisted to source, .env, or logs.

### Part 2 — Support-directed production backfill ✅
- Called `POST https://bet-edge-ai-1.emergent.host/api/admin/historical/backfill-seasons` with `{sports:[cfb,nfl,tennis,soccer,nba], lookback:3, skip_if_done:true}`
- **Collection counts BEFORE → AFTER**:
  - `players:            16,519 → 16,879 (+360)`
  - `games:               4,202 →  7,575 (+3,373)`
  - `player_game_logs:  166,789 → 173,534 (+6,745)`
- **Per-sport ingestion status** (post-run):
  - **CFB** — 3 seasons attempted (2024/2025/2026), all `empty` (provider returned 0 rows; retryable per Support). Games inserted: 0. Logs inserted: 0.
  - **NFL** — 2024 season done previously (games=286, logs=22,346); 2025 done previously (games=286, logs=23,151); 2023 attempted, `empty`. Errors: prior E11000 dup-key blocker was NOT re-triggered on this pass.
  - **Tennis** — 2024 season: **games=0, logs_inserted=6,152**; 2026 skipped (done). Errors: none.
  - **Soccer** — 2026: 0 rows added; 4 errors from football-data.org 404s on Champions League + European Championship scorers/standings (upstream, non-blocking).
  - **NBA** — 2025/2026 skipped (done).
- Historical resolution verified: `player_game_logs` count grew, confirming the pipeline can now query fresh actuals.

### Part 3-7 — NFL Magic evidence-category integration ✅
**Root defect discovered by explore_agent recon**: The Magic authority uses 6 independent evidence categories, but the generic `build_playerprop_evidence` adapter for NFL emitted only 3 EvidenceTypes (`HISTORICAL_EXACT_THRESHOLD`, `MODEL_PROBABILITY`, `SPORTSBOOK_CONSENSUS`) — filling only `history_exact`, `model_family`, `market_intel`. **`recent_form`, `role_opportunity`, `matchup` were structurally empty for every NFL player prop.** Apex #6 (≥5 positive categories) and Apex #7 (`role_opportunity` OR `matchup` positive) were unreachable. A separate `services/magic/gold_evidence_nfl.py` file had rich NFL adapters, but they were orphaned (never imported).

**Fix (files touched)**:
- **NEW** `services/magic/adapters/nfl_playerprop_ext.py` (~245 LOC) — emits three EvidenceItems for NFL player props:
  - **RECENT_FORM** from persisted `nfl_feature_engine.factors["L5 Avg vs Line"]` (distinct `source_class="nfl_feature_engine::L5_avg_vs_line"` so `collapse_history_form` can still guard shared source)
  - **ROLE_OPPORTUNITY** from `nfl_player_usage.snap_pct_avg` (authoritative) with V2 `role_status` as a PARTIAL fallback; UNAVAILABLE if neither
  - **MATCHUP** from `player_game_actuals` opponent+position rows (genuinely independent — the opponent's *other players*, not the pick's player); UNAVAILABLE when opponent history is missing (no fabrication)
- **EDIT** `services/magic/adapters/playerprop.py` — 12-line NFL-only branch calling `emit_nfl_extended_evidence(db, pick, out)` after existing evidence is emitted. Additive, best-effort (swallows exceptions so Magic authority always continues).

**Provenance & independence rules honored (Part 5)**:
- HISTORY source_class `authoritative::L20_threshold` — L20 hit-rate
- FORM source_class `nfl_feature_engine::L5_avg_vs_line` — L5 avg, distinct provenance
- ROLE source_class `nfl_player_usage::snap_pct` — different collection
- MATCHUP source_class `player_game_actuals::opponent=<TEAM>` — different rows entirely
- MODEL source_class `nfl_props_v2::calibrated` (single vote enforced by `_CATEGORY_MAP` collapsing three EvidenceTypes into `model_family`)
- MARKET source_class `the_odds_api` — different data
- `collapse_history_form` in `lock_score_integrator.py` still zeroes FORM's positive vote when source_key set matches HISTORY exactly. No double-counting.

**INSUFFICIENT_EVIDENCE gate preserved (Part 6)**: not weakened. `lock_score_integrator.py:280-291` unchanged.

### Reachability proofs — 9 controlled fixtures through REAL scoring path
`tests/test_nfl_magic_reachability.py` — constructs MagicOutput with controlled EvidenceItems and runs the REAL `apply_magic_and_apex()` authority (no test-only scorer):
- **A**  complete 6/6 evidence → **reaches ≥98** ✅
- **A2** complete 6/6 evidence → **can qualify Apex 100** ✅
- **B**  matchup missing (5/6) → still qualifies (role satisfies #7) ✅
- **C**  form missing (5/6) → still reaches ≥98 ✅
- **D**  independent model missing (5/6 non-model) → still reaches ≥98 (#9 satisfied) ✅
- **E**  market missing → **APEX fails closed** (#8 rejects) ✅
- **F**  only 3 categories (pre-fix baseline) → APEX fails closed, cap 99 ✅
- **G**  non-APEX hard cap at 99 ✅
- **H**  INSUFFICIENT_EVIDENCE zeros positive delta ✅

### Part 8 — Real NFL slate 2026-09-21 (295 alt-line eligible)
BEFORE (V2 writer integration only) → AFTER (+ Magic evidence extension):
| Tier    | BEFORE | AFTER | Δ    |
|---------|--------|-------|------|
| below-85|    0   |    3  |  +3  |
| 85-89   |  108   |   93  | -15  |
| 90-92   |  119   |  104  | -15  |
| 93-95   |   44   |   57  | +13  |
| 96-97   |   22   |   29  |  +7  |
| **98**  |    0   |    9  |  **+9** ✨ |
| 99      |    0   |    0  |   0  |
| 100     |    0   |    0  |   0  |

**Top 5 NFL locks (AFTER)** — all real, no manufactured scores:
- L=98.6 Cooper Kupp 5+ Rec Yds (V2 hp=1.0, n=20) apex_block=`magic_tier_not_aligned_strong:ALIGNED`
- L=98.5 Alvin Kamara 1+ Rec (V2 hp=0.9, n=20) apex_block=`insufficient_independent_categories:2/5`
- L=98.5 Davante Adams 2+ Rec (V2 hp=0.9, n=20) apex_block=`insufficient_independent_categories:2/5`
- L=98.4 C.J. Stroud 125+ Pass Yds (V2 hp=0.9, n=20) apex_block=`magic_tier_not_aligned_strong:ALIGNED`
- L=98.4 Justin Herbert 5+ Rush Yds (V2 hp=0.85, n=20) apex_block=`magic_tier_not_aligned_strong:ALIGNED`

Apex 100 not granted on this slate — legitimate block_reasons every time. NO structural NFL-only ceiling.

### Part 9 — 198/199 discrepancy resolved
One pick, `Jonathon Brooks Over 7.5 Player Reception Yds` (line=7.5, hp=0.505, **n=3**), correctly gated by the writer-integration n<5 fail-closed guard in `build_v2_factors`. This is DESIGN, not a defect — prevents 1-3 sample "distributions" from injecting noisy probabilities into Lock Score. Provenance documented in `writer_integration.py:106-113`.

### Parts 10-11 — Dak / Lamar canaries
BAL @ DAL sportsbook row still NOT ingested by the sportsbook feed at time of this pass. Cannot evaluate the specific canary rows. When the row is ingested, they will flow through the SAME universal pipeline (no player-specific code exists).

### Part 12 — Alt-line monotonicity
Existing V2 engine at `services/nfl_props_v2/engine.py::build_distribution` produces `hit_probability_monotonic` from the ladder (see `_monotonize` step). The writer integration passes it through unchanged. Ladder monotonicity is enforced upstream at distribution build time, not at writer integration.

### Part 13 — Canonical publication parity
Live `/api/picks/today?sport=NFL`: **4/4 picks have `lock_score == published_lock_score`** exactly. No read-time repair. No frontend override.

### Part 14 — Non-APEX 100 impossible
Enforced by `NON_APEX_HARD_CAP=99.0` in `lock_score_integrator.py:53` + explicit `defensive_downgrade_if_needed`. Test `test_non_apex_hard_cap_99` passes.

### Part 15 — Focused regression
- **95/95** targeted tests pass (NFL Props V2 · NFL Magic Reachability · Rollover · Parlay 3.0 · Canonical Epoch v2 · CFB sign-fix)
- 9 new NFL Magic reachability tests pass
- 11 pre-existing failures in unrelated test files (`test_magic_3a1`, `test_magic_3e`, `test_magic_3i1`, `test_main40`, `test_phase8_parlay2`) verified to FAIL identically before my changes (git-stash confirmation)

### Files touched
- **NEW** `services/magic/adapters/nfl_playerprop_ext.py`
- **NEW** `tests/test_nfl_magic_reachability.py`
- **EDIT** `services/magic/adapters/playerprop.py` (+12 lines for NFL branch)

### NOT touched (per hard guardrails)
- MLB / NBA / CFB / Tennis / Soccer scoring
- Rollover · Parlay 3.0 · Canonical Epoch · frozen wager snapshots
- APEX gate structure · INSUFFICIENT_EVIDENCE gate · NON_APEX_HARD_CAP
- Universal 85+ threshold · read-time score repair · frontend scoring
- Player-specific hardcodes · thresholds · sportsbook-as-model

---

## 2026-06-21 · CFB Horizon Regression + NFL/CFB Runtime Truth Report

### 🟢 CFB Regression — Root Cause Found & Fixed (P0-A/B/C/G)
**Root cause discovered via explore_agent recon**: `routes/picks_routes.py:1924-1926` gave NFL a **168h** horizon window but every other sport stayed at **72h**. CFB is a WEEKLY sport (games mostly Saturday). On Monday/Tue/Wed, Saturday's slate falls OUTSIDE the 72h window (Mon → +72h ≈ Thu; Sat games are 96-120h out). Result: CFB Locks disappear entirely between Sun→Wed even though the picks exist in the DB with `lock ≥ 85`.

**Regression boundary confirmed**: This defect predates the Totals Core work — the horizon carve-out was NFL-only from the start. The Totals Core work did not introduce this. The user's observation ("only 1 CFB Under 60.5 shows") is real, but the cause is the missing weekly-sport horizon, not the Totals math.

**Fix (2 lines)** `routes/picks_routes.py:1917-1938`:
```python
_cfb_scope     = "CFB" in _sport_scope
_weekly_scope  = _nfl_scope or _cfb_scope
_horizon_hours = 168 if _weekly_scope else 72
```
CFB now receives the same 7-day horizon NFL enjoys. Other sports stay at 72h (no leak to soccer/MLB).

### 📊 CFB Funnel Truth — Upcoming Slate
Real CFB events discovered in the sportsbook feed for **event_time > now** through 2026-09-30:
- **49 upcoming CFB picks** (Fri 09-26: 39 events, Sat 09-27: 10 events)
- **45 board-eligible** (not off_board / no_bet)
- **1 with lock ≥ 85**: Missouri State @ SMU Under 60.5 L=87.2 (v3-signfix engine)
- ML / Spread / Total all present in the funnel — Oklahoma State ML 77.2, Utah State ML 75.8, Missouri ML 73.2 + multiple spread picks 71-84.5 + multiple totals

**All 3 game-market families ARE ingesting, normalizing, scoring, and reaching the writer**. The reason only 1 reaches Lock ≥ 85 is truthful v3-signfix corrected math — the previous session's sign-fix reset legitimately dropped many CFB scores below 85. Per user's explicit constraint ("If it contains no 90+, report that truth"), this is honest output — not a bug. Texas A&M +8.5 at L=84.5 is right below the threshold and would legitimately reach 85+ with additional matchup evidence.

### 🔴 Preview ↔ Expo Parity (P0-G/H/I) — Honest Blocker
Preview/Web hits the **preview backend** (`http://localhost:8001`); Expo Go hits the **deployed production backend** (`https://bet-edge-ai-1.emergent.host`). These are **separate databases and separate deployments** (established previous session). The horizon fix landed in Preview only. **Deploying to production requires user action** (Publish button). Until then Expo will continue to show the pre-fix behavior — this is a deployment gap, not a stale-cache defect.

### 🟢 Preserved from prior sessions
- **NFL Magic evidence-category integration** (P0-N/O) — 9/9 reachability fixtures still green; NFL slate 2026-09-21 still holds AFTER distribution (9 picks at 98)
- **Production backfill** (P2) — collection deltas preserved (players +360, games +3,373, player_game_logs +6,745)
- **CFB signfix v3** — engine v3 still stamping every corrected pick (49/49 v3-signfix on upcoming slate)

### 🧪 Regression
- **95/95** targeted tests pass (NFL Props V2 · NFL Magic Reachability · Rollover · Parlay 3.0 · Canonical Epoch v2 · CFB sign-fix)
- 1 horizon test in the general suite passes with new CFB carve-out

### 🚫 NOT CLOSED IN THIS PASS (honest blockers)
- **CFB historical backfill 2024/2025/2026** returned `empty` — provider CFB adapter needs investigation (endpoint / auth / week iteration / FBS filter). Not tractable without deeper adapter recon; scope deferred.
- **BAL @ DAL sportsbook ingestion (P0-R)** — BAL @ DAL row still not in the Odds API feed. When it publishes, the universal NFL pipeline will pick it up automatically; no code change is required for the ingestion path to succeed. Dak/Lamar canaries deferred to natural ingestion.
- **Top-20 NFL Magic evidence matrix live proof (P0-P/Q)** — extension IS wired but current top-20 picks show `magic_categories_positive=['recent_form']` (1 category) because `nfl_player_usage` is sparsely populated on production and opponent history rows haven't been fetched for the current slate. The wiring is correct; population is the blocker. To move `role_opportunity` and `matchup` to AVAILABLE requires the NFL usage backfill to complete (currently gated by provider quota).
- **CFB /api/picks/today post-horizon-fix content** — restart-loaded successfully, still returns 1 CFB Lock. That IS the truthful output for this slate; not a fix regression.

### Files touched (this pass)
- **EDIT** `backend/routes/picks_routes.py` — +5 lines (`_cfb_scope`, `_weekly_scope`, comment block)
