"""Blocks 24-28 — NHL goalscorer evidence contract tests.

Declarative tests that the NHL goalscorer market remains structurally
Lock-Score-capable up to 100 Apex and that the forbidden
probability-seed list stays in place.  No formula change asserted.
"""
from __future__ import annotations

import pytest

from services.nhl_goalscorer_evidence_contract import (
    LEGITIMATE_INPUTS, FORBIDDEN_PROBABILITY_SEEDS,
    nhl_goalscorer_capability,
)
from services.sport_capability_matrix import is_universal_apex_capable


def test_nhl_goalscorer_structural_apex_reachable():
    cap = nhl_goalscorer_capability()
    assert cap.structural_apex_reachable is True
    assert cap.max_lock_score == 100
    assert is_universal_apex_capable("NHL", "nhl_goals")


def test_nhl_evidence_floors_present_in_contract():
    cap = nhl_goalscorer_capability()
    assert cap.minimum_player_recent_games == 5
    assert cap.minimum_team_recent_games == 5


def test_nhl_name_bonus_disabled():
    cap = nhl_goalscorer_capability()
    assert cap.name_bonus_enabled is False


def test_nhl_independent_probability_not_seeded_from_odds():
    cap = nhl_goalscorer_capability()
    assert cap.independent_probability_seeded_from_odds is False


def test_nhl_sportsbook_implied_in_forbidden_seeds():
    assert "sportsbook_implied_probability" in FORBIDDEN_PROBABILITY_SEEDS
    assert "book_implied_prob" in FORBIDDEN_PROBABILITY_SEEDS
    assert "devig_implied_probability" in FORBIDDEN_PROBABILITY_SEEDS


def test_nhl_legitimate_inputs_include_role_evidence():
    assert "pp1_role_flag" in LEGITIMATE_INPUTS
    assert "expected_toi" in LEGITIMATE_INPUTS
    assert "individual_xg" in LEGITIMATE_INPUTS
    assert "opponent_sog_allowance" in LEGITIMATE_INPUTS
    assert "opponent_pk_quality" in LEGITIMATE_INPUTS


def test_nhl_sportsbook_price_is_input_to_edge_not_probability():
    # Price is a legitimate INPUT to edge-related features; it must
    # NEVER be a seed for the independent probability.
    assert "sportsbook_price" in LEGITIMATE_INPUTS
    assert "sportsbook_implied_probability" not in LEGITIMATE_INPUTS


if __name__ == "__main__":
    import subprocess, sys
    sys.exit(subprocess.call(["python", "-m", "pytest", __file__, "-v"]))
