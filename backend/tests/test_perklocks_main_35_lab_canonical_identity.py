"""PERKLOCKS-MAIN 35 · P1-5 — LAB CANONICAL IDENTITY regression.

Contracts asserted:
  * `_classify_market_family` consults `canonical_market_family` on
    the pick BEFORE falling back to string heuristics.
  * A pick with `canonical_market_family = "hitter_home_runs"` and a
    weird / unusual market string (that would otherwise fall through
    to "MLB_OTHER") is classified as "MLB_HR".
  * MLB Hits+Runs+RBIs composite maps to MLB_HITS (grouped with
    hits-family for parlay-derived filter parity).
  * NBA points_rebounds_assists composite maps to NBA_POINTS.
  * All ATP/WTA Tennis canonical families route to Lab TEN_* buckets.
  * Legacy rows without `canonical_market_family` still work via the
    string heuristics (no regression).
  * A truly-unknown market on a truly-legacy row falls through to
    `<SPORT>_OTHER` — which the `_prettify_leg` guard already ejects
    from correlation output ("CORRELATION_LEG_IDENTITY_INCOMPLETE").
"""
from __future__ import annotations

import pytest


def _mk(**overrides):
    base = {
        "sport": "MLB",
        "market": "Aaron Judge Over 0.5 Home Runs",
    }
    base.update(overrides)
    return base


def test_canonical_family_wins_over_market_string_mlb_hr():
    from lab_routes import _classify_market_family

    # Deliberately unusual market string — string heuristic alone
    # would fall through to MLB_OTHER.
    pick = _mk(
        market="Judge, A. — season-long clout wager",
        canonical_market_family="hitter_home_runs",
    )
    assert _classify_market_family(pick["sport"], pick["market"], pick=pick) == "MLB_HR"


def test_canonical_family_wins_for_pitcher_strikeouts():
    from lab_routes import _classify_market_family

    pick = _mk(
        sport="MLB",
        market="Wheeler Ks Alt 4.5",  # doesn't include "strikeout" verbatim
        canonical_market_family="pitcher_strikeouts",
    )
    assert _classify_market_family(pick["sport"], pick["market"], pick=pick) == "MLB_KS"


def test_composite_hitter_hits_runs_rbis_maps_to_mlb_hits():
    from lab_routes import _classify_market_family

    pick = _mk(canonical_market_family="hitter_hits_runs_rbis")
    assert _classify_market_family(pick["sport"], pick["market"], pick=pick) == "MLB_HITS"


def test_composite_nba_pra_maps_to_nba_points():
    from lab_routes import _classify_market_family

    pick = {
        "sport": "NBA",
        "market": "Player PRA 42.5",
        "canonical_market_family": "nba_pra",
    }
    assert _classify_market_family(pick["sport"], pick["market"], pick=pick) == "NBA_POINTS"


def test_all_tennis_canonical_families_route_to_ten_buckets():
    from lab_routes import _classify_market_family

    for fam, expected in (
        ("tennis_match_winner", "TEN_MATCH"),
        ("tennis_total_games",  "TEN_GAMES"),
        ("tennis_game_handicap", "TEN_GAMES"),
    ):
        pick = {
            "sport": "Tennis",
            "market": "irrelevant string",
            "canonical_market_family": fam,
        }
        assert _classify_market_family("Tennis", pick["market"], pick=pick) == expected, fam


def test_legacy_row_without_canonical_still_works_via_string_heuristics():
    from lab_routes import _classify_market_family

    pick = _mk(market="Aaron Judge Over 0.5 Home Runs")  # no canonical field
    assert _classify_market_family("MLB", pick["market"], pick=pick) == "MLB_HR"


def test_unknown_market_falls_to_sport_other_bucket():
    from lab_routes import _classify_market_family

    # Truly unclassifiable. Falls to MLB_OTHER — the `_prettify_leg`
    # guard downstream ejects this from correlation output.
    pick = {"sport": "MLB", "market": "Some Weird Bet 2025"}
    fam = _classify_market_family("MLB", pick["market"], pick=pick)
    assert fam.endswith("_OTHER")


def test_prettify_leg_ejects_other_identity_from_correlation_output():
    from lab_routes import _prettify_leg
    # No market string + _OTHER family → incomplete identity marker.
    out = _prettify_leg("Aaron Judge", "MLB_OTHER")
    assert out == "CORRELATION_LEG_IDENTITY_INCOMPLETE"


def test_canonical_family_beats_string_that_would_fall_to_other():
    """Regression: an MLB pick whose display string doesn't match
    any string heuristic (would return MLB_OTHER) but DOES carry a
    canonical family MUST land in the right bucket."""
    from lab_routes import _classify_market_family

    pick = {
        "sport": "MLB",
        "market": "Custom exotic parlay leg description",
        "canonical_market_family": "hitter_total_bases",
    }
    assert _classify_market_family("MLB", pick["market"], pick=pick) == "MLB_TB"
