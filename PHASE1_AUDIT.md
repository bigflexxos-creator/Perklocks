# Phase 1 Audit Report — Mutation Inventory

**Document version:** Phase 1a (2026-08-06)
**Scope:** Complete inventory of every write to a `published_*` field
across the backend, plus every consumer that reads them.

This audit is the evidence base for Phase 1b (endpoint conversion) and
Phase 1c (mutation removal + legacy field retirement).

---

## 1. Executive summary

The current pipeline has **~91 files** touching the fields that will
become immutable after Phase 1c.  Writes cluster in these categories:

| Category | Files | Nature |
|---|---|---|
| **Pipeline stages (legit — pre-publication)** | 15 | fetchers, feature builders, model, fusion, magic tier, quality gate, board validator |
| **Enrichment decorators (post-publication mutation — MUST be removed)** | 12 | SportDB, career history, xG, GK quality, matchup wiring, form ledger |
| **Learning writers (post-publication mutation — MUST be removed)** | 6 | apply_learning, bandit lift, learning_v2, adaptive_learning |
| **Read-time canonicalizer (single biggest offender)** | 1 | `server._canonicalize_lock_score` — 76-line function that MUTATES the pick dict on every read via `max(v1, v2, raw, peak)` + always-starter floor + coherence cap |
| **Settlement writers (write graded status to picks — MUST move to `settlement_events`)** | 9 | settlement_engine, prop_settlement, soccer_espn_settle, kbo_settlement, tennis_extra/settle, brain/nrfi_engine, grading_validator, stuck_pick_reaper, rollover_history_tagger |
| **Direct-inject writers (bypass canonical pipeline)** | 3 | mls_direct_inject, soccer_prop_inject, csl_espn_live |
| **Read paths (endpoints + serializers)** | 12+ | pick routes, admin routes, lab routes, analytics, market_competition |

**Standalone MongoDB constraint:** multi-doc transactions are not
available.  The publication contract does not require them — see §7.

---

## 2. Mutation hotspots — top 40 files (writes to published fields)

| File | lock_score | prob | edge | grade | conf | odds | line |
|---|---:|---:|---:|---:|---:|---:|---:|
| `routes/picks_routes.py` | 38 | 0 | 19 | 2 | 0 | 0 | 0 |
| `server.py` | 10 | 7 | 6 | 9 | 10 | 0 | 0 |
| `sports_engine.py` | 12 | 4 | 8 | 3 | 3 | 0 | 2 |
| `pick_validator.py` | 6 | 2 | 7 | 5 | 5 | 0 | 0 |
| `uefa_espn_ingest.py` | 8 | 4 | 4 | 4 | 0 | 1 | 1 |
| `elite_players.py` | 6 | 4 | 4 | 2 | 1 | 0 | 0 |
| `services/prediction_fusion_engine.py` | 0 | 6 | 0 | 2 | 7 | 0 | 1 |
| `learning_system_v2.py` | 8 | 1 | 0 | 4 | 2 | 0 | 0 |
| `lab_routes.py` | 9 | 0 | 4 | 0 | 0 | 0 | 0 |
| `routes/admin_routes.py` | 7 | 0 | 0 | 1 | 1 | 0 | 1 |
| `tennis_extra/picks.py` | 3 | 2 | 3 | 1 | 1 | 0 | 0 |
| `market_competition/routes.py` | 4 | 2 | 2 | 2 | 0 | 0 | 0 |
| `soccer/predictor.py` | 2 | 2 | 2 | 2 | 1 | 0 | 0 |
| `services/espn_soccer_fixtures.py` | 1 | 5 | 1 | 1 | 1 | 0 | 0 |
| `analytics.py` | 6 | 1 | 2 | 0 | 0 | 0 | 0 |
| `board_validator.py` | 2 | 1 | 1 | 1 | 1 | 0 | 1 |
| `sportdb_player_scorer.py` | 4 | 1 | 1 | 1 | 1 | 0 | 0 |
| `learning_engine.py` | 2 | 2 | 2 | 1 | 1 | 0 | 0 |
| `tennis_engine.py` | 2 | 0 | 1 | 2 | 3 | 0 | 0 |
| `soccer_lab.py` | 3 | 1 | 1 | 2 | 1 | 0 | 0 |
| `prop_settlement.py` | 0 | 0 | 0 | 0 | 0 | 0 | 7 |
| `services/signal_engine/rank.py` | 2 | 1 | 1 | 2 | 0 | 0 | 1 |
| `evidence_engine.py` | 3 | 1 | 1 | 1 | 1 | 0 | 0 |
| `services/data_driven_model.py` | 0 | 0 | 0 | 0 | 7 | 0 | 0 |
| `thesportsdb_scorer.py` | 2 | 1 | 1 | 1 | 1 | 0 | 0 |
| `services/mls_direct_inject.py` | 2 | 1 | 1 | 1 | 1 | 0 | 0 |
| `services/soccer_prop_inject.py` | 2 | 1 | 1 | 1 | 1 | 0 | 0 |
| `services/lock_score_performance.py` | 3 | 3 | 0 | 0 | 0 | 0 | 0 |
| `routes/analytics_routes.py` | 2 | 3 | 1 | 0 | 0 | 0 | 0 |
| `ufc_espn_ingest.py` | 2 | 1 | 1 | 1 | 0 | 0 | 0 |
| `soccer_hot_scorers.py` | 2 | 1 | 1 | 1 | 0 | 0 | 0 |
| `lock_calibration.py` | 5 | 0 | 0 | 0 | 0 | 0 | 0 |
| `quality_gate.py` | 0 | 1 | 1 | 0 | 0 | 0 | 2 |
| `services/trained_prediction_engine.py` | 0 | 0 | 0 | 0 | 2 | 0 | 2 |
| `services/pvt_backtest.py` | 0 | 0 | 0 | 0 | 0 | 0 | 4 |
| `brain/sim_runner.py` | 2 | 0 | 0 | 1 | 1 | 0 | 0 |
| `brain/candidates.py` | 0 | 0 | 2 | 0 | 2 | 0 | 0 |
| `backtest.py` | 2 | 0 | 2 | 0 | 0 | 0 | 0 |
| `pick_enrichment.py` | 2 | 0 | 2 | 0 | 0 | 0 | 0 |
| `services/odds_provider.py` | 2 | 0 | 2 | 0 | 0 | 0 | 0 |

**Total files touched:** 91.

---

## 3. Read-time canonicalizer — the single biggest offender

**Location:** `server.py:255-450` (`_canonicalize_lock_score`)
**Called from:** every `/api/picks/*` and `/api/lab/*` response builder.

This function **mutates every pick document at read time** with three
overlapping repair paths:

1. **MAX-of-shadow-fields** (line 343):
   ```python
   canonical = max(v1, v2, raw, peak)   # of lock_score / lock_score_v2
                                        # / lock_score_raw / lock_score_peak
   ```
   Rationale in the code: "Multiple writers across the codebase
   (evidence_engine, validator, learning_v2, govern_pick, lazy
   governance, bandit, player_form) all touch lock_score
   independently."

2. **Always-starter read-time floor at 85** (lines 344–386):
   For soccer players in the `is_always_starter_soccer` whitelist
   (Kane, Mbappé, Haaland...), the canonicalizer floors `lock_score` at
   85 REGARDLESS of what the pipeline said.

3. **Coherence cap ceiling clamp** (lines 387–400):
   If `quality_gate` earlier stamped `coherence_cap_ceiling`, we cap
   `canonical = min(canonical, ceiling)` here.

**Verdict:** every one of these is a defensive band-aid for the fact
that ~30 different writers can independently mutate `lock_score`.  The
publication contract removes the underlying cause.  In Phase 1b this
function is replaced with a pure pass-through that returns
`published_lock_score` verbatim.

---

## 4. Current generation entrypoints (the "canonical tail")

Confirmed via code inspection: **there is exactly one canonical tail
today, but three side-injectors bypass it.**

### 4.1 Canonical tail
`server.py:_refresh_picks(date_str, sport_filter)` — line 1559.  Called
from:
- `server.startup_event`
- The hourly refresh loop
- The MLB pregame loop (`sport_filter="MLB"`)
- Admin endpoint `POST /api/admin/picks/force-refresh`
- The tomorrow-slate refresh path

Publication is now wired at line 2510+ in `_refresh_picks`, immediately
after `db.picks.insert_many(safe_picks)`.  See migration report §3.

### 4.2 Side-injectors that bypass the canonical tail
The following services write directly to `db.picks` **without** going
through `_refresh_picks`:

| Service | Cadence | Writes to |
|---|---|---|
| `services/mls_direct_inject.py` | 3×/day snapshot | `db.picks.bulk_write` |
| `services/soccer_prop_inject.py` | 3×/day snapshot | `db.picks.bulk_write` |
| `services/espn_soccer_fixtures.py` | ESPN refresh | `db.picks.bulk_write` |
| `services/signal_engine/engine.py` | signal rank cycle | `db.picks.bulk_write` — post-publication mutation of signal metadata |
| `services/signal_engine/rank.py` | rank cycle | `db.picks.bulk_write` — post-publication mutation |
| `brain/nrfi_engine.py:393` | NRFI generator | `db.picks.update_one(upsert=True)` |
| `_ensure_csl_elite_picks` (in server.py) | post-refresh | direct upsert |

**Phase 1b action:** each of these must also call
`PredictionPublicationService.publish()` at the tail of their write
path, OR write to a dedicated staging collection that then flows
through the canonical tail on the next refresh.

**Phase 1c action:** signal_engine writes must move to
`pick_enrichment` (side-car) — signal rank / percentile is presentation
data, never a published field.

---

## 5. All endpoint & downstream consumers

Comprehensive list of files that READ published fields:

**HTTP routes:**
- `/api/picks/today` → `routes/picks_routes.py:_home_feed` (multiple sort/filter reads)
- `/api/picks/{id}` → `routes/picks_routes.py:pick_detail`
- `/api/picks/hot` → `routes/picks_routes.py:hot_picks`
- `/api/picks/upset` → `routes/picks_routes.py:upset_picks`
- `/api/lab/*` → `lab_routes.py` (9 lock_score reads, 4 edge reads)
- `/api/admin/*` → `routes/admin_routes.py` (7 lock_score reads incl. pick evidence + alt-lines)
- `/api/analytics/*` → `routes/analytics_routes.py`
- `/api/market-competition/*` → `market_competition/routes.py`
- `/api/parlays/*` → parlay routes (in server.py)
- `/api/bet-slip/*` → bet-slip routes (in server.py)

**Internal consumers:**
- `services/prediction_fusion_engine.py` — reads `win_probability`, `confidence`
- `services/pick_matchup_wiring.py` — reads for matchup grade computation
- `services/pick_fusion_decorator.py` — reads for fusion enrichment
- `services/signal_engine/*` — reads for percentile ranks
- `services/lock_score_performance.py` — reads for bucket ROI
- `learning_engine.py` — reads for calibration input
- `learning_system_v2.py` — reads for v2 learning
- `bandit.py` — reads for arm assignment
- `analytics.py` — reads for slate analytics
- `settlement_engine.py`, `prop_settlement.py`, `soccer_espn_settle.py`, `kbo_settlement.py`, `tennis_extra/settle.py`, `brain/nrfi_engine.py` (settlement) — read grade + line + odds

---

## 6. Direct writes to `db.picks` — full inventory

Every `db.picks.update_*` / `replace_*` / `bulk_write` call outside of
the canonical insert:

| File:line | Operation | Purpose |
|---|---|---|
| `server.py:816` | `db.picks.bulk_write` | contradiction reconciler |
| `server.py:2494` | `db.picks.insert_many` | **canonical publication insert** |
| `server.py:2645` | `db.picks.update_many` | `_atomic_mark_no_bet` |
| `server.py:3056` | `db.picks.insert_many` | tomorrow-slate insert |
| `soccer/pipeline.py:213` | `update_one` | soccer pipeline write |
| `kbo_settlement.py:273` | `update_one` | KBO settlement result |
| `settlement_engine.py:347,476` | `update_many` / `update_one` | settlement result |
| `rollover_history_tagger.py:208,216` | `update_many` | rollover tagging |
| `grading_validator.py:297,319,330` | `update_one` (×3) | grade validation |
| `tennis_extra/settle.py:205` | `update_one` | tennis extra settlement |
| `stuck_pick_reaper.py:83` | `update_many` | stuck pick reaper |
| `ufc_espn_ingest.py:170` | `update_one` | UFC ingest |
| `prop_settlement.py:1346` | `update_one` | prop settlement |
| `mlb_lineup.py:156` | `update_one` | MLB lineup enrichment |
| `steam_detector.py:162` | `update_one` | steam detection tagging |
| `analytics.py:213` | `update_one` | analytics tagging |
| `soccer_hot_scorers.py:283` | `update_one` | hot scorer tagging |
| `services/espn_soccer_fixtures.py:323` | `bulk_write` | ESPN fixture write |
| `services/mls_direct_inject.py:514` | `bulk_write` | MLS direct inject |
| `services/signal_engine/engine.py:627` | `bulk_write` | signal rank write |
| `services/signal_engine/rank.py:367` | `bulk_write` | signal rank write |
| `services/soccer_prop_inject.py:445` | `bulk_write` | soccer prop inject |
| `brain/nrfi_engine.py:393,585` | `update_one` | NRFI pick + settlement |
| `soccer_espn_settle.py:860` | `update_one` | ESPN soccer settlement |

**Total: 25 files write to `db.picks`** post-generation.  This is the
complete Phase 1b/1c refactor surface.

---

## 7. Idempotency + concurrency design (proof of safety without transactions)

Since MongoDB is standalone, multi-doc transactions are not available.
The publication service is proven safe against retry and concurrency
using **only single-doc atomic ops**:

**Invariants:**
- `prediction_snapshots.(prediction_id, snapshot_version)` unique
- `prediction_snapshots.(prediction_id, idempotency_key)` unique
- `idempotency_key = SHA256(prediction_id | board_version |
  probability_6dp | lock_score_2dp | edge_3dp | line_4dp | odds)`

**Retry safety proof:**
1. Two concurrent `publish(candidate)` calls compute the same
   `idempotency_key`.
2. Both attempt `insert_one` on `prediction_snapshots`.
3. MongoDB's single-doc `insert` is atomic.  Exactly one succeeds; the
   other receives `E11000 DuplicateKeyError`.
4. The loser fetches the existing snapshot and returns it as
   `was_new=False`.
5. The `payload_hash` is compared; if it drifts (would indicate a bug
   in the idempotency-key formula), a WARN is logged.

**Failure recovery:**
- Snapshot insert succeeded, dual-write failed → snapshot is source of
  truth; the mismatch report will flag it; next refresh re-runs the
  dual-write idempotently.
- Both failed → caller retries; idempotency prevents duplicates.

**This is sufficient** for the Phase 1 requirement.  Transactions
would let us couple snapshot+dual-write into a single atomic unit, but
that coupling is *not* required by the contract — the snapshot is the
single source of truth, and the dual-write is a projection that will
eventually go away.

---

## 8. Dual-write mismatch results (first hour of live wiring)

- Snapshots created (manual test batch, 10 picks): **10**
- Mismatches logged: **10** (all 10 picks had drift between snapshot
  and legacy fields)
- Field-level drift breakdown (all 10 rows):
  - `win_probability`: legacy pick doc stored **percentages**
    (`62.1`), publication service normalised to a **fraction**
    (`0.621`).  This is a genuine data-quality bug in the current
    pipeline — see §9 below.
  - `lock_score`: legacy value differs from the value the publication
    service saw when it built the payload from the same pick doc.
    Cause: the pick doc had already been mutated in-place by the
    canonicalizer after read.

**Interpretation:** the dual-write is doing exactly what it's supposed
to — surfacing hidden drift.  Phase 1b will normalize the pipeline so
that `win_probability` is always a fraction and `lock_score` is stable
across reads.

---

## 9. Unrelated bugs discovered — deferred per Phase 1a rules

Following the "bug fixes only when directly part of mutation-removal"
rule, these are catalogued for post-Phase-1a follow-up:

| Severity | Bug | Evidence | Recommended follow-up |
|---|---|---|---|
| MEDIUM | `win_probability` field is inconsistently stored (fraction vs percentage) across services | 10/10 manual publications had drift here | Phase 1b: coerce `published_probability` to `[0, 1]` at publication; migrate stored values in Phase 1c |
| MEDIUM | 100% of existing picks lack ALL `*_version` metadata fields | Backfill dry-run showed 100% gap across all 7 version fields | Phase 1c: stamp `"legacy_unknown"` on all v0 backfill records; add version stamping to every pipeline stage in Phase 1b so v1+ has real values |
| LOW | `_canonicalize_lock_score` re-derives `probability` on every read via `unified_probability_report()` even for non-tennis picks | server.py:293 | Phase 1b: probability is a `published_*` field; remove read-time recompute |
| LOW | `_atomic_mark_no_bet` sets `status="blocked"` on `picks` even after publication | server.py:2620 | Phase 1c: move to `settlement_events` or `pick_status_events` |
| LOW | Multiple sport-specific settlement writers duplicate the same "mark won/lost/void" logic | 6 different settle modules | Phase 1c: centralise into `SettlementService` that writes only to `settlement_events` |

**None of these bugs are being fixed in Phase 1a** to keep the diff
reviewable.  All will be addressed in Phase 1b/1c per the plan.

---

## 10. Risks + decisions needed before Phase 1b

**Non-blocking:**
- Legacy `win_probability` values in `[0, 100]` vs `[0, 1]` inconsistency — Phase 1b will need a coercion pass.
- `_canonicalize_lock_score` removal will change endpoint response values for any pick where the shadow fields disagreed.  We already log every such drift into `publication_mismatch_report`; before Phase 1b flip we should snapshot the drift counts and get user sign-off on the delta.

**Potentially blocking — need user decision:**
- Three side-injectors (`mls_direct_inject`, `soccer_prop_inject`, `nrfi_engine`) currently bypass the canonical `_refresh_picks` path.  Options: (a) route them through the same publication service inline; (b) migrate them to write into a `pending_publication` collection that the canonical tail drains.  User decision requested for Phase 1b.
- Settlement is currently a mutation on `picks`.  Moving it to `settlement_events` is a **breaking change** for any consumer that reads `pick.status`.  Frontend impact must be reviewed before Phase 1c.
