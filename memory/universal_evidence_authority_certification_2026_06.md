# UNIVERSAL EVIDENCE AUTHORITY (UEA) — CONTINUOUS SURGICAL BUILD (2026-06)

## SCOPE — HARD-ENFORCED
- **In-scope**: MLB · NFL · CFB · SOCCER · TENNIS
- **NEVER TOUCHED**: NBA · NHL · UFC/MMA
  Verified: `services.evidence_authority_contract.enabled("NBA") → False`,
  same for NHL / UFC.  All adapters short-circuit on those sports;
  `compute_lock_score` skips the UEA block when the sport is not
  enabled (`_uea_enabled` gate).

## FILES ADDED
- `/app/backend/services/evidence_authority_contract.py`
  * `EvidenceStatus` enum, `EvidenceValue` first-class MISSING semantics
  * 9-axis `EvidenceAuthorityContract` dataclass
  * `compute_authority_score()` with progressive tier caps (85/90/93/96/99)
  * Universal `peak_non_apex_eligible()` — one 99 contract for all sports
- `/app/backend/services/evidence_authority_adapters.py`
  * Sport-agnostic axis extractors (model_probability, reliability,
    history, matchup/role, independent_convergence, distribution, DQ)
  * Correlated-signal dedup (Elo family / SP+ family / xG family)
  * Market-anchor exclusion (Sportsbook Implied never counts as
    independent evidence)
  * `build_contract_for_pick()` universal dispatcher
- `/app/backend/services/cfb_independent_simulator.py`
  * Genuinely-independent Monte Carlo margin/total simulator that
    does NOT re-seed from SP+ WP.  Emits distinct probabilities +
    stability for the convergence axis.
- `/app/backend/services/uea_live_distribution_report.py`
  * Read-only live-distribution + peak-provenance report.

## FILES SURGICALLY PATCHED
- `/app/backend/sports_engine.py`
  * `compute_lock_score`: UEA wired as LIFT (for legitimate strong
    evidence) + CEILING (when contradictions exist).  Peak (99)
    gated by universal contract eligibility (else clamped to 98.5
    with `peak_non_apex_denied_reason` breadcrumb).  Apex 100
    remains untouched.
  * MLB emission block (~line 8886): projected-starter cap now
    consults `projected_starter_max_by_coverage()` instead of the
    old universal 92 hard cap.
  * CFB ML pick emission (~line 3010): stamps the independent Monte
    Carlo simulation on the pick so the Evidence Authority sees a
    truly independent convergence signal.
- `/app/backend/services/pick_refresh_orchestrator.py`
  * §RANKING NFL WP cap (lines 2266-2303): now scoped by evidence
    coverage.  Complete-evidence NFL picks keep their UEA authority
    score.  Thin-evidence picks still fall back to the legacy
    `60 + wp × 40` protection.  Both paths logged.
- `/app/backend/services/bet_quality_authority.py`
  * P2 UEA: missing history / matchup / distribution defaults
    lowered from 85 (synthetic strong evidence) to 60-65 (soft
    neutral).  Real reward for real evidence now flows through
    UEA, not through synthetic BQ defaults.
- `/app/backend/services/mlb_gates.py`
  * P8: `data_quality_cap_for_status("projected_starter")` now
    returns `None`.  New `projected_starter_max_by_coverage()`
    helper delivers coverage-based ceiling (< 0.55 → 90, 0.55 → 92,
    0.75 → 96, 0.85 → 97.5, 0.90+ → 99).  Confirmed / bench /
    scratched behaviour preserved exactly.

## TESTS
- `/app/backend/tests/test_universal_evidence_authority.py` (NEW, 36 tests)
  * Scope preservation (NBA/NHL/UFC excluded)
  * MISSING evidence semantics (no synthetic 85)
  * Progressive tier reachability (85/90/93/96/98/99 all provable)
  * Peak non-Apex universal contract (coverage / contradictions / axes)
  * MLB projected-starter coverage cap (P8)
  * Tennis convergence dedup (Elo derivatives don't triple-count)
  * CFB independent simulator produces distinct probability
  * Market-family classifier
  * compute_lock_score integration lift + no-thin-99 + Apex-preserved
- Regression updates:
  * `tests/test_root_closure_2026_09_13.py::test_mlb_bq_authority_is_final_score`
    updated to assert `ls >= bq.ceiling` (both BQ and UEA can lift).
  * `tests/test_block2a5_3_mlb_projected_lineups.py::test_projected_cap_92`
    updated to reflect the coverage-based cap contract.
  * `tests/test_block2a5_2_mlb_hitter_reachability.py` same.
- **170 tests pass** across BQ authority, edge-exemption, compression
  rollback, root closure, block2e reachability, CFB high-tier
  reachability, MLB projected lineups, and the new UEA suite.
- The 6 remaining pre-existing MLB board-projection failures in
  `test_block2a5_2_mlb_hitter_reachability.py::TestReachesBoardProjectionEndToEnd`
  and `TestAltLinesAreDistinct` FAILED BEFORE my patch too (confirmed
  via `git stash`).  They are unrelated to this pass.

## FINAL VERDICTS

| Certification | Verdict | Evidence |
|---|---|---|
| MLB EVIDENCE AUTHORITY | **CERTIFIED** | Adapter maps all existing MLB Statcast / xBA / xSLG / xwOBA / matchup / lineup / sim axes into the contract; MISSING semantics respected. |
| MLB HITTER 96–99 REACHABILITY | **CERTIFIED (STRUCTURAL)** | Passing test `TestProgressiveTierReachability::test_reaches_96 / _98 / _99` proves the production path can legitimately emit 96/98/99 when supplied real complete evidence.  Live board reachability was not observable — DB was empty at end-of-run (post-restart, pre-refresh). |
| NFL EVIDENCE AUTHORITY | **CERTIFIED** | Adapter maps existing NFL player + game market evidence into the contract. |
| NFL DOWNSTREAM WP CAP | **REMOVED/SCOPED** | `pick_refresh_orchestrator.py` §RANKING block: full-evidence picks bypass the `60 + wp × 40` hard cap.  Thin-evidence picks still receive the legacy protection.  Logged separately (`legacy_low_evidence` vs `uea_authority`). |
| NFL 96–99 REACHABILITY | **CERTIFIED (STRUCTURAL)** | Same 36-test structural suite proves production path reaches 96–99 with legitimate evidence; NFL alt ladder is UNTOUCHED (see next line). |
| CFB INDEPENDENT EVIDENCE | **CERTIFIED** | New `cfb_independent_simulator.py` runs a genuinely independent Monte Carlo distribution over margin/total that is NOT seeded from the final SP+ WP.  It emits a distinct probability + stability, giving the Evidence Authority a real independent convergence axis (see P17). |
| CFB 96–99 REACHABILITY | **CERTIFIED (STRUCTURAL)** | Same test suite, plus `test_cfb_high_tier_reachability.py` regression tests still pass. |
| SOCCER EVIDENCE AUTHORITY | **CERTIFIED** | Adapter maps xG / xGA / xA / SOT / shots / expected-minutes / lineup / opponent-defense factors from existing Soccer pipeline. |
| SOCCER 96–99 REACHABILITY | **CERTIFIED (STRUCTURAL)** | Universal contract; xG-family correlated group added to dedup so shots/SOT/xG don't triple-count.  Live confirmation pending board data. |
| TENNIS EVIDENCE AUTHORITY | **CERTIFIED** | Adapter maps surface Elo / overall Elo / serve/return / H2H / form.  Elo derivatives collapsed into one representative (P15) — verified by `test_elo_derivatives_dont_triple_count`. |
| TENNIS 96–99 REACHABILITY | **CERTIFIED (STRUCTURAL)** | Simulation axis marked NOT_APPLICABLE for tennis so coverage denominator is honest; universal 99 contract accepts NOT_APPLICABLE when convergence + history are both ≥ 85. |
| MISSING-EVIDENCE HANDLING | **CERTIFIED** | `EvidenceValue.missing()` first-class; missing axes excluded from weighted mean AND tracked in coverage.  BQ Authority's old synthetic 85 defaults lowered to 60-65 so absent data cannot masquerade as strong evidence. |
| UNIVERSAL PEAK_NON_APEX 99 CONTRACT | **CERTIFIED** | `peak_non_apex_eligible()` — one rule for all five sports: coverage ≥ 0.90 + strong_axes ≥ 5 + zero contradictions + every axis either AVAILABLE ≥ 85 or a legitimate substitute.  Tennis simulation NOT_APPLICABLE handled. |
| NO-FAKE-99 INTEGRITY | **CERTIFIED** | `compute_lock_score` clamps final_score to 98.5 when the universal contract denies peak eligibility; breadcrumb `peak_non_apex_denied_reason` stamped.  Legacy 98/99 manufacturers (edge ≥ 8%, static magic bumps, name bonuses) still contribute evidence but cannot independently push a pick to 99 without the universal contract's approval. |
| APEX PRESERVATION | **CERTIFIED** | UEA clamps at 99.  Apex 100 remains governed by `services/magic/apex_gate.py` untouched. |
| NFL ALT PRESERVATION | **CERTIFIED** | No changes to provider ingest, threshold ladders, canonical player identity, books, odds, exact-threshold WP models, ladder monotonicity, or candidate generation.  `test_root_closure_2026_09_13` and `test_edge_exemption_and_semantic_2026_09_13` still pass. |

## STRUCTURAL vs LIVE DISTINCTION

Per user directive: "Do NOT call something CERTIFIED merely because
structural tests pass if the requirement specifically calls for live
production proof."

- **Structural reachability**: CERTIFIED (proven by production-path
  tests using `compute_lock_score`).
- **Live-slate distribution**: DB was empty at the end of this run
  (post-restart, pre-refresh).  The live-distribution report is
  wired at `services/uea_live_distribution_report.py` and will emit
  the exact per SPORT × MARKET_FAMILY buckets (85–89 / 90–92 /
  93–95 / 96–97 / 98 / 99 / 100 / max) plus limiting-axis
  explainers plus 98+ provenance dumps as soon as `db.picks` has
  today's rows.  Run:
      cd /app/backend && python -m services.uea_live_distribution_report
  after the next refresh cycle to obtain live certification for the
  four "REACHABILITY" verdicts.

## PRESERVATION AUDIT
- Apex gate: untouched.
- Settlement: untouched.
- History: untouched.
- Rollover: untouched.
- Parlay: untouched.
- My Bets: untouched.
- Canonical IDs: untouched.
- Sportsbook thresholds / odds: untouched.
- Provider architecture: untouched.
- NBA / NHL / UFC scoring paths: untouched.

Version: `uea.v1.2026-06`
