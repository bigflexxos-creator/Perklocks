"""PERKLOCKS ROOT FIX (2026-09-03) — Universal Hitter Emission Floor.

Regression: for every MLB evening game (7 PM+ ET first pitch) the
board carried ZERO hitter props during the mid-afternoon window
because the producer's 3-factor gate silently dropped every batter
candidate.  Three of the eleven hitter factors (Recent L10 Hit Rate,
Platoon Advantage, BvP) require CONFIRMED batting orders, which
don't post until ~3h pre-first-pitch — at 5-6h out the projected-
lineup fallback returned empty for non-anchor players and each pick
failed ``has_enough_real_data("hitter_prop")``.

The universal fix lowers ``MIN_FACTORS_HITTER_PROP`` from 3 → 2.  It
is SAFE because three independent downstream gates still enforce
quality (board_validator evidence 3-of-6, canonical publication
boundary MODEL_PROVENANCE + IDENTITY_CLASS + real odds, and the
publication healer never publishes a pick failing the boundary).

These tests pin the new floor + the ratchet safety net.
"""
from __future__ import annotations

from services.mlb_feature_engine import (
    MIN_FACTORS_HITTER_PROP,
    MIN_FACTORS_K_PROP,
    MIN_FACTORS_ML,
    MIN_FACTORS_TOTAL,
    has_enough_real_data,
)


def test_hitter_prop_floor_is_two():
    """The producer-side hitter-prop threshold is 2 — anything higher
    starves the board for pre-lineup evening MLB games."""
    assert MIN_FACTORS_HITTER_PROP == 2, MIN_FACTORS_HITTER_PROP


def test_other_market_type_floors_are_unchanged():
    """K prop / ML / Total floors remain at their canonical values —
    the fix is targeted, not a broad quality reduction."""
    assert MIN_FACTORS_K_PROP == 3
    assert MIN_FACTORS_ML     == 4
    assert MIN_FACTORS_TOTAL  == 4


def test_pre_lineup_hitter_pick_now_emits():
    """Two real factors (Park + Recent L10 Hit Rate) — this is the
    typical pre-lineup shape at 5h pre-game.  Under the old floor of
    3 this failed; under 2 it emits.
    """
    factors = {
        "Recent L10 Hit Rate":       0.312,
        "Matchup vs Defense":        None,
        "Home/Away Splits":          0.278,
        "Platoon Advantage":         None,
        "BvP (career vs pitcher)":   None,
        "Expected BA (Statcast)":    None,
        "Barrel% (Quality of Contact)": None,
        "Hard-Hit % (Statcast)":     None,
        "Regression Signal (xBA-BA)": None,
        "Umpire Zone (Hitter Bias)": None,
        "DFS Projection vs Line":    None,
    }
    assert has_enough_real_data(factors, "hitter_prop") is True


def test_single_factor_hitter_pick_still_blocked():
    """A pick with only ONE real factor is still blocked — the floor
    of 2 is genuine, not a soft-hinge that lets book-odds-only picks
    slip through with no supporting hitter signal.
    """
    factors = {
        "Recent L10 Hit Rate":       0.312,
        "Matchup vs Defense":        None,
        "Home/Away Splits":          None,
        "Platoon Advantage":         None,
        "BvP (career vs pitcher)":   None,
        "Expected BA (Statcast)":    None,
        "Barrel% (Quality of Contact)": None,
        "Hard-Hit % (Statcast)":     None,
        "Regression Signal (xBA-BA)": None,
        "Umpire Zone (Hitter Bias)": None,
        "DFS Projection vs Line":    None,
    }
    assert has_enough_real_data(factors, "hitter_prop") is False


def test_confirmed_lineup_hitter_pick_still_emits():
    """When the lineup IS posted (all 11 factors resolve), the pick
    obviously still emits.  Sanity coverage.
    """
    factors = {name: 0.5 for name in (
        "Recent L10 Hit Rate", "Matchup vs Defense", "Home/Away Splits",
        "Platoon Advantage", "BvP (career vs pitcher)",
        "Expected BA (Statcast)", "Barrel% (Quality of Contact)",
        "Hard-Hit % (Statcast)", "Regression Signal (xBA-BA)",
        "Umpire Zone (Hitter Bias)", "DFS Projection vs Line",
    )}
    assert has_enough_real_data(factors, "hitter_prop") is True
