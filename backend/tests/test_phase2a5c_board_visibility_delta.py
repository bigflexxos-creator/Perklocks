"""Phase 2A.5C — Live Preview Soccer board reachability delta.

Targeted regression tests for the exact fix identified during the live
funnel trace: `services.board_visibility.compute_off_board` was reading
the stale legacy V1 ``lock_score`` field, causing every real-line
Soccer pick whose V1 landed low (55.0) but whose ``published_lock_score``
was 85-98 to be silently marked ``off_board=True`` and filtered out of
`/api/picks/today?sport=Soccer`.

Contracts enforced:
    * Canonical Lock Score wins (published_lock_score → max(V1, V2)).
    * Playable + APEX Lock are visible-grade tiers.
    * Stale ``grade`` field is refreshed from canonical LS.
    * ≥85 board rule preserved — pick with canonical LS = 84.9 stays off.
    * No new production regression on the previously-certified suites.
"""
from __future__ import annotations

import os
import sys

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)


# ═════════════════════════════════════════════════════════════════════
# Canonical Lock Score preference
# ═════════════════════════════════════════════════════════════════════
def test_canonical_lock_score_prefers_published():
    from services.board_visibility import _canonical_lock_score
    p = {"lock_score": 55.0, "lock_score_v2": 88.0, "published_lock_score": 98.0}
    assert _canonical_lock_score(p) == 98.0


def test_canonical_lock_score_falls_back_to_max_of_v1_v2():
    from services.board_visibility import _canonical_lock_score
    p = {"lock_score": 55.0, "lock_score_v2": 89.0}
    assert _canonical_lock_score(p) == 89.0


def test_canonical_lock_score_all_missing_returns_zero():
    from services.board_visibility import _canonical_lock_score
    assert _canonical_lock_score({}) == 0.0


# ═════════════════════════════════════════════════════════════════════
# Grade bands — Playable / APEX Lock are visible
# ═════════════════════════════════════════════════════════════════════
def test_canonical_grade_band_mapping():
    from services.board_visibility import _canonical_grade
    assert _canonical_grade(100.0) == "APEX Lock"
    assert _canonical_grade(98.0) == "Elite Lock"
    assert _canonical_grade(95.0) == "Strong Lock"
    assert _canonical_grade(90.0) == "Lock"
    assert _canonical_grade(85.0) == "Playable"
    assert _canonical_grade(84.9) == "Pass"


def test_visible_grades_include_playable_and_apex():
    from services.board_visibility import _VISIBLE_GRADES
    assert "Playable" in _VISIBLE_GRADES
    assert "APEX Lock" in _VISIBLE_GRADES
    assert "Elite Lock" in _VISIBLE_GRADES
    assert "Strong Lock" in _VISIBLE_GRADES
    assert "Lock" in _VISIBLE_GRADES
    # Pass is NOT visible.
    assert "Pass" not in _VISIBLE_GRADES


# ═════════════════════════════════════════════════════════════════════
# compute_off_board — canonical LS wins over stale V1
# ═════════════════════════════════════════════════════════════════════
def test_off_board_false_when_published_lock_score_ge_85_but_v1_low():
    """The exact defect: Martín Ojeda TSoA with V1=55, V2=98, published=98.
    Before the delta: off_board=True (V1<85). After: off_board=False.
    """
    from services.board_visibility import compute_off_board
    p = {
        "lock_score": 55.0,
        "lock_score_v2": 98.0,
        "published_lock_score": 98.0,
        "grade": "Pass",  # stale — must be ignored
        "no_bet": False,
        "book_odds": -132,
    }
    off, reasons = compute_off_board(p)
    assert off is False, f"canonical LS 98 must be on-board, got reasons={reasons}"


def test_off_board_true_when_canonical_lock_below_85():
    from services.board_visibility import compute_off_board
    p = {
        "lock_score": 84.9,
        "lock_score_v2": 84.9,
        "published_lock_score": 84.9,
        "no_bet": False,
    }
    off, reasons = compute_off_board(p)
    assert off is True
    assert any("lock<85" in r for r in reasons)


def test_off_board_boundary_85_passes():
    """≥85 rule preserved — exactly 85 is on-board."""
    from services.board_visibility import compute_off_board
    p = {"lock_score": 85.0, "lock_score_v2": 85.0, "published_lock_score": 85.0,
         "grade": "Playable"}
    off, _ = compute_off_board(p)
    assert off is False


def test_off_board_ignores_stale_grade_when_canonical_ls_qualifies():
    """A stale ``grade='Pass'`` from a pre-V2 build must be ignored when
    the canonical Lock Score is ≥ 85."""
    from services.board_visibility import compute_off_board
    p = {"lock_score": 55.0, "lock_score_v2": 89.0,
         "grade": "Pass",   # stale
         "no_bet": False}
    off, reasons = compute_off_board(p)
    assert off is False, f"stale grade must not hide a canonical LS 89, got {reasons}"


def test_off_board_chalk_trap_still_hides_regardless_of_lock():
    """Chalk-trap ejection rule preserved (Phase 1D safety)."""
    from services.board_visibility import compute_off_board
    p = {"lock_score": 98.0, "lock_score_v2": 98.0, "published_lock_score": 98.0,
         "chalk_trap": True}
    off, reasons = compute_off_board(p)
    assert off is True
    assert "chalk_trap" in reasons


def test_off_board_longshot_trap_still_hides():
    from services.board_visibility import compute_off_board
    p = {"published_lock_score": 92.0, "longshot_trap": True}
    off, reasons = compute_off_board(p)
    assert off is True
    assert "longshot_trap" in reasons


def test_off_board_no_bet_still_hides():
    from services.board_visibility import compute_off_board
    p = {"published_lock_score": 98.0, "no_bet": True}
    off, reasons = compute_off_board(p)
    assert off is True
    assert "no_bet" in reasons


def test_off_board_model_only_still_hides():
    from services.board_visibility import compute_off_board
    p = {"published_lock_score": 98.0, "is_model_only": True}
    off, reasons = compute_off_board(p)
    assert off is True
    assert "model_only" in reasons


# ═════════════════════════════════════════════════════════════════════
# tag_board_visibility — refreshes stale grade in place
# ═════════════════════════════════════════════════════════════════════
def test_tag_refreshes_stale_grade_from_canonical_lock_score():
    from services.board_visibility import tag_board_visibility
    picks = [
        {"selection": "Martín Ojeda", "lock_score": 55.0,
         "lock_score_v2": 98.0, "published_lock_score": 98.0, "grade": "Pass"},
        {"selection": "Below floor", "lock_score": 55.0, "lock_score_v2": 55.0,
         "grade": "Pass"},
        {"selection": "Playable pick", "lock_score": 55.0, "lock_score_v2": 86.0,
         "grade": "Pass"},
    ]
    stats = tag_board_visibility(picks)
    assert picks[0]["grade"] == "Elite Lock"       # 98 → Elite Lock
    assert picks[0]["off_board"] is False
    assert picks[1]["grade"] == "Pass"             # unchanged (canonical still Pass)
    assert picks[1]["off_board"] is True
    assert picks[2]["grade"] == "Playable"         # 86 → Playable
    assert picks[2]["off_board"] is False
    assert stats["on_board"] == 2
    assert stats["off_board"] == 1


# ═════════════════════════════════════════════════════════════════════
# Alignment with picks_routes ``grade != "Pass"`` contract
# ═════════════════════════════════════════════════════════════════════
def test_playable_grade_matches_picks_routes_visibility_contract():
    """picks_routes.py enforces ``grade != "Pass"``.  A pick canonically
    graded ``Playable`` (85-89) must survive both the off_board tagger
    AND that Mongo filter."""
    from services.board_visibility import _canonical_grade
    grade = _canonical_grade(87.0)
    assert grade == "Playable"
    assert grade != "Pass"


# ═════════════════════════════════════════════════════════════════════
# Scorer / game-model preservation
# ═════════════════════════════════════════════════════════════════════
def test_phase2a5_scorer_bridge_still_intact():
    from services.soccer_scorer_bridge import compute_soccer_scorer_factors_sync
    r = compute_soccer_scorer_factors_sync(
        player="Erling Haaland", market_key="player_goal_scorer_anytime",
        book_implied=0.55,
        form_row={"xg": 18.5, "goals": 20, "minutes": 2700, "games": 30,
                  "starts": 30, "position": "FW", "form_score": 82,
                  "shots_per_90": 4.5, "sot_per_90": 2.1},
        league="Premier League",
    )
    assert r is not None
    assert "Scorer Model Probability" in r["factors"]


def test_phase2a5b_game_model_still_intact():
    from services.soccer_game_model import estimate_soccer_game_probabilities
    ctx = {
        "home_xg_rolling": {"xg_avg": 2.3, "xga_avg": 0.9, "matches": 15,
                            "source": "understat"},
        "away_xg_rolling": {"xg_avg": 1.0, "xga_avg": 1.9, "matches": 15,
                            "source": "understat"},
    }
    r = estimate_soccer_game_probabilities(ctx, "H", "A")
    assert r.available is True
    assert r.p_home > r.p_away
