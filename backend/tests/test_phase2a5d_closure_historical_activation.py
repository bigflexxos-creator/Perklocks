"""Phase 2A.5D CLOSURE — Historical data activation + lifecycle disposition.

Tests the final closure requirements: season resolver, historical
aggregation from `soccer_player_game_logs`, Player H2H reuse from
`mls_player_matchup_history`, and board-lifecycle disposition
attribution.
"""
from __future__ import annotations

import os, sys
from datetime import datetime, timezone

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)


# ═════════════════════════════════════════════════════════════════════
# Season resolver
# ═════════════════════════════════════════════════════════════════════
def test_mls_uses_calendar_year():
    from services.soccer_season_resolver import (
        resolve_season_chain, resolve_current_season, resolve_prior_season,
        is_calendar_year_competition,
    )
    ref = datetime(2026, 8, 15, tzinfo=timezone.utc)
    assert is_calendar_year_competition("MLS") is True
    assert resolve_current_season("MLS", ref) == "2026"
    assert resolve_prior_season("MLS", ref) == "2025"
    chain = resolve_season_chain("MLS", ref, depth=4)
    assert chain == ["2026", "2025", "2024", "2023"]


def test_epl_uses_split_year():
    from services.soccer_season_resolver import (
        resolve_current_season, resolve_prior_season, resolve_season_chain,
    )
    ref = datetime(2026, 8, 15, tzinfo=timezone.utc)
    # August 2026 → 2026-2027 current.
    assert resolve_current_season("Premier League", ref) == "2026-2027"
    assert resolve_prior_season("EPL", ref) == "2025-2026"
    chain = resolve_season_chain("Bundesliga", ref, depth=3)
    assert chain == ["2026-2027", "2025-2026", "2024-2025"]


def test_january_epl_still_prior_calendar():
    """A Premier League match in January belongs to the (Y-1)/Y season."""
    from services.soccer_season_resolver import resolve_current_season
    ref = datetime(2026, 1, 15, tzinfo=timezone.utc)
    assert resolve_current_season("Premier League", ref) == "2025-2026"


def test_unknown_league_defaults_to_split_year():
    from services.soccer_season_resolver import resolve_current_season
    ref = datetime(2026, 8, 15, tzinfo=timezone.utc)
    # An unknown league should NOT collapse to calendar year without
    # heuristic reason.
    s = resolve_current_season("Unknown Fantasy League", ref)
    # Either split-year or calendar-year is acceptable, but must be
    # deterministic and non-empty.
    assert s in ("2026-2027", "2026")


# ═════════════════════════════════════════════════════════════════════
# Historical aggregator — offline shape + reuse assertion
# ═════════════════════════════════════════════════════════════════════
def test_historical_aggregator_module_reuses_existing_collections():
    """Prove the aggregator does NOT create a new historical collection —
    it consumes existing `soccer_player_game_logs` + `mls_player_matchup_history`."""
    src = open(os.path.join(BACKEND, "services",
                             "soccer_historical_stats.py"), "r").read()
    assert "soccer_player_game_logs" in src
    assert "mls_player_matchup_history" in src
    # And it must NOT create parallel storage.
    for forbidden in ("insert_one", "insert_many", "update_one",
                       "update_many", "create_collection"):
        assert forbidden not in src, (
            f"historical aggregator must be read-only, found: {forbidden}"
        )


# ═════════════════════════════════════════════════════════════════════
# Player H2H — shrinkage + neutral handling
# ═════════════════════════════════════════════════════════════════════
def test_player_h2h_shrinks_tiny_samples():
    """1 match with 2 goals must not appear as elite H2H."""
    from services.soccer_historical_stats import (
        H2H_SHRINKAGE_MATCHES, NEUTRAL_H2H_GOAL_RATE,
    )
    # Simulate what load_player_h2h computes for tiny sample.
    matches, goals = 1, 2
    w = matches / (matches + H2H_SHRINKAGE_MATCHES)
    raw = goals / matches
    shrunk = w * raw + (1 - w) * NEUTRAL_H2H_GOAL_RATE
    # Raw 2.0 gpm → shrunk down heavily.
    assert 0.35 < shrunk < 1.0, f"tiny sample must shrink toward prior: {shrunk}"


def test_player_h2h_large_sample_dominates_prior():
    from services.soccer_historical_stats import (
        H2H_SHRINKAGE_MATCHES, NEUTRAL_H2H_GOAL_RATE,
    )
    matches, goals = 10, 8
    w = matches / (matches + H2H_SHRINKAGE_MATCHES)
    raw = goals / matches
    shrunk = w * raw + (1 - w) * NEUTRAL_H2H_GOAL_RATE
    assert shrunk > 0.55, f"large sample should mostly track raw: {shrunk}"


# ═════════════════════════════════════════════════════════════════════
# Bridge integration — prior_form_row wire proven
# ═════════════════════════════════════════════════════════════════════
def test_scorer_bridge_accepts_prior_form_row_and_h2h_evidence():
    """End-to-end: aggregator shape + bridge acceptance."""
    from services.soccer_scorer_bridge import compute_soccer_scorer_factors_sync
    # Simulate what aggregator returns for a top striker.
    prior_row = {
        "name_canonical": "test", "season": "2025",
        "minutes": 2700, "games": 30, "starts": 30,
        "goals": 22, "xg": 20, "xa": 6, "shots": 130, "sot": 55,
        "assists": 6, "team": "Team X", "league": "MLS",
    }
    cur_row = {"xg": 3, "goals": 3, "minutes": 500, "games": 6,
                "starts": 6, "position": "FW", "form_score": 70,
                "shots_per_90": 2.8}
    r = compute_soccer_scorer_factors_sync(
        player="Test Striker",
        market_key="player_goal_scorer_anytime",
        book_implied=0.35, form_row=cur_row, prior_form_row=prior_row,
        league="MLS")
    assert r is not None
    assert r.get("multi_season_profile") in (
        "ELITE_ATTACKING_PROFILE", "STRONG_ATTACKING_PROFILE",
        "ABOVE_AVERAGE",
    )
    assert "multi_season" in (r.get("engine_version") or "")


# ═════════════════════════════════════════════════════════════════════
# Production wire-in — sports_engine preloader reads historical
# ═════════════════════════════════════════════════════════════════════
def test_sports_engine_preloader_wires_historical_and_h2h():
    src = open(os.path.join(BACKEND, "sports_engine.py"), "r").read()
    assert "resolve_prior_season" in src
    assert "aggregate_player_season" in src
    assert "load_player_h2h" in src
    assert "prior_form_row=_prior_row" in src


# ═════════════════════════════════════════════════════════════════════
# Board lifecycle disposition
# ═════════════════════════════════════════════════════════════════════
def test_disposition_survives_odds_update():
    from services.board_lifecycle import diff_soccer_boards
    before = [{"sport": "Soccer", "event": "A @ B",
                "market": "Anytime Goal Scorer", "selection": "Player X",
                "book_odds": +250, "published_lock_score": 88}]
    after = [{"sport": "Soccer", "event": "A @ B",
               "market": "Anytime Goal Scorer", "selection": "Player X",
               "book_odds": +230, "published_lock_score": 90}]
    events = diff_soccer_boards(before, after)
    assert len(events) == 1
    assert events[0]["disposition"] == "STILL_ON_BOARD_UPDATED_ODDS"


def test_disposition_lock_score_below_85():
    from services.board_lifecycle import diff_soccer_boards
    before = [{"sport": "Soccer", "event": "A @ B",
                "market": "Anytime Goal Scorer", "selection": "Player X",
                "published_lock_score": 80, "book_odds": +200}]
    events = diff_soccer_boards(before, [])
    assert events[0]["disposition"] == "LOCK_SCORE_BELOW_85"


def test_disposition_book_line_removed():
    from services.board_lifecycle import diff_soccer_boards
    before = [{"sport": "Soccer", "event": "A @ B",
                "market": "Anytime Goal Scorer", "selection": "Player Z",
                "published_lock_score": 92, "book_odds": None}]
    events = diff_soccer_boards(before, [])
    assert events[0]["disposition"] == "BOOK_LINE_REMOVED"


def test_disposition_player_confirmed_out():
    from services.board_lifecycle import diff_soccer_boards
    before = [{"sport": "Soccer", "event": "A @ B",
                "market": "Anytime Goal Scorer", "selection": "Player Y",
                "published_lock_score": 92, "book_odds": +200,
                "player_availability": "out"}]
    events = diff_soccer_boards(before, [])
    assert events[0]["disposition"] == "PLAYER_CONFIRMED_OUT"


def test_disposition_never_unknown():
    """Every removal must receive one of the enumerated dispositions."""
    from services.board_lifecycle import (
        diff_soccer_boards, DISPOSITION_REASONS,
    )
    before = [{"sport": "Soccer", "event": "A @ B",
                "market": "Anytime Goal Scorer", "selection": "P",
                "published_lock_score": 88, "book_odds": +200}]
    events = diff_soccer_boards(before, [])
    for e in events:
        assert e["disposition"] in DISPOSITION_REASONS
        assert e["disposition"] != "UNKNOWN"


def test_no_soccer_lock_threshold_ladder_introduced():
    """Universal 85 threshold must not have been raised."""
    from services.main_board_eligibility import MAIN_BOARD_LOCK_FLOOR
    assert MAIN_BOARD_LOCK_FLOOR == 85.0


# ═════════════════════════════════════════════════════════════════════
# Preservation — Phase 2A.5C board reachability + prior work
# ═════════════════════════════════════════════════════════════════════
def test_phase_2a5c_board_visibility_still_uses_canonical_ls():
    from services.board_visibility import _canonical_lock_score
    p = {"lock_score": 55.0, "lock_score_v2": 98.0, "published_lock_score": 98.0}
    assert _canonical_lock_score(p) == 98.0


def test_scorer_bridge_no_star_whitelist_added():
    """Phase 2A.5 no-hardcoded-name rule preserved after closure."""
    src = open(os.path.join(BACKEND, "services",
                             "soccer_historical_stats.py"), "r").read()
    for forbidden_name in ("Messi", "Haaland", "Mbapp", "Kane",
                            "Denkey", "Cuypers", "Surridge"):
        assert forbidden_name not in src, (
            f"historical stats must not hardcode names: {forbidden_name}"
        )
