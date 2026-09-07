"""Item 4 — Alt-Line Magic real-line hard gate.

Verifies that:
  1. Model-only chips (source="model_projection") are FILTERED OUT of
     the emitted bundle for both the player-market and game-market
     paths.  Only chips backed by a real sportsbook quote survive.
  2. Chips carry an explicit ``bettable`` boolean that mirrors the
     hard gate for downstream consumers.
"""
from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def test_game_market_bundle_filters_model_only_chips():
    """Build a bundle with NO market_alt_lines.  Expect zero chips."""
    from services.alt_line_engine.game_markets import (
        build_game_market_alt_lines, GameMarketParse,
    )
    parsed = GameMarketParse(
        market_type="total", line=44.5, side="Over", label="Over 44.5",
        win_prob=0.58,
    )
    pick = {"sport": "nfl", "selection": "Over 44.5",
            "home_team": "A", "away_team": "B"}
    bundle = build_game_market_alt_lines(
        sport="NFL", pick=pick, parsed=parsed, top_n=8,
        market_alt_lines=None,
    )
    assert bundle["alt_lines"] == []
    joined_notes = " ".join(bundle["notes"])
    assert "hard gate" in joined_notes.lower()


def test_game_market_bundle_keeps_market_chips():
    from services.alt_line_engine.game_markets import (
        build_game_market_alt_lines, GameMarketParse,
    )
    parsed = GameMarketParse(
        market_type="total", line=44.5, side="Over", label="Over 44.5",
        win_prob=0.58,
    )
    pick = {"sport": "nfl", "selection": "Over 44.5",
            "home_team": "A", "away_team": "B"}
    # Provide one book-priced Over at 48.5 (in the NFL total grid).
    market_alt_lines = [
        {"line": 48.5, "side": "Over", "american": -110,
         "bookmaker": "fanduel"},
        {"line": 48.5, "side": "Under", "american": -110,
         "bookmaker": "fanduel"},
    ]
    bundle = build_game_market_alt_lines(
        sport="NFL", pick=pick, parsed=parsed, top_n=8,
        market_alt_lines=market_alt_lines,
    )
    assert bundle["alt_lines"], "expected bettable chip(s) to survive"
    for chip in bundle["alt_lines"]:
        assert chip["bettable"] is True
        assert chip["source"] == "market"


def test_player_market_ranker_filters_model_only_chips():
    """generate_alt_lines with no market_alt_lines and a mocked DB
    must emit an empty ``alt_lines`` list (all model_projection)."""
    from services.alt_line_engine.ranker import generate_alt_lines
    # A minimal async DB mock that returns nothing for all lookups.
    class _NoDB:
        def __getattr__(self, _n):  # collection lookup
            return self
        def find(self, *_a, **_k):  # cursor factory
            class _C:
                def sort(self, *_a, **_k): return self
                def limit(self, *_a, **_k): return self
                async def to_list(self, *_a, **_k): return []
                def __aiter__(self): return self
                async def __anext__(self): raise StopAsyncIteration
            return _C()
        async def find_one(self, *_a, **_k): return None
    async def _run():
        bundle = await generate_alt_lines(
            _NoDB(), sport="nba", player="LeBron James",
            stat="points", opponent="lakers",
            market_alt_lines=None,
            pick={"selection": "Over 25.5"},
        )
        d = bundle.to_dict()
        assert d["alt_lines"] == [], d["alt_lines"]
        assert any("hard gate" in (n or "").lower() for n in d["notes"])
    asyncio.get_event_loop().run_until_complete(_run())


if __name__ == "__main__":
    test_game_market_bundle_filters_model_only_chips()
    test_game_market_bundle_keeps_market_chips()
    test_player_market_ranker_filters_model_only_chips()
    print("OK — alt-line magic real-line hard gate verified.")
