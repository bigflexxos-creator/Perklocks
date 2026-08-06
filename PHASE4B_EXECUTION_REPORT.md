# Phase 4B — Execution Report

**Status:** COMPLETE. Simulator truthfulness + reproducibility + calibration
segmentation foundation shipped. **No sport-specific feature models
were modified.** Phase 4C has NOT started.

**Companion docs:**
- `/app/PHASE4B_CALIBRATION_BASELINE.md` — live baseline metrics
- `/app/PHASE4B_SIMULATOR_BASELINE.json` — machine-readable segmented baseline
- Phase 4A audit docs (`/app/PHASE4_*.md`) — unchanged

---

## 1. Deliverables

### 1.1 Files created (9)

| File | Purpose |
|---|---|
| `backend/brain/simulator_contract.py` | Typed `SimulatorResult` dataclass; guardrail against illegal `simulator_type` + posterior-sampler-claiming-independence. |
| `backend/services/simulation_seed.py` | Deterministic BLAKE2b-based seed helper (`build_seed`, `SeedError`, `describe_seed_inputs`). |
| `backend/services/calibration_segmentation.py` | Segmentation policy (BucketKey, hierarchy, odds/line bands, min-sample gates). |
| `backend/scripts/phase4b_calibration_baseline.py` | Read-only baseline report generator (0 writes). |
| `backend/tests/test_phase4b_simulator_and_calibration.py` | 29 guardrail + determinism tests. |
| `backend/tests/test_phase4b_sim_stability.py` | 7 end-to-end sim reproducibility tests. |
| `/app/PHASE4B_CALIBRATION_BASELINE.md` | Human-readable baseline. |
| `/app/PHASE4B_SIMULATOR_BASELINE.json` | Machine-readable segmented baseline (12 092 picks scored). |
| `/app/PHASE4B_EXECUTION_REPORT.md` | This file. |

### 1.2 Files changed (3)

| File | Delta |
|---|---|
| `backend/brain/simulator.py` | Rewritten. Rebrand: `run_simulator` → `run_posterior_uncertainty` + backward-compat wrapper. Deterministic per-pick seed. Truthful metadata (`method="beta_bernoulli_posterior"`, `simulator_type="posterior_uncertainty"`, `independent_evidence=False`). Adds credible-interval outputs (`lower_bound`, `upper_bound`, `uncertainty_width`, `standard_error`, `effective_sample_size`, `input_probability`, `posterior_mean`, `seed`, `simulator_version`, typed `contract`). Preserves legacy keys (`win_probability`, `expected_value`, `variance`, `agreement_score`) so no schema breaks. |
| `backend/brain/filter.py` | `sim_ev<0` and `sim_var_high` gates now fire ONLY when `simulator.independent_evidence` is True. New soft gate `posterior_uncertainty_wide` (uncertainty_width > 0.35) allows fragility flagging without blocking. |
| `backend/brain/sim_runner.py` | (a) `_anchor_pick_to_sim` rewritten as SYMMETRIC BOUNDED-RESIDUAL — `SIM_RESIDUAL_MAX = 3.0` pp in both directions, elite floor 95 preserved. (b) `simulate_pick` now injects a deterministic per-pick seed into the global `random` and stamps truthful metadata (`simulator_name`/`_version`/`_type`/`seed`/`independent_evidence=True`/`valid=True`). (c) `apply_simulations` respects `sim_meta` — refuses to anchor if `independent_evidence=False` or `valid=False`. |

**Total: 9 new files, 3 modified files. Zero frontend changes. Zero production writes.**

---

## 2. Baseline calibration report

Ran read-only baseline against **12 092 settled picks** from the live
`picks` collection in **0.5 seconds**. Segmented by:

- **9 axes:** by_sport, by_sport_market, by_sport_market_side,
  by_sport_market_side_line, by_sport_market_side_odds, by_main_alt,
  by_lock_band, by_magic_band, by_sim_used.
- **Metrics per bucket:** n, W/L/P/V, hit_rate, avg_pred_prob,
  avg_odds, Brier, log_loss, ROI%, units, CLV mean, calibration_gap.
- **Min-sample gate:** 30 (buckets below are labelled `INSUFFICIENT_SAMPLE`).

Artefacts stored at `/app/PHASE4B_CALIBRATION_BASELINE.md` (human) and
`/app/PHASE4B_SIMULATOR_BASELINE.json` (machine-readable, ~470 KB).

**Assertion:** `test_baseline_script_makes_zero_production_writes`
statically scans the script for `insert_one`, `update_one`, `delete_one`,
`drop(`, etc. — all absent. **Guardrail passes.**

---

## 3. Posterior-uncertainty redesign

**Before Phase 4B:**
- Module `brain/simulator.py` labelled "Hidden Monte Carlo Simulator".
- Emitted `win_probability` / `expected_value` / `variance` /
  `agreement_score` — consumed by `brain/filter.py` as an "independent
  Monte Carlo evidence" signal.
- Uses unseeded `_RNG = random.Random()` at module load → non-reproducible.
- No self-labelling of independence status.

**After Phase 4B:**
- Docstring rewritten with a top-of-file ⚠️ RECLASSIFIED notice.
- Public API: `run_posterior_uncertainty(picks, memory)`. Legacy
  `run_simulator` remains as a thin wrapper for backward compat.
- Every output row carries:
  - `method = "beta_bernoulli_posterior"`
  - `simulator_type = "posterior_uncertainty"`
  - `simulator_name = "posterior_uncertainty"`, `simulator_version = "2.0.0"`
  - `independent_evidence = False` — enforced by `SimulatorResult.__post_init__`
  - `input_probability`, `posterior_mean`, `lower_bound`, `upper_bound`,
    `uncertainty_width`, `standard_error`, `effective_sample_size`, `seed`
  - Typed `contract` sub-dict (from `SimulatorResult.to_dict()`).
- Deterministic BLAKE2b seed from
  `services.simulation_seed.build_seed(pick, "posterior_uncertainty", "2.0.0")`.
- Legacy keys (`win_probability`, `expected_value`, `variance`,
  `agreement_score`, `prior_alpha`, `prior_beta`, `n_samples`) kept so
  downstream analytics that read them still work.

---

## 4. Independent-evidence caller changes

`brain/filter.py` audit:

| Old gate | Behaviour | Phase 4B behaviour |
|---|---|---|
| `sim.expected_value < MIN_EV → PASS` | Applied to every top-K pick | Applied ONLY when `sim.independent_evidence=True`. Since the current in-use sampler is posterior, this gate is **effectively dormant**. |
| `sim.variance > MAX_VARIANCE → PASS` | Same | Same (dormant for posterior). |
| `sim.agreement_score < MIN_AGREEMENT → PASS` | Same | Preserved (factor-variance fragility, not model vote). |
| — | — | **NEW:** `posterior_uncertainty_wide` — soft flag when `uncertainty_width > 0.35`. Contributes to `pass_reasons` but does not itself set `no_bet` (V2 LIVE mode). |

Callers of `brain.simulator.run_simulator` — **only `brain/pipeline.py`**.
No mutation there: `pipeline.py` still calls `run_simulator(picks, memory)`
which now delegates to `run_posterior_uncertainty` and stamps truthful
labels. The pipeline summary now reports `simulator_type` and
`independent_evidence=False`.

`services/prediction_fusion_engine.py::_run_simulator_component` (line 380)
does NOT feed `brain.simulator`. It runs its own sport-specific
simulator via `sim_runner.simulate_pick` — which is the SPORT
simulator, correctly marked `independent_evidence=True` by the new
`simulate_pick` wrapper.

No other production caller treats `brain.simulator` output as
independent evidence.

---

## 5. Deterministic seed design

`services/simulation_seed.py`:
- **BLAKE2b-8byte truncated to 63 bits** (fits `random.Random(seed)`).
- **NO** `hash()` (Python's process-randomised hash).
- **NO** display-name-only seed unless `allow_name_only_fallback=True`.
- Seed inputs (in order): `prediction_id`, `event_id`,
  `market_key`, `participant_id`, `side`, `line`, `simulator_name`,
  `simulator_version`.
- Same pick + same simulator version → same seed. Test guarantees:
  - `test_seed_same_pick_same_version_same_seed`
  - `test_seed_different_line_different_seed`
  - `test_seed_different_player_different_seed`
  - `test_seed_different_version_different_seed`
  - `test_seed_refuses_name_only_by_default`
  - `test_seed_no_python_hash_used` — asserts against a hard-coded
    expected BLAKE2b digest for a known input.

Wired into:
- `brain/simulator.py::_posterior_one` — seed per pick before Beta sampling.
- `brain/sim_runner.py::simulate_pick` — seeds global `random.seed(seed)`
  before EACH sport-specific sim (sequential execution, no cross-pick
  contamination).

**Not modified** (still unseeded — DEFERRED per user directive "no sport-
specific feature model changes"): `brain/sim_mlb.py`, `sim_nba.py`,
`sim_tennis.py`, `sim_soccer.py`, `sim_soccer_scorer.py`. However their
outputs are now DETERMINISTIC because the caller (`simulate_pick`) seeds
the global RNG before invoking each sim. **Live verification confirmed
identical output across two runs of identical inputs** — see §11.

---

## 6. Symmetric anchor design

`brain/sim_runner._anchor_pick_to_sim` — full rewrite:

- **Bounds:** `applied_delta = clip(baseline − prior_lock, −SIM_RESIDUAL_MAX, +SIM_RESIDUAL_MAX)` where `SIM_RESIDUAL_MAX = 3.0` pp.
- **Elite floor:** If `pick.elite_player=True` or `prior_lock ≥ 95`,
  `new_lock` is floored at `max(95.0, prior_lock)`.
- **Independence gate:** `sim_meta.independent_evidence=False` → zero
  adjustment, records `sim_anchor_skip_reason="posterior_uncertainty_not_independent"`.
- **Validity gate:** `sim_meta.valid=False` → zero adjustment, records
  `sim_anchor_skip_reason="sim_invalid"`.
- **Audit fields** (always stamped):
  - `sim_lock_anchor` — the raw baseline (or None if skipped)
  - `sim_lock_prior` — pre-anchor lock
  - `sim_lock_residual` — baseline − prior (raw)
  - `sim_lock_applied_delta` — clamped adjustment
  - `lock_anchored_to_sim` — bool

Default `sim_meta=None` treats the sim as `independent=True, valid=True`
so pre-Phase-4B untyped sport simulators still work.

---

## 7. Simulator metadata contract

`brain/simulator_contract.py` — `SimulatorResult` dataclass with 20
fields (see file header). Enforces at construction:

- `simulator_type` MUST be one of the six allowed types.
- `posterior_uncertainty` MUST have `independent_evidence=False`.

Every simulator publishes:
- `simulator_name`, `simulator_version`, `simulator_type`, `seed`,
  `iterations`, `input_line`, `input_side`, `raw_probability`,
  `stabilized_probability`, `standard_error`, `lower_bound`,
  `upper_bound`, `push_probability`, `valid`, `invalid_reason`,
  `independent_evidence`, `duration_ms`, `method`, `extras`.

Sport-specific sims populate the contract via `simulate_pick` wrapping:

| Sport | `simulator_type` | `simulator_version` |
|---|---|---|
| MLB | `distribution_monte_carlo` | `1.1.0` |
| NBA | `distribution_monte_carlo` | `1.1.0` |
| Soccer | `distribution_monte_carlo` | `1.1.0` |
| Tennis | `event_simulation` | `1.1.0` |
| Posterior (all sports, top-K) | `posterior_uncertainty` | `2.0.0` |

---

## 8. Stability-test results

`tests/test_phase4b_sim_stability.py` — 7 tests, all pass.

| Test | Assertion |
|---|---|
| `test_mlb_apply_simulations_reproducible` | Same pick → same `sim_win_probability`, `sim_ci_lower`, `sim_ci_upper`. |
| `test_mlb_different_line_different_seed` | Different lines → different seeds and different sim outputs. |
| `test_apply_simulations_stamps_simulator_metadata` | Every simulated pick carries `simulator_name`, `_version`, `_type`, `seed`, `independent_evidence`, `valid`. |
| `test_apply_simulations_records_anchor_audit_fields` | `sim_lock_prior`, `sim_lock_applied_delta`, `sim_lock_residual` always stamped. |
| `test_apply_simulations_bounded_delta` | `|applied_delta| ≤ SIM_RESIDUAL_MAX`. |
| `test_legacy_run_simulator_still_importable` | Backward-compat wrapper works. |
| `test_apply_simulations_returns_correct_counts_shape` | Counts dict includes `skipped_not_independent`, `skipped_invalid`. |

Total Phase 4B tests: **36 passing (29 + 7)**.

---

## 9. Calibration segmentation foundation

`services/calibration_segmentation.py` ships:

- **`BucketKey`** — 6-field immutable identity (sport, market_family,
  side, line_band, odds_band, main_or_alt).
- **`hierarchy(key)`** — 6-level fallback L1→L6 (most specific to global).
- **`DEFAULT_MIN_SAMPLE`** — `L1=200, L2=100, L3=60, L4=40, L5=30, L6=0`.
- **`classify_odds_band`** — 8 bands (deep_chalk → deep_dog).
- **`classify_line_band`** — half-lines + integers + negative lines.
- **`SegmentedCalibrator`** — dataclass CONTRACT (knots, out-of-sample
  Brier / log-loss / hit-rate, promotion gate).

**Not shipped in Phase 4B (deferred to a later sub-phase per user
directive):** the isotonic fitting logic + runtime resolver. The
segmentation POLICY is now defined; the FITTING will happen after
Phase 4B is reviewed. Baseline report ships now to inform that fit.

---

## 10. Before/after fixture comparison

Live verification via `apply_simulations`:

| Scenario | Prior lock | sim_wp% | Old (lift-only) | Phase 4B (symmetric ±3) |
|---|---|---|---|---|
| Engine understated | 70.0 | 90 → baseline 96 | 70 → 96 (lift +26) | 70 → **73** (bounded +3) |
| Engine overstated | 90.0 | 55 → baseline 65 | 90 → 90 (no demote) | 90 → **87** (bounded −3) |
| Posterior sampler input | 70.0 | 95 → baseline 99 | 70 → 99 (lift +29) | 70 → **70** (skipped, `posterior_uncertainty_not_independent`) |
| Invalid sim | 70.0 | 95 | 70 → 99 (lift +29) | 70 → **70** (skipped, `sim_invalid`) |
| Elite floor | 96.0 | 45 → baseline 57 | 96 → 96 (no demote) | 96 → **96** (elite floor preserved) |

---

## 11. Live runtime verification

```
$ sudo supervisorctl restart backend
backend: stopped
backend: started
$ curl -s http://localhost:8001/api/health
{"status":"ok","ts":"2026-08-06T20:10:56.147727+00:00"}
```

Startup log confirms: calibration curve loaded (6304 samples, 8 knots),
signal-rank refreshed (172 picks), all scheduled jobs armed, no import
errors, no exception on brain.pipeline.

Twin-run posterior determinism (identical seed=6085645822415890008,
identical posterior_mean=0.5975, identical uncertainty_width=0.2665,
identical bounds):
```
=== Posterior determinism ===
seed A = 6085645822415890008
seed B = 6085645822415890008
posterior_mean A = 0.5975, B = 0.5975
uncertainty_width A = 0.2665, B = 0.2665
independent_evidence = False  (must be False)
method = beta_bernoulli_posterior
simulator_type = posterior_uncertainty
```

Symmetric anchor verified in both directions with bounded ±3.0 clamp;
both refusal paths (posterior + invalid) fire correctly.

---

## 12. Test commands and results

```
cd /app/backend
python -m pytest tests/test_phase4b_simulator_and_calibration.py \
                 tests/test_phase4b_sim_stability.py \
                 tests/test_iter131_user_bet_ledger.py \
                 tests/test_iter132_user_bets_schema_extension.py \
                 tests/test_iter133_legacy_parlay_backfill.py \
                 tests/test_iter134_legacy_parlay_execute.py \
                 tests/test_iter135_writer_cutover.py \
                 tests/test_iter136_reader_settlement_cutover.py --tb=short -q
```

Result: **183 passed, 1 warning in 35.8s.**

Adjacent-suite regression check (`test_brain.py`, `test_sim_engine.py`,
`test_sim_engine_session2.py`, `test_sim_phase_a.py`, `test_sim_phase_b.py`)
against pre-Phase-4B `git stash` baseline:

| Suite | Before Phase 4B | After Phase 4B |
|---|---|---|
| `test_brain.py::test_brain_pipeline_smoke` | FAIL (`WARN` verdict, pre-existing V2 LIVE mode) | FAIL — **same** |
| `test_sim_phase_a.py` — 1 fail, 3 err | Same | Same |
| `test_sim_phase_b.py` — 5 fail, 3 err | Same | Same |
| Others in these suites | 16 passed | 39 passed (test-collection order effects lifted more collection) |

**No new regressions introduced by Phase 4B.**

Phase 3G suite (147 tests): **all pass unchanged.**

---

## 13. Runtime verification checklist

- ✅ Backend restart clean.
- ✅ `GET /api/health` returns 200.
- ✅ Startup log clean (all schedulers armed).
- ✅ Twin-run posterior determinism verified (identical seed + output).
- ✅ Simulator metadata stamped on the pick (verified via direct call).
- ✅ Symmetric anchor bounded ±3.0 in both directions (verified).
- ✅ Posterior sampler skipped by anchor (verified).
- ✅ Invalid sim skipped by anchor (verified).
- ✅ Frontend response schemas unchanged (posterior fields ADDED to
  `pick.brain.simulator`, no keys removed).
- ✅ Baseline artefacts exist and are non-empty.

---

## 14. Remaining Phase 4C blockers

Phase 4C is MLB models + H+R+RBI corrections. No Phase 4B item blocks
Phase 4C. The Phase 4B foundation is READY to consume in Phase 4C:

- **Calibration baseline** → will inform which MLB market families
  need per-line/per-odds segmentation first.
- **Deterministic seeds** → any Phase 4C MLB simulator changes can
  now be regression-tested against pinned seeds.
- **Symmetric anchor** → any newly-corrected MLB model outputs will
  no longer be double-counted by the sim (bounded ±3 pp cap).
- **Simulator contract** → Phase 4C MLB engine updates must return a
  `SimulatorResult`-compatible dict to be honoured by the anchor.

**No blockers.**

---

## 15. Suggested Git commit message

```
Phase 4B — Simulator truthfulness + reproducibility + calibration
baseline. No sport-specific feature model changes.

Simulator layer:
  • Rebrand brain/simulator.py: run_simulator → run_posterior_uncertainty.
    Beta-Bernoulli sampler now truthfully labels itself with
    method="beta_bernoulli_posterior", simulator_type="posterior_uncertainty",
    and independent_evidence=False (enforced by contract dataclass).
    Legacy run_simulator kept as wrapper for backward compat; legacy
    output keys retained so downstream analytics unchanged.
  • Deterministic BLAKE2b seed helper (services/simulation_seed.py).
    Same pick + simulator_version → same seed. Different line, event,
    or participant → different seed. No Python hash() used.
  • sim_runner._anchor_pick_to_sim rewritten as SYMMETRIC BOUNDED-
    RESIDUAL. SIM_RESIDUAL_MAX = 3.0 pp — sim can now DEMOTE as well
    as lift. Elite-floor 95 preserved. Skips when sim is posterior
    or invalid.
  • simulate_pick stamps truthful metadata (simulator_type / version /
    seed / independent_evidence / valid) on every sport sim result.

Calibration layer (foundation only):
  • services/calibration_segmentation.py — BucketKey, hierarchy L1-L6,
    per-level min-sample gates, odds/line-band classifiers, typed
    SegmentedCalibrator contract. Fitting logic deferred.
  • scripts/phase4b_calibration_baseline.py — read-only reporter
    (0 writes). Scored 12 092 settled picks in 0.5s. Segments by 9
    axes; buckets below min-sample flagged INSUFFICIENT.

Filter layer:
  • brain/filter.py — sim EV / variance gates now fire ONLY when
    simulator.independent_evidence=True. Posterior sampler no longer
    creates artificial model agreement. Wide uncertainty caps
    confidence (soft), never raises it.

Contract layer:
  • brain/simulator_contract.py — SimulatorResult dataclass with 20
    fields. __post_init__ rejects illegal simulator_type and enforces
    independent_evidence=False on posterior_uncertainty.

Tests:
  • tests/test_phase4b_simulator_and_calibration.py — 29 guardrails.
  • tests/test_phase4b_sim_stability.py — 7 end-to-end determinism.

Reports:
  • /app/PHASE4B_CALIBRATION_BASELINE.md — human-readable.
  • /app/PHASE4B_SIMULATOR_BASELINE.json — machine-readable.
  • /app/PHASE4B_EXECUTION_REPORT.md — this deliverable.

All Phase 3G + Phase 4B tests pass (183 total). Adjacent test suites
retain their pre-Phase-4B pass/fail pattern (no new regressions).
Frontend response schemas unchanged. Backend runtime verified.
```

---

## 16. Rollback instructions

Phase 4B is **code-only + docs**. No data migration, no index changes,
no schema mutations.

Full rollback:

```bash
cd /app
git checkout backend/brain/simulator.py
git checkout backend/brain/sim_runner.py
git checkout backend/brain/filter.py
rm backend/brain/simulator_contract.py
rm backend/services/simulation_seed.py
rm backend/services/calibration_segmentation.py
rm backend/scripts/phase4b_calibration_baseline.py
rm backend/tests/test_phase4b_simulator_and_calibration.py
rm backend/tests/test_phase4b_sim_stability.py
rm PHASE4B_CALIBRATION_BASELINE.md
rm PHASE4B_SIMULATOR_BASELINE.json
rm PHASE4B_EXECUTION_REPORT.md
sudo supervisorctl restart backend
```

Post-rollback the app returns to the pre-Phase-4B lift-only anchor
and posterior-sampler-as-Monte-Carlo behaviour. Zero data cleanup
required.

---

**Phase 4B is COMPLETE. Awaiting user review before beginning Phase 4C.**
