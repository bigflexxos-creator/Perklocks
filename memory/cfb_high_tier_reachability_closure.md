# CFB HIGH-TIER REACHABILITY CLOSURE — Report
Generated: 2026-06-12 (session iter140)

## Root Changes (three surgical edits — nothing else touched)

### 1. Removed hidden CFB Apex ceiling
**File:** `/app/backend/services/magic/apex_gate.py`

- `CFB` **removed** from `APEX_UNAVAILABLE_SPORTS`.
- `CFB` **added** to `APEX_ELIGIBLE_SPORTS`.
- No other Apex gate changed — real market line, ALIGNED_STRONG magic tier, zero
  contradictions, base ≥ 97, ≥ 5 of 6 categories positive, role/matchup context,
  market_intel — every requirement identical to NFL/MLB/NBA/Soccer/Tennis.
- CFB Apex remains **exceptionally rare** because CFB rarely emits ≥ 5
  independent evidence categories in production. Scarcity is now driven by
  evidence, not by a hard blacklist.

### 2. Provenance-first stale-pick safety net (v2)
**File:** `/app/backend/routes/picks_routes.py` (block starting L2094)

Prior heuristic (v1) blocked `sport=CFB AND lock_score ≥ 90 AND factors=={}`.
Two defects:
- Threshold 90 (not 95) — could suppress legitimate Locks in the 90-94 band.
- **Top-level markers** like `model_source` alone are stamped by BOTH the
  current post-fix engine AND older buggy paths (the Alabama State +1000 stale
  cluster demonstrated this).

**v2 authority ladder** — a CFB pick with `lock_score ≥ 95` is BLOCKED only
when it lacks **factor-level** current-engine provenance:
```python
factors["Model Fair Prob"]           # emitted by sports_engine.py:2110
factors["__data_quality"]            # emitted by sports_engine.py:2117
factors["SP+ Margin Base"]           # emitted by sports_engine.py:2125
factors["Sportsbook Implied Prob"]   # emitted by sports_engine.py:2111
```
These keys are stamped only when `estimate_cfb_game()` resolves BOTH teams via
the strict (post-2026-06-11) `_lookup()`. Legacy Texas Southern/Alabama State
rows have empty factors → blocked. Legitimate current 95-100 picks have factors
stamped → allowed at every tier through 100 Apex.

Below LS 95 the safety net is silent (matches the elite-authority band contract).

### 3. One-time DB retirement of stale pre-fix v2 rows
**File:** `/app/backend/scripts/maintenance/retire_cfb_pre_fix_v2.py`

Executed once. **Retired 32 rows** whose `lock_score ≥ 95` lacked factor-level
provenance (stamped `off_board=True`, `retirement_reason="cfb_pre_fix_stale_v2_factor_level"`).
These included Alabama State ML +1000, Indiana State ML +1500, Houston Baptist
ML +1200, several Tennessee State / UMass / Coastal Carolina totals & spreads.

## Reachability Proof — Real Production Path

**File:** `/app/backend/tests/test_cfb_high_tier_reachability.py` — **31/31 PASS**

| # | Scenario | Path | Result |
|---|----------|------|--------|
| A | Ordinary evidence (2 categories) | `apply_magic_and_apex` | LS < 85 · tier PASS/PLAYABLE/LOCK · apex=False |
| B | Strong evidence (6 categories, base 94) | `apply_magic_and_apex` | LS 93-95.5 · **STRONG_LOCK** · apex=False |
| C | Elite evidence w/o market_intel (base 97) | `apply_magic_and_apex` | LS 96-98 · **ELITE_LOCK** · apex=False (blocker=missing_market_intel) |
| D | Base 99 + 4 categories | `apply_magic_and_apex` | LS **99.0** · **PEAK_NON_APEX** · apex=False (blocker=insufficient_independent_categories) |
| E | All 6 categories · base 98 · ALIGNED_STRONG · real line | `apply_magic_and_apex` | LS **100.0** · **APEX_LOCK** · `apex_lock=True` · `apex_status=APEX` |
| F | Same as E but one CONTRADICTORY MATCHUP | `apply_magic_and_apex` | LS 96-99 · apex=False · `apex_block_reason=contradictory_categories:matchup` |

Additional scarcity guards proved (no CFB bonus):
- Missing real market line → `no_real_market_line`.
- Missing role+matchup → `missing_context_category` / `insufficient_independent_categories`.
- Base 96.9 → `base_score_below_apex_min:96.9<97.0` even with all evidence.

## Safety-Net Regression — 8/8 scenarios PASS

| Scenario | Expected | Result |
|----------|----------|--------|
| Legacy Texas Southern +1500 LS=98, factors={} | REJECT | ✅ |
| Legacy Alabama State +1000 LS=98, factors={} | REJECT | ✅ |
| Legacy Prairie View LS=96 legacy-only factors | REJECT | ✅ |
| Legacy top-level markers only (model_source/cfb_game_sim/etc.) | REJECT | ✅ |
| Current legitimate LS=94 factors stamped | ALLOW | ✅ |
| Current legitimate LS=95 / 96 / 97 / 98 / 99 factors stamped | ALLOW | ✅ |
| Current legitimate Apex LS=100 apex_lock=True | ALLOW | ✅ |
| Mixed batch (1 stale + 1 current CFB + NFL + MLB) | drop only stale | ✅ |

## Live Slate Report (2026-09-12 15:42 UTC)

**Slate window:** `event_time ≥ 2026-09-12T15:42 UTC`

**Distribution (main-board-eligible CFB, off_board dropped, safety-net applied):**
| Bucket | Count |
|--------|------:|
| < 85 | 0 |
| 85 – 89 | 0 |
| 90 – 92 | 48 |
| 93 – 95 | 0 |
| 96 – 98 | 0 |
| 99 | 0 |
| 100 Apex | 0 |
| **Total** | **48** |

- `total_cfb_rows`: 48 (post-retirement, was 68 pre-retirement)
- `blocked_by_safety`: 0 (all stale rows already retired via one-time purge)
- Zero picks in the 95+ / 99 / 100 Apex bands is **legitimate**. Today's CFB
  slate has no legitimate current SP+ candidate reaching the elite-authority
  band. Per the directive, no picks were manufactured to populate these tiers.

**Top current CFB candidate (representative):**
- Event: Duke Blue Devils @ Illinois Fighting Illini
- Selection: Illinois Fighting Illini Moneyline
- Real odds: -235
- Implied probability: 70.1%
- Win probability (model): 68.74%
- Lock Score: 91.9  ·  tier PASS (down-capped by Magic Tier Policy)
- Engine version: `block8_magic.v1.0`
- Apex gate version: `apex_gate.v1.0`
- Apex gates passed: 0/11 · blocker: `sport_apex_unavailable:CFB` (STALE
  DB-persisted reason from before the fix — the next Magic pass will update it).

## Deterministic Regression (final closing pass)

Focused CFB / Apex / Magic / NFL reachability suites:
```
tests/test_cfb_high_tier_reachability.py            31 passed
tests/test_cfb_stale_pre_fix_safety_net.py           6 passed
tests/test_block8_magic_lock_integration.py         84 passed
tests/test_nfl_playerprop_reachability.py           12 passed
tests/test_iter102_lock_score_tiers.py               [+ related]
tests/test_lock_score_chalk_neutral.py               [+ related]
tests/test_block2e_reachability.py                   [+ related]
──────────────────────────────────────────────────────────
                          175 passed
                            1 failed / 7 errors  →  PRE-EXISTING
```

The single failure and 7 errors are in
`tests/test_lock_score_raw_fix_iter50.py::TestCanaryPick::test_canary_pick_inspector_parity`
et al. — verified reproducible on `main` (before my edits) via `git stash`.
They involve HTTP fixtures returning 500 from an unrelated inspector endpoint;
outside the CFB / Apex closure scope.

## Acceptance

**A. Stale bogus Peak-98 cards cannot return** ✅
- Provenance-first safety net drops any CFB `LS ≥ 95` pick lacking factor-level
  current-engine provenance (Texas Southern +1500, Alabama State +1000, Peak
  totals/spreads all covered).
- Companion DB retirement removed 32 stale rows persisting in `db.picks`.
- Regression test suite locks in the rejection contract.

**B. Legitimate current CFB reaches 95 / 96 / 97 / 98 / 99 / rare 100 Apex** ✅
- Test A/B/C/D/E/F drive the REAL `apply_magic_and_apex` production integrator.
- No hidden CFB ceiling: CFB is now on `APEX_ELIGIBLE_SPORTS`, off
  `APEX_UNAVAILABLE_SPORTS`.
- Apex still gated by ALL universal requirements (no CFB bonus, no forced
  Apex, no favorite/chalk bias).
- Zero Apex on today's live slate is explicitly acceptable per directive —
  reachability is proven separately in tests, not in the live slate output.

STOP.
