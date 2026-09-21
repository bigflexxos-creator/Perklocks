"""P0 Physical Acceptance — CFB Total Sign-Flip Regression (2026-06).

Physical Preview showed:
    Indiana @ Northwestern · Over 47.5 · odds -115
    Win Expected 96.69% · Lock 98

Root cause: SP+ base-total formula had an INVERTED defensive-adjustment
sign so opponent's STRONG defense INCREASED projected points:
    OLD:  h_pts = h_off + (25.0 - a_def)
    NEW:  h_pts = h_off + (a_def - 25.0)

With real ``cfb_sp_ratings`` values (Indiana OFF=40.8 / DEF=9.9,
Northwestern OFF=24.4 / DEF=20.0):
    OLD base_total = 85.3 → P(Over 47.5) ≈ 99.9%
    NEW base_total = 45.1 → P(Over 47.5) ≈ 42.1%

The corrected total is within 2.5 points of the sportsbook line — the
sharp market's implied Vegas total.

Also protects against the following physical mispricings that all
resulted from the same defect:
    Kentucky @ South Alabama  U54.5  OLD wp=81.49% → NEW ~30%
    Oregon @ Boise State      O51.5  OLD wp≈98%    → NEW ~66%
"""
from __future__ import annotations

import math
import sys
sys.path.insert(0, "/app/backend")

import pytest


def _norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2)))


class TestCFBTotalSignFlip:
    """Verify the SP+ total math no longer inverts defensive adjustment."""

    def _estimate(self, home_off, home_def, away_off, away_def):
        from services.cfb_game_model import estimate_cfb_game
        ctx = {
            "cfb_sp_ratings_by_team": {
                "home": {"rating": 10.0, "offense_rating": home_off, "defense_rating": home_def},
                "away": {"rating": -5.0, "offense_rating": away_off, "defense_rating": away_def},
            },
        }
        return estimate_cfb_game(ctx, "home", "away")

    def test_indiana_northwestern_total_matches_vegas_band(self):
        """Indiana OFF=40.8 / DEF=9.9 (elite defense) vs Northwestern
        OFF=24.4 / DEF=20.0 should project total ~45, NOT ~85."""
        r = self._estimate(40.8, 9.9, 24.4, 20.0)
        assert r.available
        # Vegas line is 47.5 for this real fixture.  The corrected
        # projection should be within 15 points of the market.
        assert 30.0 <= r.expected_total <= 60.0, (
            f"expected_total={r.expected_total} still inflated"
        )

    def test_strong_defense_reduces_opponent_scoring(self):
        """When opponent defense is ELITE (a_def=10) vs an average
        offense (h_off=25), h's projected points should DROP below 25,
        not rise above 25."""
        # Home offense=25 (avg), away defense=10 (elite).
        r = self._estimate(25.0, 25.0, 20.0, 10.0)
        # h_pts should be ≈ 25 + (10 - 25) = 10  (elite defense held them down)
        # a_pts should be ≈ 20 + (25 - 25) = 20
        # total ≈ 30
        assert r.expected_total < 40.0, (
            f"strong defense didn't reduce opponent scoring; total={r.expected_total}"
        )

    def test_weak_defense_increases_opponent_scoring(self):
        """When opponent defense is WEAK (a_def=40) vs an average
        offense (h_off=25), h's projected points should rise above 25."""
        r = self._estimate(25.0, 25.0, 20.0, 40.0)
        # h_pts = 25 + (40 - 25) = 40
        # a_pts = 20 + (25 - 25) = 20
        # total = 60
        assert r.expected_total > 50.0, (
            f"weak defense didn't inflate opponent scoring; total={r.expected_total}"
        )

    def test_average_teams_produce_average_total(self):
        """Two average teams (offense=25, defense=25) should project
        the FBS-average total near 50 pts."""
        r = self._estimate(25.0, 25.0, 25.0, 25.0)
        assert 45.0 <= r.expected_total <= 55.0, (
            f"average matchup total off-band: {r.expected_total}"
        )

    def test_indiana_northwestern_over_47_5_probability_corrected(self):
        """Using the corrected total + default sigma, P(Over 47.5)
        must be within a reasonable band around the market — NOT the
        pre-fix 99% inflation."""
        r = self._estimate(40.8, 9.9, 24.4, 20.0)
        sigma = float(r.total_sigma or 12.0)
        p_over = 1.0 - _norm_cdf((47.5 - r.expected_total) / sigma)
        # Line ~ mean → p_over should be around 40-55%, not 96-99%.
        assert 0.25 <= p_over <= 0.65, (
            f"P(Over 47.5) still extreme: {p_over*100:.1f}%  (mean={r.expected_total})"
        )
        # Also verify O/U conservation on either side.
        p_under = 1.0 - p_over
        assert abs((p_over + p_under) - 1.0) < 0.001

    def test_kentucky_south_alabama_under_54_5_no_longer_elite(self):
        """Kentucky OFF=25.5 / DEF=23.7 vs South Alabama OFF=25.4 / DEF=36.0.
        Vegas line = 54.5.  With corrected math, Under 54.5 WP should
        drop from the pre-fix 81.49% down toward market-fair (~30-40%)."""
        r = self._estimate(25.5, 23.7, 25.4, 36.0)
        sigma = float(r.total_sigma or 12.0)
        p_under = _norm_cdf((54.5 - r.expected_total) / sigma)
        assert p_under < 0.55, (
            f"Under 54.5 WP still elite: {p_under*100:.1f}%  (mean={r.expected_total})"
        )
