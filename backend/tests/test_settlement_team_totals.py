"""Unit tests for the settlement_engine Team Total / Under / Alt fixes.

Regression coverage for the 2026-07-04 bug where the settler used the
GAME total (away+home) against the TEAM TOTAL line, inflating win rates
on MLB Team Total Over picks to ~95%+ across the board.

Also covers previously-unsettleable variants:
  • Team Total Under
  • Team Total (Alt)
  • Game Total Under
  • Run Line (Alt) / Puck Line

Run: python -m pytest backend/tests/test_settlement_team_totals.py -q
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from settlement_engine import (  # noqa: E402
    settle_pick,
    _parse_team_total,
    _parse_spread,
    _parse_total_line,
    _parse_total_side,
)


PAD = {
    "completed": True,
    "scores": [
        {"name": "St. Louis Cardinals", "score": 2},
        {"name": "Chicago Cubs", "score": 8},
    ],
}


def _p(market: str, sport: str = "MLB") -> dict:
    return {"market": market, "event": "St. Louis Cardinals @ Chicago Cubs", "sport": sport}


# ── Parsers ────────────────────────────────────────────────────────────────

def test_parse_team_total_basic():
    assert _parse_team_total("St. Louis Cardinals Team Total Over 3.5") == (
        "St. Louis Cardinals", "over", 3.5
    )


def test_parse_team_total_under_alt():
    assert _parse_team_total("New York Yankees Team Total Under 4.5 (Alt)") == (
        "New York Yankees", "under", 4.5
    )


def test_parse_team_total_rejects_non_tt():
    assert _parse_team_total("Yankees +1.5 Spread") == (None, None, None)


def test_parse_total_side():
    assert _parse_total_side("Team Total Over 3.5") == "over"
    assert _parse_total_side("Total Runs Under 8.5") == "under"


def test_parse_spread_variants():
    assert _parse_spread("Yankees +1.5 Run Line (Alt)") == ("Yankees", 1.5)
    assert _parse_spread("Dodgers -1.5 Puck Line") == ("Dodgers", -1.5)
    assert _parse_spread("Rockies +1.5 Spread") == ("Rockies", 1.5)


# ── settle_pick behaviour ─────────────────────────────────────────────────

def test_team_total_over_loses_when_team_falls_short():
    # Cardinals scored 2; Over 3.5 → LOST (not WON as the old code returned)
    assert settle_pick(_p("St. Louis Cardinals Team Total Over 3.5"), PAD) == "lost"


def test_team_total_under_wins_when_team_falls_short():
    assert settle_pick(_p("St. Louis Cardinals Team Total Under 3.5"), PAD) == "won"


def test_team_total_over_wins_when_team_beats_line():
    # Cubs scored 8; Over 3.5 → WON
    assert settle_pick(_p("Chicago Cubs Team Total Over 3.5"), PAD) == "won"


def test_team_total_alt_variant_settles():
    # Cubs scored 8; Over 7.5 (Alt) → WON
    assert settle_pick(_p("Chicago Cubs Team Total Over 7.5 (Alt)"), PAD) == "won"


def test_team_total_push_on_equal_line():
    # Cardinals scored 2; Over 2 → PUSH (not that MLB books offer this, defensive)
    assert settle_pick(_p("St. Louis Cardinals Team Total Over 2"), PAD) == "push"


def test_game_total_over_and_under():
    # Combined total = 10
    assert settle_pick(_p("Total Over 8.5 Runs"), PAD) == "won"
    assert settle_pick(_p("Total Under 8.5 Runs"), PAD) == "lost"


def test_run_line_and_alt_run_line():
    # Cards 2, Cubs 8 → Cards +1.5 = 2-8+1.5 = -4.5 → LOST
    assert settle_pick(_p("St. Louis Cardinals +1.5 Run Line"), PAD) == "lost"
    # Cubs -1.5 = 8-2-1.5 = 4.5 → WON, Alt form should behave same
    assert settle_pick(_p("Chicago Cubs -1.5 Run Line (Alt)"), PAD) == "won"


def test_incomplete_game_returns_none():
    assert settle_pick(_p("Chicago Cubs Team Total Over 3.5"), {"completed": False, "scores": []}) is None
