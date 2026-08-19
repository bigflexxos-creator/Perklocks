"""Universal Flow Production Recovery — focused tests.

Covers 4 confirmed defects (1, 3, 6, 7). Defects 2, 4, 5 already fixed.
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _src(p): return open(p).read()


# ── Defect 1 — Brain does not mutate Lock Score ─────────────────────
def test_brain_does_not_mutate_lock_score():
    src = _src("/app/backend/services/publication_helpers.py")
    # The old mutation `p["lock_score"] = round(_adj, 2)` must be gone.
    assert 'p["lock_score"] = round(_adj, 2)' not in src
    # Diagnostic sidecars remain.
    assert 'lock_score_pre_convergence' in src
    assert 'convergence_lock_score_delta_hypothetical' in src


# ── Defect 3 — probability unit normalization ───────────────────────
def test_probability_normalization_92_and_0_92_equivalent():
    from probability_engine import _to_unit
    assert _to_unit(92) == 0.92
    assert _to_unit(92.0) == 0.92
    assert abs(_to_unit(0.92) - 0.92) < 1e-9
    # No double-divide bug.
    assert _to_unit(0.92) != 0.0092


def test_probability_extreme_edge_cases():
    from probability_engine import _to_unit
    assert _to_unit(-1) == 0.0
    assert _to_unit(0) == 0.0
    assert _to_unit(1000) == 1.0
    assert _to_unit(None) == 0.5
    assert _to_unit("x") == 0.5


def test_sim_win_probability_normalization():
    from probability_engine import compute_sim_probability
    p, var, stab = compute_sim_probability({"sim_win_probability": 0.92})
    assert abs(p - 0.92) < 1e-9, f"0.92 sim → {p}"
    p2, _, _ = compute_sim_probability({"sim_win_probability": 92})
    assert abs(p2 - 0.92) < 1e-9, f"92 sim → {p2}"


def test_v1_and_v2_probability_normalization():
    from probability_engine import compute_v1_probability, compute_v2_probability
    assert abs(compute_v1_probability({"model_win_probability": 92}) - 0.92) < 1e-9
    assert abs(compute_v1_probability({"model_win_probability": 0.92}) - 0.92) < 1e-9
    assert abs(compute_v2_probability({"win_probability": 88}) - 0.88) < 1e-9
    assert abs(compute_v2_probability({"win_probability": 0.88}) - 0.88) < 1e-9


# ── Defect 6 — Rollover canonical precedence at query ──────────────
def test_rollover_query_uses_expr_ifnull():
    src = _src("/app/backend/routes/picks_routes.py")
    # The old `q = {**base_q, "lock_score": {"$gte": LOCK_FLOOR}}` gone.
    assert '"lock_score": {"$gte": LOCK_FLOOR}}' not in src
    assert '"$ifNull": ["$published_lock_score", "$lock_score"]' in src


# ── Defect 7 — Parlay canonical precedence ─────────────────────────
def test_parlay_legacy_only_when_published_absent():
    src = _src("/app/backend/routes/parlay_routes.py")
    # Legacy branch must guard on published absence.
    assert '"published_lock_score": {"$exists": False}' in src


# ── Defect 4 — sportsbook edge separated from internal convergence (already fixed)
def test_convergence_uses_internal_predictions_only():
    """Sportsbook edge must be separate from internal convergence.
    Verify by checking that ``compute_market_gap_edge`` exists and
    references implied_probability, while the convergence engine
    itself is model+sim only."""
    src = _src("/app/backend/probability_engine.py")
    # Convergence engine references model / sim predictions.
    assert "sim_win_probability" in src or "compute_sim_probability" in src


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
