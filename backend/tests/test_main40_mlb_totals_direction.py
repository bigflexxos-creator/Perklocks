"""MAIN 40 · Closure item #4 — MLB totals factor direction fix.

Regression guard for the ``build_mlb_total_factors`` side-normalization
bug: prior to this fix, ``Combined Bullpen`` and ``Starter Quality``
were in the OVER_FAVOURABLE set, so Under picks had those factors
inverted — turning legitimate Under evidence (better bullpens / better
starters → fewer runs) into anti-Under noise, and Over picks used the
raw Under-favourable value as if it were pro-Over.

Post-fix invariant:
    higher factor value  == stronger evidence FOR the selected side.
"""
from __future__ import annotations
import pytest
from services.mlb_feature_engine import build_mlb_total_factors


def _ctx_with(starters=(120.0, 120.0), bullpen_era=(2.80, 2.80),
              team_runs=(3.0, 3.0), park_run_total=(3.0, 3.0)):
    """Ctx with peak Under-favourable inputs by default."""
    return {
        "home_team": "boston red sox",
        "away_team": "new york yankees",
        "starting_pitcher_home": {"stuff_plus": starters[0]},
        "starting_pitcher_away": {"stuff_plus": starters[1]},
        "bullpens": {
            "boston red sox": {"era": bullpen_era[0]},
            "new york yankees": {"era": bullpen_era[1]},
        },
        "team_runs": {
            "boston red sox": team_runs[0],
            "new york yankees": team_runs[1],
        },
        "park_run_total": park_run_total[0],   # engine reads one field
    }


def test_under_pick_elite_bullpen_stays_high_signal():
    """Elite combined bullpens (low ERA) MUST show as strong Under
    evidence (factor ≈ 1.0) on an Under pick — not inverted to ~0.0."""
    ctx = _ctx_with(bullpen_era=(2.80, 2.80))
    factors, _ = build_mlb_total_factors(ctx, side="under")
    assert factors["Combined Bullpen"] is not None
    assert factors["Combined Bullpen"] >= 0.7, (
        f"elite bullpens gave Under factor {factors['Combined Bullpen']} — "
        "should be a strong Under signal (>=0.7)")


def test_under_pick_elite_starters_stays_high_signal():
    """Elite combined starters (stuff+ ≈ 120) MUST show as strong
    Under evidence on an Under pick."""
    ctx = _ctx_with(starters=(120.0, 120.0))
    factors, _ = build_mlb_total_factors(ctx, side="under")
    assert factors["Starter Quality"] is not None
    assert factors["Starter Quality"] >= 0.7, (
        f"elite starters gave Under factor {factors['Starter Quality']} — "
        "should be a strong Under signal (>=0.7)")


def test_over_pick_weak_bullpen_stays_high_signal():
    """Weak bullpens on an Over pick MUST produce a STRONGER Over
    signal than elite bullpens on an Over pick (both directions
    exercised through the flip)."""
    weak, _ = build_mlb_total_factors(_ctx_with(bullpen_era=(5.20, 5.20)), side="over")
    elite, _ = build_mlb_total_factors(_ctx_with(bullpen_era=(2.80, 2.80)), side="over")
    assert weak["Combined Bullpen"] is not None and elite["Combined Bullpen"] is not None
    assert weak["Combined Bullpen"] > elite["Combined Bullpen"], (
        f"weak bullpen Over factor ({weak['Combined Bullpen']}) must exceed "
        f"elite bullpen Over factor ({elite['Combined Bullpen']})")


def test_over_pick_elite_starters_becomes_low_signal():
    """Elite combined starters on an Over pick MUST show as WEAK Over
    evidence (they're Under-favourable, so we flip to ~0.0-0.3 for Over)."""
    ctx = _ctx_with(starters=(120.0, 120.0))
    factors, _ = build_mlb_total_factors(ctx, side="over")
    assert factors["Starter Quality"] is not None
    assert factors["Starter Quality"] <= 0.3, (
        f"elite starters gave Over factor {factors['Starter Quality']} — "
        "should be weak Over signal (<=0.3)")


def test_over_favourable_offense_unchanged_for_over():
    """`Combined Team Offense` is Over-favourable — an Over pick with
    strong combined offense must show a HIGH factor (unflipped)."""
    ctx = _ctx_with(team_runs=(6.0, 6.0))
    factors, _ = build_mlb_total_factors(ctx, side="over")
    assert factors["Combined Team Offense"] is not None
    assert factors["Combined Team Offense"] >= 0.7
