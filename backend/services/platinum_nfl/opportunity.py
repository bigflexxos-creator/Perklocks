"""Role / opportunity distributions (Block 2B.1A §5, §11, §14).

Every player-prop simulation MUST start from opportunity, not from
recent stat averages.  This module produces per-play / per-drive
opportunity distributions for QB / RB / WR / TE that the player-market
simulator then combines with efficiency samples.

Preseason regime (§11): starter/back-up rotation is a first-class
input — a QB expected to play 1 quarter cannot be treated like a
full-game regular-season QB.  ``QBOpportunity.expected_quarters``
scales the plays-in-game to a fraction of full-game.

Committee / target-share uncertainty is explicit (``share_uncertainty``
field) — small samples widen it, established roles narrow it.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Optional

from services.platinum_nfl.football_core import (
    ShrinkageEstimator, LEAGUE_QB_COMP_RATE, LEAGUE_WR_TARGET_CATCH_RATE,
)


# ═════════════════════════════════════════════════════════════════════
# QB
# ═════════════════════════════════════════════════════════════════════
@dataclass
class QBOpportunity:
    """QB per-game opportunity envelope.

    Preseason-aware:
        expected_quarters ∈ (0, 4]     — QB fraction of full game
        role_certainty    ∈ [0, 1]     — 1.0 for locked starter
        rotation_risk     bool         — set when preseason rotation
                                           creates a workload cliff

    Attempts distribution parameters:
        att_mean          expected pass attempts (full game equiv)
        att_std           standard deviation
        target_share_qb   fraction of team dropbacks (≈ 1.0 for
                           starter, 0.0 for backup) — used for the
                           EFFECTIVE attempt count once quarters
                           are accounted for.
    """
    expected_quarters: float = 4.0
    role_certainty:    float = 1.0
    rotation_risk:     bool  = False
    att_mean:          float = 32.0
    att_std:           float = 5.5
    ypa_mean:          float = 7.2
    ypa_std:           float = 1.5
    comp_rate_mean:    float = LEAGUE_QB_COMP_RATE
    comp_rate_std:     float = 0.05
    rush_att_mean:     float = 3.0
    rush_att_std:      float = 2.0
    rush_ypc_mean:     float = 5.0
    rush_ypc_std:      float = 2.5

    def effective_scale(self) -> float:
        """Fraction of a full-game QB workload this player is
        expected to see, accounting for preseason quarters + role
        certainty."""
        q = max(0.25, min(4.0, self.expected_quarters))
        return (q / 4.0) * max(0.0, min(1.0, self.role_certainty))


@dataclass
class RBOpportunity:
    """RB per-game opportunity envelope.

    Committee uncertainty is explicit — ``share_uncertainty`` above
    ~0.20 signals a true committee where sampling variance in carries
    must be inflated.
    """
    expected_quarters:    float = 4.0
    role_certainty:       float = 1.0
    carry_share_mean:     float = 0.55       # of team rushes
    carry_share_std:      float = 0.10
    target_share_mean:    float = 0.08       # of team targets
    target_share_std:     float = 0.03
    goal_line_share:      float = 0.60
    ypc_mean:             float = 4.35
    ypc_std:              float = 1.1
    receiving_catch_rate: float = 0.75
    receiving_ypr:        float = 8.0
    share_uncertainty:    float = 0.10       # aggregate uncertainty

    def effective_scale(self) -> float:
        q = max(0.25, min(4.0, self.expected_quarters))
        return (q / 4.0) * max(0.0, min(1.0, self.role_certainty))


@dataclass
class WROpportunity:
    """WR/TE per-game opportunity envelope.

    ``target_share_mean`` is fraction of team pass attempts targeted
    at this player.  ``route_participation`` is fraction of team
    dropbacks the player runs a route on — used to scale
    target_share by playing time.
    """
    expected_quarters:      float = 4.0
    role_certainty:         float = 1.0
    route_participation:    float = 0.85     # 85% of dropbacks
    target_share_mean:      float = 0.20
    target_share_std:       float = 0.06
    catch_rate_mean:        float = LEAGUE_WR_TARGET_CATCH_RATE
    catch_rate_std:         float = 0.08
    ypt_mean:               float = 8.2
    ypt_std:                float = 2.5
    red_zone_share:         float = 0.15

    def effective_scale(self) -> float:
        q = max(0.25, min(4.0, self.expected_quarters))
        return (q / 4.0) * max(0.0, min(1.0, self.role_certainty))


# ═════════════════════════════════════════════════════════════════════
# Preseason adjustments (§11)
# ═════════════════════════════════════════════════════════════════════
def apply_preseason_regime(opp) -> None:
    """Inflate uncertainty and clip effective quarters for preseason
    role objects.  Mutates ``opp`` in place.  Idempotent per-object.

    Rules:
        * QB usually plays 1 quarter (Wk1), 1-2 (Wk2), 2-3 (Wk3).
          We use a generic 1.5-quarter default when role_certainty
          is high; unknown roles get 1.0 quarter.
        * Role certainty capped at 0.85 (never claim locked starter
          in preseason).
        * Attempt / target / carry variance inflated by +35%.
        * Committee uncertainty inflated by +50% for RBs.
    """
    if isinstance(opp, QBOpportunity):
        if opp.role_certainty >= 0.7:
            opp.expected_quarters = min(opp.expected_quarters, 1.5)
        else:
            opp.expected_quarters = min(opp.expected_quarters, 1.0)
        opp.role_certainty = min(0.85, opp.role_certainty)
        opp.rotation_risk = True
        opp.att_std       *= 1.35
        opp.ypa_std       *= 1.20
        opp.comp_rate_std *= 1.15
        opp.rush_att_std  *= 1.35
    elif isinstance(opp, RBOpportunity):
        opp.expected_quarters = min(opp.expected_quarters, 2.0)
        opp.role_certainty    = min(0.80, opp.role_certainty)
        opp.carry_share_std  *= 1.35
        opp.target_share_std *= 1.35
        opp.ypc_std          *= 1.20
        opp.share_uncertainty = max(opp.share_uncertainty * 1.5, 0.15)
    elif isinstance(opp, WROpportunity):
        opp.expected_quarters   = min(opp.expected_quarters, 2.0)
        opp.role_certainty      = min(0.80, opp.role_certainty)
        opp.target_share_std   *= 1.35
        opp.catch_rate_std     *= 1.20
        opp.ypt_std            *= 1.25
        opp.route_participation = min(0.80, opp.route_participation)


# ═════════════════════════════════════════════════════════════════════
# Samplers
# ═════════════════════════════════════════════════════════════════════
def sample_qb_opportunity(opp: QBOpportunity, seed: random.Random,
                          *, game_pass_rate: float,
                          team_plays: float) -> dict:
    """Draw one game realization of QB opportunity given game context.

    Returns:
        attempts:  int passing attempts
        rush_att:  int rushing attempts
        comp_rate: float completion rate for THIS game
        ypa:       float yards per attempt for THIS game
        int_rate:  float per-attempt INT rate
    """
    scale = opp.effective_scale()
    # Team dropbacks = team_plays * pass_rate; QB attempts is (dropbacks - sacks) * role_share.
    team_dropbacks = team_plays * game_pass_rate
    # Approx: 6% of dropbacks -> sacks (not attempts).
    team_attempts = team_dropbacks * 0.94 * scale
    # Sample attempts around expected.
    att_expected = max(4.0, team_attempts)
    # Blend expected with the player's opp.att_mean * scale.
    att_blend = 0.7 * att_expected + 0.3 * (opp.att_mean * scale)
    attempts = max(1, int(round(seed.gauss(att_blend, opp.att_std * scale))))
    comp_rate = max(0.30, min(0.85, seed.gauss(
        opp.comp_rate_mean, opp.comp_rate_std)))
    ypa = max(2.5, seed.gauss(opp.ypa_mean, opp.ypa_std))
    rush_att = max(0, int(round(seed.gauss(
        opp.rush_att_mean * scale, opp.rush_att_std * scale))))
    return {
        "attempts":  attempts,
        "comp_rate": comp_rate,
        "ypa":       ypa,
        "int_rate":  0.023,
        "rush_att":  rush_att,
    }


def sample_rb_opportunity(opp: RBOpportunity, seed: random.Random,
                          *, team_plays: float,
                          game_pass_rate: float) -> dict:
    scale = opp.effective_scale()
    team_rushes = team_plays * (1.0 - game_pass_rate)
    team_targets = team_plays * game_pass_rate * 0.94
    carry_share = max(0.0, min(1.0, seed.gauss(
        opp.carry_share_mean, opp.carry_share_std)))
    target_share = max(0.0, min(0.5, seed.gauss(
        opp.target_share_mean, opp.target_share_std)))
    carries = max(0, int(round(team_rushes * carry_share * scale)))
    targets = max(0, int(round(team_targets * target_share * scale)))
    ypc = max(0.5, seed.gauss(opp.ypc_mean, opp.ypc_std))
    return {
        "carries":     carries,
        "targets":     targets,
        "ypc":         ypc,
        "catch_rate":  opp.receiving_catch_rate,
        "ypr":         opp.receiving_ypr,
        "goal_line":   opp.goal_line_share * scale,
    }


def sample_wr_opportunity(opp: WROpportunity, seed: random.Random,
                          *, team_plays: float,
                          game_pass_rate: float) -> dict:
    scale = opp.effective_scale()
    # WR/TE opportunity = team dropbacks * route_participation *
    #   target_share.  We sample target_share once per game.
    team_dropbacks = team_plays * game_pass_rate
    routes = team_dropbacks * opp.route_participation * scale
    target_share = max(0.02, min(0.42, seed.gauss(
        opp.target_share_mean, opp.target_share_std)))
    targets = max(0, int(round(routes * target_share)))
    catch_rate = max(0.35, min(0.90, seed.gauss(
        opp.catch_rate_mean, opp.catch_rate_std)))
    ypt = max(3.0, seed.gauss(opp.ypt_mean, opp.ypt_std))
    return {
        "routes":     int(round(routes)),
        "targets":    targets,
        "catch_rate": catch_rate,
        "ypt":        ypt,
        "red_zone_share": opp.red_zone_share * scale,
    }


__all__ = [
    "QBOpportunity", "RBOpportunity", "WROpportunity",
    "apply_preseason_regime",
    "sample_qb_opportunity", "sample_rb_opportunity",
    "sample_wr_opportunity",
]
