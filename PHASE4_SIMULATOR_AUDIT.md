# PHASE 4A — SIMULATOR AUDIT

**Status:** Read-only audit.
**Files inspected:** `brain/simulator.py`, `brain/sim_runner.py`, `brain/sim_mlb.py`, `brain/sim_nba.py`, `brain/sim_tennis.py`, `brain/sim_soccer.py`, `brain/sim_soccer_scorer.py`, `brain/sim_distribution.py`, `brain/sim_backtest.py`, `brain/nrfi_engine.py` (docstring only).

---

## 1. Classification table

| Simulator | Sport | Classification | Where wired |
|---|---|---|---|
| `brain/simulator.py::run_simulator` | ALL (top-K flagged picks) | **⚠️ MISLABELLED SIMULATOR** — Beta-Bernoulli sampler seeded from the pick's OWN `confidence_calibrated`; not an independent event simulator. | `brain/pipeline.py:74` — runs during refresh on TOP_K picks. Outputs stored under `brain.simulator`. Docstring explicitly says *"The simulator NEVER surfaces in the UI. Its outputs feed the Decision Filter."* |
| `brain/sim_mlb.py::simulate_mlb_pick` | MLB | **Distribution-based Monte Carlo** (per-AB Bernoulli). | `brain/sim_runner.apply_simulations` → `_anchor_pick_to_sim`. Runs when a pick has `sport=MLB` and player_stats available. |
| `brain/sim_nba.py::simulate_nba_pick` | NBA | **Distribution-based Monte Carlo** (Poisson counts + normal noise). | `brain/sim_runner`. |
| `brain/sim_tennis.py::simulate_tennis_pick` | Tennis | **True event simulation** — point-by-point via serve probability → game → set → match. | `brain/sim_runner`. |
| `brain/sim_soccer.py::simulate_soccer_pick` | Soccer (game markets) | **Distribution-based Monte Carlo** (dual-Poisson goal model). | `brain/sim_runner`. |
| `brain/sim_soccer_scorer.py::simulate_soccer_scorer_pick` | Soccer (scorer markets) | **Distribution-based Monte Carlo** (per-minute expected xG Poisson). | Called by `sim_soccer.py::simulate_soccer_pick` when the pick market matches a scorer market. |
| `brain/nrfi_engine.py` | MLB NRFI/YRFI | **Independent model** — inspected by docstring only (639 LOC). Full audit deferred to Phase 4B if wired to emission. | Publication path not confirmed in this audit pass. |
| `brain/sim_distribution.py::compute_percentiles` | ALL | **Utility** (Not a sim) — five-number summary of a sample distribution. | Called from `sim_mlb` output. |
| `brain/sim_backtest.py` | ALL | **Backtest harness** — replays historical picks. | Admin/on-demand — verified not on the daily emission path. |

---

## 2. `brain/simulator.py::run_simulator` — MISLABELLED (P0)

**Code:** `/app/backend/brain/simulator.py` — 134 LOC total.

**Behaviour:**
```python
mu = float(brain.get("confidence_calibrated") or (pick["win_probability"] / 100))
alpha = μ·strength + 1;  β = (1-μ)·strength + 1
for _ in range(N_SAMPLES):
    p = _beta_sample(α, β)
    outcome = 1 if _RNG.random() < p else 0
```

- `mu` = the pick's OWN calibrated confidence.
- The Beta prior is centred on `mu`.
- `_RNG.random() < p` samples a Bernoulli from a Beta centred on `mu`.
- The output `win_probability` converges (n=1500) to `mu`.

**Consequence:** the sim ALWAYS agrees with the model by construction. Its `agreement_score` is factor stdev (line 53-60), NOT independent evidence. The `expected_value` is monotonic in `mu` + `book_odds`.

**Not just cosmetic:** the sim feeds the Decision Filter (`brain/filter.py`) as an INDEPENDENT signal, which promotes false confidence.

**Additional defects:**
- **`_RNG = random.Random()`** at module load (line 35) — NO SEED. Every run produces different output for identical inputs.
- No physical event structure. No sport-specific dynamics.
- No push handling.

**Recommendation (Phase 4B):**
- Rename to `posterior_uncertainty` to reflect what it actually computes.
- Do NOT feed as independent evidence to the Decision Filter — instead surface as a variance/CI bracket only.
- Fix reproducibility (seeded RNG per pick).

---

## 3. `brain/sim_mlb.py` — DISTRIBUTION-BASED MC

**Runs:** 20 000 per pick.
**Distribution family per market:**
- Hits — Bernoulli(BA) over `EXPECTED_ABS_HITTER = 4.2` ABs.
- HR — Bernoulli(HR_per_AB) over 4.2 ABs.
- H+R+RBI — Compound: Bernoulli(BA) + Bernoulli(run_p=BA×0.45) + Bernoulli(rbi_p) + Bernoulli(HR×0.4) per AB.
- Pitcher K — Bernoulli(K_rate) over `EXPECTED_BF_PITCHER = 22.0`.
- Pitcher Outs — Bernoulli(p_out=0.680, league-avg fixed) over `expected_innings × bf_per_inning × 1.2`.

**Reproducibility:** ❌ — uses `random.random()` from the **global** module (`import random` at line 19). Same input, different output every run.

**Stability with sufficient iterations:** ✅ at N=20 000 the Wilson CI width is ~0.5-1.0pp — acceptable.

**Push handling:** ❌ — the win check is `x < threshold` (under) or `x > threshold` (over). Strict inequality; **the equality case (x == threshold on an integer-line prop) counts as a LOSS on the over side, not a push.** For a 1.5-Hits line this doesn't apply (integer outcomes vs 0.5 line), but for **integer-line H+R+RBI (Over 2.5) hit exactly 2 → correctly a loss**. However for `pitcher_outs` with an integer half line (e.g. Over 17.5 outs), a value of 17 = under, 18 = over — no ambiguity. **Push handling is by convention correct for 0.5/1.5/2.5 lines but incorrect if any pick reaches sim_mlb with a whole-number line.**

**Sport-specific assumptions:**
- Hits: `p = max(0.05, min(0.55, batter_ba))`.
- HR: `p = max(0.001, min(0.15, hr_per_ab))`.
- H+R+RBI: **hardcoded `run_p = BA×0.45`** — no lineup-slot / team-scoring adjustment. **Structural weakness** for a batter deep in a low-scoring lineup.
- H+R+RBI: `random.random() < hr * 0.4` extra HR bump — likely **partial double-count** of HR (ba already includes HR).
- Pitcher outs: fixed `p_out = 0.680` (league OBP inverse) — no pitcher-specific K/BB/hits-allowed.

**Alt-line sensitivity table** (`sim_alt_lines`): +/-0.5/1.0/1.5 around threshold — this is a useful signal but **currently is NOT surfaced in the frontend response** per static reading. Verify in Phase 4B.

**Improves calibration / Brier / log-loss / ROI / CLV?**
- No live A/B or backtest evidence in the code confirms these claims. `brain/sim_backtest.py` exists but its output is not visible in this audit. **Unverified.**

**Actually used in published picks?**
- Yes — `apply_simulations` runs on every pick, and `_anchor_pick_to_sim` LIFTS `lock_score` to `sim_wp_to_lock_baseline(sim_wp)` if the baseline exceeds the prior lock.
- BUT the sim can ONLY LIFT (never demote). Weakness → false-confidence risk.

---

## 4. `brain/sim_nba.py` — POISSON MC

**Runs:** 20 000 per pick.
**Distribution family:** Poisson-Normal blend on `_calibrate_lambda(line, target_over_prob)` — attempts to fit the Poisson λ so that the sim reproduces the book-implied over prob at the actual line.

**Critical observation:** the calibration function makes the sim's `sim_win_probability` a monotonic function of `target_over_prob`, which is derived from `book_implied`. **This means the NBA sim's output tracks book_implied by construction** (with small factor adjustments via `_factor_adjustment`).

**Factor adjustments:** `_factor(pick, key, default=50.0)` reads from `pick["factors"]` — for NBA, that dict is `{"Book Implied Probability": mp}` (see Model Audit §2.4). So `_factor_adjustment` returns the default 50.0 → **zero net factor adjustment** → the sim is 100 % book-follow for NBA picks emitted today.

**Push handling:** Similar to MLB — half-line boundaries safe, integer lines not.

**Reproducibility:** ❌ — global `random`, no seed.

**Improves calibration / Brier / etc?** For NBA picks that emit from the book-follow path, the sim ADDS NOTHING beyond the book. Its output correlates 1:1 with input.

---

## 5. `brain/sim_tennis.py` — TRUE EVENT SIMULATION ✅

**Runs:** 20 000.
**Mechanism:** point-level serve probability → game → set → match. Follows `_simulate_game(server_pt_win)`, `_simulate_tiebreak`, `_simulate_set(p_serve, o_serve, pick_serves_first)`, `_simulate_match(p_serve, o_serve, bo=3)`. Includes bo=3 vs bo=5 via `SETS_BO3` / `SETS_BO5`.

**Calibration:** `_calibrate_serve_gap_for_spread(spread_line, target_cover_pct)` and `_calibrate_serve_gap(target_match_wp)` — solves for the serve-gap that reproduces the book-implied cover/match win prob at the sim's structural level. This is a **calibrated event simulator** — the serve percentages are back-solved from book prices.

**Consequence:** even though the physics engine is real, the INPUT (serve gap) is back-solved from book_implied. So the sim's output for a moneyline pick will match book_implied by construction. Where the sim provides value is on **spread and total games** (structural inference derived from the calibrated serves).

**Push handling:** implicit through integer game counts and half-lines.
**Reproducibility:** ❌ — global `random`, no seed.
**Sport-specific assumptions:** BO3 default; BO5 auto-detected via `_extract_threshold` heuristic on the market string. May misclassify Grand Slams if the market label doesn't say "Best of 5".

---

## 6. `brain/sim_soccer.py` + `brain/sim_soccer_scorer.py` — DUAL-POISSON MC

**`sim_soccer.py`:**
- Dual-Poisson goal model — home λ, away λ.
- `_derive_lambdas(pick)` extracts λ from pick's factors (Book Implied is the primary source for non-scorer, book-follow markets).
- Handles 1X2, BTTS, totals.

**`sim_soccer_scorer.py`:**
- Per-minute xG Poisson.
- For scorer markets, called via `simulate_soccer_pick`.

**Reproducibility:** ❌.
**Push handling:** Soccer total-goals main lines are integer (2.5) or half-lines — no push ambiguity.
**BTTS:** correctly handled via Poisson intersection.

**Fires on live emission today?** Yes for soccer game & scorer picks.

---

## 7. Global simulator concerns

### 7.1 Reproducibility failure across ALL simulators

Every simulator uses either the global `random` module or `random.Random()` without a seed. Two consequences:
1. Regression tests cannot pin sim output.
2. Two refreshes for the same pick produce different `sim_win_probability` — potentially different lock anchor → potentially different published lock.

**Impact:** ⚠️ mid-high. Not user-visible (0.5-1.0 pp shifts) but corrupts test isolation and slate-to-slate reproducibility.

### 7.2 One-way sim anchor

`sim_runner._anchor_pick_to_sim` LIFTS lock UP if `sim_baseline > prior_lock`, but leaves lock UNCHANGED if `sim < prior`. Justified by user intent ("Elite players not dragged down by low sim"). But **prevents the sim from correcting engine over-confidence**.

**Recommendation:** allow sim to LIFT AND DEMOTE within a `SIM_RESIDUAL_MAX = 3.0` band symmetrically, keeping elite floor protection.

### 7.3 Ensemble illusion

`brain/simulator.py::run_simulator` executes AFTER the sport-specific sims in `sim_runner.apply_simulations`. Its Beta-Bernoulli output is written to `pick["brain"]["simulator"]`, and its `agreement_score` field mimics an independent uncertainty signal. **A user or downstream aggregator reading `brain.simulator` alongside `sim_win_probability` will over-count evidence.**

### 7.4 Book-follow simulators for book-follow sports

For NBA / CFB / non-scorer Soccer / Tennis composite / UFC — the sim's inputs are book-implied. So the sim adds **nothing beyond book_implied variance**. The user prompt calls this out explicitly: *"Do not allow a heuristic stress test to be presented as independent Monte Carlo evidence."* — NBA sim in particular meets this criterion.

### 7.5 H+R+RBI simulator specific concerns

`_simulate_hrr` (sim_mlb.py:78) has two structural weaknesses:
1. `run_p = ba * 0.45` — hardcoded coefficient with no lineup context.
2. `random.random() < hr * 0.4` — partial extra HR contribution that likely double-counts (since ba includes HR already).

For a heavy hitter (BA .300, HR/AB .05) the sim will slightly over-predict H+R+RBI Over 1.5 vs the true rate.

### 7.6 CI reporting

Every sim exposes Wilson CI (`sim_ci_lower`, `sim_ci_upper`) — good UX signal. But no downstream consumer uses the CI as a rejection gate. A pick with `sim_wp=90%` but CI `[65%, 95%]` should be down-weighted.

---

## 8. Verdicts

- ✅ **TRUE simulators:** `sim_tennis`, `sim_soccer`/`sim_soccer_scorer` (for scorer markets), `sim_mlb` (structurally, though reproducibility fails).
- ⚠️ **BOOK-FOLLOW simulators (mislabelled as independent evidence):** `sim_nba` (feeds NBA picks that are already book-follow — no signal beyond book).
- ❌ **MISLABELLED simulators:** `brain/simulator.py::run_simulator` — Beta-Bernoulli around the model's own confidence, presented as independent evidence.

---

## 9. Defects raised (Simulator layer)

| ID | Description | Impact | Likelihood |
|---|---|---|---|
| S-1 | `brain/simulator.py::run_simulator` is a mislabelled Beta-Bernoulli sampler; presented as independent evidence but tracks the model by construction. | 🔴 High | Certain |
| S-2 | All simulators use unseeded global `random` → not reproducible across refreshes. | 🟡 Medium | Certain |
| S-3 | `sim_runner._anchor_pick_to_sim` is asymmetric (lift-only) — sim cannot correct engine over-confidence. | 🔴 High | Certain (structural) |
| S-4 | `sim_nba` calibrates λ to book_implied — adds no signal beyond book. | 🟡 Medium | Certain |
| S-5 | `sim_mlb._simulate_hrr` uses hardcoded `run_p = BA*0.45` and partial extra HR (likely double-counts). | 🟡 Medium | Certain |
| S-6 | Wilson CI reported per pick but never used as a rejection gate. | 🟡 Low | Certain |
| S-7 | Push handling assumes half-line markets — integer-line picks would be misgraded (may not fire today but latent). | 🟡 Low | Latent |
| S-8 | NBA sim reads `_factor(pick, key, default=50.0)` — for NBA picks the pick's factors dict has only `"Book Implied Probability"`, so factor adjustments are always zero. | 🟡 Medium | Certain |
