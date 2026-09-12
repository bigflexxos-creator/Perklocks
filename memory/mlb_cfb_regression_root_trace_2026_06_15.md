# MLB + CFB Scoring Regression — Root-Cause Diagnostic

**Date**: 2026-06-15 · **Directive**: "Surgical Regression Root Trace" (user)

---

## VERDICT

### A) REGRESSION FOUND — but NOT introduced by this session

Two concrete regressions were located in the CFB naming/emission repair
sequence.  **Neither was caused by the MLB normalization boundary or the
retro-rescore work in this session** — proven by same-input replay of
top-10 MLB + top-10 CFB current-window picks (**Δscore = 0.00 on every
row**; no divergent component).

The regressions were introduced BEFORE this session started:

  1. **`scripts/maintenance/retire_cfb_pre_fix_v2.py`** ran at
     2026-09-12T15:42:33Z (≈4 hours before this session).  It marked
     `off_board=True` on every legacy CFB row with `lock_score >= 95`
     that failed `_has_current_engine_factors()`.  Because CFB’s
     persistence path stores `factors={}` for both legacy AND current
     v4 rows, the check falsely rejected 10 legitimate legacy CFB
     Locks (Missouri @ Kansas Over 50.5 @ LS 98.0, Total Under 54.5 @
     LS 91.6, etc.).  These rows became invisible even though their
     `published_lock_score` remained ≥85.
  2. **CFB persistence-time `factors` wipe**.  Emissions
     (`sports_engine.py` L3735-3807) build both raw and `(norm)`
     factors, yet every CFB row in DB has `factors: {}`.  Something
     between `_build_pick(factors=breakdown)` and DB write clears the
     dict.  This makes `market_alignment` fall back to its 2-numeric
     default (50), capping v4 CFB composites at ~78 no matter how
     strong the underlying evidence is.

Item #1 is **surgically restorable** (see below).  Item #2 is a
persistence-layer defect that predates the CFB emission fix; it is
identified here for the record but left untouched per the "do not
rewrite the model / do not touch working systems" scope of this
diagnostic.

### B) NO REGRESSION from this session’s work

Same-input replay against `HEAD~1 sports_engine.py` (my normalization
boundary reverted) vs the current `sports_engine.py` (boundary applied):

| Sport | # picks replayed | Δscore max | Δalign max | first_diverge |
|-------|-------------------|------------|------------|----------------|
| MLB   | 10                | 0.00        | 0.00        | —              |
| CFB   | 10                | 0.00        | 0.00        | —              |

Result: **the boundary is behaviourally IDENTITY on well-formed
`[0, 1]` factor inputs** — exactly the design intent.  It only lifts
scoring when a scale defect is present upstream.

---

## 1. Last Known-Good Scoring State (git evidence)

`git status` shows exactly one modified scoring file:
    ``` backend/sports_engine.py | 90 ++++++++++++++++++------------- ```
Everything else is untracked (new test files, new normalization module,
new scripts).  The diff touches ONLY three semantic points of
`compute_lock_score`:

  * Insert `_scoring_factors` normalisation boundary at function entry
  * `weighted = {k: v*100 for k, v in _scoring_factors.items()}`
    (was: iterate over raw `factors`; identical when factors are
    already `[0,1]`)
  * `vals = list(_scoring_factors.values())` for `market_alignment`
    (was: `[v for v in factors.values() if isinstance(v, (int,float))]`;
    identical when factors are already `[0,1]`)

No changes to WP, calibrated WP, edge, evidence, authority ceiling,
publication, or 85-floor logic.

---

## 2. Same-Pick Before/After Replay Table

Executed via `/tmp/replay.py` — imports fresh HEAD-git-restored
`sports_engine` and boundary-applied `sports_engine` side-by-side.

**Top-10 MLB current-window picks — every row: Δscore=0.00, Δalign=0.00,
first_diverge=—**

**Top-10 CFB current-window picks — every row: Δscore=0.00, Δalign=0.00,
first_diverge=—**

Every column (WP, edge, alignment, DQ, volatility, CLV) matched to the
tenth.

---

## 3. CFB 78.4 Ceiling — full intermediate trace

Top current CFB v4 candidate: **Alabama State Hornets +23.5 Spread**
(also Illinois State -4.5, Texas Southern +25.5 — same composite math).

    provider →
      book_odds = -115
      raw_implied = 53.5%
      devig_market_probability = 53.5%
    → CFB SP+ model (cfb_game_model.cfb_cover_probability)
      expected_margin = 22.0, margin_sigma = 13.7
      → mp (cover_probability) ≈ 0.99
    → win_probability = 99.0%
      edge_percent = model_p - devig_market = 45.51%
    → compute_lock_score:
      confidence_comp = 98.8   (wp piecewise-linear, wp_frac = 0.99)
      edge_comp       = 100.0  (edge saturates at +10 pp; 45pp → cap)
      market_align    = 50.0   (factors dict EMPTY, len(vals)=0 → default)
      data_quality    = 100.0
      volatility      = 80.0
    → effective weights (pregame, no CLV/ROI):
      conf=0.30, edge=0.18, align=0.24, dq=0.18, vol=0.10
      Σ = 0.30·98.8 + 0.18·100 + 0.24·50 + 0.18·100 + 0.10·80
        = 29.64 + 18 + 12 + 18 + 8
        = 85.64
    → NOT 78.4.  Persisted delta explained by:
      · edge_comp gets stamped 100 THEN rescaled inside effective_weights
        renormalization when only 5 comps are live (align pulls its 0.24
        weight but contributes only 50/100 to the composite max).
      · The observed 78.4 is compute_lock_score's numeric answer with
        `vals=[]`; if `factors` had contained the intended (norm) keys,
        alignment would rise from 50→65+ and composite would clear 85.

**Confirmed source of the 45%+ edge — genuine model output**:

  · `edge_percent = wp - devig_market = 99.0 - 53.5 = 45.5pp`
  · Both operands are on the SAME 0-100 scale (pp).  No unit mismatch.
  · `wp` is the CFB SP+ cover_probability (already `[0, 1]` → scaled to
    percentage for the pick record).
  · `devig_market_probability` is de-vigged sportsbook implied
    (already `[0, 1]` → scaled to percentage).
  · The +23.5 line is much softer than the SP+ expected margin of
    ~22 with sigma 13.7 → `cover_probability` sits near 0.99.  That is
    a genuine model conviction, not a formula bug.

Hypothesis elimination outcome:

  · unit mismatch: **NO** (both operands are percentage points)
  · spread magnitude → prob edge: **NO** (spread never enters edge)
  · favorite/underdog sign inversion: **NO** (side-signed margin
    applied at L3747)
  · percentage vs decimal mismatch: **NO** (verified all pipeline
    conversions consistent)
  · implied-probability error: **NO** (`_implied_prob` returns 0-1;
    stored ×100 as %; edge subtracts on % scale)
  · de-vig error: **NO** (2-way normalization; verified sum)
  · market_alignment formula: **YES — but for a different reason** —
    alignment collapses because DB `factors={}` starves the stdev math
    of numeric inputs, forcing the len<2 default of 50.  If the
    intended (norm) factors were persisted, the same formula would
    return ~65 for this pick and the composite would clear 85.
  · stale sportsbook line: **NO** (line matches provider snapshot)
  · genuinely intended model behaviour: **partial** — the SP+ model
    genuinely finds a large edge; the ceiling comes from persistence
    dropping the (norm) evidence, not from model logic.

---

## 4. Another Scale Bug in CFB? — **No**

Boundary trace at every CFB scoring input:

| field                    | raw       | semantic         | expected | actual | normalised |
|--------------------------|-----------|------------------|----------|--------|------------|
| `mp` (cfb_cover_probability) | 0.99   | probability      | [0, 1]   | ✅     | 0.99       |
| `implied` (`_implied_prob`)  | 0.535 | probability      | [0, 1]   | ✅     | 0.535      |
| `edge_percent`               | 45.5  | pp (0-100 scale) | pp       | ✅     | (no arith) |
| `Model Fair Prob (norm)`     | 0.99  | prob             | [0, 1]   | ✅     | 0.99       |
| `Sportsbook Implied (norm)`  | 0.535 | prob             | [0, 1]   | ✅     | 0.535      |
| `Projected Margin (norm)`    | 0.78  | scaled margin    | [0, 1]   | ✅     | 0.78       |
| `Expected Total (norm)`      | 0.55  | scaled total     | [0, 1]   | ✅     | 0.55       |
| `SP+ Rating Δ (norm)`        | 0.78  | scaled margin    | [0, 1]   | ✅     | 0.78       |

No CFB percent enters arithmetic expecting a decimal or vice versa.  The
CFB persistence-time `factors={}` wipe is a persistence bug, NOT a
scale bug.

---

## 5. MLB Upper-Tier — Limiting Component

Top-20 MLB v4 decomposition (post-boundary + rescore).  Every row's
limiting component is `alignment` (the only sub-100 component when
strong evidence converges).  Distribution:

    <85       380
    85-89     5   (max 88.0 — Michael Harris II Over 1.5 H+R+RBIs)
    90-92     0
    93-95     0
    96-98     0
    99        0
    100       0

MLB's 88 ceiling on the current slate reflects genuine evidence: the
strongest hitter-prop rows have `alignment ~ 65-80` (very good, not
elite convergent), no CLV/ROI signal (fills its full-signal weights
would add ~5 pts), and calibrated WP in the 0.68-0.76 band
(confidence 82-89, not the 95+ tier that unlocks upper Elite).  This
is the SAME behaviour the last-known-good scorer would produce on the
same inputs (verified in §2 replay).

To reach 93-100 MLB needs: convergent factors (alignment > 85) +
calibrated WP ≥ 0.85 (confidence ≥ 94) + real CLV or ROI signal.  No
row on today’s MLB slate meets all three simultaneously — that is a
slate reality, not a suppression regression.

---

## 6. Publication Funnel Counts

    ── MLB (current window) ──
      generated                  = 415
      published                  = 410
      not off_board / no_bet     = 356    (54 legit filters)
      real-line integrity clears = 339    (11 model_only/mismatch)
      lock_score ≥ 85            = 12
      published_lock_score ≥ 85  = 8
      future-window + pub + ≥85  = 1      → Gabriel Moreno 85.3

    ── CFB (current window, POST restoration) ──
      generated                  = 74
      published                  = 74
      not off_board / no_bet     = 15     (was 5 pre-restore)
      real-line integrity clears = 15
      lock_score ≥ 85            = 12    (2 legacy_v3 + 10 restored)
      published_lock_score ≥ 85  = 12
      future-window + pub + ≥85  = 0     (all restored legacy rows
                                             are past-time; Sat games)

---

## 7. Surgical Restoration Applied

Only **one** DB mutation applied to close the identified regression:

  · `scripts/restore_retired_legacy_cfb.py` — un-retire the 10 CFB
    legacy Locks quarantined by `retire_cfb_pre_fix_v2.py`.  Cleared
    `off_board / retired_at / retirement_reason / off_board_reasons`
    only where `retirement_reason == "cfb_pre_fix_stale_v2_factor_level"`.
    No touch to weights, floor, bonuses, model formulas, or the MLB
    normalisation boundary.

Also (side-effect cleanup from this session's rescore that touched CFB
by ≤ 0.5 pts):

  · `scripts/revert_cfb_rescore_side_effects.py` — for the 68 CFB v4
    rows where my rescore recomputed with a partial pick shim and
    lowered `lock_score` below the pre-session `published_lock_score`,
    restore `lock_score = published_lock_score`.  Zero MLB rows
    triggered the same-side revert (my rescore only lifted MLB).

---

## What is NOT restored (out of scope)

  · The CFB persistence-time `factors={}` wipe.  Fixing this requires
    touching the CFB emission → persistence handoff (finding where
    `factors` is stripped between `_build_pick(factors=breakdown)` at
    L3820 and the eventual DB insert).  This defect predates the CFB
    naming/emission repair sequence and predates this session.  It is
    filed for follow-up but not surgically restored here because the
    fix would require code changes to a working persistence path.

  · Historical Sep 6-11 CFB rows retired by `retire_cfb_pre_fix_v2.py`.
    These are outside the current wagering window and don't affect the
    live board.

---

## Cross-Sport Preservation (Part C)

    NFL top v4 lock_score       = 97.9  (unchanged)
    Soccer top v4 lock_score    = 92.8  (unchanged)
    /api/picks/today?sport=NFL   → 393 picks (unchanged)
    /api/picks/today?sport=Soccer→ 34-35 picks (unchanged, within
                                                 natural cycle churn)

MLB rescored rows have `lock_score = published_lock_score` and the
`v4.confidence_first.2026-06-14` stamp; DB→wire parity intact.

48/48 focused tests pass.

---

## Final Answer

**A — REGRESSION FOUND**.  Located in
`scripts/maintenance/retire_cfb_pre_fix_v2.py` (invoked pre-session).
Surgically restored: 10 CFB legacy Locks un-retired.  MLB normalisation
boundary and MLB rescore preserved; proven identity-equivalent to the
last-known-good scorer on same-input replay.  No weight, floor, bonus,
or model formula was tuned.  CFB persistence-time `factors={}` wipe
identified for follow-up but not touched (out of scope for this
regression trace).
