"""Main Board Strictness Fix — INCLUSIVE `>= 85` certification.

Perklocks Main Locks Board rule (2026-08):

    FINAL LOCK SCORE >= 85   ⇒ eligible
    FINAL LOCK SCORE  < 85   ⇒ ineligible

85–100 inclusive are score-eligible.  85.00 MUST NOT be rejected
merely because it is 85.

But: score >= 85 alone is NOT sufficient.  A pick must still pass
real-line integrity + canonical/safety requirements.  This suite
proves both the corrected threshold AND the intact legitimate
rejection reasons.
"""
from __future__ import annotations

import pytest


_REAL_LINE = {"book_odds": -110, "implied_probability": 0.524}


# ═════════════════════════════════════════════════════════════════════
# §A  Boundary: INCLUSIVE >= 85
# ═════════════════════════════════════════════════════════════════════

class TestInclusiveBoundary:
    def test_84_99_ineligible(self):
        from services.main_board_eligibility import is_main_board_eligible
        assert is_main_board_eligible(
            {"lock_score": 84.99, **_REAL_LINE}) is False

    def test_85_00_eligible(self):
        from services.main_board_eligibility import is_main_board_eligible
        assert is_main_board_eligible(
            {"lock_score": 85.00, **_REAL_LINE}) is True

    def test_85_int_eligible(self):
        from services.main_board_eligibility import is_main_board_eligible
        assert is_main_board_eligible(
            {"lock_score": 85, **_REAL_LINE}) is True

    def test_86_eligible(self):
        from services.main_board_eligibility import is_main_board_eligible
        assert is_main_board_eligible(
            {"lock_score": 86, **_REAL_LINE}) is True

    def test_99_eligible(self):
        from services.main_board_eligibility import is_main_board_eligible
        assert is_main_board_eligible(
            {"lock_score": 99, **_REAL_LINE}) is True

    def test_100_eligible(self):
        from services.main_board_eligibility import is_main_board_eligible
        assert is_main_board_eligible(
            {"lock_score": 100, **_REAL_LINE}) is True

    def test_published_lock_score_85_eligible(self):
        """Canonical source: 85.0 published_lock_score is ON board."""
        from services.main_board_eligibility import is_main_board_eligible
        assert is_main_board_eligible(
            {"published_lock_score": 85.0, "lock_score": 50.0,
              **_REAL_LINE}) is True

    def test_lock_score_v2_85_eligible(self):
        from services.main_board_eligibility import is_main_board_eligible
        assert is_main_board_eligible(
            {"lock_score": 50.0, "lock_score_v2": 85.0,
              **_REAL_LINE}) is True


# ═════════════════════════════════════════════════════════════════════
# §B  Legitimate rejection reasons (score >= 85 but still off board)
# ═════════════════════════════════════════════════════════════════════

class TestLegitimateRejections:
    def test_85_without_book_odds_rejected(self):
        """No real sportsbook line → rejected regardless of Lock Score."""
        from services.main_board_eligibility import is_main_board_eligible
        assert is_main_board_eligible({"lock_score": 99.0}) is False

    def test_85_with_no_real_book_line_flag_rejected(self):
        from services.main_board_eligibility import is_main_board_eligible
        assert is_main_board_eligible({
            "lock_score": 99.0, **_REAL_LINE,
            "no_real_book_line": True,
        }) is False

    def test_85_with_model_only_flag_rejected(self):
        from services.main_board_eligibility import is_main_board_eligible
        assert is_main_board_eligible({
            "lock_score": 99.0, **_REAL_LINE,
            "model_only": True,
        }) is False

    def test_85_with_hide_from_main_board_flag_rejected(self):
        from services.main_board_eligibility import is_main_board_eligible
        assert is_main_board_eligible({
            "lock_score": 99.0, **_REAL_LINE,
            "hide_from_main_board": True,
        }) is False


# ═════════════════════════════════════════════════════════════════════
# §C  Mongo predicate builder — INCLUSIVE $gte
# ═════════════════════════════════════════════════════════════════════

class TestMongoPredicate:
    def test_base_predicate_uses_gte_85(self):
        from services.main_board_eligibility import main_board_lock_score_query
        q = main_board_lock_score_query()
        lock_gate = q["$and"][-1]
        # Published branch uses $gte:85.
        assert lock_gate["$or"][0] == {
            "published_lock_score": {"$gte": 85.0}}
        # Legacy fallback branch uses $gte:85 for both fields.
        inner_or = [c for c in lock_gate["$or"][1]["$and"]
                    if "$or" in c][0]["$or"]
        assert {"lock_score":    {"$gte": 85.0}} in inner_or
        assert {"lock_score_v2": {"$gte": 85.0}} in inner_or

    def test_narrowing_min_lock_uses_gte(self):
        from services.main_board_eligibility import main_board_lock_score_query
        q = main_board_lock_score_query(min_lock=99)
        lock_gate = q["$and"][-1]
        assert lock_gate["$or"][0] == {
            "published_lock_score": {"$gte": 99.0}}

    def test_narrowing_below_85_clamps_to_85(self):
        from services.main_board_eligibility import main_board_lock_score_query
        q = main_board_lock_score_query(min_lock=70)
        lock_gate = q["$and"][-1]
        assert lock_gate["$or"][0] == {
            "published_lock_score": {"$gte": 85.0}}


# ═════════════════════════════════════════════════════════════════════
# §D  BoardProjectionService — 85 lands on the projected Locks board
# ═════════════════════════════════════════════════════════════════════

class TestBoardProjectionInclusive:
    def test_85_pick_reaches_projected_board(self):
        from services.board_projection_service import BoardProjectionService
        pick = {
            "id": "strict-85", "sport": "MLB",
            "market": "Aaron Judge Over 0.5 Hits", "side": "Over",
            "line": 0.5, "book_odds": -180,
            "implied_probability": 0.643,
            "lock_score": 85.0, "published_lock_score": 85.0,
            "event_id": "e1", "event_time": "2026-08-15T23:05:00Z",
            "no_bet": False, "off_board": False,
            "hide_from_main_board": False,
        }
        ids = BoardProjectionService().project_ids([pick])
        assert "strict-85" in ids, (
            "85.0 pick MUST reach the Locks board under INCLUSIVE contract")

    def test_84_99_pick_does_not_reach_board(self):
        from services.board_projection_service import BoardProjectionService
        pick = {
            "id": "strict-84-99", "sport": "MLB",
            "market": "x", "side": "Over", "line": 0.5,
            "book_odds": -180, "implied_probability": 0.643,
            "lock_score": 84.99, "published_lock_score": 84.99,
            "event_id": "e1", "event_time": "2026-08-15T23:05:00Z",
        }
        assert "strict-84-99" not in \
            BoardProjectionService().project_ids([pick])


# ═════════════════════════════════════════════════════════════════════
# §E  Backwards-compat aliases still importable, both == 85.0 now
# ═════════════════════════════════════════════════════════════════════

class TestBackwardsCompatConstants:
    def test_main_board_lock_floor_is_85(self):
        from services.main_board_eligibility import MAIN_BOARD_LOCK_FLOOR
        assert MAIN_BOARD_LOCK_FLOOR == 85.0

    def test_exclusive_alias_now_equals_inclusive_85(self):
        from services.main_board_eligibility import (
            MAIN_BOARD_LOCK_FLOOR_EXCLUSIVE,
            MAIN_BOARD_LOCK_FLOOR_INCLUSIVE,
            MAIN_BOARD_LOCK_FLOOR,
        )
        assert MAIN_BOARD_LOCK_FLOOR_EXCLUSIVE == 85.0
        assert MAIN_BOARD_LOCK_FLOOR_INCLUSIVE == 85.0
        assert MAIN_BOARD_LOCK_FLOOR_EXCLUSIVE == MAIN_BOARD_LOCK_FLOOR


# ═════════════════════════════════════════════════════════════════════
# §F  No filler — a pick without a Lock Score is still off the board
# ═════════════════════════════════════════════════════════════════════

class TestNoFillerRegression:
    def test_missing_lock_score_ineligible(self):
        from services.main_board_eligibility import is_main_board_eligible
        assert is_main_board_eligible({**_REAL_LINE}) is False

    def test_zero_lock_score_ineligible(self):
        from services.main_board_eligibility import is_main_board_eligible
        assert is_main_board_eligible({"lock_score": 0.0, **_REAL_LINE}) is False

    def test_none_pick_ineligible(self):
        from services.main_board_eligibility import is_main_board_eligible
        assert is_main_board_eligible(None) is False

    def test_non_dict_pick_ineligible(self):
        from services.main_board_eligibility import is_main_board_eligible
        assert is_main_board_eligible("not a dict") is False
