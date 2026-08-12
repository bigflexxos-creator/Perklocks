"""Pre-Magic Certification — market catalogue.

Enumerates the sport × market surfaces that MUST be certified for
Pre-Magic readiness.  This catalogue is authoritative for the
certifier — every entry becomes a row in the certification matrix.

The catalogue is intentionally EXPLICIT and honest.  Sports currently
classified as SOURCE_INSUFFICIENT / UNAVAILABLE in the pod (NHL Team,
CFB Team, UFC) are still listed — the certifier will emit
``UNAVAILABLE`` rows for them.  This module NEVER converts a missing
history source into a fake PASS (§17).

Each entry provides:

* ``atoms``      the historical stat atoms that resolve the market.
                  A market with an empty ``atoms`` list cannot pass
                  MARKET_NORMALIZATION.
* ``notes``      free-text human context (never machine-consumed).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


@dataclass(frozen=True)
class MarketAtom:
    """One production market and the historical atoms it resolves to."""
    sport:   str        # canonical sport code, upper case
    market:  str        # canonical market key (lower case)
    atoms:   tuple[str, ...] = field(default_factory=tuple)
    threshold_supported: bool = True
    milestone: bool = False
    notes:   str = ""


# ═══════════════════════════════════════════════════════════════════
# PLAYER markets by sport (Phase 5.3 Stage 2)
# ═══════════════════════════════════════════════════════════════════
PLAYER_MARKETS: tuple[MarketAtom, ...] = (
    # ── MLB ───────────────────────────────────────────────────
    MarketAtom("MLB", "player_hits",                ("hits",)),
    MarketAtom("MLB", "player_total_bases",         ("total_bases",)),
    MarketAtom("MLB", "player_home_runs",           ("home_runs",), milestone=True),
    MarketAtom("MLB", "player_rbis",                ("rbis",)),
    MarketAtom("MLB", "player_runs_scored",         ("runs",)),
    MarketAtom("MLB", "batter_strikeouts",          ("strikeouts",)),
    MarketAtom("MLB", "pitcher_strikeouts",         ("pitcher_strikeouts",)),
    MarketAtom("MLB", "pitcher_outs",               ("pitcher_outs",)),
    # ── NFL ───────────────────────────────────────────────────
    MarketAtom("NFL", "player_passing_yards",       ("passing_yards",)),
    MarketAtom("NFL", "player_rushing_yards",       ("rushing_yards",)),
    MarketAtom("NFL", "player_receiving_yards",     ("receiving_yards",)),
    MarketAtom("NFL", "player_receptions",          ("receptions",)),
    MarketAtom("NFL", "player_passing_tds",         ("passing_tds",), milestone=True),
    MarketAtom("NFL", "player_rushing_tds",         ("rushing_tds",), milestone=True),
    MarketAtom("NFL", "player_receiving_tds",       ("receiving_tds",), milestone=True),
    MarketAtom("NFL", "player_anytime_td",          ("rushing_tds", "receiving_tds"),
               milestone=True, notes="derived: sum of rushing+receiving TDs"),
    # ── NBA ───────────────────────────────────────────────────
    MarketAtom("NBA", "player_points",              ("points",)),
    MarketAtom("NBA", "player_rebounds",            ("rebounds",)),
    MarketAtom("NBA", "player_assists",             ("assists",)),
    MarketAtom("NBA", "player_threes",              ("threes", "fg3m")),
    MarketAtom("NBA", "player_points_rebounds_assists",
                ("points", "rebounds", "assists"),
                notes="derived: PRA — atoms must all be present"),
    MarketAtom("NBA", "player_points_rebounds",
                ("points", "rebounds"),
                notes="derived: PR"),
    MarketAtom("NBA", "player_points_assists",
                ("points", "assists"),
                notes="derived: PA"),
    MarketAtom("NBA", "player_rebounds_assists",
                ("rebounds", "assists"),
                notes="derived: RA"),
    # ── Soccer ────────────────────────────────────────────────
    MarketAtom("SOCCER", "player_goals",            ("goals",), milestone=True),
    MarketAtom("SOCCER", "player_assists",          ("assists",), milestone=True),
    MarketAtom("SOCCER", "player_shots",            ("shots",)),
    MarketAtom("SOCCER", "player_shots_on_target",  ("shots_on_target",)),
    MarketAtom("SOCCER", "player_goal_or_assist",   ("goals", "assists"),
                milestone=True, notes="derived — either atom qualifies"),
    # ── Tennis ────────────────────────────────────────────────
    MarketAtom("TENNIS", "player_aces",             ("aces",)),
    MarketAtom("TENNIS", "player_double_faults",    ("double_faults",)),
    MarketAtom("TENNIS", "player_games_won",        ("games_won",)),
    MarketAtom("TENNIS", "player_sets_won",         ("sets_won",),
                notes="whole-number line — pushes possible"),
    # ── UFC (currently UNAVAILABLE by handoff classification) ─
    MarketAtom("UFC", "fight_result",               (), notes="SOURCE INSUFFICIENT"),
    MarketAtom("UFC", "method_of_victory",          (), notes="SOURCE INSUFFICIENT"),
    # ── NHL player (currently UNAVAILABLE by handoff)  ─────────
    MarketAtom("NHL", "player_shots_on_goal",       (), notes="SOURCE INSUFFICIENT"),
    MarketAtom("NHL", "player_goals",               (), notes="SOURCE INSUFFICIENT"),
    # ── CFB player (currently UNAVAILABLE by handoff)  ─────────
    MarketAtom("CFB", "player_passing_yards",       (), notes="SOURCE INSUFFICIENT"),
)


# ═══════════════════════════════════════════════════════════════════
# TEAM markets by sport (Phase 5.3 Stage 3)
# ═══════════════════════════════════════════════════════════════════
TEAM_MARKETS: tuple[MarketAtom, ...] = (
    # ── MLB ───────────────────────────────────────────────────
    MarketAtom("MLB", "spreads",   ("team_score", "opponent_score")),
    MarketAtom("MLB", "totals",    ("team_score", "opponent_score")),
    MarketAtom("MLB", "h2h",       ("result",)),
    # ── NFL ───────────────────────────────────────────────────
    MarketAtom("NFL", "spreads",   ("team_score", "opponent_score")),
    MarketAtom("NFL", "totals",    ("team_score", "opponent_score")),
    MarketAtom("NFL", "h2h",       ("result",)),
    # ── Soccer ────────────────────────────────────────────────
    MarketAtom("SOCCER", "spreads",       ("team_score", "opponent_score")),
    MarketAtom("SOCCER", "totals",        ("team_score", "opponent_score")),
    MarketAtom("SOCCER", "h2h",           ("result",)),
    MarketAtom("SOCCER", "btts",          ("team_score", "opponent_score"),
                milestone=True,
                notes="both teams scored — derived from atoms"),
    MarketAtom("SOCCER", "double_chance", ("result",)),
    # ── NBA (per handoff: team NORMALIZED collection empty in pod;
    #        adapter exists — classified honestly at runtime) ──
    MarketAtom("NBA", "spreads",   ("team_score", "opponent_score")),
    MarketAtom("NBA", "totals",    ("team_score", "opponent_score")),
    MarketAtom("NBA", "h2h",       ("result",)),
    # ── NHL team (SOURCE INSUFFICIENT per handoff) ─────────────
    MarketAtom("NHL", "spreads",   (), notes="SOURCE INSUFFICIENT"),
    MarketAtom("NHL", "totals",    (), notes="SOURCE INSUFFICIENT"),
    MarketAtom("NHL", "h2h",       (), notes="SOURCE INSUFFICIENT"),
    # ── CFB team (SOURCE INSUFFICIENT per handoff) ─────────────
    MarketAtom("CFB", "spreads",   (), notes="SOURCE INSUFFICIENT"),
    MarketAtom("CFB", "totals",    (), notes="SOURCE INSUFFICIENT"),
    MarketAtom("CFB", "h2h",       (), notes="SOURCE INSUFFICIENT"),
)


PLAYER_SPORTS: tuple[str, ...] = ("MLB", "NBA", "NFL", "NHL", "SOCCER", "TENNIS", "UFC")
TEAM_SPORTS:   tuple[str, ...] = ("MLB", "NBA", "NFL", "NHL", "CFB", "SOCCER")


def player_markets_for(sport: str) -> tuple[MarketAtom, ...]:
    s = sport.upper()
    return tuple(m for m in PLAYER_MARKETS if m.sport == s)


def team_markets_for(sport: str) -> tuple[MarketAtom, ...]:
    s = sport.upper()
    return tuple(m for m in TEAM_MARKETS if m.sport == s)


__all__ = [
    "MarketAtom",
    "PLAYER_MARKETS", "TEAM_MARKETS",
    "PLAYER_SPORTS", "TEAM_SPORTS",
    "player_markets_for", "team_markets_for",
]
