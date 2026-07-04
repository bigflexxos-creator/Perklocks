"""Regression tests for the parlay-optimizer same-player and FGS→AGS
preference fixes (2026-07-04).

User complaint: "app keep putting mbappe in there twice for first
goalscorer and shouldn't it be anytime goalscorer".

Coverage:
  1. Same player CAN'T appear twice in a parlay (accent-insensitive,
     across different markets AGS/FGS/SoA, and across different events).
  2. First Goal Scorer stability is deprioritised so the optimizer
     prefers Anytime Goal Scorer for the same player when both exist.

Run: python -m pytest backend/tests/test_parlay_optimizer_dedupe.py -q
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from parlay_optimizer import diversification_ok, score_leg  # noqa: E402


# ── Same-player HARD BLOCK ────────────────────────────────────────────

def test_same_player_ags_then_fgs_blocked():
    ags = {"id": "a", "sport": "Soccer", "event": "PSG @ Marseille",
           "market": "Kylian Mbappé - Anytime Goal Scorer", "selection": "Yes"}
    fgs = {"id": "b", "sport": "Soccer", "event": "France @ England",
           "market": "Kylian Mbappe First Goal Scorer", "selection": "Yes"}
    ok, reason = diversification_ok([ags], fgs, target_legs=3, single_sport_mode=True)
    assert ok is False
    assert "same player" in reason.lower()


def test_same_player_different_event_still_blocked():
    a = {"id": "a", "sport": "Soccer", "event": "PSG @ Lyon",
         "market": "Mbappé - Anytime Goal Scorer", "selection": "Yes"}
    b = {"id": "b", "sport": "Soccer", "event": "PSG @ Marseille",
         "market": "Mbappé Anytime Goal Scorer", "selection": "Yes"}
    ok, _ = diversification_ok([a], b, target_legs=3, single_sport_mode=True)
    assert ok is False


def test_different_players_allowed():
    a = {"id": "a", "sport": "Soccer", "event": "PSG @ Lyon",
         "market": "Mbappé - Anytime Goal Scorer", "selection": "Yes"}
    b = {"id": "b", "sport": "Soccer", "event": "Man City @ Chelsea",
         "market": "Haaland - Anytime Goal Scorer", "selection": "Yes"}
    ok, _ = diversification_ok([a], b, target_legs=3, single_sport_mode=True)
    assert ok is True


def test_same_player_across_ags_and_score_or_assist():
    a = {"id": "a", "sport": "Soccer", "event": "PSG @ Marseille",
         "market": "Mbappé - Anytime Goal Scorer", "selection": "Yes"}
    b = {"id": "b", "sport": "Soccer", "event": "PSG @ Marseille",
         "market": "Mbappé - To Score or Assist", "selection": "Yes"}
    ok, _ = diversification_ok([a], b, target_legs=3, single_sport_mode=True)
    assert ok is False  # same player AND same event → double-block


def test_non_player_markets_unaffected():
    """A team moneyline + an AGS shouldn't accidentally match on team-name.
    Ensure the player extractor only applies to goalscorer / prop markets."""
    ml = {"id": "a", "sport": "Soccer", "event": "PSG @ Marseille",
          "market": "PSG Moneyline", "selection": "PSG"}
    ags = {"id": "b", "sport": "Soccer", "event": "PSG @ Marseille",
           "market": "Mbappé - Anytime Goal Scorer", "selection": "Yes"}
    ok, _ = diversification_ok([ml], ags, target_legs=3, single_sport_mode=True)
    # The dedup does NOT apply to non-player-market legs (soft correlation
    # penalty still applies inside score_leg). Same-event blocking still
    # rejects for MLB/NBA/etc but Soccer allows up to 2 legs from same
    # event, so this should pass.
    assert ok is True


# ── FGS stability penalty ─────────────────────────────────────────────

def test_ags_scores_higher_than_fgs_same_player():
    """When AGS and FGS picks have identical lock / edge / win-prob,
    the AGS variant should out-score FGS thanks to the stability
    penalty on First Goal Scorer."""
    base = {
        "sport": "Soccer",
        "event": "PSG @ Marseille",
        "lock_score": 92.0, "edge_percent": 6.0,
        "win_probability": 0.55, "book_odds": -110,
        "is_alt": False,
    }
    ags = {**base, "market": "Mbappé - Anytime Goal Scorer"}
    fgs = {**base, "market": "Mbappé - First Goal Scorer"}
    s_ags = score_leg(ags, current_legs=[], bucket_map={}, target_legs=3,
                       single_sport_mode=True)
    s_fgs = score_leg(fgs, current_legs=[], bucket_map={}, target_legs=3,
                       single_sport_mode=True)
    assert s_ags > s_fgs, f"AGS ({s_ags}) should beat FGS ({s_fgs})"
