"""UNIVERSAL STAT RESOLVER — shared by every downstream consumer.

Locks in the contract that ``services.pick_matchup_wiring.resolve_market_stat``
is the ONE canonical entry point for pick → stat-family routing.
Every consumer (Alt-Line Magic, Matchup Intelligence, Similar-Matchup,
Prop H2H) MUST see the same stat family for the same pick.
"""
from __future__ import annotations


def test_universal_resolver_exposes_public_name():
    from services import pick_matchup_wiring as pmw
    assert callable(getattr(pmw, "resolve_market_stat", None)), (
        "resolve_market_stat must be a public exported callable"
    )


def test_universal_resolver_matches_legacy_for_simple_cases():
    """When no pick / threshold context is supplied and the market is
    unambiguous, the resolver returns EXACTLY the same key as the
    legacy ``_detect_stat``.  Zero-drift for existing call sites.
    """
    from services.pick_matchup_wiring import _detect_stat, resolve_market_stat
    cases = [
        ("MLB",    "Aaron Judge (NYY) Over 1.5 Total Bases"),
        ("MLB",    "Aaron Judge (NYY) Over 0.5 Hits"),
        ("MLB",    "Aaron Judge (NYY) Over 0.5 Home Runs"),
        ("NFL",    "Joe Burrow Over 249.5 Passing Yards"),
        ("NFL",    "Josh Allen Over 24.5 Rushing Yards"),
        ("NBA",    "Nikola Jokic Over 24.5 Points"),
        ("NBA",    "Nikola Jokic Over 9.5 Rebounds"),
        ("TENNIS", "Novak Djokovic Over 8.5 Aces"),
        ("SOCCER", "Erling Haaland Anytime Goal Scorer"),
    ]
    for sport, market in cases:
        legacy = _detect_stat(sport, market)
        resolved = resolve_market_stat(sport, market)
        assert legacy == resolved, (
            f"drift on {sport}/{market!r}: legacy={legacy!r} "
            f"resolved={resolved!r}"
        )


def test_universal_resolver_disambiguates_mlb_pitcher_k_by_pick_metadata():
    """The resolver must promote MLB "strikeouts" to
    "pitcher_strikeouts" when the pick carries canonical or provider
    metadata proving the market is a pitcher K prop.
    """
    from services.pick_matchup_wiring import resolve_market_stat
    # canonical_market_family authoritative
    stat = resolve_market_stat(
        "MLB", "Kevin Gausman (CHC) Over 4.5 Strikeouts",
        pick={"canonical_market_family": "pitcher_strikeouts"},
    )
    assert stat == "pitcher_strikeouts"
    # provider_market_key authoritative
    stat = resolve_market_stat(
        "MLB", "Zack Wheeler (PHI) Over 7.5 Strikeouts",
        pick={"provider_market_key": "pitcher_strikeouts_alternate"},
    )
    assert stat == "pitcher_strikeouts"


def test_universal_resolver_disambiguates_mlb_pitcher_k_by_threshold():
    """Legacy rows have no canonical metadata; the resolver must
    fall back to line ≥ 3.5 → pitcher.  This is the exact failure
    mode the user reported on the Kevin Gausman Over 3.5 K card.
    """
    from services.pick_matchup_wiring import resolve_market_stat
    # Explicit threshold arg
    stat = resolve_market_stat(
        "MLB", "Kevin Gausman (TOR) Over 3.5 Strikeouts",
        threshold=3.5,
    )
    assert stat == "pitcher_strikeouts"
    # Threshold inferred from market string alone (no pick, no
    # explicit threshold — resolver must still parse the market
    # text and route correctly).
    stat = resolve_market_stat(
        "MLB", "Kevin Gausman (TOR) Over 3.5 Strikeouts",
    )
    assert stat == "pitcher_strikeouts"


def test_universal_resolver_batter_k_props_unchanged():
    from services.pick_matchup_wiring import resolve_market_stat
    for line in (0.5, 1.5, 2.5):
        assert resolve_market_stat(
            "MLB", f"Aaron Judge (NYY) Over {line} Strikeouts",
        ) == "strikeouts"


def test_matchup_payload_uses_universal_resolver():
    """Matchup Intelligence must route through the same resolver so
    pitcher K picks trigger pitcher-model paths there too, not the
    empty-batter-Ks fallback.
    """
    import inspect
    from services import pick_matchup_wiring as pmw
    src = inspect.getsource(pmw.build_matchup_payload)
    assert "resolve_market_stat" in src, (
        "build_matchup_payload must delegate to resolve_market_stat "
        "so Alt-Line Magic and Matchup Intelligence never disagree "
        "on stat family for a pitcher K pick."
    )


def test_fusion_parse_uses_universal_resolver():
    """The Alt-Line Magic parser must also delegate to the resolver."""
    import inspect
    from services import pick_fusion_decorator as pfd
    src = inspect.getsource(pfd._parse_pick)
    assert "resolve_market_stat" in src
