"""PERKLOCKS ROOT FIX (2026-09-03) — Universal Wrong-Series Grading.

Regression: ``_mlb_find_game`` in ``prop_settlement`` sorts candidate
matchups by ``(tier, dist, gameDate)`` where ``tier=0`` means Final
and ``tier=1`` means Live.  ``_settle_group`` merges (date, date-1)
schedules for cross-midnight ET-vs-UTC coverage, so when a series
plays the SAME (away, home) matchup twice in two days (Chicago White
Sox @ Houston Astros on 2026-09-02 AND 2026-09-03), the Final from
yesterday beats the Live from today under the old sort — and every
Yordan Alvarez / Taylor Trammell / LaMonte Wade Jr. pick from today's
live game was graded against YESTERDAY'S completed boxscore, marked
WON/LOST on stats that belonged to a different game.

The fix flips the priority when the pick carries an ``event_time`` —
distance to event_time dominates within a 12 h window, so today's
Live game (dist ≈ 0) always beats yesterday's Final (dist ≈ 86 400 s).
Falls back to the legacy tier-first ordering only when no candidate
is within the window.
"""
from __future__ import annotations

from prop_settlement import _mlb_find_game


def _mk_game(away: str, home: str, gameDate: str, state: str,
              gamePk: int) -> dict:
    return {
        "gamePk":    gamePk,
        "gameDate":  gameDate,
        "status":    {"abstractGameState": state},
        "teams": {
            "away": {"team": {"name": away}},
            "home": {"team": {"name": home}},
        },
    }


def test_series_repeats_today_live_wins_over_yesterday_final():
    """Two CWS @ HOU games — yesterday Final + today Live.  The pick's
    ``event_time`` targets today.  Under the old sort (tier→dist)
    yesterday's Final was selected → wrong-boxscore grading.  Under
    the fix (dist→tier within 12 h), today's Live is chosen.
    """
    yesterday = _mk_game(
        "Chicago White Sox", "Houston Astros",
        "2026-09-02T18:10:00Z", "Final", 824100,
    )
    today = _mk_game(
        "Chicago White Sox", "Houston Astros",
        "2026-09-03T18:10:00Z", "Live", 824144,
    )
    pick_event_time = "2026-09-03T18:10:00Z"
    chosen = _mlb_find_game(
        [yesterday, today], "Chicago White Sox", "Houston Astros",
        event_time=pick_event_time,
    )
    assert chosen is not None
    assert chosen["gamePk"] == 824144, (
        f"expected TODAY's live game 824144, got {chosen['gamePk']} "
        f"({chosen['status']})"
    )


def test_series_repeats_today_final_wins_over_yesterday_final():
    """When both games are already Final, still prefer the one closer
    to the pick's event_time — otherwise a Friday pick could be
    graded on Thursday's game just because Thursday's Final row
    happened to sort first by gameDate.
    """
    yesterday = _mk_game(
        "Chicago White Sox", "Houston Astros",
        "2026-09-02T18:10:00Z", "Final", 824100,
    )
    today = _mk_game(
        "Chicago White Sox", "Houston Astros",
        "2026-09-03T18:10:00Z", "Final", 824144,
    )
    chosen = _mlb_find_game(
        [yesterday, today], "Chicago White Sox", "Houston Astros",
        event_time="2026-09-03T18:10:00Z",
    )
    assert chosen["gamePk"] == 824144


def test_single_match_still_returns_it():
    """Sanity: when only one candidate exists, the resolver returns
    it regardless of tier / event_time.  Preserves legacy behaviour
    for non-series slates.
    """
    only = _mk_game(
        "Chicago White Sox", "Houston Astros",
        "2026-09-03T18:10:00Z", "Live", 824144,
    )
    chosen = _mlb_find_game(
        [only], "Chicago White Sox", "Houston Astros",
        event_time="2026-09-03T18:10:00Z",
    )
    assert chosen is not None
    assert chosen["gamePk"] == 824144


def test_far_out_of_window_falls_back_to_legacy_ordering():
    """When NO candidate is within 12 h of the pick's event_time
    (impossible in practice with the (date, date-1) merge, but a
    safety net regardless), the resolver falls back to legacy
    tier-first ordering so a Final is still preferred over a
    scheduled game far in the future.
    """
    a = _mk_game("Team A", "Team B", "2026-01-01T00:00:00Z",
                  "Final", 111)
    b = _mk_game("Team A", "Team B", "2026-06-01T00:00:00Z",
                  "Preview", 222)
    chosen = _mlb_find_game(
        [a, b], "Team A", "Team B",
        event_time="2026-12-15T00:00:00Z",
    )
    assert chosen is not None
    # Both are far > 12h out, so tier decides: Final (a) beats
    # Preview (b).
    assert chosen["gamePk"] == 111
