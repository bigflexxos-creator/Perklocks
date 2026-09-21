# P0 · Rollover True Immutability + Parlay 3.0 Root Closure — 2026-06-21

## Overview

Two P0 root closures delivered as ONE surgical build on the Canonical
Parity Preview.  Production intentionally NOT republished.

## Part A — Rollover True Immutability

### Root Cause

`GET /api/picks/rollover` was serving wager truth from mutable
`db.picks` documents (line, odds, WP, Lock, book), using the frozen
`rollover_slates.legs[]` only for MEMBERSHIP + ORDER.  Every board
regen / line move / model rescore drifted the "frozen" values
underneath the user.

Additional defects:
* PICK_MISSING (mutable row absent) auto-triggered leg replacement,
  even when the wager itself was untouched.
* No DB uniqueness on `(slate_date, scope)` — two workers could each
  freeze a different "official" slate.
* Frozen leg schema was identity-only — did not carry `sportsbook` or
  a stable price snapshot.

### Files Changed

* `backend/services/rollover_official_slate.py` — v2 freeze contract
  (full wager snapshot: market/selection/line/sportsbook/odds/WP/lock
  score/grade/publication_version/board_version/frozen_at) with
  `FROZEN_WAGER_VERSION = 2`.  `_pregame_invalid_reason` rewritten:
  PICK_MISSING alone no longer invalidates a leg; only explicit
  off-board / no-bet / voided / cancelled / line-removed-with-
  provenance evidence triggers repair.  Added `ensure_slate_indexes()`
  creating the unique index on `(slate_date, scope)`.
* `backend/services/rollover_frozen_view.py` — NEW.  `build_frozen_view
  (leg, live_doc)` renders wager truth STRICTLY from the leg for v2
  legs; only status/actual/result/event_status/graded_at flow from the
  live doc.  v1 legs get partial fallback with
  `frozen_wager_provenance="legacy_v1_partial"`.
* `backend/routes/picks_routes.py` — both reader paths (READER-FIRST
  block and freeze-and-serve block) now use `build_frozen_views` to
  build the response documents.  Wager fields are never rehydrated
  from `db.picks` for v2 slates.
* `backend/server.py` — startup calls `ensure_slate_indexes(db)` to
  install the DB-enforced uniqueness constraint.
* `backend/scripts/migrate_rollover_slates_frozen_wager_v2.py` — NEW.
  Idempotent migration that reads the frozen `rollover_slate_events`
  FROZEN events (which embedded original leg snapshots at freeze
  time), and ONLY backfills fields provably recoverable from that
  immutable evidence.  Fields not in the FROZEN event are marked
  `unavailable` — never fabricated from today's mutable data.

### Migration Result

```
Migrated rollover:2026-09-18:official: recovered=3 unrecoverable=0
Migrated rollover:2026-09-19:official: recovered=3 unrecoverable=0
Migrated rollover:2026-09-20:official: recovered=3 unrecoverable=0
Migrated rollover:2026-09-21:official: recovered=1 unrecoverable=0
DONE — total_slates=4 recovered_legs=10 unrecoverable_legs=0
```

Historical `sportsbook` / `board_version` / `league` remain
`unavailable` on legs where the FROZEN event did not carry them — per
user directive we never manufacture historical truth.

### Immutability Proofs

**Mutation Proof** — mutated live `db.picks` from
line=1.5 → 999.5, odds=-190 → +999, WP=74.56 → 5.5, Lock=89.8 → 55.0:
```
BEFORE:  rank=1 sel='Matthew Stafford' line=1.5 odds=-189 wp=76.3 lock=89.8
AFTER:   rank=1 sel='Matthew Stafford' line=1.5 odds=-189 wp=76.3 lock=89.8
```

**Restart Proof** — full backend restart (RAM cache wiped):
```
BEFORE: [[1,"Matthew Stafford",1.5,-189,76.3,89.8]]
AFTER:  rank=1 sel='Matthew Stafford' line=1.5 odds=-189 wp=76.3 lock=89.8
```

**Multi-Worker Proof** — 3 workers concurrently attempting to freeze
2099-01-01: DB-level unique index made all 3 return the same winner
(`test-A-1`), slate_count = 1.

**PICK_MISSING Proof** — reconcile with mutable pick deleted: leg
preserved (canonical_pick_id='p1', line=8.5).

## Part B — Parlay 3.0 Root Closure

### Root Cause

HIGH_RISK NFL / MLB returned NO parlays because:
1. Route said "10 legs required" but only 5 unique NFL events existed.
2. Optimizer's `damage_control_ok` used `MAX_ABS_DROP_HIGH_RISK = 0.30`
   which effectively became an undeclared minimum win-probability
   floor on the 8th+ leg.
3. Market-family cap of 2 + candidate cap + same-event hard blocks
   compounded into geometric impossibility with zero diagnostic.
4. Three competing mode definitions across route / optimizer /
   intelligence layer.

### Files Changed

* `backend/services/parlay/mode_policy.py` — NEW.  ONE authoritative
  `ModePolicy` per mode (STANDARD / ADVANCED_SAFER / ADVANCED_HIGH_EV
  / HIGH_RISK / TODAY_WINDOW).  Each policy carries lock_floor,
  min_edge_pct, max_relative_drop_per_leg, max_absolute_drop_per_leg,
  same_sport_soft_cap_ratio, max_same_market_family, min_useful_legs,
  and health_weights.  HIGH_RISK risk budget expanded (0.55 relative /
  0.35 absolute) so 10–20 leg builds are geometrically feasible.
* `backend/services/parlay/feasibility.py` — NEW.  `compute_funnel()`
  returns a `FeasibilityReport` with canonical/mode_eligible/real_line/
  unique_events/market_family counts + status `READY | PARTIAL_ONLY |
  INSUFFICIENT` + machine-readable reason codes
  (`INSUFFICIENT_CANONICAL_LEGS`, `INSUFFICIENT_UNIQUE_EVENTS`,
  `MODE_THRESHOLD_STARVATION`, `MARKET_CONCENTRATION_LIMIT`,
  `NO_REAL_ODDS`, `NO_SUPPORTED_COMBINATION`).
* `backend/parlay_optimizer.py` — `damage_control_ok`,
  `build_one_parlay`, `build_top_parlays` accept a `policy` argument
  and use `policy.max_relative_drop_per_leg` / `min_useful_legs`
  instead of the legacy per-mode constants.
* `backend/routes/parlay_routes.py` — resolves ModePolicy via
  `resolve_mode`, computes feasibility funnel BEFORE the optimizer
  runs, drops effective target to `dependency_safe_max_legs` when
  PARTIAL, emits structured reason codes on empty state, threads
  `policy` into every `build_top_parlays` call, stamps each returned
  card with `actual_legs / requested_target_legs / effective_target
  _legs / is_partial / mode_key / mode_display`.
* `backend/parlay_history.py` — SAVED PARLAY WAGER FREEZE.  Every leg
  snapshot now stores `sportsbook`, `published_odds`,
  `published_probability`, `canonical_event_id`, and stamps
  `frozen_wager_version = 2` at both leg and doc levels — same
  principle as Rollover: the wager the user tapped is immutable.

### Runtime Funnel Proofs (Canonical Parity Preview, 2026-09-21)

| Mode / Filter                | canonical | real_line | mode_eligible | unique_events | max_feasible | Status         | Parlays |
| ---------------------------- | --------: | --------: | ------------: | ------------: | -----------: | -------------- | ------: |
| STANDARD auto                |        97 |        97 |            97 |             6 |            6 | READY          |       3 |
| STANDARD NFL single          |        56 |        56 |            56 |             1 |            1 | INSUFF+expand  |       3 |
| STANDARD MLB single          |        40 |        40 |            40 |             4 |            4 | READY          |       3 |
| ADVANCED_SAFER NFL           |        12 |        12 |            12 |             1 |            1 | INSUFF+expand  |       3 |
| ADVANCED_HIGH_EV MLB         |        40 |        40 |            40 |             4 |            4 | READY          |       3 |
| HIGH_RISK NFL 10             |        56 |        56 |            51 |             1 |            1 | INSUFF (truthful) |     0 |
| HIGH_RISK MLB 10             |        40 |        40 |            39 |             4 |            4 | expand→72h     |       3 (6/10 partial) |
| HIGH_RISK MLB 15             |        40 |        40 |            39 |             4 |            4 | expand→72h     |       3 (6/15 partial) |
| HIGH_RISK MLB 20             |        40 |        40 |            39 |             4 |            4 | expand→72h     |       3 (6/20 partial) |

Before: HIGH_RISK MLB 10/15/20 returned `parlays: []` with generic
"Not enough qualifying picks". After: 3 truthful partial cards with
`actual_legs=6, requested_target_legs=10/15/20`.

HIGH_RISK NFL 10 legitimately returns zero (only 1 unique event on the
current slate — no dependency-safe 10-leg build possible) with
`reason_codes: ["INSUFFICIENT_UNIQUE_EVENTS"]` — this is TRUTHFUL, not
a regression.

## Regression Suite

`backend/tests/test_rollover_immutability_and_parlay30.py` — 17 tests,
all passing:
* Freeze v2 has full snapshot ✓
* Frozen view wager immutable under live mutation ✓
* Frozen view survives missing live doc ✓
* PICK_MISSING alone does not invalidate ✓
* Explicit-evidence gate requires off-board/no-bet/void ✓
* Reconcile does not replace on PICK_MISSING ✓
* Concurrent freeze yields single authoritative slate ✓
* ModePolicy resolves correctly ✓
* clamp_target_legs respects bands ✓
* Feasibility READY / PARTIAL_ONLY / INSUFFICIENT semantics ✓
* NO_REAL_ODDS reason ✓
* MODE_THRESHOLD_STARVATION reason ✓
* ADVANCED_HIGH_EV gates negative edge ✓
* HIGH_RISK expanded risk budget ✓
* Saved parlay wager freeze marker ✓

## Known Limitations

* SGP (Same-Game Parlay) pricing / joint probability remains NOT
  implemented — cross-event parlays only.  Dependent sports (MLB / NFL
  / NBA / NHL / UFC / Tennis) still fail-closed same-event.  Building
  real joint probability is out of scope.
* Health-score `health_weights` per policy is defined but the existing
  `parlay_health()` calculation still uses the legacy fixed weights
  (0.35/0.25/0.20/0.10/0.10).  Migrating the weights is an
  incremental follow-up — no user-visible defect blocks this.
* Frontend UI already renders `actual_legs` (existing LEGS field) and
  our additive backend fields (`is_partial`, `requested_target_legs`,
  `feasibility`) are available for a future "X OF Y TARGET LEGS"
  banner but not yet displayed distinctly.
* `test_canonical_revision_uniform_across_surfaces_at_rest` from the
  earlier session remains outside this task's scope.

## Production Published: NO
