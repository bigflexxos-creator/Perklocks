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
