"""Item P0-C — Multi-book price policy.

Verifies that when multiple sportsbooks quote the SAME (line, side),
the engine selects the BEST bettor-facing price and preserves
provenance (never relabels one book's quote as another).
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def test_best_price_across_multiple_books_wins():
    from services.alt_line_engine.game_markets import (
        build_game_market_alt_lines, GameMarketParse,
    )
    parsed = GameMarketParse(
        market_type="total", line=44.5, side="Over",
        label="Over 44.5", win_prob=0.55,
    )
    pick = {"sport": "nfl", "selection": "Over 44.5",
            "home_team": "A", "away_team": "B"}
    # Two sportsbooks quote Over/Under 48.5.  FanDuel offers -110,
    # DraftKings offers +105 (better for the bettor).  Engine MUST
    # pick DraftKings.
    market_alt = [
        {"line": 48.5, "side": "Over",  "american": -110,
         "bookmaker": "fanduel"},
        {"line": 48.5, "side": "Over",  "american":  105,
         "bookmaker": "draftkings"},
        {"line": 48.5, "side": "Under", "american": -105,
         "bookmaker": "fanduel"},
        {"line": 48.5, "side": "Under", "american": -115,
         "bookmaker": "draftkings"},
    ]
    bundle = build_game_market_alt_lines(
        sport="NFL", pick=pick, parsed=parsed,
        market_alt_lines=market_alt, top_n=8,
    )
    chips = bundle["alt_lines"]
    over  = next((c for c in chips if c["line"] == 48.5 and c["side"] == "Over"),  None)
    under = next((c for c in chips if c["line"] == 48.5 and c["side"] == "Under"), None)
    # Best Over price = +105 → DraftKings.  Best Under = -105 → FanDuel.
    assert over is not None
    assert over["american"] == 105
    assert over["bookmaker"] == "draftkings"
    assert under is not None
    assert under["american"] == -105
    assert under["bookmaker"] == "fanduel"


if __name__ == "__main__":
    test_best_price_across_multiple_books_wins()
    print("OK — multi-book price policy verified.")
