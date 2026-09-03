"""ALT-LINE MAGIC · UNIVERSAL COVERAGE CONTRACT (2026-06-30).

Locks in the mandate that Alt-Line Magic surfaces chips for every
supported player-prop family across every supported sport, using the
universal projected-distribution fallback (Poisson for counts, Normal
for continuous stats) when a trained ML model is unavailable.

The two inputs the fallback consumes — ``win_probability`` and
``line`` — are ALREADY present on every published pick.  No mock
data, no fabricated probabilities: every P(over) is math derived
from the pick's own model output.
"""
from __future__ import annotations

import math
from services.alt_line_engine.universal_projection import (
    universal_distribution,
    _poisson_sf,
    _solve_poisson_mean,
    _solve_normal_mean,
    _normal_sf,
)


# ─────────────────────────────────────────────────────────────────
# Math primitives
# ─────────────────────────────────────────────────────────────────
def test_poisson_sf_matches_hand_computed_values():
    # P(X > 3.5) with λ=5 == 1 - CDF(3) = 1 - (e^-5)(1 + 5 + 25/2 + 125/6)
    lam = 5.0
    cdf3 = math.exp(-lam) * (1 + 5 + 12.5 + 125.0 / 6.0)
    assert abs(_poisson_sf(3.5, lam) - (1 - cdf3)) < 1e-9


def test_solve_poisson_matches_target_probability():
    # Given a real screenshot case: Gausman Over 16.5 Outs, wp≈0.60.
    # Back-solved λ must reproduce the SAME P(over) at the same line.
    lam = _solve_poisson_mean(16.5, 0.60)
    assert lam is not None
    assert abs(_poisson_sf(16.5, lam) - 0.60) < 1e-3
    # λ should land in the realistic Outs range (16-20).
    assert 14.0 < lam < 22.0


def test_solve_normal_matches_target_probability():
    # NFL passing yards: Burrow Over 249.5, wp=0.58.
    mu, sigma = _solve_normal_mean(249.5, 0.58)
    assert mu is not None
    assert abs(_normal_sf(249.5, mu, sigma) - 0.58) < 1e-2


# ─────────────────────────────────────────────────────────────────
# Universal fallback across sports
# ─────────────────────────────────────────────────────────────────
def test_universal_fallback_mlb_pitcher_outs():
    # Gausman-style Outs pick: proj=18, line=16.5, wp=60%.
    dist = universal_distribution(
        stat="pitcher_outs", line=16.5, win_probability=60.0,
        grid=[12.5, 13.5, 14.5, 15.5, 16.5, 17.5, 18.5, 19.5, 20.5],
    )
    assert dist and dist["supported"]
    assert len(dist["thresholds"]) == 9
    # Line at the anchor (16.5) must reproduce ~0.60.
    p_at_line = {ln: p for ln, p, _ in dist["thresholds"]}[16.5]
    assert 0.55 < p_at_line < 0.65


def test_universal_fallback_mlb_pitcher_strikeouts():
    dist = universal_distribution(
        stat="pitcher_strikeouts", line=5.5, win_probability=59.56,
        grid=[3.5, 4.5, 5.5, 6.5, 7.5, 8.5, 9.5, 10.5, 11.5],
    )
    assert dist and len(dist["thresholds"]) == 9
    # Monotone decreasing with threshold — sanity check.
    probs = [p for _, p, _ in dist["thresholds"]]
    assert all(probs[i] >= probs[i + 1] for i in range(len(probs) - 1))


def test_universal_fallback_nfl_passing_yards_continuous():
    # Burrow Over 249.5 Passing Yards, wp=57%.
    dist = universal_distribution(
        stat="passing_yards", line=249.5, win_probability=57.0,
        grid=[150.5, 200.5, 225.5, 249.5, 275.5, 300.5, 325.5],
    )
    assert dist and dist["supported"]
    # Continuous stat → normal model tag.
    assert "normal" in dist["thresholds"][0][2]["model"]


def test_universal_fallback_nba_rebounds():
    dist = universal_distribution(
        stat="rebounds", line=7.5, win_probability=61.0,
        grid=[3.5, 5.5, 7.5, 9.5, 11.5],
    )
    assert dist and len(dist["thresholds"]) == 5


def test_universal_fallback_soccer_shots():
    dist = universal_distribution(
        stat="shots_on_target", line=1.5, win_probability=54.0,
        grid=[0.5, 1.5, 2.5, 3.5],
    )
    assert dist and len(dist["thresholds"]) == 4


def test_universal_fallback_returns_none_on_degenerate_inputs():
    # Missing inputs
    assert universal_distribution(
        stat="hits", line=None, win_probability=50.0, grid=[0.5, 1.5]
    ) is None
    assert universal_distribution(
        stat="hits", line=1.5, win_probability=None, grid=[0.5, 1.5]
    ) is None
    # Impossible probabilities
    assert universal_distribution(
        stat="hits", line=1.5, win_probability=0.0, grid=[0.5, 1.5]
    ) is None
    assert universal_distribution(
        stat="hits", line=1.5, win_probability=100.0, grid=[0.5, 1.5]
    ) is None


# ─────────────────────────────────────────────────────────────────
# Contract: whitelist + grid coverage across every player-prop
# family the runtime can publish.
# ─────────────────────────────────────────────────────────────────
def test_whitelist_covers_every_grid_family():
    from services.alt_line_engine.safeguards import _SUPPORTED_STAT_WHITELIST
    from services.alt_line_engine.distribution import _THRESHOLD_GRIDS
    for (sport, stat) in _THRESHOLD_GRIDS.keys():
        assert sport in _SUPPORTED_STAT_WHITELIST, (
            f"grid {sport}/{stat} but sport not whitelisted"
        )
        assert stat in _SUPPORTED_STAT_WHITELIST[sport], (
            f"grid {sport}/{stat} but stat not whitelisted"
        )


def test_grid_covers_every_whitelist_stat():
    """Every whitelisted stat MUST have a threshold grid; otherwise
    the safeguard admits a market that the distribution engine
    immediately rejects.
    """
    from services.alt_line_engine.safeguards import _SUPPORTED_STAT_WHITELIST
    from services.alt_line_engine.distribution import _THRESHOLD_GRIDS
    for sport, stats in _SUPPORTED_STAT_WHITELIST.items():
        for stat in stats:
            assert (sport, stat) in _THRESHOLD_GRIDS, (
                f"whitelisted {sport}/{stat} has no threshold grid — "
                "distribution engine will reject with 'no threshold grid'"
            )


def test_pitcher_outs_detected_from_market_string():
    """The MLB market-stat table MUST recognise "Outs Recorded" and
    "Pitching Outs" so pitcher_outs picks reach the alt-line path.
    """
    from services.pick_matchup_wiring import _detect_stat
    assert _detect_stat("MLB",
                         "Kevin Gausman (TOR) Over 17.5 Outs Recorded") == "pitcher_outs"
    assert _detect_stat("MLB",
                         "Pitching Outs Over 15.5") == "pitcher_outs"


def test_safeguard_bypasses_history_when_pick_has_wp_and_line():
    """Universal-fallback bypass — a pick with win_probability + line
    MUST pass the safeguard even when the player has zero historical
    rows (e.g. rookie / mid-season call-up).
    """
    import asyncio
    from services.alt_line_engine.safeguards import is_safe_for_alt_lines

    class _FakeColl:
        async def find_one(self, *a, **k):
            return None
        async def count_documents(self, *a, **k):
            return 0

    class _FakeDb:
        def __getitem__(self, name):
            return _FakeColl()
        players = _FakeColl()
        player_game_actuals = _FakeColl()
        player_identities = _FakeColl()
        player_game_logs = _FakeColl()

    async def _drive():
        # Rookie with 0 history but pick carries wp + line via market.
        safe, reason = await is_safe_for_alt_lines(
            _FakeDb(), sport="MLB", player_name="Rookie Callup",
            stat="hits",
            pick={"win_probability": 62.5,
                    "market": "Rookie Callup (LAD) Over 0.5 Hits",
                    "line": None},
        )
        assert safe is True, f"blocked with reason: {reason}"

    asyncio.run(_drive())
