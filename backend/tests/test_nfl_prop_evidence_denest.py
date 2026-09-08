"""Surgical regression test — 2026-06-09.

Proves the SURGICAL DE-NEST in ``board_validator.evidence_threshold``:

  • ``len(factors) >= 3`` earns its factor-evidence point **independently**
    of whether the identity-evidence point was earned.
  • Identity-evidence point still gates on
    ``canonical_player_id`` AND ``player_team``.
  • ``MIN_EVIDENCE_COUNT`` remains ``3``.
  • Nothing else in the accounting has moved.

Deliberately does NOT touch Lock-score threshold or model probabilities —
those live in other modules and are unrelated to the accounting fix.
"""
from __future__ import annotations

import copy
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from board_validator import evidence_threshold, MIN_EVIDENCE_COUNT


BASE_NFL_PROP = {
    "sport": "NFL",
    "market": "Passing Yards Over/Under",
    "selection": "Patrick Mahomes Over 275.5",
    "event": "Kansas City Chiefs @ Buffalo Bills",
    "source": "odds_api",
    "canonical_player_id": "00-0033873",
    "player_team": "KC",
    "factors": {"season_avg": 288.1, "l4_avg": 301.2, "opp_pass_dvoa": -0.02},
    "edge_percent": 3.4,
    "lock_score": 88,
    "lock_components": {"bucket_n": 0, "ev_units": 0.0},
}


def _score(p: dict) -> int:
    """Returns evidence-count pre-sport-block snapshot for a survivor,
    or a sentinel -1 if the pick was dropped."""
    survivors, _stats = evidence_threshold([copy.deepcopy(p)])
    if not survivors:
        return -1
    return int(survivors[0]["evidence_count"])


def _survives(p: dict) -> bool:
    survivors, _ = evidence_threshold([copy.deepcopy(p)])
    return len(survivors) == 1


def test_min_evidence_count_unchanged():
    assert MIN_EVIDENCE_COUNT == 3, (
        "Guardrail: MIN_EVIDENCE_COUNT must remain 3"
    )


def test_factor_evidence_independent_of_identity():
    """Player_team temporarily unresolved — factor point still earned."""
    p = copy.deepcopy(BASE_NFL_PROP)
    p["player_team"] = None                # identity point should be LOST
    p["canonical_player_id"] = "00-0033873"
    # factors dict still has 3 real factors → factor point still EARNED.
    survivors, stats = evidence_threshold([p])
    assert len(survivors) == 1, (
        f"NFL prop with unresolved player_team but 3 real factors was "
        f"dropped. stats={stats}"
    )
    # Expected signals meeting the >=3 bar:
    #   • generic factors >= 3                      (+1)
    #   • edge_percent >= 1.5                       (+1)
    #   • NFL-specific factor evidence (de-nested)  (+1)
    # Identity NFL-specific point NOT earned (player_team missing).
    # (evidence_count on the pick is a pre-sport-block snapshot; the
    # actual gate uses the running total, so survival IS the proof.)


def test_identity_evidence_still_gated():
    """Identity signal must remain a strict AND of both fields.

    Uses a stripped-down pick where the ONLY thing separating a 3/3
    survival from a 2/3 drop is the identity-evidence point.  This
    proves the identity point is neither granted when missing nor
    lost when present.
    """
    # Minimal base: no factors → no factor points; no edge → no edge
    # point.  So the only bump available is the identity point.
    base = copy.deepcopy(BASE_NFL_PROP)
    base["factors"] = {}
    base["edge_percent"] = 0
    base["lock_components"] = {"bucket_n": 0, "ev_units": 0}
    # Pull baseline to 2 signals via rationale + a sport-rationale key
    # so identity is the *deciding* signal.
    base["pick_rationale"] = {"recent_l5": "ok"}
    # → generic-rationale (+1) + recent_l5 sport-key (+1) = 2 signals.

    p_no_id = copy.deepcopy(base)
    p_no_id["canonical_player_id"] = None
    p_no_id["player_team"] = "KC"
    assert not _survives(p_no_id), (
        "Identity point granted despite missing canonical_player_id"
    )

    p_no_team = copy.deepcopy(base)
    p_no_team["canonical_player_id"] = "00-0033873"
    p_no_team["player_team"] = None
    assert not _survives(p_no_team), (
        "Identity point granted despite missing player_team"
    )

    p_both = copy.deepcopy(base)
    assert _survives(p_both), (
        "Identity point NOT granted with both canonical_player_id and "
        "player_team present — regression."
    )


def test_factor_point_lost_when_factors_lt_3():
    """<3 real factors → factor-evidence point must NOT be earned."""
    p = copy.deepcopy(BASE_NFL_PROP)
    p["factors"] = {"season_avg": 288.1, "l4_avg": 301.2}   # only 2
    p["player_team"] = None                                  # no identity
    survivors, stats = evidence_threshold([p])
    # With 2 factors + edge only (edge_percent still >=1.5) the pick has
    # 2 signals: generic-factors is skipped (len<3), edge yes, identity
    # no, NFL-factor no.  It should drop.
    assert survivors == [], (
        f"NFL prop with only 2 factors and no identity must NOT pass "
        f"the {MIN_EVIDENCE_COUNT}-signal bar. stats={stats}"
    )


def test_non_nfl_props_unaffected():
    """Sanity: the de-nest only touches the NFL-specific block."""
    p = copy.deepcopy(BASE_NFL_PROP)
    p["sport"] = "NBA"
    p["market"] = "Points Over/Under"
    survivors, _ = evidence_threshold([p])
    # NBA prop with 3 factors + edge → generic factors (+1) + edge (+1)
    # = 2 signals → must drop under the standard 3-of-6 bar.
    assert survivors == [], (
        "Non-NFL prop should not receive NFL-specific evidence bumps."
    )


if __name__ == "__main__":
    test_min_evidence_count_unchanged()
    test_factor_evidence_independent_of_identity()
    test_identity_evidence_still_gated()
    test_factor_point_lost_when_factors_lt_3()
    test_non_nfl_props_unaffected()
    print("OK — all NFL prop evidence de-nest regression checks passed.")
