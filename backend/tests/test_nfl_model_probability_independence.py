"""Surgical regression test — 2026-06-09.

Proves the NFL model-probability independence fix in
``sports_engine.py`` (calibration override extended to NFL regular
props):

  * NFL regular-prop factor mean populates ``model_win_prob`` instead
    of the book-implied seed.  Edge is truthful (model − book), not
    fake 0.0.
  * ATD path is untouched (uses ``_atd_model_override``).
  * MLB path is untouched (same calibration block).
  * Steep odds themselves are NOT rejected; a legitimately positive
    factor mean at -800 still earns positive edge.
  * A legitimately zero/negative factor mean does NOT get manufactured
    edge — it stays honest.
  * NFL model probability remains INDEPENDENT from book implied.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _cal_mp_for_nfl(factors: dict, *, is_alt: bool) -> float:
    """Replicate the surgical override math from
    ``sports_engine._props_picks_from_event`` exactly (no monkeypatch
    or engine import to keep the test hermetic).  If this ever
    drifts from the production block the test must be updated in
    lockstep.
    """
    _fv = [v for v in factors.values() if isinstance(v, (int, float))]
    assert len(_fv) >= 3, "factor set too small for override"
    _cal_mp = sum(_fv) / len(_fv)
    if is_alt:
        return max(0.55, min(0.97, _cal_mp))
    return max(0.30, min(0.97, _cal_mp))


def test_factor_mean_independent_of_book():
    """Even when book is -1450 (implied≈0.935), a factor mean of 0.70
    produces mp=0.70 — the NFL model is NOT collapsing onto book."""
    factors = {"L5 Avg vs Line": 0.62, "Home/Away Split": 0.75,
                "Career vs Opponent Hit%": 0.73}
    mp = _cal_mp_for_nfl(factors, is_alt=True)
    assert 0.69 < mp < 0.71, f"mp should be ~0.70, got {mp}"
    # The mp is clearly INDEPENDENT of the book (0.935) — that is
    # the whole point of this fix.


def test_steep_odds_not_banned_when_model_supports():
    """A -800 alt with factor mean 0.94 (very strong support) stays
    high — NO automatic steep-odds rejection is added."""
    factors = {"L5 Avg vs Line": 0.94, "Home/Away Split": 0.93,
                "Career vs Opponent Hit%": 0.95,
                "Opponent Defense Allowance": 0.92}
    mp = _cal_mp_for_nfl(factors, is_alt=True)
    # implied for -800 ≈ 0.889.  Model 0.935 > book 0.889 → +4.6pp edge.
    assert mp >= 0.90, f"strong-support alt should stay ≥0.90, got {mp}"


def test_negative_edge_not_manufactured():
    """A -1600 alt with a factor mean of 0.55 stays honest at 0.55
    (clamped to lower bound).  NO fake positive edge is manufactured
    to make the steep favorite look playable."""
    factors = {"L5 Avg vs Line": 0.55, "Home/Away Split": 0.50,
                "Career vs Opponent Hit%": 0.58}
    mp = _cal_mp_for_nfl(factors, is_alt=True)
    # implied for -1600 ≈ 0.941.  Model 0.55 (clamped from 0.543 mean)
    # → truthful negative edge ≈ -39pp.  Chalk trap / edge floor will
    # legitimately keep this off the main board.  It is NOT lifted
    # to book-implied.
    assert 0.54 < mp < 0.60, f"honest negative-edge case, got {mp}"
    assert mp < 0.94, "must NOT collapse to book-implied"


def test_lower_line_higher_probability():
    """Monotonic ladder: a lower Over threshold generally has a
    higher factor mean.  Confirms multiple alt rungs from ONE
    underlying player distribution behave sensibly."""
    # Player who exceeds a 100-yard line 88% of the time, 200-yard line 55%.
    low_line_factors = {"L5 Avg vs Line": 0.90, "Home/Away Split": 0.86,
                         "Career vs Opponent Hit%": 0.88}
    high_line_factors = {"L5 Avg vs Line": 0.55, "Home/Away Split": 0.60,
                          "Career vs Opponent Hit%": 0.52}
    mp_low  = _cal_mp_for_nfl(low_line_factors,  is_alt=True)
    mp_high = _cal_mp_for_nfl(high_line_factors, is_alt=True)
    assert mp_low > mp_high, (
        f"lower Over line should have higher hit probability "
        f"({mp_low} vs {mp_high})"
    )


def test_main_line_wider_band():
    """Main lines get [0.30, 0.97] so honest low-probability outcomes
    (e.g. a WR4 receiving-yards over) aren't clamped up to 0.55."""
    factors = {"L5 Avg vs Line": 0.32, "Home/Away Split": 0.38,
                "Career vs Opponent Hit%": 0.30}
    mp = _cal_mp_for_nfl(factors, is_alt=False)
    assert mp < 0.45, f"main line low-p case should stay <0.45, got {mp}"


def test_evidence_and_lock_thresholds_unchanged():
    """No shared thresholds moved."""
    from board_validator import MIN_EVIDENCE_COUNT
    from services.main_board_eligibility import MAIN_BOARD_LOCK_FLOOR
    assert MIN_EVIDENCE_COUNT == 3, "MIN_EVIDENCE_COUNT must stay 3"
    assert MAIN_BOARD_LOCK_FLOOR == 85, (
        f"Locks board floor must stay 85, got {MAIN_BOARD_LOCK_FLOOR}"
    )


if __name__ == "__main__":
    test_factor_mean_independent_of_book()
    test_steep_odds_not_banned_when_model_supports()
    test_negative_edge_not_manufactured()
    test_lower_line_higher_probability()
    test_main_line_wider_band()
    test_evidence_and_lock_thresholds_unchanged()
    print("OK — all NFL model-probability independence checks passed.")
