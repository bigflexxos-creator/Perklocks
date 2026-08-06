# PHASE 4A — CALIBRATION BASELINE

**Status:** Read-only audit. Baseline before any Phase 4B changes.
**Ground truth:** `lock_calibration.py`, `learning_engine.py`, `learning_system_v2.py`, `learning_buckets.py`, `services/lock_score_performance.py`, `services/backtest_framework.py`, `services/pvt_backtest.py`, `services/prediction_fusion_engine.py`.

---

## 1. Calibration architecture as it stands

### 1.1 Isotonic-regression lock calibration

`backend/lock_calibration.py` fits an isotonic (Pool Adjacent Violators, no sklearn) curve mapping:
```
raw_lock_score  →  calibrated_probability
```
using **all** historical settled picks (won=1, lost=0, push excluded).

**Blend formula** for display:
```
display_lock_score = 0.40·calibrated + 0.25·market_edge
                    + 0.15·consensus + 0.10·sample_strength + 0.10·data_quality
```
Auto-refits every 100 newly-settled picks.

**Constraints honoured:**
- Existing 0-99 scale preserved.
- 99-cap only for top-1-2% picks by raw score.
- Never rewrites already-settled picks.

**Pooled across all sport/market combinations.** No segmented curves per sport or per market. This means:
- MLB Hits Over 0.5 (deep chalk, high hit-rate) and Anytime TD (long-shot) are mixed into the same curve.
- The curve's slope in the 60-90 band is set by whichever sport dominates the sample.

### 1.2 Learning system v2

`learning_system_v2.py::recompute_and_persist` computes per-market-family weight adjustments (visible in startup log: *"Learning v2 recomputed: N rows, N weight overrides, N log entries"*). Buckets: sport × market_family. Not per-line, not per-odds-band.

### 1.3 Signal-rank refresh

`signal_rank` (visible in startup log): daily refresh at start-up, `n_total=172, bands={90+:24, 75+:54, 50+:108, 25+:160}`. This is a **ranking** distribution, not a calibration.

---

## 2. What we cannot measure without live DB access

The user prompt asks for per-sport / per-market:
- Sample size, predicted vs actual buckets, Brier, log-loss, ROI, avg odds, CLV, push rate, void rate, slope/intercept, CI, per-line-band, per-odds-band, favourite/underdog, main/alt, per-confidence-tier, per-Magic-Tier, per-sample-size.

**Producing these numbers requires querying the `picks` + `user_bets` + `prediction_snapshots` collections.** Since Phase 4A is audit-only and **no production writes are allowed**, this baseline document catalogues the **available tooling and the queries to run in Phase 4B** rather than the numbers themselves.

The Phase 4A deliverable therefore lists:
1. Which reports / queries EXIST and can be executed without code change.
2. Where the baseline must be **recomputed** in Phase 4B before any calibration change.
3. Known gaps in the historical record (e.g. CLV not captured before 2026-Q2).

---

## 3. Available baseline reports (existing endpoints / scripts)

| Report | Location | Segmentation | Notes |
|---|---|---|---|
| `me_performance` (per-user ROI) | `routes/me_performance_routes.py` | Per user | Uses `user_bets`; sport & market breakdowns available. |
| Learning snapshots | `learning_snapshots` collection | Per sport × market × bucket | Fed by `learning_engine.py`; provides bucket ROI. |
| Parlay intelligence backtest | `routes/parlay_history_routes.py::/api/parlay/intelligence/backtest` | Full parlay slate | Windowed (default 60 days). |
| Backtest framework | `services/backtest_framework.py` | Configurable | Admin-only. |
| Signal rank daily | daily job — writes to `signal_rank_*` collections | Per pick | Ranking distribution only, not calibration. |
| Lock calibration knots | `lock_calibration.py` — persisted in `lock_calibration_curve` doc | Global | Auto-refits every 100 settled picks. |
| CLV closing-line snapshotter | `closing_line_snapshotter.py` | Per pick | Populates `prediction_snapshots` with closing-line data. |
| Odds-usage audit | `scripts/odds_usage_audit.py` + `odds_usage_projection.py` | API-call level | Budget audit, not model calibration. |

---

## 4. Recommended calibration report to build in Phase 4B (audit-tool only)

**No production writes**, admin-only, single script:

```
scripts/phase4_calibration_report.py
  Segmentation axes:
    • sport
    • market_family (per _PROP_FAMILY_MAP)
    • main_line vs alt_line
    • odds band  (-500+, -300 to -499, -180 to -299, -140 to -179, -100 to -139, +100 to +200, +200+)
    • line band  (0.5, 1.5, 2.5, 3.5+)
    • favourite/underdog (spread/ML picks)
    • confidence tier  (Lock 95+, 88-94, 80-87, 74-79, 65-73, <65)
  Metrics per bucket:
    • n_picks
    • n_settled
    • n_won
    • n_lost
    • n_pushed
    • n_voided
    • hit_rate (won / (won + lost))
    • Brier_score  = mean((prob - outcome)^2)
    • log_loss     = − mean(y·log p + (1-y)·log(1-p))
    • ROI          = sum(pnl) / sum(risked)
    • avg_odds
    • CLV_mean     — from prediction_snapshots.closing_line_delta
    • CLV_median
    • slope, intercept  (linear fit of predicted vs actual per bucket)
    • CI 95% for hit_rate
```

**Runs against `picks` + `user_bets` (canonical) + `prediction_snapshots` — read-only.**

**Deliverable:** JSON + Markdown table per axis, plus a global `PHASE4_CALIBRATION_REPORT_YYYYMMDD.md`. Retained in `/app/reports/`.

---

## 5. Known calibration weaknesses

| ID | Description | Impact |
|---|---|---|
| CAL-1 | Isotonic curve is pooled across all sport/market pairs. | 🔴 High — a well-calibrated MLB pitcher K bucket can be dragged down by a poorly-calibrated soccer scorer bucket. |
| CAL-2 | No per-line-band segmentation — 0.5 Hits and 2.5 Hits share a bucket. | 🔴 High |
| CAL-3 | No per-odds-band segmentation — chalk (-500) and moderate (-150) share a bucket. | 🔴 High |
| CAL-4 | Push rate not calibrated separately — pushes are dropped, not modelled. | 🟡 Medium |
| CAL-5 | CLV is captured only for picks after `closing_line_snapshotter` was armed — historical CLV backfill is incomplete. | 🟡 Medium |
| CAL-6 | Auto-refit trigger (every 100 settled picks) may starve calibration for sports with low daily volume (e.g. UFC — 1-2 events/week). | 🟡 Medium |
| CAL-7 | Magic Tier composite score is not calibrated against historical ROI (see Magic Tier Audit MT-2). | 🟡 Medium |
| CAL-8 | Lock-score blend weights (0.40 / 0.25 / 0.15 / 0.10 / 0.10) are fixed constants — never re-tuned against out-of-sample performance. | 🟡 Medium |

---

## 6. Recommended slate of calibration curves to fit in Phase 4B

**Segmented isotonic curves**, one per axis combination:
1. `(sport, market_family)` — 6 sports × ~10 families = ~40-50 curves.
2. `(sport, market_family, main_vs_alt)` — doubles the count.
3. `(sport, market_family, odds_band)` — larger.

Smallest useful segmentation: `(sport, market_family)`. Larger segmentations need ≥30 settled picks per bucket to avoid overfitting.

**Practical recommendation:**
- Ship `(sport, market_family)` curves in Phase 4B.
- Add `main_vs_alt` split for MLB hitter markets (H+R+RBI, Hits, HR, RBI, TB) — enough sample to justify.
- Defer `odds_band` split unless a bucket shows systemic miscalibration.

---

## 7. Push / void / postponed rate baseline (to be measured)

Per user prompt: "Do not aggregate unlike markets into one misleading global hit rate."

Push / void / postponed rate segmented per market:
- **MLB pitcher props** — starter scratches (rain/injury) → **postponed** in MLB rules. In our schema, `stuck_pick_reaper.py` should void picks whose starting pitcher does not appear. Verify in Phase 4B.
- **MLB Hits / H+R+RBI / HR / RBI / TB** — batter did not play → **void** per sportsbook standard rule. Same verification needed.
- **NFL Anytime TD / Passing yards** — player inactive → **void**. `prop_settlement.py::_espn_player_appeared` (line 557) is present.
- **CFB** — settlement path not verified (feature engine dark; settlement side likely reads from a shared prop_settlement).
- **NBA** — DNP-CD or "load management" → **void**. Verify.
- **Soccer scorer** — did not enter the pitch → **void**. `_espn_player_started` (line 494) + `_espn_player_appeared` present. `_espn_did_score_goal` (line 658), `_espn_did_score_or_assist` (line 751).
- **Tennis** — retirement / walkover handling exists in `tennis_extra/settle.py` and `espn_settlement._tennis_pick_outcome` (line 118).

**All settlement paths must be re-run against the Phase 4B report to confirm push/void rates fall within sportsbook-standard bands** (typically <2% for player props, <0.5% for game markets).

---

## 8. Files audited (Calibration layer)

- `lock_calibration.py` — full read of docstring / blend formula.
- `learning_system_v2.py`, `learning_engine.py`, `learning_buckets.py` — signature-level.
- `services/lock_score_performance.py`, `services/backtest_framework.py`, `services/pvt_backtest.py` — signature-level.
- `services/prediction_fusion_engine.py` — signature-level (700 LOC, deferred to Phase 4B for deep read).
- Runtime evidence — startup log `Calibration loaded: 6304 samples, 8 knots, fit_at=2026-08-06T19:21:02` and `Signal-rank refresh for 2026-08-06: {'ok': True, 'cached': False, 'n_total': 172, 'n_computed_raw': 134, 'n_persisted': 172, 'bands': {'90+': 24, '75+': 54, '50+': 108, '25+': 160}}`.

The **6304-sample calibration curve** is real and refits automatically. The pool is global. Segmentation is the primary Phase 4B calibration improvement.

---

## 9. Baseline snapshot to capture BEFORE any Phase 4B change

Recommended Phase 4B step-0 action (before any calibration edit):
1. Run the calibration report described in §4 above.
2. Persist the JSON + Markdown output in `/app/reports/phase4_baseline_YYYYMMDD/`.
3. Snapshot the current lock_calibration knots and the current learning_snapshots collection to `mongodump` in `/app/reports/phase4_baseline_YYYYMMDD/db_snapshot/`.

This gives a **frozen baseline** to compare against after each Phase 4B iteration.
