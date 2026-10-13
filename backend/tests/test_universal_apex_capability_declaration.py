"""Block 48 — Universal Apex capability declaration.

Each supported market family MUST declare whether it is STRUCTURALLY
Apex-reachable.  No arbitrary per-market ceilings.
"""
from __future__ import annotations

import pytest

from services.universal_apex_capability import is_universal_apex_capable


_EXPECTED_APEX_CAPABLE = [
    # NFL
    ("NFL", "moneyline"), ("NFL", "point_spread"), ("NFL", "game_total"),
    ("NFL", "qb_passing_yards"), ("NFL", "qb_passing_tds"),
    ("NFL", "rb_rushing_yards"), ("NFL", "wr_receiving_yards"),
    ("NFL", "wr_receptions"),
    # MLB
    ("MLB", "hitter_hits"), ("MLB", "hitter_total_bases"),
    ("MLB", "hitter_home_runs"), ("MLB", "hitter_rbis"),
    ("MLB", "pitcher_strikeouts"), ("MLB", "pitcher_outs"),
    ("MLB", "nrfi_yrfi"),
    # NHL — Block 27 reachability contract
    ("NHL", "nhl_goals"), ("NHL", "nhl_sog"),
    ("NHL", "nhl_assists"), ("NHL", "nhl_points"),
    ("NHL", "moneyline"), ("NHL", "puck_line"),
    # Soccer — Block 20 reachability contract
    ("Soccer", "goalscorer_anytime"),
    ("Soccer", "goalscorer_score_or_assist"),
    ("Soccer", "moneyline"), ("Soccer", "game_total"),
    # Tennis — Block 54
    ("Tennis", "tennis_match_winner"),
    ("Tennis", "tennis_total_games"),
    # CFB
    ("CFB", "moneyline"), ("CFB", "game_total"),
    # NBA — structural capability, not current (model still MODEL_UNAVAILABLE)
    ("NBA", "nba_points"), ("NBA", "nba_rebounds"),
    ("NBA", "nba_assists"), ("NBA", "nba_threes"),
]


@pytest.mark.parametrize("sport,family", _EXPECTED_APEX_CAPABLE)
def test_market_is_structurally_apex_capable(sport, family):
    assert is_universal_apex_capable(sport, family), (
        f"{sport}/{family} should be structurally Apex-reachable per Block 48"
    )


def test_nba_structural_capability_does_not_imply_current_activation():
    """Blocks 39-40: NBA families are structurally Apex-capable but
    their actual model remains MODEL_UNAVAILABLE until a certified
    independent model is wired.  Capability ≠ activation."""
    from services.sport_capability_matrix import (
        is_universal_apex_capable, is_model_capable, is_model_unavailable,
    )
    # Structurally capable
    assert is_universal_apex_capable("NBA", "nba_points")
    # But currently NOT model-capable
    assert is_model_unavailable("NBA", "nba_points")
    assert not is_model_capable("NBA", "nba_points")


if __name__ == "__main__":
    import subprocess, sys
    sys.exit(subprocess.call(["python", "-m", "pytest", __file__, "-v"]))
