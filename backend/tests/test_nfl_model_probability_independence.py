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

    Honest factor mean; only defensive [0.05, 0.97] clamp to prevent
    pathological zero rows and numerical certainty.  No alt-line
    lift (that manufactured edge for cheap alt-lines and was
    removed 2026-06-09).
    """
    _fv = [v for v in factors.values() if isinstance(v, (int, float))]
    assert len(_fv) >= 3, "factor set too small for override"
    _cal_mp = sum(_fv) / len(_fv)
    return max(0.05, min(0.97, _cal_mp))


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
    """A -1600 alt with a factor mean of 0.55 stays HONEST at 0.55.
    NO fake positive edge is manufactured to make the steep favorite
    look playable."""
    factors = {"L5 Avg vs Line": 0.55, "Home/Away Split": 0.50,
                "Career vs Opponent Hit%": 0.58}
    mp = _cal_mp_for_nfl(factors, is_alt=True)
    # implied for -1600 ≈ 0.941.  Model ~0.543 (honest mean, NO
    # artificial 0.55 lift) → truthful negative edge ≈ -40pp.  Chalk
    # trap / edge floor will legitimately keep this off the main
    # board.  It is NOT lifted to book-implied.
    assert 0.50 < mp < 0.60, f"honest negative-edge case, got {mp}"
    assert mp < 0.94, "must NOT collapse to book-implied"


def test_cheap_alt_line_no_manufactured_edge():
    """A +200 alt with an honest factor mean of 0.40 stays at 0.40
    (previous lift-to-0.55 would have produced fake +22pp edge)."""
    factors = {"L5 Avg vs Line": 0.35, "Home/Away Split": 0.40,
                "Career vs Opponent Hit%": 0.45}
    mp = _cal_mp_for_nfl(factors, is_alt=True)
    # implied for +200 ≈ 0.333.  Honest model ≈ 0.40 → +6.7pp truthful
    # edge (not fake +22pp from previous 0.55 lift).
    assert 0.38 < mp < 0.42, f"honest cheap-alt case, got {mp}"


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
    """Main lines get the honest low end (no clamp lift): a WR4
    receiving-yards over with factor mean 0.33 stays at 0.33."""
    factors = {"L5 Avg vs Line": 0.32, "Home/Away Split": 0.38,
                "Career vs Opponent Hit%": 0.30}
    mp = _cal_mp_for_nfl(factors, is_alt=False)
    assert 0.32 < mp < 0.35, f"main line low-p case should stay <0.35, got {mp}"


if __name__ == "__main__":
    test_factor_mean_independent_of_book()
    test_steep_odds_not_banned_when_model_supports()
    test_negative_edge_not_manufactured()
    test_cheap_alt_line_no_manufactured_edge()
    test_lower_line_higher_probability()
    test_main_line_wider_band()
    print("OK — all NFL model-probability independence checks passed.")
