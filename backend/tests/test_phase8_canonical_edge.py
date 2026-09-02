"""Phase 8 — CANONICAL EDGE / MARKET COMPARISON invariants.

  E1. Canonical Edge for Totals uses JOINT de-vig — ONE canonical
      formula: edge = model_prob − fair_market_prob.
  E2. `canonical_totals_edge` is side-symmetric: given identical
      inputs but swapped side, edge signs flip predictably.
  E3. Joint de-vig removes vig from BOTH sides so fair probs sum to 1.
  E4. When paired odds are missing, `canonical_totals_edge`
      returns `available=False` — never falls back to raw one-sided
      implied probability (that would be SYNTHETIC_EDGE).
  E5. Two picks with same model_prob and same paired odds must
      produce the SAME canonical edge — the formula is the sole
      source of truth (no other Edge formula permitted on totals).
"""
from __future__ import annotations
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest


def test_joint_devig_fair_probs_sum_to_one():
    from services.totals_devig import joint_devig
    dv = joint_devig(-110, -110)
    assert dv["available"]
    assert abs(dv["fair_over"] + dv["fair_under"] - 1.0) < 1e-6
    assert dv["vig_pct"] > 0   # standard -110 has ~4.5% vig


def test_canonical_edge_over_side():
    from services.totals_devig import canonical_totals_edge
    r = canonical_totals_edge(model_prob=0.60, side="Over",
                                over_odds=-110, under_odds=-110)
    assert r["available"]
    # fair_over @ -110/-110 = 0.5 → edge = 0.10.
    assert abs(r["edge"] - 0.10) < 1e-6
    assert r["source"] == "canonical_totals_edge_v1"


def test_canonical_edge_under_side_symmetry():
    from services.totals_devig import canonical_totals_edge
    r = canonical_totals_edge(model_prob=0.60, side="Under",
                                over_odds=-110, under_odds=-110)
    # For same model_prob=0.60 on Under with symmetric fair 0.5,
    # edge = 0.10 too (same formula).
    assert abs(r["edge"] - 0.10) < 1e-6


def test_canonical_edge_fails_closed_on_missing_paired_odds():
    """No one-sided implied fallback — a missing paired odds MUST
    return available=False.  Boundary would then reject as
    SYNTHETIC_EDGE if the pick attached a nonzero edge anyway."""
    from services.totals_devig import canonical_totals_edge
    r = canonical_totals_edge(model_prob=0.60, side="Over",
                                over_odds=None, under_odds=-110)
    assert r["available"] is False


def test_canonical_edge_deterministic_given_same_inputs():
    from services.totals_devig import canonical_totals_edge
    a = canonical_totals_edge(0.55, "Over", -105, -115)
    b = canonical_totals_edge(0.55, "Over", -105, -115)
    assert a == b


def test_canonical_edge_uses_devig_not_raw_implied():
    """Prove the formula is DEVIG-anchored: raw one-sided implied
    ≠ fair_market_prob when the pair has vig."""
    from services.totals_devig import (
        canonical_totals_edge, _american_to_implied, joint_devig,
    )
    over, under = -140, 120
    raw_over = _american_to_implied(over)      # 0.5833
    fair_over = joint_devig(over, under)["fair_over"]
    assert abs(raw_over - fair_over) > 0.001
    r = canonical_totals_edge(0.62, "Over", over, under)
    # Edge must use fair, not raw.
    assert abs(r["fair_market_prob"] - fair_over) < 1e-6
    assert abs(r["edge"] - (0.62 - fair_over)) < 1e-6


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
