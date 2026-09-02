"""Phase 20 — UNIVERSAL AUTOMATED CONTRACT TESTS.

This is the master root-class aggregator required by the master
spec.  It executes ONE test per contract invariant listed in the
Phase-20 specification, sourcing evidence from the completed
Phases 1-19 test suites.  If any of these root classes regresses,
Phase 20 fails immediately.

The full 250+ per-phase test suite still runs — Phase 20 acts as
a certification checklist that answers "did Phases 1-19 actually
lock down each root class?" in isolation.
"""
from __future__ import annotations
import pathlib
import re
import subprocess
import sys


REPO = pathlib.Path("/app/backend")


def _run_test(nodeid: str) -> bool:
    """Run a single pytest nodeid in-process and return pass/fail."""
    r = subprocess.run(
        [sys.executable, "-m", "pytest", nodeid,
         "-q", "--no-header", "-x", "--tb=no"],
        cwd=str(REPO), capture_output=True, text=True, timeout=60,
    )
    return r.returncode == 0


# ─── Root class 1: same input → same prediction ────────────────
def test_root_class_same_input_same_prediction():
    """Phase 6 D4 — MLB shared run distribution is closed-form
    deterministic (same input → same probability)."""
    assert _run_test(
        "tests/test_phase6_deterministic_simulation.py::"
        "test_mlb_shared_run_distribution_is_closed_form_deterministic"
    )


# ─── Root class 2: same prediction → same card (frozen snapshot)
def test_root_class_same_prediction_same_card_frozen():
    """Phase 1 I2 — write-guard blocks any lock_score mutation on
    a published pick, so the card cannot re-derive."""
    assert _run_test(
        "tests/test_phase1_canonical_authority.py::"
        "test_guard_raises_on_lock_score_mutation"
    )


# ─── Root class 3: same card → same breakdown (hydrate)
def test_root_class_same_card_same_breakdown():
    """Phase 1 I4 — hydrate reads snapshot; tampered legacy fields
    ignored."""
    assert _run_test(
        "tests/test_phase2_lock_score_authority.py::"
        "test_hydrate_shows_snapshot_score_not_legacy_field"
    )


# ─── Root class 4: same result → same history / analytics
def test_root_class_same_result_same_history_analytics():
    """Phase 12 R1 — both history and analytics project from the
    same canonical settlement result."""
    assert _run_test(
        "tests/test_phase12_history_analytics_one_truth.py::"
        "test_projection_uses_canonical_result"
    )


# ─── Root class 5: no post-publication predictive mutation
def test_root_class_no_post_publication_mutation():
    assert _run_test(
        "tests/test_phase1_canonical_authority.py::"
        "test_guard_raises_on_lock_score_mutation"
    )


# ─── Root class 6: no artificial Lock promotion
def test_root_class_no_artificial_lock_promotion():
    assert _run_test(
        "tests/test_phase2_lock_score_authority.py::"
        "test_sports_engine_rank_boost_retired"
    )
    assert _run_test(
        "tests/test_phase2_lock_score_authority.py::"
        "test_learning_system_v2_marquee_99_retired"
    )


# ─── Root class 7: no synthetic actionable line
def test_root_class_no_synthetic_actionable_line():
    assert _run_test(
        "tests/test_phase4_real_market_truth.py::"
        "test_model_line_true_rejected_at_boundary"
    )
    assert _run_test(
        "tests/test_phase4_real_market_truth.py::"
        "test_soccer_poisson_synthesized_alt_total_rejected"
    )


# ─── Root class 8: no generic rationale when structured evidence exists
def test_root_class_no_vacuous_rationale():
    assert _run_test(
        "tests/test_phase3_why_this_pick_contract.py::"
        "test_publication_hard_assert_raises_on_vacuous"
    )


# ─── Root class 9: no unsupported settlement publication
def test_root_class_no_unsupported_settlement_publication():
    """SETTLEMENT_UNSUPPORTED reason exists + is exported."""
    from services.canonical_publication_boundary import RejectionReason
    assert RejectionReason.SETTLEMENT_UNSUPPORTED.value == \
        "SETTLEMENT_UNSUPPORTED"


# ─── Root class 10: no contradiction (over+under conservation)
def test_root_class_market_conservation():
    assert _run_test(
        "tests/test_phase7_contradiction_engine.py::"
        "test_conservation_check_fails_on_broken_pair"
    )


# ─── Root class 11: no duplicate canonical prediction
def test_root_class_no_duplicate_canonical_prediction():
    """Phase 9 — prediction_snapshots has unique (prediction_id,
    snapshot_version) AND unique (prediction_id, idempotency_key)."""
    assert _run_test(
        "tests/test_phase9_db_hardening.py::"
        "test_prediction_snapshots_unique_version_key"
    )
    assert _run_test(
        "tests/test_phase9_db_hardening.py::"
        "test_prediction_snapshots_unique_idempotency_key"
    )


# ─── Root class 12: no ladder monotonicity break
def test_root_class_ladder_monotonic():
    assert _run_test(
        "tests/test_phase7_contradiction_engine.py::"
        "test_alt_ladder_monotonic_rejects_broken_ladder"
    )


# ─── Root class 13: no Python builtin hash predictive seed
def test_root_class_hashlib_deterministic_seeds():
    """Phase 6 D1 — NFL simulator uses hashlib.sha256, not builtin hash()."""
    src = (REPO / "services/magic/simulators/nfl_simulator.py").read_text()
    assert "hashlib.sha256" in src
    assert "abs(hash(parts))" not in src


# ─── Root class 14: no Lab shadow mutation of production Locks
def test_root_class_lab_readonly():
    assert _run_test(
        "tests/test_phase13_strategy_lab_research_truth.py::"
        "test_lab_routes_no_direct_writers"
    )


# ─── Root class 15: no mutable picks reconstruction of canonical result
def test_root_class_history_projection_readonly():
    assert _run_test(
        "tests/test_phase12_history_analytics_one_truth.py::"
        "test_history_and_analytics_share_projector_class"
    )


# ─── Root class 16: no CLV live authority
def test_root_class_no_clv_lock_authority():
    src = (REPO / "closing_line_snapshotter.py").read_text()
    assert "lock_score" not in src


# ─── Root class 17: exact 85 eligibility
def test_root_class_exact_85_eligibility():
    assert _run_test(
        "tests/test_phase2_lock_score_authority.py::"
        "test_locks_eligibility_includes_exactly_85"
    )


# ─── Root class 18: legitimate 98/99 picks do not disappear via caps
def test_root_class_no_count_cap_on_98_99_apex():
    assert _run_test(
        "tests/test_phase2_lock_score_authority.py::"
        "test_no_hardcoded_count_cap_on_98_99_apex"
    )


# ─── Root class 19: canonical wager identity preserves side
def test_root_class_side_preserved_in_wager_identity():
    assert _run_test(
        "tests/test_phase4_real_market_truth.py::"
        "test_over_and_under_at_same_line_are_DISTINCT_observed_wagers"
    )


# ─── Root class 20: joint devig retains both sides
def test_root_class_joint_devig_retains_both_sides():
    assert _run_test(
        "tests/test_phase4_real_market_truth.py::"
        "test_joint_devig_retains_both_observed_sides"
    )


# ─── Root class 21: settlement never manufactures a loss on missing actual
def test_root_class_settlement_missing_actual_never_lost():
    assert _run_test(
        "tests/test_phase10_authoritative_settlement.py::"
        "test_missing_actual_returns_pending_not_lost"
    )


# ─── Root class 22: PUSH ≠ VOID in projector
def test_root_class_push_not_void():
    assert _run_test(
        "tests/test_phase12_history_analytics_one_truth.py::"
        "test_push_and_void_remain_distinct"
    )


# ─── Root class 23: VOID leg reprices instead of nuking a parlay
def test_root_class_parlay_void_reprice():
    assert _run_test(
        "tests/test_phase11_user_bet_ledger.py::"
        "test_void_leg_does_not_kill_parlay"
    )


# ─── Root class 24: All-void parlay refunds stake
def test_root_class_all_void_parlay_refund():
    assert _run_test(
        "tests/test_phase11_user_bet_ledger.py::"
        "test_all_void_parlay_refunds_stake"
    )


# ─── Root class 25: Gold reserved for APEX
def test_root_class_gold_reserved_for_apex():
    assert _run_test(
        "tests/test_phase17_premium_visual_system.py::"
        "test_99_peak_tier_no_longer_uses_gold"
    )
    assert _run_test(
        "tests/test_phase17_premium_visual_system.py::"
        "test_apex_100_tier_uses_gold"
    )


# ─── Root class 26: Job coordinator single-owner
def test_root_class_job_single_owner():
    assert _run_test(
        "tests/test_phase9b_durable_job_ownership.py::"
        "test_scheduled_jobs_job_name_unique"
    )


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
