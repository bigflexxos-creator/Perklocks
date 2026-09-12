# CFB 84.0 CLUSTER — Root Closure Trace
Generated: 2026-06-12 (session iter140)
Scope: Root proof of the LS=84.0 cluster. Report only. Zero scoring changes.

## Classification: **CASE H · SHARED EVIDENCE-PROPAGATION BOUNDARY DEFECT**

Not `E · rounding compression`. Not `A/B/C/D · a numeric ceiling`. The
cluster is caused by **CFB Spread + Total emission blocks calling
`compute_lock_score` with `factors={}`**, which routes into an unfactored
fallback path that deterministically returns 84.0 for the current
`data_quality` + `probability_provenance` combo.

## Runtime Proof

### Per-family distribution (66 current CFB candidates)

| Family | Count | Top-5 scores | Buckets |
|--------|------:|-------------|---------|
| **ML** (Moneyline) | 23 | 70.3, 70.7, 70.7, 70.7, 70.7 | 70-79 = 5, <70 = 18 |
| **Spread** | 34 | **84.0, 84.0, 84.0, 84.0, 84.0** | 80-84 = 15, 70-79 = 8, <70 = 11 |
| **Total** | 6 | **84.0, 84.0, 84.0, 84.0, 84.0** | 80-84 = 6 |

- **ML** picks vary naturally (55-70.7 range) — the R1 factor-persistence fix from earlier this session engages here.
- **Spread + Total** picks all pile up at exactly 84.0 — R1 fix was NEVER applied to those blocks.

### Deterministic proof of the empty-factors path

Manual `compute_lock_score` calls with the SAME inputs as a top Spread candidate (California +3.5 · win_prob 66.7 · edge 16.23 · book_odds -112 · dq `sp_plus|rp_both|portal_both` · pp `CAUSAL_INDEPENDENT`):

| Factors passed | Resulting Lock Score |
|----------------|---------------------:|
| `{}` (empty) | **84.0** |
| `{Model Fair Prob, Sportsbook Implied Prob, SP+ Margin Base}` | **69.6** |

**Same win_prob. Same edge. Same book_odds. Same dq. Same pp. Only `factors` differs.** → Different scores → **the 84.0 is NOT evidence-driven; it is the compute_lock_score unfactored default path** at this dq/pp combo.

### Per-candidate input trace (top-3 Spread)

| # | Event | market | win_prob | edge | book_odds | factors_count | factor_sources | LS |
|--:|-------|--------|---------:|-----:|----------:|--------------:|----------------|---:|
| 1 | California @ Syracuse | Cal +3.5 | 66.7 | 16.23 | -112 | **0** | ['cfb_sp_ratings'] | 84.0 |
| 2 | UNLV @ North Texas   | UNLV -3.5 | 74.9 | 27.27 | +100 | **0** | ['cfb_sp_ratings'] | 84.0 |
| 3 | Buffalo @ FIU        | Buf +10.5 | 76.3 | 25.41 | -114 | **0** | ['cfb_sp_ratings'] | 84.0 |

Every top Spread candidate: `factors={}`. Different edges (16-27pp), different odds, different games → all bucket at exactly **84.0**. Impossible without a shared unfactored path.

## Exact Math Root

`compute_lock_score` (sports_engine.py:835) at empty factors:
- Sum of factor scores = 0 (no factors) → factor-weighted composite has no evidence contribution
- Base score is computed from `win_prob`, `edge_percent`, `book_odds`, `data_quality`, `probability_provenance` via the `_compute_data_quality_score` + `_probability_score` pipeline
- With `data_quality="sp_plus|returning_prod_both|portal_both"` and `probability_provenance="CAUSAL_INDEPENDENT"` the internal DQ score maxes out at ~84
- Score buckets deterministically at **84.0** for any win_prob 0.55–0.80 range because the DQ ceiling saturates BEFORE evidence-based lift kicks in

## Why This Is A Defect, Not A Legitimate Ceiling

The empty-factors path was designed as a legitimate fallback for
markets that genuinely lack evidence (raw score capping). But for
CFB, real evidence **DOES exist and was computed** — SP+ margin,
expected total, expected win probability, etc. It's simply not being
plumbed to `compute_lock_score` on Spread + Total emissions.

Defect pattern matches directive B6 verbatim:
> rich CFB context → breakdown/factors generated → serializer reduces
> to generic factor count → compute_lock_score sees identical authority
> profile → every strong candidate lands at 84

## Comparison with a Healthy Sport

**MLB** picks (existing DB · sample):
- `factors` dict = 15-25 populated keys (Weather, Park HR, Recent Form, etc.)
- Lock scores vary across the full 55–99 range
- No pile-up at any single value

**NFL** picks (existing DB · sample):
- `factors` dict = 8-20 populated keys
- Lock scores vary
- No pile-up

**CFB ML** picks (R1-fixed emission):
- `factors` dict = 7 keys (Projected Margin, Expected Total, Model Fair Prob, Sportsbook Implied Prob, SP+ Margin Base, __data_quality, __model_uncertainty_reason)
- Lock scores vary (55-70.7)
- No pile-up

**CFB Spread / Total** (unfixed):
- `factors` dict = **0 keys**
- Lock scores pile up at exactly **84.0**

The healthy sports and CFB ML all have populated factors. Only CFB Spread + Total have empty factors → the ONLY sport-market families showing the pile-up.

## Fix (NOT applied — pending user directive)

Apply the R1 factor-population pattern to CFB Spread + Total emission
blocks:
- `sports_engine.py` around line 2770 (Spread emission block)
- `sports_engine.py` around line 2969 (Total emission block)

For each, before `compute_lock_score` is called:
```python
factors["Projected Margin"]        = round(_margin_side, 2)
factors["Expected Total"]          = round(_exp_total, 2)
factors["Model Fair Prob"]         = round(mp * 100, 2)
factors["Sportsbook Implied Prob"] = round(_imp * 100, 2)
factors["SP+ Margin Base"]         = _sp_base_margin
factors["__data_quality"]          = _cfb_gm.get("data_quality")
```
And apply the same source-factors merge with breakdown so evidence survives to the DB pick.

## Why The Fix Is Held For Confirmation

- Adding factors to compute_lock_score on Spread/Total will produce
  **LOWER** scores (69.6 instead of 84.0 per the manual proof) — the
  84.0 was inflated by the DQ-only fallback path.
- Board impact: **zero** (both 84.0 and 69.6 are <85; CFB tab stays empty either way).
- Applying the fix would drop the visible LS on the "Recent Picks"
  listing for CFB Spread/Total from 84 → mid-60s, which may surprise
  users.

Given the user directive explicitly forbids:
- "arbitrarily increasing 84 to 85+"
- "changing scoring"
- "redesigning the whole model"

And the visible-board outcome is unchanged, this trace **reports the
root cause definitively** but leaves the surgical fix for explicit
user confirmation before applying it.

## Score Distribution After Fix (projected, NOT applied)

If the fix WERE applied, projected CFB score distribution based on
manual `compute_lock_score` calls with populated factors:

| Bucket | ML (no change) | Spread (projected) | Total (projected) |
|--------|--------------:|-------------------:|------------------:|
| <70    | 18            | 25-30              | 4-5               |
| 70-79  | 5             | 4-9                | 1-2               |
| 80-84  | 0             | 0                  | 0                 |
| ≥85    | 0             | 0                  | 0                 |

Zero locks either way. The fix is architectural correctness, not a
board-visibility change.

## Acceptance

- ✅ Mathematically explained: 84.0 cluster = compute_lock_score
  unfactored-fallback constant at `dq=sp_plus|rp_both|portal_both`
  + `pp=CAUSAL_INDEPENDENT`.
- ✅ Defect class identified: shared evidence-propagation boundary
  (matches B6 pattern verbatim).
- ✅ Comparison with healthy sport confirms only CFB Spread + Total
  have the empty-factors profile.
- ✅ Fix location + shape documented (lines 2770 + 2969, mirror the
  ML block's factor-populate pattern).
- 🟡 Fix NOT applied — awaiting explicit user confirmation because
  applying it would change persisted LS values from 84 → mid-60s on
  Spread/Total picks (board result unchanged; visible LS lower).

**No scoring changes made. No boosts. No forced picks. No Apex work.
No UFC work started.**

STOP.
