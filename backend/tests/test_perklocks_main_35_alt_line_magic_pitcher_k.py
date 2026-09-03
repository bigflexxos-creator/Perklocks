"""ALT-LINE MAGIC — PITCHER STRIKEOUTS ROOT FIX (2026-06-30).

Locks in the mandate that a pitcher-strikeouts pick (Kevin Gausman
Over 5.5 Strikeouts, Zack Wheeler Under 6.5 Strikeouts, etc.) is
disambiguated from a BATTER strikeouts pick BEFORE it reaches the
alt-line distribution grid.

Root cause (pre-fix):
  * ``_detect_stat`` returned ``"strikeouts"`` for BOTH batter and
    pitcher K props (market string is identical: "... Strikeouts").
  * Downstream ``_THRESHOLD_GRIDS`` keys the two families separately:
        (MLB, strikeouts)         → [0.5, 1.5, 2.5, 3.5]   (batter)
        (MLB, pitcher_strikeouts) → [3.5 – 11.5]           (pitcher)
  * A pitcher K prop mapped to the batter grid returned
    ``supported: False`` from every threshold because the ML batter
    model has no pitcher inference path, silently emptying the
    Alt-Line Magic bundle.  Every pitcher K card showed
    "No alternate lines available".

The fix is a surgical amend in ``pick_fusion_decorator._parse_pick``:
after ``_detect_stat`` returns "strikeouts" for an MLB pick, the
parser now routes to ``pitcher_strikeouts`` when EITHER:

    1. ``canonical_market_family`` starts with ``pitcher_strikeouts``
       (definitive — set by canonical publication),
    2. ``provider_market_key`` contains ``pitcher_strikeouts``
       (definitive — from Odds API market key), or
    3. threshold line > 3.5 (heuristic — batter K props top out at
       3.5, pitcher K props start at 3.5 and go up to 11.5).

No mock data, no fabricated probabilities — only the correct route
into the existing (already-trained) pitcher_strikeouts model &
threshold grid.
"""
from __future__ import annotations

from services.pick_fusion_decorator import _parse_pick


def _pick(*, market: str, canonical_market_family: str = None,
          provider_market_key: str = None) -> dict:
    return {
        "sport":                   "MLB",
        "market":                  market,
        "selection":               "Over",
        "event":                   "MIL @ CHC",
        "canonical_market_family": canonical_market_family,
        "provider_market_key":     provider_market_key,
    }


def test_pitcher_k_routed_by_canonical_market_family():
    p = _pick(
        market="Kevin Gausman (CHC) Over 5.5 Strikeouts",
        canonical_market_family="pitcher_strikeouts",
    )
    parsed = _parse_pick(p)
    assert parsed is not None
    assert parsed["stat"] == "pitcher_strikeouts", (
        f"canonical_market_family=pitcher_strikeouts must route to "
        f"pitcher_strikeouts, got {parsed['stat']!r}"
    )


def test_pitcher_k_alt_routed_by_canonical_market_family():
    """Alt-variant provider key must resolve the same way."""
    p = _pick(
        market="Kevin Gausman (CHC) Over 5.5 Strikeouts",
        canonical_market_family="pitcher_strikeouts",
        provider_market_key="pitcher_strikeouts_alternate",
    )
    assert _parse_pick(p)["stat"] == "pitcher_strikeouts"


def test_pitcher_k_routed_by_provider_market_key():
    p = _pick(
        market="Zack Wheeler (PHI) Over 7.5 Strikeouts",
        provider_market_key="pitcher_strikeouts",
    )
    assert _parse_pick(p)["stat"] == "pitcher_strikeouts"


def test_pitcher_k_routed_by_line_threshold_heuristic():
    """When canonical/provider metadata is missing (legacy row), a
    line ≥ 3.5 unambiguously identifies a pitcher K prop.  Real
    example: the screenshot pick "Kevin Gausman Over 3.5/5.5
    Strikeouts" carried no canonical_market_family and would previously
    return "strikeouts" (batter grid → 0 alt lines).
    """
    p = _pick(market="Kevin Gausman (CHC) Over 5.5 Strikeouts")
    parsed = _parse_pick(p)
    assert parsed["stat"] == "pitcher_strikeouts", (
        f"line=5.5 must route to pitcher grid; got {parsed['stat']!r}"
    )
    # Boundary — line = 3.5 is the START of the pitcher grid and
    # essentially never quoted for batters; routes to pitcher.
    p_boundary = _pick(market="Kevin Gausman (TOR) Over 3.5 Strikeouts")
    assert _parse_pick(p_boundary)["stat"] == "pitcher_strikeouts", (
        "line=3.5 must route to pitcher grid — batter K props are "
        "not quoted at 3.5 in practice; this is the Gausman case."
    )


def test_pitcher_k_routed_by_alt_lock_marker():
    """The " · ALT LOCK" market suffix is emitted only for
    pitcher_strikeouts_alternate (MLB Odds API has no batter K alt
    market).  Even without a threshold, the marker alone must route
    the pick to pitcher_strikeouts.
    """
    p = _pick(market="Kevin Gausman (TOR) Over Strikeouts  · ALT LOCK")
    parsed = _parse_pick(p)
    assert parsed is not None
    assert parsed["stat"] == "pitcher_strikeouts", (
        f"' · ALT LOCK' marker must route to pitcher grid, "
        f"got {parsed['stat']!r}"
    )


def test_batter_k_prop_still_maps_to_batter_grid():
    """Regression guard — a real batter K prop (line < 3.5, no
    canonical/pitcher signals) must remain routed to the batter grid.
    """
    for line in (0.5, 1.5, 2.5):
        p = _pick(market=f"Aaron Judge (NYY) Over {line} Strikeouts")
        assert _parse_pick(p)["stat"] == "strikeouts", (
            f"batter K prop at line={line} must stay on batter grid"
        )


def test_pitcher_strikeouts_has_matching_threshold_grid():
    """The stat name emitted by the parser MUST match a grid key in
    ``_THRESHOLD_GRIDS`` — otherwise the Alt-Line Magic bundle falls
    back to an empty state.
    """
    from services.alt_line_engine.distribution import _THRESHOLD_GRIDS
    assert ("MLB", "pitcher_strikeouts") in _THRESHOLD_GRIDS
    grid = _THRESHOLD_GRIDS[("MLB", "pitcher_strikeouts")]
    # Pitcher K lines span 3.5-11.5 — must include the screenshot
    # scenario (5.5) as a real threshold.
    assert 5.5 in grid, f"pitcher_strikeouts grid missing 5.5: {grid}"


def test_pitcher_strikeouts_whitelisted_by_safeguard():
    """The safeguard whitelist MUST accept pitcher_strikeouts;
    otherwise even the correct stat name gets blocked before the
    distribution engine runs.
    """
    from services.alt_line_engine.safeguards import _SUPPORTED_STAT_WHITELIST
    assert "pitcher_strikeouts" in _SUPPORTED_STAT_WHITELIST["MLB"]
