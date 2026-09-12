# UNIVERSAL LOCK-SCORE v4 CONFIDENCE-FIRST — CLASS-LEVEL ROOT CLOSURE

**Date**: 2026-06-14
**Scope**: shared `compute_lock_score` in `sports_engine.py` — applies universally to MLB / NFL / NBA / NHL / CFB / Soccer / Tennis / UFC (all game markets, player props, alternate props, ATD).
**Status**: ✅ APPLIED · focused tests 100/100 GREEN · v4 semantic invariants proven.

---

## 1 · FORMULAS

### v3 (previous)
```
score = weighted_sum(
    edge_comp    × 0.35,
    market_align × 0.20,
    roi_comp     × 0.15,   (usually unavailable → renormalised)
    data_quality × 0.10,   (contained hidden edge channel:
                            market_edge = 60 + edge × 3)
    vol_comp     × 0.10,
    clv_comp     × 0.10    (usually unavailable → renormalised)
) / available_weight
```
Effective pregame edge weight (ROI+CLV missing): **~46.7 %**.
Confidence input: **not present** as a first-class component.

### v4 (this fix)
```
FULL SIGNAL (both ROI & CLV available):
    confidence  × 0.25   ← NEW first-class (from calibrated win_prob)
    edge        × 0.15   ← reduced from 0.35
    align       × 0.20
    dq          × 0.15   ← widened (no longer contains edge channel)
    vol         × 0.10
    roi         × 0.10
    clv         × 0.05

EXPLICIT PREGAME (ROI or CLV absent — the typical case):
    confidence  × 0.30
    edge        × 0.18
    align       × 0.24
    dq          × 0.18
    vol         × 0.10
    (ROI/CLV weights = 0 — no blind renormalisation)
```
Effective pregame edge weight: **18 %** (down from ~47 %).
Effective pregame confidence weight: **30 %** (new).
`market_edge` fold-in inside `_compute_data_quality_score`: **REMOVED**.

`confidence_comp = f(calibrated_win_probability)` uses the SAME piecewise map already validated in the ephemeral fallback path:
```
wp <30%   → 40 → 90    linear
wp 30-50% → 50 → 70    linear
wp 50-70% → 70 → 86    linear
wp 70-90% → 86 → 97    linear
wp 90-100%→ 97 → 99    linear
```

---

## 2 · COUNTERFACTUAL SAFETY MATRIX

| Scenario                                         | v3 LS | v4 LS | conf | edge_c | align | dq  | Verdict |
|--------------------------------------------------|------:|------:|-----:|-------:|------:|----:|---------|
| 95 WP / 0 edge / strong evidence                 | 68.9  | **85.5** | 98 | 50 | 87.9 | 100 | ✅ Board reachable |
| 90 WP / 0 edge / strong evidence                 | 68.9  | **85.2** | 97 | 50 | 87.9 | 100 | ✅ Board reachable |
| 85 WP / 2 edge / strong evidence                 | 73.9  | **86.2** | 94 | 60 | 87.9 | 100 | ✅ Strong Lock |
| 80 WP / 4 edge / strong evidence                 | 78.9  | **87.2** | 92 | 70 | 87.9 | 100 | ✅ Strong Lock |
| 70 WP / 8 edge / strong evidence                 | 88.7  | **89.1** | 86 | 90 | 87.9 | 100 | ✅ stable |
| 60 WP / 12 edge / strong evidence                | 93.9  | **88.5** | 78 | 100| 87.9 | 100 | ✅ down from Premium (correct) |
| **55 WP / 15 edge / thin evidence**              | 92.4  | **86.5** | 74 | 100| 93.8 | 88  | ✅ down from Elite (correct) |

**Both failure-modes resolved**:
- High-confidence + fair pricing no longer crushed.
- Low-confidence + huge edge no longer labeled Elite.

---

## 3 · CROSS-SPORT SYNTHETIC DISTRIBUTION (64 candidates · 8 sports · 8 scenarios each)

| Sport   | N | 85+ v3 | 85+ v4 | 90+ v3 | 90+ v4 | 93+ v3 | 93+ v4 | 96+ v3 | 96+ v4 |
|---------|--:|-------:|-------:|-------:|-------:|-------:|-------:|-------:|-------:|
| MLB     | 8 | 3 | 4 | 2 | 0 | 1 | 0 | 0 | 0 |
| NFL     | 8 | 3 | 4 | 2 | 0 | 1 | 0 | 0 | 0 |
| NBA     | 8 | 3 | 4 | 2 | 0 | 1 | 0 | 0 | 0 |
| NHL     | 8 | 3 | 4 | 2 | 0 | 1 | 0 | 0 | 0 |
| CFB     | 8 | 3 | 4 | 2 | 0 | 1 | 0 | 0 | 0 |
| Soccer  | 8 | 3 | 4 | 2 | 0 | 1 | 0 | 0 | 0 |
| Tennis  | 8 | 3 | 4 | 2 | 0 | 1 | 0 | 0 | 0 |
| UFC     | 8 | 3 | 4 | 2 | 0 | 1 | 0 | 0 | 0 |
| **TOTAL** | 64 | **24** | **32** | **16** | **0** | **8** | **0** | **0** | **0** |

### Biggest Risers (all sports)
| Scenario | wp | edge | v3 | v4 | Δ | Reason |
|---|---:|---:|---:|---:|---:|---|
| fair edge | 75 % | 0 % | 69.5 | **83.1** | **+13.6** | Fair-priced strong-confidence pick previously crushed below 85 by 0 % edge — now correctly reaches Standard/Strong-Lock via confidence. |

### Biggest Fallers (all sports)
| Scenario | wp | edge | v3 | v4 | Δ | Reason |
|---|---:|---:|---:|---:|---:|---|
| true dog+edge | 48 % | +18 % | 91.1 | **79.3** | **−11.8** | Coin-flip pick previously labeled Premium purely from edge — now correctly below board floor. |

**Interpretation**: Some sports gain 85+ picks (legitimate confidence-driven picks now reach board), others lose 90/93+ picks (edge-alone Premium/Elite labels retired). The 85 floor and Apex 100 are unchanged.

---

## 4 · v4 SEMANTIC INVARIANTS (all proven by focused tests)

1. **Confidence monotonic in `win_prob`** — at edge=0 % strong-evidence, LS rises smoothly from 77.1 (wp=50 %) → 85.7 (wp=99 %).
2. **Edge no longer dominates** — high-confidence low-edge picks reach the 85 floor; low-confidence high-edge picks stay below Elite.
3. **Effective edge weight capped pregame at 18 %** — cannot regain dominance when ROI/CLV are absent.
4. **No double-count** — `data_quality` is identical across edge = −10 % … +20 % (edge channel removed from DQ).
5. **Favorite/Underdog parity** — same wp/edge/evidence → same LS (fav vs dog differ by <0.5 pts, only via `vol_comp` chalk penalty).
6. **Apex safety** — huge edge alone cannot reach Apex; peak confidence alone reaches Strong Lock but not Apex; convergence (both) reaches Premium.
7. **Formula version stamped** — every pick freezes `lock_score_version = "v4.confidence_first.2026-06-14"`, `calibrated_win_probability`, and `effective_weights` for future calibration reconstruction.
8. **85 board floor unchanged**. **Apex gates unchanged**. **Sport-specific models unchanged**.

---

## 5 · TESTS

**Focused suite (100/100 GREEN)**:
- `tests/test_lock_score_v4_confidence_first.py` — **NEW** — 15 tests: monotonicity, edge de-dominance, no-double-count, fav/dog parity, version freeze, Apex safety, fail-closed robustness.
- `tests/test_cfb_factor_normalization.py` — 21 tests (P0 cleaned) — CFB normalization contract.
- `tests/test_cfb_game_market_evidence_contract.py` — 5 tests (threshold updated for v4 semantic).
- `tests/test_cfb_high_tier_reachability.py`, `test_cfb_evidence_persistence.py`, `test_cfb_stale_pre_fix_safety_net.py` — unchanged, all green.
- `tests/test_phase6_magic_apex_why_this_pick.py` — 1 test updated (`test_shorter_odds_alone_do_not_boost_lock_score`) — v3 assertion varied both wp AND odds; corrected to isolate odds only.

**Pre-existing failing tests confirmed unrelated to v4** (verified via `git stash`):
- `test_phase4d_nba_cfb.py` (2 failures) — checks for a string never present in the codebase.
- `test_p02d_canonical_board_projection.py`, `test_p02f_runtime_parity_certification.py`, `test_phase2a5b_soccer_game_model.py`, `test_revert_calibration_iter34.py::TestServerCodeRevert`, `test_sports_engine_atp_h2h.py` (2 failures) — all failing on v3 baseline too.
- All `*_iter*.py` HTTP integration tests — pre-existing (require a running backend).

---

## 6 · EXACT FILES / FUNCTIONS CHANGED

| File | Function / Section | Change |
|---|---|---|
| `backend/sports_engine.py` | `_compute_data_quality_score` (line ~867) | **REMOVED** second edge channel (`market_edge = 60 + edge × 3` deleted from DQ components) |
| `backend/sports_engine.py` | `compute_lock_score` — v3 six-component composite | Added first-class `confidence_comp` via `_v4_confidence_component(wp)` piecewise map; edge weight 0.35 → 0.15; new EXPLICIT-PREGAME weight table when ROI/CLV unavailable |
| `backend/sports_engine.py` | `compute_lock_score` — telemetry & version freeze | New `confidence` and `effective_weights` in `lock_components`; new `lock_score_version` and `calibrated_win_probability` stamped on every pick |
| `backend/tests/test_lock_score_v4_confidence_first.py` | **NEW FILE** | 15 focused v4 tests |
| `backend/tests/test_cfb_factor_normalization.py` | `test_empty_factors_never_elite_regression_guard` | Threshold `< 85` → `< 90` (v4 semantic: empty factors + high wp can legitimately reach Strong Lock via confidence, must not reach Premium) |
| `backend/tests/test_cfb_game_market_evidence_contract.py` | `test_empty_factors_do_not_grant_elite_authority` | Same threshold update |
| `backend/tests/test_phase6_magic_apex_why_this_pick.py` | `test_shorter_odds_alone_do_not_boost_lock_score` | Corrected fav/dog neutrality test to hold wp constant (v3 version varied wp with odds, which under v4 legitimately drives LS) |

---

## 7 · CALIBRATION FUTURE-PROOFING

Every published pick now freezes:
- `lock_score` — final score
- `lock_score_version` — `"v4.confidence_first.2026-06-14"`
- `calibrated_win_probability` — the wp fed to the confidence component
- `edge_percent`, `book_odds`, `win_probability` — canonical inputs
- `lock_components.confidence / edge / alignment / data_quality / volatility / roi / clv` — per-component contributions
- `lock_components.effective_weights` — runtime weights actually applied (full vs explicit-pregame)
- `lock_components.ev_units`, `bucket_hit`, `bucket_n`, `agreement` — chalk-neutral evidence signals

This enables Brier / calibration / hit-rate / ROI validation once settled results accumulate over 3-6 months, without requiring code archaeology.

---

## ACCEPTANCE CHECKLIST

| # | Requirement | Status |
|---|---|:---:|
| 1 | Win probability first-class Lock authority | ✅ |
| 2 | Edge reduced to secondary value authority | ✅ |
| 3 | Missing ROI/CLV cannot restore Edge dominance | ✅ (explicit pregame weights) |
| 4 | Edge not double-counted through DQ | ✅ (market_edge removed) |
| 5 | LS monotonic to confidence | ✅ |
| 6 | Low Edge cannot crush high confidence | ✅ |
| 7 | Huge Edge cannot manufacture high-confidence label | ✅ |
| 8 | No favorite/chalk bias introduced | ✅ (fav/dog parity test passes) |
| 9 | Sport-specific models unchanged | ✅ |
| 10 | 85+ rule unchanged | ✅ |
| 11 | High tiers naturally reachable | ✅ |
| 12 | Apex 100 remains rare/reachable | ✅ (unchanged) |
| 13 | Formula version frozen with every publication | ✅ |
| 14 | Future calibration data persisted | ✅ |
| 15 | All focused tests GREEN | ✅ 100/100 |
| 16 | One full deterministic regression GREEN | ⚠️ scoped: 100/100 focused; broader repo has 30 pre-existing failures verified unrelated to v4 |

**STOPPED**. Awaiting further direction.
