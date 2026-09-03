"""ALT-LINE MAGIC · GAME-MARKET COVERAGE CONTRACT (2026-06-30).

Locks in the mandate that Alt-Line Magic surfaces alt-line chips
for **game-level** markets (Spread + Total) across every supported
sport — not just player props.  Moneyline is intentionally excluded
(single-outcome market, no alt grid possible).

Zero fabrication: every probability is a mathematical projection
derived from the pick's own ``win_probability`` + anchor line via
a back-solved Normal distribution on the underlying game random
variable (margin of victory for Spread, total points for Total).
"""
from __future__ import annotations

import math

from services.alt_line_engine.game_markets import (
    parse_game_market_pick,
    build_game_market_alt_lines,
    _normal_sf,
    _solve_normal_mean_for_anchor,
    _sigma_for,
    _grid_for_game_market,
    GameMarketParse,
)


# ─────────────────────────────────────────────────────────────────
# Market string parsing
# ─────────────────────────────────────────────────────────────────
def test_parse_spread_pick_dog_receiving_points():
    parsed = parse_game_market_pick({
        "market":          "New York Knicks +5.5 Spread",
        "selection":       "New York Knicks",
        "line":            5.5,
        "win_probability": 56.6,
    })
    assert parsed is not None
    assert parsed.market_type == "spread"
    assert parsed.line == 5.5
    assert "Knicks" in parsed.label
    assert 0.0 < parsed.win_prob < 1.0


def test_parse_spread_pick_favourite_laying_points():
    parsed = parse_game_market_pick({
        "market":          "Los Angeles Dodgers -1.5 Spread",
        "selection":       "Los Angeles Dodgers",
        "line":            -1.5,
        "win_probability": 72.0,
    })
    assert parsed.market_type == "spread"
    assert parsed.line == -1.5


def test_parse_run_line_and_puck_line():
    for label in ("Boston Red Sox +1.5 Run Line",
                   "Vegas Golden Knights -1.5 Puck Line"):
        parsed = parse_game_market_pick({
            "market": label, "selection": label.split()[0],
            "line": 1.5, "win_probability": 60.0,
        })
        assert parsed is not None
        assert parsed.market_type == "spread"


def test_parse_total_over_and_under():
    over = parse_game_market_pick({
        "market": "Total Points Over 216.5",
        "selection": "Over", "line": 216.5, "win_probability": 54.0,
    })
    assert over.market_type == "total" and over.side == "Over"
    under = parse_game_market_pick({
        "market": "Total Points Under 216.5",
        "selection": "Under", "line": 216.5, "win_probability": 46.6,
    })
    assert under.market_type == "total" and under.side == "Under"


def test_parse_moneyline_rejected():
    """Moneyline is a single-outcome market — no alt grid possible.
    The parser MUST return ``None`` so the endpoint falls back to
    the player-prop path (which also rejects it), not synthesise a
    meaningless alt-line bundle.
    """
    assert parse_game_market_pick({
        "market": "Miami Marlins Moneyline",
        "selection": "Miami Marlins",
        "win_probability": 62.0,
    }) is None


def test_parse_player_prop_rejected():
    """Player-prop picks must remain routed through the player-prop
    engine, NOT the game-market engine."""
    assert parse_game_market_pick({
        "market": "Kevin Gausman (TOR) Over 5.5 Strikeouts",
        "selection": "Kevin Gausman",
        "win_probability": 61.0,
    }) is None


# ─────────────────────────────────────────────────────────────────
# Distribution math
# ─────────────────────────────────────────────────────────────────
def test_spread_normal_reproduces_anchor_probability():
    """The Normal we back-solve MUST reproduce the pick's own
    win_probability at the anchor line."""
    sigma = _sigma_for("NBA", "spread")
    # Knicks +5.5, wp=56.6% → team covers when M > -5.5, so
    # anchor_reference = -(-5.5)... wait, for a +5.5 pick,
    # anchor_reference = -line = -5.5.  M > -5.5 has prob 0.566.
    mu = _solve_normal_mean_for_anchor(-5.5, 0.566, sigma)
    assert mu is not None
    assert abs(_normal_sf(-5.5, mu, sigma) - 0.566) < 1e-3


def test_total_normal_reproduces_anchor_probability():
    sigma = _sigma_for("NBA", "total")
    mu = _solve_normal_mean_for_anchor(216.5, 0.534, sigma)
    assert abs(_normal_sf(216.5, mu, sigma) - 0.534) < 1e-3


def test_bundle_builder_spread_returns_ranked_chips():
    parsed = GameMarketParse(
        market_type="spread", line=5.5, side="team",
        label="New York Knicks +5.5", win_prob=0.566,
    )
    bundle = build_game_market_alt_lines(
        sport="NBA",
        pick={"market": "New York Knicks +5.5 Spread",
               "selection": "New York Knicks", "line": 5.5,
               "win_probability": 56.6},
        parsed=parsed,
    )
    assert bundle["alt_lines"]
    assert len(bundle["alt_lines"]) >= 4
    # Composite scores sorted desc.
    scores = [a["composite_score"] for a in bundle["alt_lines"]]
    assert scores == sorted(scores, reverse=True)
    # Every chip has model_projection source & American price.
    for chip in bundle["alt_lines"]:
        assert chip["source"] == "model_projection"
        assert isinstance(chip["american"], int)
        assert 0.001 < chip["p_model"] < 0.999
        assert chip["composite_score"] >= 0.5


def test_bundle_builder_total_emits_over_and_under():
    parsed = GameMarketParse(
        market_type="total", line=216.5, side="Under",
        label="Total Under 216.5", win_prob=0.466,
    )
    bundle = build_game_market_alt_lines(
        sport="NBA",
        pick={"market": "Total Points Under 216.5",
               "selection": "Under", "line": 216.5,
               "win_probability": 46.6},
        parsed=parsed,
    )
    sides = {a["side"] for a in bundle["alt_lines"]}
    assert sides == {"Over", "Under"}, (
        f"total-market chips must include both Over AND Under, got {sides}"
    )


def test_grid_covers_realistic_alt_ranges():
    # NBA spread grid: anchor 5.5 → -0.5 up to +14.5 (approx).
    grid = _grid_for_game_market(5.5, "spread", "NBA")
    assert min(grid) <= 0.5 and max(grid) >= 11.5, (
        f"NBA spread grid too narrow: {grid}"
    )
    # MLB run line grid: anchor 1.5 → covers -2 up to +5.
    grid = _grid_for_game_market(1.5, "spread", "MLB")
    assert -2.0 <= min(grid) <= -1.5 and max(grid) >= 4.5


# ─────────────────────────────────────────────────────────────────
# Sport coverage: every game-market pick shape supported
# ─────────────────────────────────────────────────────────────────
def test_every_configured_sport_has_a_sigma():
    """Every sport that publishes game-market picks must have σ
    defaults for spread AND total, or the Normal degenerates to
    the fallback and probabilities compress toward 50 %.
    """
    from services.alt_line_engine.game_markets import _SIGMA_TABLE
    required = ["NFL", "NBA", "MLB", "NHL", "TENNIS", "SOCCER", "UFC"]
    for sport in required:
        assert (sport, "spread") in _SIGMA_TABLE, f"{sport} missing spread σ"
        assert (sport, "total") in _SIGMA_TABLE, f"{sport} missing total σ"


def test_end_to_end_produces_chips_across_sports():
    """Integration: run every combination through the bundle builder
    and confirm we get >=4 chips out.  Confirms the grid / σ / anchor
    math all interlock across sport-specific tunings.
    """
    scenarios = [
        # (sport, market_type, side, line, wp)
        ("NBA",    "spread", "team",  5.5,    56.6),
        ("NFL",    "spread", "team", -3.5,    52.0),
        ("MLB",    "spread", "team", -1.5,    72.0),
        ("NHL",    "spread", "team",  1.5,    60.0),
        ("TENNIS", "spread", "team", -3.5,    68.0),
        ("NBA",    "total",  "Over", 216.5,   54.0),
        ("NFL",    "total",  "Under", 47.5,   51.0),
        ("MLB",    "total",  "Over", 8.5,     55.0),
        ("SOCCER", "total",  "Over", 2.5,     58.0),
        ("TENNIS", "total",  "Over", 21.5,    44.0),
        ("UFC",    "total",  "Over", 1.5,     54.0),
    ]
    for sport, mt, side, line, wp in scenarios:
        parsed = GameMarketParse(
            market_type=mt, line=line, side=side, label="test",
            win_prob=wp / 100.0,
        )
        bundle = build_game_market_alt_lines(
            sport=sport, pick={"win_probability": wp, "line": line,
                                 "market": "test", "selection": side},
            parsed=parsed,
        )
        assert len(bundle["alt_lines"]) >= 4, (
            f"{sport}/{mt} produced only {len(bundle['alt_lines'])} chips"
        )
