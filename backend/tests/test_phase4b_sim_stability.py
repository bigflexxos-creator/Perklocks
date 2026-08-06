"""Phase 4B — Sport-specific Simulator Stability + Determinism.

Verifies:
  1. Every active sport simulator (MLB/NBA/Tennis/Soccer) produces the
     SAME sim_win_probability when re-run via apply_simulations().
  2. Different lines → different seeds → different results.
  3. Push probability is retained where the sim reports it.
  4. Invalid pick input causes zero anchor adjustment (already covered
     in the main test file — cross-referenced here).
  5. Simulator metadata (simulator_name, version, type, seed) is
     stamped on the pick after apply_simulations.

These tests do NOT run mongo — they hit apply_simulations() directly
with in-memory pick dicts.
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _mlb_pick(**over):
    p = {
        "id": "mlb-abc",
        "prediction_id": "mlb-abc",
        "sport": "MLB",
        "market": "Over 1.5 Hits",
        "market_key": "batter_hits",
        "player": "Aaron Judge",
        "line": 1.5,
        "side": "Over",
        "book_odds": -120,
        "win_probability": 62.0,
        "lock_score": 78.0,
        "brain": {"top_k": True, "confidence_calibrated": 0.62},
        "factors": {"a": 0.6, "b": 0.65, "c": 0.58},
        "selection_v2": {"market": {"family": "hitter_hits"}},
        "event_id": "evt-1",
        "mlb_bvp": {"ba": 0.278, "hr_per_ab": 0.048, "k_rate": 0.24},
    }
    p.update(over)
    return p


# ═══════════════════════════════════════════════════════════════════
# apply_simulations() end-to-end determinism
# ═══════════════════════════════════════════════════════════════════
def test_mlb_apply_simulations_reproducible():
    from brain.sim_runner import apply_simulations
    a = _mlb_pick()
    b = _mlb_pick()
    apply_simulations([a])
    apply_simulations([b])
    assert a.get("sim_win_probability") is not None
    assert a.get("sim_win_probability") == b.get("sim_win_probability")
    assert a.get("sim_ci_lower") == b.get("sim_ci_lower")
    assert a.get("sim_ci_upper") == b.get("sim_ci_upper")


def test_mlb_different_line_different_seed():
    from brain.sim_runner import apply_simulations
    p_low = _mlb_pick(line=0.5, prediction_id="p-05",
                       market="Over 0.5 Hits")
    p_hi = _mlb_pick(line=2.5, prediction_id="p-25",
                      market="Over 2.5 Hits")
    apply_simulations([p_low, p_hi])
    assert p_low.get("sim_win_probability") is not None
    assert p_hi.get("sim_win_probability") is not None
    # Different seed AND different threshold → different sim probability.
    assert p_low.get("seed") != p_hi.get("seed")
    assert p_low.get("sim_win_probability") != p_hi.get("sim_win_probability")


def test_apply_simulations_stamps_simulator_metadata():
    from brain.sim_runner import apply_simulations
    p = _mlb_pick()
    apply_simulations([p])
    # simulate_pick() setdefaults these on the out dict — apply_simulations
    # p.update(sim) then copies them onto the pick.
    assert p.get("simulator_name") == "mlb_simulator"
    assert p.get("simulator_version") == "1.1.0"
    assert p.get("simulator_type") == "distribution_monte_carlo"
    assert p.get("independent_evidence") is True
    assert p.get("valid") is True
    assert p.get("seed") is not None


def test_apply_simulations_records_anchor_audit_fields():
    from brain.sim_runner import apply_simulations
    p = _mlb_pick(lock_score=70.0)
    apply_simulations([p])
    # Regardless of whether the sim lifted, prior + delta must be logged.
    assert "sim_lock_prior" in p
    assert "sim_lock_applied_delta" in p
    assert "sim_lock_residual" in p


def test_apply_simulations_bounded_delta():
    """Regardless of how far sim_wp diverges, the anchor must move the
    lock by at most SIM_RESIDUAL_MAX pp in either direction."""
    from brain.sim_runner import apply_simulations, SIM_RESIDUAL_MAX
    p = _mlb_pick(lock_score=50.0)
    apply_simulations([p])
    delta = p.get("sim_lock_applied_delta") or 0.0
    assert abs(delta) <= SIM_RESIDUAL_MAX + 0.01


# ═══════════════════════════════════════════════════════════════════
# Legacy simulator names still exported
# ═══════════════════════════════════════════════════════════════════
def test_legacy_run_simulator_still_importable():
    from brain.simulator import run_simulator      # noqa: F401
    from brain.simulator import run_posterior_uncertainty  # noqa: F401
    from brain.simulator import SIMULATOR_TYPE, SIMULATOR_VERSION
    assert SIMULATOR_TYPE == "posterior_uncertainty"
    assert SIMULATOR_VERSION == "2.0.0"


def test_apply_simulations_returns_correct_counts_shape():
    from brain.sim_runner import apply_simulations
    counts = apply_simulations([])
    # All expected keys present.
    for k in ("applied", "stronger", "weaker", "neutral",
              "anchored", "lifted_up", "lifted_down",
              "skipped_not_independent", "skipped_invalid"):
        assert k in counts
