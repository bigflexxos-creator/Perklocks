"""P0 (2026-08-11) — Universal Settlement Contract regression tests.

Locks in every P0 requirement:

  1. MISSING DATA ≠ ZERO.
  2. Alt-line grading uses ONE authoritative actual.
  3. OVER > line, UNDER < line, PUSH == line.
  4. Milestone "N+" is `actual >= N`.
  5. Derived / combo markets return None when a component is missing.
  6. DNP / void / retired / no_contest have EXPLICIT statuses.
  7. Seymour-specific: actual=None Over 5.5 K → NEVER 'lost'.
  8. Contract violation: `lost` with actual=None raises.
"""
from __future__ import annotations

import pytest

from services.universal_settlement_contract import (
    grade_over_under, grade_milestone, grade_derived,
    grade_moneyline, grade_alt_lines_from_actual, normalise_actual,
    settlement_envelope,
    envelope_dnp, envelope_did_not_start, envelope_cancelled,
    envelope_postponed, envelope_no_contest, envelope_retired,
    envelope_provider_error,
    RESULT_WON, RESULT_LOST, RESULT_PUSH, RESULT_VOID,
    RESULT_UNRESOLVED, RESULT_PENDING,
    REASON_MISSING_ACTUAL, REASON_MISSING_LINE, REASON_MISSING_SIDE,
    REASON_DNP, REASON_NO_CONTEST, REASON_PROVIDER_ERROR,
    SettlementContractViolation,
)


# ── 1. MISSING DATA ≠ ZERO ─────────────────────────────────────
@pytest.mark.unit
def test_missing_actual_is_unresolved_never_lost():
    r = grade_over_under(actual=None, line=5.5, side="over")
    assert r["result"] == RESULT_UNRESOLVED
    assert r["settlement_verified"] is False
    assert r["settlement_reason"] == REASON_MISSING_ACTUAL


@pytest.mark.unit
def test_contract_violation_when_lost_with_no_actual():
    with pytest.raises(SettlementContractViolation):
        settlement_envelope(result=RESULT_LOST, actual=None)


@pytest.mark.unit
def test_normalise_actual_never_coerces_missing_to_zero():
    for bad in (None, "", "  ", "null", "None", "NaN", "-",
                 float("nan")):
        assert normalise_actual(bad) is None
    # But a real zero must survive.
    assert normalise_actual(0) == 0
    assert normalise_actual("0") == 0
    assert normalise_actual(0.0) == 0
    # And numeric strings.
    assert normalise_actual("7") == 7
    assert normalise_actual("5.5") == 5.5


# ── 2. The Seymour-class contract ──────────────────────────────
@pytest.mark.unit
def test_seymour_case_actual_7_beats_over_5_5():
    """The actual failure case: 7 K > 5.5 → WIN."""
    r = grade_over_under(actual=7, line=5.5, side="over")
    assert r["result"] == RESULT_WON
    assert r["actual"] == 7


@pytest.mark.unit
def test_seymour_case_missing_actual_never_becomes_lost():
    """The bug: missing actual coerced to 0 and graded 'lost'.
    Contract: missing actual → unresolved."""
    for bad in (None, "", "N/A"):
        v = normalise_actual(bad)
        r = grade_over_under(actual=v, line=5.5, side="over")
        assert r["result"] == RESULT_UNRESOLVED
        assert r["settlement_verified"] is False


# ── 3. OVER / UNDER / PUSH ─────────────────────────────────────
@pytest.mark.unit
@pytest.mark.parametrize("actual,line,side,result", [
    (10,   5.5, "over",   RESULT_WON),
    (5,    5.5, "over",   RESULT_LOST),
    (5,    5.5, "under",  RESULT_WON),
    (10,   5.5, "under",  RESULT_LOST),
    (5.5,  5.5, "over",   RESULT_PUSH),
    (5.5,  5.5, "under",  RESULT_PUSH),
    (0,    0,   "over",   RESULT_PUSH),
])
def test_over_under_matrix(actual, line, side, result):
    r = grade_over_under(actual=actual, line=line, side=side)
    assert r["result"] == result


@pytest.mark.unit
def test_missing_side_is_unresolved():
    r = grade_over_under(actual=5, line=5.5, side=None)
    assert r["result"] == RESULT_UNRESOLVED
    assert r["settlement_reason"] == REASON_MISSING_SIDE


# ── 4. Milestone (N+) ──────────────────────────────────────────
@pytest.mark.unit
@pytest.mark.parametrize("actual,thresh,result", [
    (200, 200, RESULT_WON),
    (267, 200, RESULT_WON),
    (267, 225, RESULT_WON),
    (267, 250, RESULT_WON),
    (267, 275, RESULT_LOST),
    (199, 200, RESULT_LOST),
])
def test_milestone_grader(actual, thresh, result):
    r = grade_milestone(actual, thresh)
    assert r["result"] == result


@pytest.mark.unit
def test_milestone_is_not_the_same_as_over_dot5():
    """200+ = actual >= 200.  Over 200.5 = actual > 200.5.
    These MUST NOT collide."""
    # 200 exactly
    assert grade_milestone(200, 200)["result"] == RESULT_WON
    assert grade_over_under(200, 200.5, "over")["result"] == RESULT_LOST


# ── 5. Alt-line dispatcher — one actual, many lines ────────────
@pytest.mark.unit
def test_nfl_passing_yards_267_all_alt_lines():
    """The exact P0-required matrix for a 267-yard passing game."""
    lines = [(200.5, "over"), (225.5, "over"), (250.5, "over"),
              (275.5, "over"), (200, "milestone"), (225, "milestone"),
              (250, "milestone"), (275, "milestone"),
              (266.5, "over"), (267.5, "over")]
    results = grade_alt_lines_from_actual(267, lines)
    expected = [RESULT_WON, RESULT_WON, RESULT_WON,
                 RESULT_LOST, RESULT_WON, RESULT_WON,
                 RESULT_WON, RESULT_LOST,
                 RESULT_WON, RESULT_LOST]
    for r, e in zip(results, expected):
        assert r["result"] == e, (r, e)


# ── 6. Derived / combo markets ─────────────────────────────────
@pytest.mark.unit
def test_pra_full_components():
    """25 pts + 8 reb + 7 ast = 40 PRA."""
    total = grade_derived({"points": 25, "rebounds": 8, "assists": 7})
    assert total == 40
    r = grade_over_under(total, 39.5, "over")
    assert r["result"] == RESULT_WON


@pytest.mark.unit
def test_pra_missing_component_is_unresolved():
    total = grade_derived({"points": 25, "rebounds": 8, "assists": None})
    assert total is None
    r = grade_over_under(total, 39.5, "over")
    assert r["result"] == RESULT_UNRESOLVED
    assert r["settlement_reason"] == REASON_MISSING_ACTUAL


@pytest.mark.unit
def test_mlb_h_r_rbi_missing_component():
    total = grade_derived({"hits": 1, "runs": 1, "rbi": None})
    assert total is None


# ── 7. MLB hitter 2-hit matrix ─────────────────────────────────
@pytest.mark.unit
def test_mlb_hitter_2_hits_all_lines():
    lines = [(1, "milestone"), (2, "milestone"), (3, "milestone")]
    results = grade_alt_lines_from_actual(2, lines)
    assert [r["result"] for r in results] == [
        RESULT_WON, RESULT_WON, RESULT_LOST]


# ── 8. NBA 28 points ─────────────────────────────────────
@pytest.mark.unit
def test_nba_28_points_alt_lines():
    lines = [(20, "milestone"), (25, "milestone"), (30, "milestone")]
    results = grade_alt_lines_from_actual(28, lines)
    assert [r["result"] for r in results] == [
        RESULT_WON, RESULT_WON, RESULT_LOST]


# ── 9. Moneyline / 1X2 ───────────────────────────────────
@pytest.mark.unit
def test_moneyline_correct_pick():
    r = grade_moneyline(winner="Manchester City", side="Manchester City")
    assert r["result"] == RESULT_WON


@pytest.mark.unit
def test_moneyline_wrong_pick():
    r = grade_moneyline(winner="Manchester City", side="Chelsea")
    assert r["result"] == RESULT_LOST


@pytest.mark.unit
def test_moneyline_case_insensitive():
    r = grade_moneyline(winner="manchester city", side="Manchester City")
    assert r["result"] == RESULT_WON


# ── 10. DNP / void / retired / no_contest ─────────────────
@pytest.mark.unit
def test_dnp_default_voids_not_loss():
    r = envelope_dnp()
    assert r["result"] == RESULT_VOID
    assert r["settlement_reason"] == REASON_DNP


@pytest.mark.unit
def test_ufc_no_contest_is_void_not_loss():
    r = envelope_no_contest()
    assert r["result"] == RESULT_VOID
    assert r["settlement_reason"] == REASON_NO_CONTEST


@pytest.mark.unit
def test_tennis_retired_is_void_not_loss():
    r = envelope_retired()
    assert r["result"] == RESULT_VOID


@pytest.mark.unit
def test_postponed_stays_pending():
    r = envelope_postponed()
    assert r["result"] == RESULT_PENDING
    assert r["settlement_verified"] is False


@pytest.mark.unit
def test_provider_error_never_becomes_loss():
    r = envelope_provider_error()
    assert r["result"] == RESULT_UNRESOLVED
    assert r["settlement_reason"] == REASON_PROVIDER_ERROR
    assert r["settlement_verified"] is False


# ── 11. Wrong player / wrong event cannot settle ──────────
@pytest.mark.unit
def test_wrong_player_actual_is_still_grading_input():
    """The universal contract does NOT verify identity — that's the
    caller's job.  But when the caller passes an ambiguous actual it
    must be able to signal 'unresolved' via the envelope."""
    r = settlement_envelope(
        result=RESULT_UNRESOLVED, actual=None,
        reason="player_not_in_event", verified=False)
    assert r["result"] == RESULT_UNRESOLVED
    assert r["settlement_verified"] is False


# ── 12. Alt-line consistency: ONE actual → all thresholds ─
@pytest.mark.unit
def test_alt_lines_all_come_from_same_actual():
    """The core invariant: for a given (event, participant, market
    family, actual), EVERY alt line grades deterministically from
    that ONE actual value.  There is NEVER a separate 'actual per
    alt line'."""
    actual = 267
    # 200 over is same actual as 275 over — the ONLY difference is
    # the threshold.
    lines = [(200.5, "over"), (275.5, "over")]
    r = grade_alt_lines_from_actual(actual, lines)
    assert r[0]["actual"] == 267
    assert r[1]["actual"] == 267
    assert r[0]["result"] == RESULT_WON
    assert r[1]["result"] == RESULT_LOST


# ── 13. Push allow_push=False ─────────────────────────────
@pytest.mark.unit
def test_over_equal_line_without_push_grades_lost():
    r = grade_over_under(5, 5, "over", allow_push=False)
    assert r["result"] == RESULT_LOST


@pytest.mark.unit
def test_under_equal_line_without_push_grades_won():
    r = grade_over_under(5, 5, "under", allow_push=False)
    assert r["result"] == RESULT_WON


# ── 14. Soccer scorer: player scored → WON ───────────────
@pytest.mark.unit
def test_soccer_anytime_scorer_won_and_lost():
    # Actual = number of goals; market is milestone 1+.
    won = grade_milestone(1, 1)
    assert won["result"] == RESULT_WON
    lost = grade_milestone(0, 1)
    assert lost["result"] == RESULT_LOST


@pytest.mark.unit
def test_soccer_anytime_scorer_missing_lineup_is_unresolved():
    r = grade_milestone(None, 1)
    assert r["result"] == RESULT_UNRESOLVED
