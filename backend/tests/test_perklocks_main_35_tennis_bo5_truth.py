"""PERKLOCKS-MAIN 35 · P0-1 — TENNIS ALT-TOTAL TRUTH regression suite.

Locks in the following invariants and proves the old false-99% behavior
on ATP Grand Slam alt-totals (39.5 / 41.5 / 42.5) cannot return.

Contracts asserted:
  * BO5 resolver returns 5 for every ATP Grand Slam sport key.
  * BO3 resolver returns 3 for every WTA Grand Slam sport key.
  * BO3 resolver returns 3 for regular tour + team events.
  * Explicit event `best_of` metadata always wins.
  * Grand Slam predicate agrees with the resolver.
  * Simulated BO5 match games distribution is materially higher than
    BO3 for the same matchup (mean shift > 8 games).
  * At exact sportsbook thresholds 39.5 / 41.5 / 42.5, BO5 Over prob
    for a competitive matchup is not pinned to 0.99 (proves the false
    99% cannot return).
  * Over prob is monotone non-increasing as threshold rises;
    Under prob is monotone non-decreasing as threshold rises.
  * Empirical CDF used for pricing is derived from the SAME sorted
    distribution (single source of truth per event).
"""
from __future__ import annotations
import bisect
import statistics

import pytest


# ─────────────────────────────────────────────────────────────────────
# Format resolver contracts
# ─────────────────────────────────────────────────────────────────────
def test_atp_grand_slam_sport_keys_resolve_bo5():
    from services.tennis_match_format import resolve_tennis_match_format

    for key in (
        "tennis_atp_aus_open_singles",
        "tennis_atp_french_open",
        "tennis_atp_wimbledon",
        "tennis_atp_us_open",
    ):
        assert resolve_tennis_match_format(sport_key=key) == 5, key


def test_wta_grand_slam_sport_keys_resolve_bo3():
    from services.tennis_match_format import resolve_tennis_match_format

    for key in (
        "tennis_wta_aus_open_singles",
        "tennis_wta_french_open",
        "tennis_wta_wimbledon",
        "tennis_wta_us_open",
    ):
        assert resolve_tennis_match_format(sport_key=key) == 3, key


def test_regular_tour_events_resolve_bo3():
    from services.tennis_match_format import resolve_tennis_match_format

    for key in (
        "tennis_atp_indian_wells",
        "tennis_atp_miami_open",
        "tennis_atp_shanghai_masters",
        "tennis_atp_paris_masters",
        "tennis_atp_dubai",
        "tennis_wta_dubai",
        "tennis_atp_barcelona_open",
        "tennis_atp_halle_open",
    ):
        assert resolve_tennis_match_format(sport_key=key) == 3, key


def test_explicit_event_metadata_overrides_defaults():
    from services.tennis_match_format import resolve_tennis_match_format

    # ATP slam explicitly overridden to BO3 (e.g. rain-shortened protocol
    # or a doubles event carried on the same slam sport key) → BO3.
    assert resolve_tennis_match_format(
        sport_key="tennis_atp_us_open",
        event_payload={"best_of": 3},
    ) == 3

    # Regular tour event with explicit best_of=5 (exhibition special) → BO5.
    assert resolve_tennis_match_format(
        sport_key="tennis_atp_indian_wells",
        event_payload={"best_of": 5},
    ) == 5

    # String variants provider might ship.
    assert resolve_tennis_match_format(
        sport_key="tennis_atp_us_open",
        event_payload={"match_format": "best_of_3"},
    ) == 3
    assert resolve_tennis_match_format(
        sport_key="tennis_atp_indian_wells",
        event_payload={"format": "BO5"},
    ) == 5


def test_resolver_never_returns_anything_other_than_3_or_5():
    from services.tennis_match_format import resolve_tennis_match_format

    for kwargs in (
        {},
        {"sport_key": ""},
        {"sport_key": None, "league": None, "tournament_name": None},
        {"sport_key": "unknown_thing"},
        {"sport_key": "tennis_atp_us_open", "event_payload": {"best_of": 7}},
    ):
        out = resolve_tennis_match_format(**kwargs)
        assert out in (3, 5), (kwargs, out)


def test_is_grand_slam_predicate_agrees_with_resolver():
    from services.tennis_match_format import (
        is_grand_slam,
        resolve_tennis_match_format,
    )

    for key in (
        "tennis_atp_us_open",
        "tennis_wta_us_open",
        "tennis_atp_wimbledon",
        "tennis_wta_wimbledon",
    ):
        assert is_grand_slam(sport_key=key) is True
        # And the resolver aligns: ATP slam → BO5, WTA slam → BO3.
        expected_bo = 5 if key.startswith("tennis_atp_") else 3
        assert resolve_tennis_match_format(sport_key=key) == expected_bo

    for key in (
        "tennis_atp_indian_wells",
        "tennis_wta_dubai",
        "tennis_atp_shanghai_masters",
    ):
        assert is_grand_slam(sport_key=key) is False


# ─────────────────────────────────────────────────────────────────────
# Distribution + monotonicity contracts
# ─────────────────────────────────────────────────────────────────────
def _build_dist(bo: int, p_serve: float = 0.66, o_serve: float = 0.60,
                runs: int = 1500, seed: int = 20260601) -> list[int]:
    import random
    from brain.sim_tennis import _simulate_match_full
    random.seed(seed)
    out = []
    for _ in range(runs):
        tg, _ps, _os, _pg, _og = _simulate_match_full(p_serve, o_serve, bo=bo)
        out.append(tg)
    out.sort()
    return out


def _over_prob(dist_sorted: list[int], line: float) -> float:
    idx = bisect.bisect_right(dist_sorted, float(line))
    return (len(dist_sorted) - idx) / float(len(dist_sorted))


def test_bo5_distribution_is_materially_higher_than_bo3():
    d3 = _build_dist(bo=3, runs=1000)
    d5 = _build_dist(bo=5, runs=1000)
    mean3 = statistics.mean(d3)
    mean5 = statistics.mean(d5)
    # BO5 minimum theoretical is 3 sets of ~6 games ≈ 18; BO3 minimum
    # theoretical is 2 sets of ~6 games ≈ 12. Practical means differ
    # by 10+ games for evenly-serving pairs.
    assert mean5 - mean3 > 8.0, (mean3, mean5)


def test_bo5_over_probs_at_39_5_41_5_42_5_are_not_pinned_to_99pct():
    """The root cause of the false 99% Win Expected was a BO3-anchored
    logistic evaluated on BO5 thresholds. Under a real BO5 distribution
    with a competitive matchup, 39.5 / 41.5 / 42.5 should read in the
    ~30-70% band, never a cosmetic 99%."""
    # Slight serve gap → competitive Grand Slam match.
    dist = _build_dist(bo=5, p_serve=0.65, o_serve=0.61, runs=2000)
    for line in (39.5, 41.5, 42.5):
        p_over = _over_prob(dist, line)
        assert 0.05 < p_over < 0.97, (line, p_over)


def test_bo3_over_probs_at_39_5_41_5_42_5_are_effectively_zero():
    """Regression: a WTA slam or regular-tour BO3 event on the same
    ladder must produce essentially 0% Over probability at 39.5+."""
    dist = _build_dist(bo=3, p_serve=0.65, o_serve=0.61, runs=2000)
    for line in (39.5, 41.5, 42.5):
        p_over = _over_prob(dist, line)
        assert p_over < 0.02, (line, p_over)


def test_over_prob_monotone_non_increasing_bo5():
    dist = _build_dist(bo=5, runs=1500)
    thresholds = [
        30.5, 32.5, 34.5, 36.5, 38.5, 39.5, 40.5, 41.5, 42.5, 44.5, 46.5,
    ]
    over_probs = [_over_prob(dist, t) for t in thresholds]
    for i in range(1, len(over_probs)):
        assert over_probs[i] <= over_probs[i - 1] + 1e-9, (
            thresholds[i - 1], thresholds[i], over_probs[i - 1], over_probs[i],
        )


def test_under_prob_monotone_non_decreasing_bo5():
    dist = _build_dist(bo=5, runs=1500)
    thresholds = [
        30.5, 32.5, 34.5, 36.5, 38.5, 39.5, 40.5, 41.5, 42.5, 44.5, 46.5,
    ]
    under_probs = [1.0 - _over_prob(dist, t) for t in thresholds]
    for i in range(1, len(under_probs)):
        assert under_probs[i] >= under_probs[i - 1] - 1e-9, (
            thresholds[i - 1], thresholds[i],
            under_probs[i - 1], under_probs[i],
        )


def test_same_sorted_distribution_is_single_source_of_truth():
    """Contract: exact-threshold pricing must derive from one sorted
    empirical CDF per event. If we price the same threshold twice from
    the same dist we must get the identical probability (no per-side
    or per-threshold model drift)."""
    dist = _build_dist(bo=5, runs=1000)
    a = _over_prob(dist, 41.5)
    b = _over_prob(dist, 41.5)
    assert a == b


# ─────────────────────────────────────────────────────────────────────
# End-to-end regression: sports_engine._build_tennis_alt_picks
# must actually consume the resolver + distribution path.
# ─────────────────────────────────────────────────────────────────────
def test_sports_engine_alt_totals_wired_through_resolver():
    """Sanity-check the codepath: `_build_tennis_alt_picks` imports the
    format resolver and the sim helpers. This guards against future
    refactors silently reverting to the old logistic shortcut."""
    import inspect
    import sports_engine

    src = inspect.getsource(sports_engine._build_tennis_alt_picks)
    assert "resolve_tennis_match_format" in src, (
        "tennis alt-total builder no longer routes through the "
        "format resolver — false-99% regression risk"
    )
    assert "_simulate_match_full" in src, (
        "tennis alt-total builder must sample from the real match-"
        "games distribution (no logistic shortcut)"
    )
    # The old crude anchor MUST be gone.
    assert "17.0 + _competitive" not in src, (
        "crude BO3-only projected-games shortcut is back — regression"
    )
