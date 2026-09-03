"""ALT-LINE MAGIC · TWO-WAY CHIPS + BOOK-PRICE SYNC (2026-06-30).

Locks in two contracts:

1. **Two-Way Chips.**  Every threshold-line in the returned bundle
   must produce BOTH the Over/Under (or team_covers/opp_covers) side
   so users can flip the pick straight from the chip row.

2. **Book-Price Sync.**  When the Odds API cache carries real
   sportsbook alt-line prices for a game-market pick (via
   ``alternate_spreads`` / ``alternate_totals`` /
   ``alternate_run_lines`` / ``alternate_puck_lines``), the chip
   MUST hydrate with ``source: "market"``, the real ``american``
   price, the real ``bookmaker`` key, and a computed
   ``edge_pct`` = p_model − p_implied.
"""
from __future__ import annotations

from services.alt_line_engine.game_markets import (
    build_game_market_alt_lines,
    GameMarketParse,
    _american_to_implied_prob,
)


# ─────────────────────────────────────────────────────────────────
# Two-way chip emission
# ─────────────────────────────────────────────────────────────────
def test_spread_bundle_emits_both_sides_per_line():
    parsed = GameMarketParse(
        market_type="spread", line=5.5, side="team",
        label="Knicks +5.5", win_prob=0.566,
    )
    bundle = build_game_market_alt_lines(
        sport="NBA", parsed=parsed,
        pick={"market": "New York Knicks +5.5 Spread",
               "selection": "New York Knicks", "line": 5.5,
               "win_probability": 56.6},
    )
    # Group chips by pair-key (abs threshold rounded).
    from collections import defaultdict
    pair_sides = defaultdict(set)
    for c in bundle["alt_lines"]:
        pair_sides[abs(c["line"])].add(c["side"])
    # Every unique line must have BOTH team_covers + opp_covers
    for line, sides in pair_sides.items():
        assert sides == {"team_covers", "opp_covers"}, (
            f"line={line} only produced sides={sides}"
        )


def test_total_bundle_emits_over_and_under_per_line():
    parsed = GameMarketParse(
        market_type="total", line=216.5, side="Under",
        label="Total Under 216.5", win_prob=0.466,
    )
    bundle = build_game_market_alt_lines(
        sport="NBA", parsed=parsed,
        pick={"market": "Total Points Under 216.5",
               "selection": "Under", "line": 216.5,
               "win_probability": 46.6},
    )
    from collections import defaultdict
    per_line = defaultdict(set)
    for c in bundle["alt_lines"]:
        per_line[c["line"]].add(c["side"])
    for line, sides in per_line.items():
        assert sides == {"Over", "Under"}, (
            f"total line={line} only produced {sides}"
        )


def test_player_prop_ranker_emits_both_sides_per_line():
    """The player-prop ranker groups by line and preserves BOTH
    Over/Under sides in the trimmed set."""
    import asyncio
    from unittest.mock import patch, AsyncMock
    from services.alt_line_engine.ranker import generate_alt_lines

    async def _drive():
        with patch(
            "services.alt_line_engine.ranker.is_safe_for_alt_lines",
            new=AsyncMock(return_value=(True, None)),
        ), patch(
            "services.alt_line_engine.ranker.build_outcome_distribution",
            new=AsyncMock(return_value={
                "supported": True,
                "thresholds": [
                    (3.5, 0.90, {"top_factors": [], "model": "mdl"}),
                    (4.5, 0.80, {"top_factors": [], "model": "mdl"}),
                    (5.5, 0.65, {"top_factors": [], "model": "mdl"}),
                    (6.5, 0.45, {"top_factors": [], "model": "mdl"}),
                ],
                "projected": 5.2, "residual_std": 1.1,
                "notes": [],
            }),
        ), patch(
            "services.alt_line_engine.ranker._fetch_bucket_roi",
            new=AsyncMock(return_value=None),
        ):
            bundle = await generate_alt_lines(
                db=None, sport="MLB", player="Kevin Gausman",
                stat="pitcher_strikeouts",
                pick={"win_probability": 65.0, "line": 5.5,
                       "market": "K Gausman Over 5.5 Strikeouts"},
                top_n=8,
            )
        from collections import defaultdict
        per_line = defaultdict(set)
        for a in bundle.alt_lines:
            per_line[a.line].add(a.side)
        assert len(per_line) >= 2, "must keep at least 2 lines"
        for line, sides in per_line.items():
            assert sides == {"Over", "Under"}, (
                f"player-prop line={line} produced {sides}, "
                "two-way contract broken"
            )

    asyncio.run(_drive())


# ─────────────────────────────────────────────────────────────────
# Book-price sync — spread
# ─────────────────────────────────────────────────────────────────
def test_spread_bundle_hydrates_real_book_prices():
    """When ``market_alt_lines`` carries a real sportsbook price for
    a (line, side) that lands in the emitted grid, the chip MUST
    hydrate with source=market, real american, real bookmaker,
    and computed edge_pct against the model probability.
    """
    parsed = GameMarketParse(
        market_type="spread", line=5.5, side="team",
        label="Knicks +5.5", win_prob=0.566,
    )
    pick = {"market": "New York Knicks +5.5 Spread",
             "selection": "New York Knicks", "line": 5.5,
             "win_probability": 56.6}
    # Simulate the endpoint's alt-line fetcher payload.  Odds API
    # spreads use TEAM names for outcome sides.
    book_rows = [
        {"line": 4.5, "side": "New York Knicks",
         "american": -110, "bookmaker": "draftkings"},
        # Opposing side too.
        {"line": -4.5, "side": "Boston Celtics",
         "american": -110, "bookmaker": "draftkings"},
    ]
    bundle = build_game_market_alt_lines(
        sport="NBA", parsed=parsed, pick=pick,
        market_alt_lines=book_rows,
        top_n=20,   # keep all grid lines so hydrated chip is not trimmed
    )
    # At least one chip on the picked-team side at line=4.5 should
    # hydrate.  Find it.
    match = next(
        (c for c in bundle["alt_lines"]
          if c["line"] == 4.5 and c["side"] == "team_covers"),
        None,
    )
    assert match is not None, "picked team @ +4.5 chip missing"
    assert match["source"] == "market"
    assert match["american"] == -110
    assert match["bookmaker"] == "draftkings"
    assert match["p_implied"] is not None
    assert isinstance(match["edge_pct"], (int, float))
    # notes must reflect that hydration happened.
    assert any("real book" in n.lower() for n in bundle["notes"]), (
        f"notes should announce book-price hydration, got {bundle['notes']}"
    )


# ─────────────────────────────────────────────────────────────────
# Book-price sync — total
# ─────────────────────────────────────────────────────────────────
def test_total_bundle_hydrates_real_book_prices():
    parsed = GameMarketParse(
        market_type="total", line=216.5, side="Under",
        label="Total Under 216.5", win_prob=0.466,
    )
    pick = {"market": "Total Points Under 216.5",
             "selection": "Under", "line": 216.5,
             "win_probability": 46.6}
    # Odds API alternate_totals: outcome side is Over/Under.
    # Use 213.5 which IS on the NBA total grid (3-point buckets).
    book_rows = [
        {"line": 213.5, "side": "Over",
         "american": -115, "bookmaker": "fanduel"},
        {"line": 213.5, "side": "Under",
         "american": -105, "bookmaker": "fanduel"},
    ]
    bundle = build_game_market_alt_lines(
        sport="NBA", parsed=parsed, pick=pick,
        market_alt_lines=book_rows,
        top_n=20,
    )
    for target_side, target_american in (("Over", -115), ("Under", -105)):
        m = next(
            (c for c in bundle["alt_lines"]
              if c["line"] == 213.5 and c["side"] == target_side),
            None,
        )
        assert m is not None, f"Total {target_side} @ 213.5 missing"
        assert m["source"] == "market"
        assert m["american"] == target_american
        assert m["bookmaker"] == "fanduel"


def test_american_to_implied_prob_math():
    """Sanity check the utility function that powers edge_pct."""
    # -110 → 52.38 %
    assert abs(_american_to_implied_prob(-110) - 0.5238) < 1e-3
    # +100 → 50 %
    assert abs(_american_to_implied_prob(+100) - 0.5000) < 1e-3
    # +200 → 33.33 %
    assert abs(_american_to_implied_prob(+200) - 0.3333) < 1e-3
    # -200 → 66.67 %
    assert abs(_american_to_implied_prob(-200) - 0.6667) < 1e-3


def test_chip_without_book_price_stays_model_projection():
    """Chips whose (line, side) has no matching book row must keep
    the model_projection tag — no fabricated 'market' source."""
    parsed = GameMarketParse(
        market_type="spread", line=5.5, side="team",
        label="Knicks +5.5", win_prob=0.566,
    )
    bundle = build_game_market_alt_lines(
        sport="NBA", parsed=parsed,
        pick={"market": "New York Knicks +5.5 Spread",
               "selection": "New York Knicks", "line": 5.5,
               "win_probability": 56.6},
        # No market_alt_lines
        market_alt_lines=None,
    )
    for c in bundle["alt_lines"]:
        assert c["source"] == "model_projection"
        assert c["bookmaker"] is None
        assert c["edge_pct"] is None
