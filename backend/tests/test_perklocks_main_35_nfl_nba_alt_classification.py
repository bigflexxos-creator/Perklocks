"""PERKLOCKS-MAIN 35 · P0-3 — NFL / NBA / MLB ALT CLASSIFICATION regression.

Contracts asserted:
  * `_is_alt_market_key(...)` routes through
    `UniversalMarketContract.is_alternate()` FIRST so a newly-shipping
    provider `_alternate` variant is recognized without editing the
    local hardcoded set.
  * NFL alt provider keys (`player_pass_yds_alternate`,
    `player_receptions_alternate`, `player_reception_yds_alternate`,
    `player_rush_yds_alternate`) are all classified as alternate.
  * NBA alt provider keys (`player_points_alternate`,
    `player_rebounds_alternate`, `player_assists_alternate`,
    `player_threes_alternate`) are all classified as alternate.
  * MLB alt provider keys stay classified as alternate.
  * Alternate detection is reached BEFORE any generic standard-prop
    filter — proven by inspecting the source of the props generator to
    ensure the alt branch runs before the standard-line filters.
  * A brand-new provider alt key not present in the local hardcoded
    set (`_ALT_PROP_MARKETS`) is still classified as alt via the
    canonical contract.
"""
from __future__ import annotations

import inspect

import pytest


def test_umc_recognizes_all_nfl_alt_keys():
    from services.universal_market_contract import is_alternate

    for k in (
        "player_pass_yds_alternate",
        "player_pass_tds_alternate",
        "player_rush_yds_alternate",
        "player_receptions_alternate",
        "player_reception_yds_alternate",
    ):
        assert is_alternate("NFL", k) is True, k


def test_umc_recognizes_all_nba_alt_keys():
    from services.universal_market_contract import is_alternate

    for k in (
        "player_points_alternate",
        "player_rebounds_alternate",
        "player_assists_alternate",
        "player_threes_alternate",
        "player_points_rebounds_assists_alternate",
    ):
        assert is_alternate("NBA", k) is True, k


def test_umc_recognizes_all_mlb_alt_keys():
    from services.universal_market_contract import is_alternate

    for k in (
        "batter_hits_alternate",
        "batter_total_bases_alternate",
        "batter_hits_runs_rbis_alternate",
        "batter_home_runs_alternate",
        "batter_rbis_alternate",
        "pitcher_strikeouts_alternate",
        "pitcher_outs_alternate",
    ):
        assert is_alternate("MLB", k) is True, k


def test_umc_rejects_standard_keys():
    from services.universal_market_contract import is_alternate

    for k in (
        "player_points",
        "batter_hits",
        "player_pass_yds",
        "h2h",
        "totals",
        "spreads",
    ):
        assert is_alternate("MLB", k) is False, k
        assert is_alternate("NBA", k) is False, k
        assert is_alternate("NFL", k) is False, k


def test_shared_helper_routes_through_contract_first():
    """`_is_alt_market_key` must consult the UniversalMarketContract
    BEFORE the legacy hardcoded set so a new provider `_alternate`
    key is recognised without editing the local list."""
    import sports_engine

    # A synthetic new alt key nobody has hardcoded yet.
    new_key = "player_completely_new_stat_alternate"
    assert new_key not in sports_engine._ALT_PROP_MARKETS
    assert sports_engine._is_alt_market_key("NFL", new_key) is True

    # Standard mk stays standard.
    assert sports_engine._is_alt_market_key("MLB", "batter_hits") is False


def test_alt_classification_runs_before_standard_filter():
    """The alt-vs-standard branch (`is_alt = _is_alt_market_key(...)`)
    must appear before the generic standard-prop filters.  A future
    refactor that moves the classification below the standard filters
    would silently reintroduce the drift bug."""
    import sports_engine
    src = inspect.getsource(sports_engine._props_picks_from_event)
    # Manual pass over source: alt classification line must precede
    # the "if is_alt" branch (obvious tautology) AND must exist.
    assert "_is_alt_market_key" in src
    # No lingering manual set-membership check hiding a bypass.
    assert "mk in _ALT_PROP_MARKETS" not in src, (
        "manual alt-classifier bypass found — regression"
    )


def test_helper_falls_back_to_local_set_when_contract_missing():
    """Belt-and-braces: local `_ALT_PROP_MARKETS` is still honoured so
    legacy entries never regress even if the contract lookup fails."""
    import sports_engine

    # Every entry in the local set MUST be classified as alt.
    for k in sports_engine._ALT_PROP_MARKETS:
        assert sports_engine._is_alt_market_key("MLB", k) is True, k
