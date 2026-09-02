"""Phase 10 — AUTHORITATIVE SETTLEMENT invariants.

  S1. MISSING DATA ≠ ZERO.  ``actual is None`` MUST NEVER grade to
      ``lost`` — always ``pending`` or ``unresolved``.
  S2. OVER > line, UNDER < line, PUSH == line (when allowed).
  S3. Milestone N+ markets grade on ``actual >= N`` (never
      confused with Over N.5).
  S4. Derived combo markets (PRA, H+R+RBI) return None if any
      component is missing — the loss must not be manufactured.
  S5. Moneyline grades correctly for both sides + returns
      ``unresolved`` on missing winner.
  S6. Explicit event states (postponed / suspended / cancelled /
      DNP / retired / no_contest) never auto-grade as loss.
"""
from __future__ import annotations
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest


def test_missing_actual_returns_pending_not_lost():
    from services.universal_settlement_contract import grade_over_under
    e = grade_over_under(actual=None, line=8.5, side="Over")
    assert e["result"] in ("pending", "unresolved")
    assert e["result"] != "lost"


def test_over_under_grades_correctly():
    from services.universal_settlement_contract import grade_over_under
    assert grade_over_under(actual=9, line=8.5, side="Over")["result"] == "won"
    assert grade_over_under(actual=8, line=8.5, side="Over")["result"] == "lost"
    assert grade_over_under(actual=9, line=8.5, side="Under")["result"] == "lost"
    assert grade_over_under(actual=8, line=8.5, side="Under")["result"] == "won"


def test_integer_line_push():
    from services.universal_settlement_contract import grade_over_under
    e = grade_over_under(actual=8, line=8, side="Over", allow_push=True)
    assert e["result"] == "push"


def test_milestone_over_ns_not_confused_with_greater_equal_n():
    from services.universal_settlement_contract import (
        grade_over_under, grade_milestone,
    )
    # 200+ passing yards on 199 → LOSS for milestone (needs >=200).
    m = grade_milestone(actual=199, threshold_min=200)
    assert m["result"] == "lost"
    m = grade_milestone(actual=200, threshold_min=200)
    assert m["result"] == "won"
    # Over 199.5 on 199 → LOSS by OU rules too.
    e = grade_over_under(actual=199, line=199.5, side="Over")
    assert e["result"] == "lost"


def test_derived_returns_none_on_any_missing_component():
    from services.universal_settlement_contract import grade_derived
    total = grade_derived({"points": 25, "rebounds": None, "assists": 8})
    assert total is None
    total_full = grade_derived({"points": 25, "rebounds": 6, "assists": 8})
    assert total_full == 39


def test_moneyline_unresolved_on_missing_winner():
    from services.universal_settlement_contract import grade_moneyline
    e = grade_moneyline(winner=None, side="Lakers")
    assert e["result"] in ("pending", "unresolved")
    assert e["result"] != "lost"


def test_moneyline_side_matching():
    from services.universal_settlement_contract import grade_moneyline
    assert grade_moneyline(winner="Lakers", side="Lakers")["result"] == "won"
    assert grade_moneyline(winner="Lakers", side="Celtics")["result"] == "lost"


def test_settlement_envelope_shape():
    from services.universal_settlement_contract import settlement_envelope
    env = settlement_envelope(
        result="won", actual=42, line=39.5, side="Over",
        verified=True, reason="graded_ok",
    )
    for k in ("result", "actual", "line", "side",
              "settlement_verified", "settlement_reason",
              "settlement_status"):
        assert k in env
    # settlement_status must mirror result for uniform consumers.
    assert env["settlement_status"] == env["result"]


def test_settlement_envelope_rejects_lost_with_none_actual():
    """Hard contract: cannot fabricate a loss when actual is None."""
    from services.universal_settlement_contract import (
        settlement_envelope, SettlementContractViolation,
    )
    with pytest.raises(SettlementContractViolation):
        settlement_envelope(result="lost", actual=None, line=8.5, side="Over")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
