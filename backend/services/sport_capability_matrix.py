"""Per sport × per market family capability declarations (Blocks 40, 48).

Thin wrapper around ``universal_market_contract`` that lets the
orchestrator ask: "Is (sport, family) currently model-capable?"
without opening up the full MarketEntry.  Blocks 39-40 require:

  * NBA history runs INDEPENDENT of board MODEL_UNAVAILABLE state
  * each NBA family is independently gated (ML, Spread, Total,
    Points, Rebounds, Assists, Threes, PRA)
  * one unavailable family MUST NOT disable the whole sport

Universal Apex capability (Block 48): declaratively state which
(sport, family) tuples are STRUCTURALLY capable of 100 if evidence
converges.  This module is additive — the actual Apex gate math in
``apex_gate.py`` is unchanged.
"""
from __future__ import annotations

from services.universal_market_contract import (
    all_entries, capability, ACTIVE, RESEARCH_ONLY, MODEL_UNAVAILABLE,
    PROVIDER_UNAVAILABLE, SETTLEMENT_UNAVAILABLE,
)


def family_capability(sport: str, family: str) -> str:
    """Return the capability_state string for (sport, family)."""
    return capability(sport, family)


def is_model_capable(sport: str, family: str) -> bool:
    return family_capability(sport, family) == ACTIVE


def is_model_unavailable(sport: str, family: str) -> bool:
    return family_capability(sport, family) == MODEL_UNAVAILABLE


def families_for_sport(sport: str) -> list[tuple[str, str]]:
    """Return every (family, capability_state) tuple registered for
    the given sport.  Order is deterministic (sorted by family)."""
    rows: list[tuple[str, str]] = []
    for (s, fam), entry in all_entries().items():
        if s.upper() == (sport or "").upper():
            rows.append((fam, entry.capability_state))
    rows.sort(key=lambda t: t[0])
    return rows


def model_capable_families_for_sport(sport: str) -> list[str]:
    """Return the subset of families that are model-capable for the
    given sport (one-unavailable-family-doesn't-disable-sport contract)."""
    return [fam for fam, cap in families_for_sport(sport) if cap == ACTIVE]


# Universal Apex structural capability (Block 48).  A row listed here
# declares that the (sport, family) combination CAN structurally
# reach a Lock Score of 100 if the full evidence contract is met.
# This does NOT force Apex — ``apex_gate.evaluate_apex`` still has to
# clear every gate.  It only states that the family isn't ceiling-
# capped by the engine architecture.
_UNIVERSAL_APEX_CAPABLE: frozenset[tuple[str, str]] = frozenset({
    # NFL
    ("NFL", "moneyline"), ("NFL", "point_spread"), ("NFL", "game_total"),
    ("NFL", "qb_passing_yards"), ("NFL", "qb_passing_tds"),
    ("NFL", "rb_rushing_yards"), ("NFL", "wr_receiving_yards"),
    ("NFL", "wr_receptions"),
    # MLB
    ("MLB", "moneyline"), ("MLB", "run_line"), ("MLB", "game_total"),
    ("MLB", "nrfi_yrfi"),
    ("MLB", "hitter_hits"), ("MLB", "hitter_total_bases"),
    ("MLB", "hitter_home_runs"), ("MLB", "hitter_rbis"),
    ("MLB", "pitcher_strikeouts"), ("MLB", "pitcher_outs"),
    # NHL
    ("NHL", "moneyline"), ("NHL", "puck_line"), ("NHL", "game_total"),
    ("NHL", "nhl_goals"), ("NHL", "nhl_sog"),
    ("NHL", "nhl_assists"), ("NHL", "nhl_points"),
    # Soccer
    ("Soccer", "moneyline"), ("Soccer", "game_total"),
    ("Soccer", "btts"), ("Soccer", "double_chance"),
    ("Soccer", "goalscorer_anytime"),
    ("Soccer", "goalscorer_score_or_assist"),
    # Tennis
    ("Tennis", "tennis_match_winner"),
    ("Tennis", "tennis_game_handicap"),
    ("Tennis", "tennis_total_games"),
    # CFB
    ("CFB", "moneyline"), ("CFB", "point_spread"), ("CFB", "game_total"),
    # NBA — the FAMILIES ARE STRUCTURALLY APEX-CAPABLE when a model
    # ships; the actual capability_state stays MODEL_UNAVAILABLE until
    # certified independent models are wired.  Apex gate is
    # structurally reachable, not currently reachable.
    ("NBA", "moneyline"), ("NBA", "point_spread"), ("NBA", "game_total"),
    ("NBA", "nba_points"), ("NBA", "nba_rebounds"),
    ("NBA", "nba_assists"), ("NBA", "nba_threes"), ("NBA", "nba_pra"),
})


def is_universal_apex_capable(sport: str, family: str) -> bool:
    """Declarative: can (sport, family) STRUCTURALLY reach Lock=100?"""
    return (sport, family) in _UNIVERSAL_APEX_CAPABLE


__all__ = [
    "family_capability", "is_model_capable", "is_model_unavailable",
    "families_for_sport", "model_capable_families_for_sport",
    "is_universal_apex_capable",
]
