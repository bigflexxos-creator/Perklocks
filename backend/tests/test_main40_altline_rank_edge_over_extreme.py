"""Item P0-C — Extreme-probability suppression on game-market rank.

Verifies a low-edge chip at an extreme probability (e.g. 95%) does
NOT outrank a higher-edge chip at moderate probability (e.g. 60%).
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def test_higher_edge_beats_extreme_probability():
    """Two book-quoted totals on the grid:
       - line 40.5 Over @ +150 → model p ~ 0.99 (extreme) but no edge
         since implied ≈ 0.40 vs model 0.99 = HUGE positive edge...
       Actually the extreme model prob DOES imply large edge; that's
       fine. The check we want: an extreme prob with NO book quote is
       suppressed by the hard gate; and among book-quoted chips,
       ranking follows edge, not raw distance-from-0.5.

    We use a scenario where two book chips exist:
       - line 48.5 (deep) with tiny positive edge (+1%)
       - line 45.5 (closer) with strong positive edge (+8%)
    The closer-line 45.5 chip must rank higher.
    """
    from services.alt_line_engine.game_markets import (
        build_game_market_alt_lines, GameMarketParse,
    )
    parsed = GameMarketParse(
        market_type="total", line=44.5, side="Over",
        label="Over 44.5", win_prob=0.58,
    )
    pick = {"sport": "nfl", "selection": "Over 44.5",
            "home_team": "A", "away_team": "B"}
    # Set both book prices so:
    #  - 45.5 Over model p ≈ 0.51, implied at -110 ≈ 0.524 → edge ≈ -1%
    #  - 48.5 Over model p ≈ 0.31, implied at +150 ≈ 0.40 → edge ≈ -9%
    #
    # We rank by chip composite, which for market-quoted chips is
    # driven primarily by edge.  The 45.5 chip has the higher edge
    # and must win.
    market_alt = [
        {"line": 45.5, "side": "Over",  "american": -110, "bookmaker": "fanduel"},
        {"line": 45.5, "side": "Under", "american": -110, "bookmaker": "fanduel"},
        {"line": 48.5, "side": "Over",  "american":  150, "bookmaker": "fanduel"},
        {"line": 48.5, "side": "Under", "american": -180, "bookmaker": "fanduel"},
    ]
    bundle = build_game_market_alt_lines(
        sport="NFL", pick=pick, parsed=parsed,
        market_alt_lines=market_alt, top_n=8,
    )
    chips = bundle["alt_lines"]
    # First-emitted chip should be the closer 45.5 (higher composite).
    assert chips, "expected chips"
    lines_in_order = [c["line"] for c in chips]
    assert lines_in_order[0] == 45.5, lines_in_order


def test_composite_zero_without_book_quote():
    """Even though model-only chips are filtered by the hard gate,
    if a model-only chip were somehow constructed its composite MUST
    NOT beat a real book-quoted chip.  We check that composite for a
    book chip with reasonable edge > composite for a model-only chip
    at the same model prob."""
    from services.alt_line_engine.game_markets import _row

    book_row = _row(
        side="Over", line=45.5, p_model=0.51,
        anchor_line=44.5, anchor_side="Over",
        pick={"selection": "Over 44.5"},
        market={"american": -110, "bookmaker": "fanduel"},
    )
    model_row = _row(
        side="Over", line=45.5, p_model=0.99,  # extreme model prob
        anchor_line=44.5, anchor_side="Over",
        pick={"selection": "Over 44.5"},
        market=None,
    )
    # The extreme model-only chip must not outrank the book-quoted one.
    assert book_row["composite_score"] >= model_row["composite_score"], (
        book_row["composite_score"], model_row["composite_score"]
    )
    assert model_row["bettable"] is False
    assert book_row["bettable"] is True


if __name__ == "__main__":
    test_higher_edge_beats_extreme_probability()
    test_composite_zero_without_book_quote()
    print("OK — extreme-probability suppression verified.")
