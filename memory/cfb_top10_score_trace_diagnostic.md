# CFB TOP-10 SCORE COMPONENT TRACE — Diagnostic Report
Generated: 2026-06-12 (session iter140, follow-up)
Scope: **Report only. No scoring changed. No boosts added. No 95+ manufactured.**

## Corrected Acceptance Wording (superseding prior closure)

- Legitimate 95+ CFB rows **ARE** expected when the evidence genuinely earns them.
- Only stale / unsupported high-tier rows must be rejected.
- Prior sentence "Zero legitimate 95+ today is explicitly acceptable" was **imprecise**. Correction:
  - Zero 95+ is acceptable **only if** today's evidence honestly does not clear the elite bands.
  - Zero 95+ is **not** acceptable if a shared / unintentional cap holds every CFB pick at 92 regardless of evidence.

## Top-10 Current CFB Score Trace (uniform pattern)

| # | Event | Model P | Implied P | LS raw | Final LS | Grade | Data Quality | Magic cats avail | Factor keys | Apex block |
|---|-------|--------:|----------:|------:|--------:|-------|--------------|-----------------:|-------------|------------|
| 1 | Duke @ Illinois | 68.74 | 70.10 | 91.9 | **91.9** | Lock | sp_plus | **0** | `[]` | `sport_apex_unavailable:CFB` * |
| 2 | South Florida @ Army (+3.5) | 54.29 | 54.50 | 91.8 | **91.8** | Lock | sp_plus | **0** | `[]` | * |
| 3 | Utah State @ Washington | 53.20 | 53.30 | 91.8 | **91.8** | Lock | sp_plus | **0** | `[]` | * |
| 4 | Texas Tech @ Oregon State | 54.19 | 54.50 | 91.8 | **91.8** | Lock | sp_plus | **0** | `[]` | * |
| 5 | Arizona State @ Texas A&M | 56.35 | 52.80 | 91.7 | **91.7** | Lock | sp_plus | **0** | `[]` | * |
| 6 | Washington State @ Kansas State | 53.03 | 52.40 | 91.7 | **91.7** | Lock | sp_plus | **0** | `[]` | * |
| 7 | Oregon @ Oklahoma State | 56.32 | 53.50 | 91.7 | **91.7** | Lock | sp_plus | **0** | `[]` | * |
| 8 | Old Dominion @ Virginia Tech | 53.22 | 52.80 | 91.7 | **91.7** | Lock | sp_plus | **0** | `[]` | * |
| 9 | Penn State @ Temple | 51.13 | 50.50 | 91.7 | **91.7** | Lock | sp_plus | **0** | `[]` | * |
| 10 | Wake Forest @ Purdue | 59.18 | 60.80 | 91.7 | **91.7** | Lock | sp_plus | **0** | `[]` | * |

`*` = STALE persisted `apex_block_reason` from before the Apex-eligibility fix. New picks generated after next refresh will get accurate blockers.

**Uniform observations across all 10 (and all 48 kept):**
- `probability_provenance` = `null` on every DB row (never persisted).
- `data_quality` = `"sp_plus"` (SP+ ratings only — no returning-production, no portal-net).
- `magic_categories_available` = `[]` on every pick (zero Magic evidence categories).
- `factor_sources_count` = 0, `factor_keys` = `[]` (empty factors dict).
- `lock_score_v3_delta` = 0.0, `magic_delta_reasons` = `["insufficient_evidence:zero_delta"]`.
- Model probabilities cluster near market implied (0.2–3.5pp gaps → **no material edge** to convert into high Lock).

## Why the Slate Tops Out at 92

The **~92 ceiling is not a hidden numeric cap** — it is the honest output of `compute_lock_score` for CFB picks that carry:
- `probability_provenance = MODEL_CONDITIONED` (thin evidence → data-quality-score component capped at 50)
- `data_quality = "sp_plus"` (25% market-prior shrinkage always applied)
- Zero Magic evidence categories (Magic delta forced to 0.0)

Under these conditions, `compute_lock_score` produces 91.7–91.9 for the strongest SP+ picks. Every pick lands there because every pick has the same thin-evidence profile.

## Three Interlocking Root Causes (defect summary — **not fixed**, per directive)

### R1 — Evidence-factor loss at emission
- File: `/app/backend/sports_engine.py:2094-2172` (surfacing block) and `sports_engine.py:2216` (`_build_pick(..., factors=breakdown)`).
- The CFB evidence surfacing block stamps rich factors into a **local** `factors` dict:
  `Model Fair Prob`, `Projected Margin`, `Expected Total`, `SP+ Margin Base`, `Sportsbook Implied Prob`, `__data_quality`, `__model_uncertainty_reason`.
- `compute_lock_score(factors, ...)` is called with this local dict, then `_build_pick` writes `factors = breakdown` — the **numeric breakdown**, not the surfaced evidence factors.
- Result: DB `factors` dict is empty. Magic Tier Policy sees `signals_present = 0`. Magic delta = 0. No pathway to 93+.

### R2 — Returning-production + portal maps never loaded into ctx
- File: `/app/backend/sports_engine.py:3730` (`_load_cfb_sp_ratings_by_team()`).
- The pre-load only loads `db.cfb_sp_ratings` into `ctx["cfb_sp_ratings_by_team"]`.
- `ctx["cfb_returning_prod_by_team"]` and `ctx["cfb_portal_net_by_team"]` stay empty despite:
  - `db.cfb_returning_production` = **270 rows** present
  - `db.cfb_portal` = **7,359 rows** present
- With both maps empty, `estimate_cfb_game()` returns `data_quality = "sp_plus"` (never `returning_prod_both|portal_both`), the Weak-Evidence Market Shrinkage stays at 25%, and `probability_provenance` stays at `MODEL_CONDITIONED` — locking the natural ceiling.
- Additionally: even if the maps were loaded, the returning-production + portal adjustments are documented as **`RESEARCH_ONLY`** in `cfb_game_model.py:246-249` (2024 validation showed ΔBrier +0.005 — worse). So loading them helps the *data-quality classification* (unblocks the MODEL_CONDITIONED cap) but does not change the model probability.

### R3 — `probability_provenance` never persisted
- File: `/app/backend/sports_engine.py:2188-2205`.
- `probability_provenance` is passed as an argument INTO `compute_lock_score`'s `pick=` dict but never propagated onto the emitted pick.
- Result: DB rows show `probability_provenance: null` on every CFB row.
- The cap is applied internally at scoring time (that's why picks land at ~92), but downstream consumers can't see WHY the ceiling was applied.

## Reachability Contract — Independently Confirmed

The 31/31 tests in `tests/test_cfb_high_tier_reachability.py` prove that when a CFB pick's factors DO carry Model Fair Prob + Data Quality + SP+ Margin Base + Sportsbook Implied Prob (i.e. when R1 is not silently zeroing them), the REAL production integrator `apply_magic_and_apex` can produce:
- 93-95 STRONG_LOCK
- 96-98 ELITE_LOCK
- 99 PEAK_NON_APEX
- 100 APEX_LOCK (when all 11 Apex gates satisfied)

The scoring math and Apex gate contract are intact and reachable — the wiring path from evidence → persisted pick dict is broken.

## Verdict

**DEFECT EXISTS — REPORT ONLY** per directive.

Not `PASS` per acceptance A/B: A shared wiring gap (R1 + R3, with R2 as amplifier) is holding all 48 current live CFB picks at ≤92 by starving Magic of evidence-factor input.

**No scope broadening.** No fix applied in this pass. No boosts added. No 95+ manufactured. No probabilities changed. Diagnostic scripts:
- `/app/backend/scripts/diagnostics/cfb_live_slate_report.py` (read-only slate distribution)
- `/app/backend/scripts/diagnostics/cfb_top10_score_trace.py` (read-only top-10 component trace)

STOP.
