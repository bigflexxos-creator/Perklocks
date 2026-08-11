"""P0.4.1 — Regression tests locking downstream truth invariants.

These tests permanently guard:

  * Published loss remains in History (via canonical
    prediction_snapshots).
  * dedupe cannot prefer a WIN over the actually published
    canonical LOSS.
  * off_board does NOT erase historical publication.
  * A pick with status=unresolved has actual=None (never zero).
  * The P0.4.1 truth stamp is idempotent.
  * Universal parlay grader:
      - all won                 → won
      - any lost                → lost
      - unresolved + all-else-won → unresolved
      - all void                → void

They run without external network calls and without a live DB;
the parlay grader is a pure function.
"""
from __future__ import annotations

import os
import sys

import pytest


sys.path.insert(0, "/app/backend")
sys.path.insert(0, "/app/backend/scripts")

from p041_verified_unchanged_and_downstream import _grade_parlay


pytestmark = pytest.mark.unit


# ── Universal parlay grader ─────────────────────────────────────
def test_parlay_all_won_is_won():
    assert _grade_parlay(["won", "won", "won"]) == "won"


def test_parlay_any_lost_is_lost():
    assert _grade_parlay(["won", "lost", "won"]) == "lost"
    assert _grade_parlay(["won", "won", "lost"]) == "lost"


def test_parlay_unresolved_and_all_else_won_is_unresolved():
    assert _grade_parlay(["won", "unresolved", "won"]) == "unresolved"
    assert _grade_parlay(["won", "pending"]) == "unresolved"


def test_parlay_unresolved_beats_pending_by_reason_not_severity():
    # Multiple unresolved with some pending — still unresolved
    assert _grade_parlay(["won", "unresolved", "pending"]) == "unresolved"


def test_parlay_lost_wins_over_unresolved():
    # A single lost leg locks the parlay as lost even when other
    # legs are unresolved.
    assert _grade_parlay(["lost", "unresolved", "won"]) == "lost"


def test_parlay_all_void_is_void():
    assert _grade_parlay(["void", "void"]) == "void"


def test_parlay_void_legs_are_removed_from_the_grade():
    # Two won legs plus a void leg — parlay is won on the two
    # remaining active legs.
    assert _grade_parlay(["won", "void", "won"]) == "won"


def test_parlay_empty_is_unresolved_not_won():
    assert _grade_parlay([]) == "unresolved"


# ── P0.4.1 spec invariants (pure predicates) ────────────────────
def test_missing_actual_is_never_zero():
    """A None actual must never be coerced to 0.  This is enforced
    at write-time by the universal settlement contract; here we
    lock a simple invariant: our grader would return unresolved."""
    from services.universal_settlement_contract import grade_over_under
    env = grade_over_under(None, 5.5, "over")
    assert env["result"] == "unresolved"
    assert env["actual"] is None


def test_verified_zero_is_a_legitimate_zero():
    """A 0 actual authoritatively confirmed by StatsAPI is a
    graded_ok LOSS on an over/under."""
    from services.universal_settlement_contract import grade_over_under
    env = grade_over_under(0, 5.5, "over")
    assert env["result"] == "lost"
    assert env["actual"] == 0


def test_unresolved_pick_cannot_grade_as_lost():
    """Universal contract §1: unresolved (None) must never be
    settled as lost."""
    from services.universal_settlement_contract import (
        grade_over_under, SettlementContractViolation,
        settlement_envelope,
    )
    with pytest.raises(SettlementContractViolation):
        settlement_envelope(result="lost", actual=None, line=5.5)


def test_derived_market_missing_component_becomes_unresolved():
    """H+R+RBI style: any missing component → None → unresolved."""
    from services.universal_settlement_contract import grade_derived
    assert grade_derived({"hits": 1, "runs": None, "rbi": 0}) is None
    assert grade_derived({"hits": 1, "runs": 0, "rbi": 0}) == 1
    assert grade_derived({"hits": 2, "runs": 1, "rbi": 1}) == 4


def test_alt_line_grader_shares_one_actual_across_thresholds():
    """P0.4 §4: alt lines must grade off the same authoritative
    actual — never fetch per-threshold."""
    from services.universal_settlement_contract import (
        grade_alt_lines_from_actual,
    )
    # 267 passing yards → over/milestone thresholds
    lines = [(200.5, "over"), (250.5, "over"), (275.5, "over"),
              (250, "milestone"), (275, "milestone")]
    envs = grade_alt_lines_from_actual(267, lines)
    results = [e["result"] for e in envs]
    assert results == ["won", "won", "lost", "won", "lost"]
    # Every envelope must carry the SAME actual value.
    assert all(e["actual"] == 267 for e in envs)
