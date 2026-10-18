# Perklocks — MASTER SURGICAL FIX BUILD · FINAL CLOSURE REPORT

**Session:** Master Surgical Fix Build (continuous pass, no mid-step approvals)
**Base SHA:** `af6b714cd4f323dc9cc20736c607fb8965695442`
**Working head:** `daa9f5a6` + 1 uncommitted surgical patch (not pushed, not deployed)
**Scope:** Phase 7 / 8 / 9 cutover blocker triage → surgical repair → FINAL DONE path
**Report time:** 2026-10-03 (continuous session)
**Status:** ✅ **FINAL DONE — ALL CUTOVER BLOCKERS CLEARED**

---

## 1 · Executive Summary

The previous agent discovered **7 failing backend tests** acting as hard blockers for
the Phase 7/8/9 production cutover. All 7 have been **surgically closed in a single
continuous pass** without:

- pushing or deploying,
- altering Lock Score weights, thresholds, or formulas,
- faking telemetry (HONEST ZERO preserved),
- starting a new migration session or resetting the existing one,
- destructive deletes,
- Production writes.

Every pre-existing P2 failure was **confirmed pre-existing by stash-and-re-run**
and left untouched per the surgical scope rule.

**Final footprint:** 3 files changed · +53 / −3 lines.

---

## 2 · The 7 Blockers — root cause, fix, proof

### 2A · Identity Gate — 3 failures on `mlb_hitter_wrong_game`

| Test | File | Prior verdict |
|------|------|---------------|
| `test_9Q_negative_identity_fixture_rejected[mlb_hitter_wrong_game]` | `tests/test_phase9_consumer_surface_closure.py` | returned `NOT_APPLICABLE` instead of `PLAYER_EVENT_IDENTITY_MISMATCH` |
| `test_9F_negative_identity_blocked_at_publication_boundary[mlb_hitter_wrong_game]` | same | returned `PUBLISHED` instead of `REJECTED` |
| `test_9L_negative_identity_blocked_by_canonical_eligibility[mlb_hitter_wrong_game]` | same | `is_canonical_eligible` returned `True` for an identity-mismatch MLB hitter pick |

**Root cause.** The 2026-08-27 game-total / spread short-circuit in
`services/player_event_identity_gate.py::_is_player_market` was unconditional:

```python
if market.startswith("total ") or market.endswith(" spread"):
    return False
```

MLB player props such as **`Total Bases Over 1.5`** legitimately start with `total `
yet are player markets (they always carry `player_name`). The guard silently
classified them as team-side (`NOT_APPLICABLE`), bypassing the entire player→event
identity check — exactly the Phase-9 defect class the gate was built to prevent.

**Surgical fix.** `services/player_event_identity_gate.py` — gate the short-circuit
on absence of a `player_name` / `player` field. Comment in the module already stated
the invariant: *"Team markets NEVER carry a player_name / player field"* — we now
honour it in the code:

```python
_has_player_field = bool(pick.get("player_name") or pick.get("player"))
if not _has_player_field and (
    market.startswith("total ") or market.endswith(" spread")
):
    return False
```

- Preserves CFB/NFL/NBA/NHL/MLB **game-total** protection (no regressions).
- Correctly classifies MLB "Total Bases", NBA "Total Rebounds", etc. as player props
  so the home/away vs player_team match runs.
- Zero changes to Lock Score, scoring, or publication thresholds.

**Proof.** All 3 identity tests pass; all 7 positive-identity controls (`9R`) and the
unresolved-identity contract (`9D`) still pass.

---

### 2B · Parlay 2.0 Eligibility — 4 failures (8K, 8X, 8AM-A, 8AM-G)

| Test | File | Prior verdict |
|------|------|---------------|
| `test_8K_extreme_chalk_gated_by_standard_edge_floor` | `tests/test_phase8_parlay2_closure.py` | rejection reason was `"not canonically eligible"` instead of citing **edge** |
| `test_8X_below_85_leg_rejected_by_standard_eligibility` | same | rejection reason was `"not canonically eligible"` instead of citing **lock** |
| `test_8AM_proof_A_strong_independent_2leg_parlay` | same | `build_top_parlays` returned `[]` |
| `test_8AM_proof_G_extreme_chalk_does_not_dominate_every_parlay` | same | `build_top_parlays` returned `[]` |

**Root cause #1 — Test fixture missed the 2026-10-02 real-line integrity tightening.**
`_base_leg` stamped `provider="test_provider"` but **no `odds_source`**. The hardened
`services.canonical_publication_boundary._real_line_state` (2026-10-02 P0) now
classifies unknown-provenance picks as `SYNTHETIC`, so every parlay fixture leg
failed `is_canonical_eligible` — the fixture, not the engine, was out of date.

**Root cause #2 — Phase-8K "extreme chalk" semantic was absent from the engine.**
The 2026-06-21 Parlay 3.0 Universal Closure correctly turned edge into a
RANKING-only signal (fix for optimizer starvation). But Phase 8K requires that
**structurally heavy chalk cannot buy entry with razor-thin edge** — a -700 price
with 1.5 % edge gets its win_prob inflated BY the price itself, so admitting it on
lock+win_prob alone violates the "extreme price cannot manufacture entry" rule.

**Root cause #3 — Fixtures A1 / A12 under-specified published_lock_score.**
`_base_leg` sets `published_lock_score=90.0`. Fixtures A1 ("high-lock extreme chalk")
and A12 ("below-85 lock") overrode only `lock_score`, so the canonical read
(`published_lock_score`-first per Phase-1c) saw 90 and bypassed each fixture's
intent.

**Surgical fixes.** All in `tests/test_phase8_parlay2_closure.py` + a NARROW engine
guard in `parlay_optimizer.py`.

#### (i) `tests/test_phase8_parlay2_closure.py` · `_base_leg`

```python
"book_odds": -150,
"odds_source": "the_odds_api",   # ← aligns fixture with real-line integrity
"provider": "test_provider",
```

#### (ii) A1 `_extreme_chalk` — honour its own "high-lock" intent

```python
_base_leg(id="A1_extreme_chalk", event="Dodgers @ Rockies",
          event_id="evt_lad_col", selection="Dodgers",
          home_team="Rockies", away_team="Dodgers",
          book_odds=-700, win_probability=88.0,
          lock_score=97.0, published_lock_score=97.0,
          edge_percent=1.5, roi_bucket_pct=2.0)
```

#### (iii) A12 `_below85` — honour its "below-85" intent

```python
_base_leg(id="A12_below85", sport="mlb", league="MLB",
          event="Astros @ Rangers", event_id="evt_hou_tex",
          home_team="Rangers", away_team="Astros",
          market="Moneyline", selection="Astros",
          book_odds=-105, win_probability=54.0,
          lock_score=82.0, published_lock_score=82.0,
          edge_percent=2.0)
```

#### (iv) `test_8AM_proof_J` · also set `published_lock_score=80.0` on the mutated apex leg

Honours the test's "below-85 lock" intent against the canonical read; it only
touched `lock_score`.

#### (v) `parlay_optimizer.is_eligible_leg` — narrow Phase-8K extreme-chalk edge floor

```python
# Phase 8K EXTREME-CHALK EDGE FLOOR — closes "extreme price manufactures entry"
# WITHOUT reintroducing a universal edge gate (that caused the Parlay-3.0
# starvation we deliberately removed in 2026-06-21).
_EXTREME_CHALK_AMERICAN: int = -300
_EXTREME_CHALK_MIN_EDGE: float = 3.0
if (
    _bo_val < 0
    and _bo_val <= _EXTREME_CHALK_AMERICAN
    and not _is_alt_leg
    and edge < _EXTREME_CHALK_MIN_EDGE
):
    return False, (
        f"edge {edge:+.1f}% < {_EXTREME_CHALK_MIN_EDGE:+.0f}% "
        f"for extreme chalk ({_bo_val})"
    )
```

- Fires **only** for American ≤ −300 that is **not** an `is_alt` ladder leg.
- Alt-chalk ladders remain exempt via the pre-existing `is_alt` carve-out
  (comment-preserved).
- Normal-priced legs are untouched → anti-starvation preserved.
- **No Lock Score weight / threshold modified.** This is an edge gate scoped
  to heavy chalk, not a lock gate.

---

## 3 · Verification Matrix

### Targeted blocker suite

```
tests/test_phase9_consumer_surface_closure.py  — 55 passed (incl. all 3 identity blockers)
tests/test_phase8_parlay2_closure.py           — 20 passed (incl. all 4 parlay blockers)
tests/test_canonical_cutover.py                — 20 passed
tests/test_canonical_cutover_batch_manifest.py —  5 passed
tests/test_canonical_cutover_r2_schema.py      —  5 passed
tests/test_canonical_cutover_r3_safety.py      —  3 passed
───────────────────────────────────────────────────────────────
TOTAL:                                           108 passed, 0 failed
```

### Broader Phase 7/8/9 cutover family

```
test_phase7_contradiction_engine.py
test_phase7_rollover_closure.py
test_phase7_spread_supersession.py
test_phase8_canonical_edge.py
test_phase8_parlay2_closure.py
test_phase9_consumer_surface_closure.py
test_phase9_db_hardening.py
test_phase9b_durable_job_ownership.py
test_canonical_cutover.py                      ← see §4 note (test-isolation only)
test_canonical_cutover_batch_manifest.py
test_canonical_cutover_r2_schema.py
test_canonical_cutover_r3_safety.py
───────────────────────────────────────────────
147 passed, 0 cutover regressions
```

### Downstream parlay / identity regression sweep (clean-room comparison)

Comparing failing-test list **with my changes** vs **after `git stash`**:

```
Pre-existing P2 (identical before & after my changes):
  test_parlay_optimizer.py          — 3 failures
  test_parlay_optimizer_dedupe.py   — 1 failure
  test_phase5_universal_identity.py — 1 failure
  test_main40_frozen_identity_priority.py — 3 failures
  test_parlay_v11.py                — 1 failure
  test_main_board_strictness_85_inclusive.py — 8 failures
───────────────────────────────────────────────────────────
New regressions introduced by my changes: 0
```

---

## 4 · Explicitly Out-of-Scope (left untouched by directive)

Per the handoff rule *"Only fix pre-existing issues if they directly block
final closure or Phase 7/8 cutover"*, the following P2 issues were **confirmed
pre-existing** (same before & after my patch) and were **not touched**:

| Area | Status |
|------|--------|
| `services/settlement_authority.py` global collection error (`SystemExit: 2`) | Pre-existing; blocks only `tests/test_p041_downstream_truth.py` collection. Not a cutover blocker. |
| `tests/test_nhl_historical_intelligence.py` (17 failures) | Pre-existing. Out of scope. |
| `tests/test_main_board_strictness_85_inclusive.py` (8 failures) | Pre-existing. Caused by minimal picks lacking `odds_source` colliding with 2026-10-02 real-line tightening. Fixture gap, not a cutover blocker. |
| `tests/test_canonical_cutover.py::test_routing_canonical_when_flag_true` | Passes in isolation and under `-p no:randomly`. Fails only via cross-test state pollution from `test_phase7_rollover_closure.py` touching `db_mod._state`. Test-isolation hygiene issue, not a cutover routing defect. |
| `tests/test_parlay_optimizer.py` + `test_parlay_optimizer_dedupe.py` + `test_parlay_v11.py` live-data skips | Pre-existing; depend on live canonical slate size. Not Phase 8 cutover blockers. |
| `tests/test_main40_frozen_identity_priority.py` (3 failures) | Pre-existing. Independent of the identity-gate change (verified by stash). |
| `tests/test_alt_parlay_deep_dive_fix.py` + `tests/test_csl_espn_live_iter64.py` | Collection errors due to missing `EXPO_PUBLIC_BACKEND_URL` env; not cutover-related. |
| Stale `MongoClient after close` on `/api/auth/login` | Pre-existing stale-pool defect flagged in handoff; not touched. |
| `main39_slice2.static.runner.js`, `main40_native_reset.runner.js` | Frontend static runners; untouched (surgical scope = backend cutover). |

---

## 5 · Files Changed (full surgical footprint)

```
 backend/parlay_optimizer.py                    | 31 ++++++++++++++++++++++++
 backend/services/player_event_identity_gate.py | 15 ++++++++++--
 backend/tests/test_phase8_parlay2_closure.py   | 10 +++++++--
 3 files changed, 53 insertions(+), 3 deletions(-)
```

- **Zero** production routes modified.
- **Zero** DB migrations run / started / reset.
- **Zero** DB writes.
- **Zero** config / `.env` / Lock-Score formula / threshold edits.
- **Zero** telemetry fabrication; HONEST ZERO contract preserved.

---

## 6 · Guardrail Attestation

| Guardrail | Status |
|-----------|--------|
| Base SHA preserved (`af6b714c…` ancestry) | ✅ |
| No push | ✅ |
| No deploy | ✅ |
| No Lock Score weight / threshold / formula change | ✅ |
| HONEST ZERO preserved for thin / stub engines | ✅ |
| No new migration session started | ✅ |
| Existing migration session not reset | ✅ |
| No destructive deletes | ✅ |
| No Production writes | ✅ |
| Continuous pass — no mid-step approvals requested | ✅ |

---

## 7 · Path to FINAL DONE

All Phase 7/8/9 cutover **blockers are now green** on the current working head.
Pre-existing P2 failures listed in §4 are deliberately left untouched per the
surgical scope rule and are not cutover gates.

**Recommended next step** (deferred to the user per the "no push, no deploy"
directive):

1. User reviews this report + the 3-file diff.
2. User commits the surgical patch (or asks a follow-up pass to split it into
   two commits: one for the identity-gate fix, one for the parlay fixture +
   Phase-8K edge floor).
3. On explicit user approval, the fix moves through the user's normal cutover
   publishing pipeline.

**Awaiting user confirmation to proceed.** No further automated action taken.

— End of report —
