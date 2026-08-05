"""Soccer scorer-market DNP → VOID regression tests (iter110, 2026-06).

User report (screenshots):
  • Tobias Heintz (IFK Göteborg) — 02.08 vs Degerfors: **SUSPENDED**,
    match ended 2-0 W for Göteborg.  His Anytime Goal Scorer pick was
    graded LOST.  Correct behavior: VOID (money refunded).
  • Jens Hjertø-Dahl (Tromsø) — 02.08 vs Aalesund: **not in the squad**,
    match ended 2-6 W for Tromsø.  His prop pick was graded LOST.
    Correct behavior: VOID.

Both are Nordic-league (Allsvenskan / Eliteserien) fixtures where
ESPN doesn't publish squad rosters, so `_espn_player_appeared` returns
None → the settler fell through to the goal-scorer check and graded
LOST because the player's name wasn't in the scorer list.

Fix:
  1. `soccer_fotmob_settle._fotmob_player_participation()` — new
     helper that reads FotMob's `content.lineup.{homeTeam,awayTeam}
     .{starters,subs}` and classifies participation as:
       "played" / "unused_sub" / "not_in_squad" / None.
  2. `soccer_fotmob_settle._settle_scorer_market()` — VOID whenever
     participation is "not_in_squad" OR "unused_sub".
  3. `prop_settlement.settle_pending_soccer_props()` — when ESPN
     returns unknown, invoke the FotMob lineup check as fallback for
     Nordic leagues so DNP → VOID instead of LOST.

This file uses in-memory fixtures ONLY (no network calls).
"""
from __future__ import annotations

from typing import Any

import pytest


def _fake_starter(name: str, rating: float | None = 6.5) -> dict:
    return {
        "name": name,
        "performance": ({"rating": rating} if rating is not None else {}),
    }


def _fake_sub(name: str, minutes_in: int | None = None,
                rating: float | None = None) -> dict:
    perf: dict[str, Any] = {}
    if minutes_in is not None:
        perf["substitutionEvents"] = [{"type": "subIn", "time": minutes_in}]
    if rating is not None:
        perf["rating"] = rating
    return {"name": name, "performance": perf}


def _fake_detail(*,
                   home_starters=None, home_subs=None,
                   away_starters=None, away_subs=None,
                   scorers=None) -> dict:
    return {
        "content": {
            "lineup": {
                "homeTeam": {
                    "starters": home_starters or [],
                    "subs":     home_subs or [],
                },
                "awayTeam": {
                    "starters": away_starters or [],
                    "subs":     away_subs or [],
                },
            },
            "matchFacts": {
                "events": {
                    "events": [
                        {"type": "goal",
                          "player": {"name": s}}
                        for s in (scorers or [])
                    ],
                },
            },
        },
    }


# ═════════════════════════════════════════════════════════════════════
# A. _fotmob_player_participation classifier
# ═════════════════════════════════════════════════════════════════════
def test_A1_starter_returns_played():
    from soccer_fotmob_settle import _fotmob_player_participation
    d = _fake_detail(home_starters=[_fake_starter("Tobias Heintz")])
    assert _fotmob_player_participation(d, "Tobias Heintz") == "played"


def test_A2_sub_with_subIn_returns_played():
    from soccer_fotmob_settle import _fotmob_player_participation
    d = _fake_detail(
        away_subs=[_fake_sub("Erling Haaland", minutes_in=62)],
    )
    assert _fotmob_player_participation(d, "Erling Haaland") == "played"


def test_A3_sub_with_rating_only_returns_played():
    """FotMob sometimes omits substitutionEvents but populates rating —
    that still means the player took the field."""
    from soccer_fotmob_settle import _fotmob_player_participation
    d = _fake_detail(
        home_subs=[_fake_sub("Bukayo Saka", rating=7.1)],
    )
    assert _fotmob_player_participation(d, "Bukayo Saka") == "played"


def test_A4_unused_sub_returns_unused_sub():
    """Named on the bench but no substitutionEvents / rating."""
    from soccer_fotmob_settle import _fotmob_player_participation
    d = _fake_detail(
        home_subs=[_fake_sub("Unused Bench Player")],
        home_starters=[_fake_starter("Someone Else")],
    )
    assert _fotmob_player_participation(
        d, "Unused Bench Player") == "unused_sub"


def test_A5_not_in_squad_returns_not_in_squad():
    """Player was suspended / omitted from the 18 — not in EITHER
    starters or subs on either side."""
    from soccer_fotmob_settle import _fotmob_player_participation
    d = _fake_detail(
        home_starters=[_fake_starter("Other A"),
                        _fake_starter("Other B")],
        away_starters=[_fake_starter("Other C")],
    )
    assert _fotmob_player_participation(
        d, "Tobias Heintz") == "not_in_squad"


def test_A6_empty_lineup_returns_none():
    """Zero-players in the payload → we couldn't verify."""
    from soccer_fotmob_settle import _fotmob_player_participation
    d = _fake_detail()   # completely empty
    assert _fotmob_player_participation(d, "Someone") is None


def test_A7_accent_and_partial_match():
    """Name-matching must be diacritic-insensitive and tolerant of
    last-name-only ('Salah' → 'Mohamed Salah')."""
    from soccer_fotmob_settle import _fotmob_player_participation
    d = _fake_detail(home_starters=[_fake_starter("Jonathan Ægidius")])
    assert _fotmob_player_participation(d, "Jonathan Aegidius") == "played"


# ═════════════════════════════════════════════════════════════════════
# B. FotMob scorer settler → VOID on DNP / unused sub
# ═════════════════════════════════════════════════════════════════════
def test_B1_suspended_starter_returns_void():
    """User's exact case — Tobias Heintz suspended, Göteborg 2-0 W.
    The scorer list has two OTHER Göteborg scorers.  Previously LOST,
    must now be VOID."""
    from soccer_fotmob_settle import _settle_scorer_market
    d = _fake_detail(
        home_starters=[_fake_starter("Someone Else"),
                        _fake_starter("Another Player")],
        scorers=["Someone Else", "Another Player"],
    )
    r = _settle_scorer_market(
        d, "Tobias Heintz", "tobias heintz anytime goal scorer",
    )
    assert r == "void"


def test_B2_unused_sub_returns_void():
    """Player was named on the bench but never came on."""
    from soccer_fotmob_settle import _settle_scorer_market
    d = _fake_detail(
        home_subs=[_fake_sub("Jens Hjerto-Dahl")],   # no subIn events
        home_starters=[_fake_starter("Someone Else")],
        scorers=["Someone Else"],
    )
    r = _settle_scorer_market(
        d, "Jens Hjerto-Dahl", "anytime goal scorer",
    )
    assert r == "void"


def test_B3_played_and_scored_returns_won():
    """Sanity: player played AND scored → WON."""
    from soccer_fotmob_settle import _settle_scorer_market
    d = _fake_detail(
        home_starters=[_fake_starter("Mohamed Salah")],
        scorers=["Mohamed Salah", "Cody Gakpo"],
    )
    r = _settle_scorer_market(
        d, "Mohamed Salah", "anytime goal scorer",
    )
    assert r == "won"


def test_B4_played_but_didnt_score_returns_lost():
    """Player played but didn't score → LOST (correct)."""
    from soccer_fotmob_settle import _settle_scorer_market
    d = _fake_detail(
        home_starters=[_fake_starter("Mohamed Salah")],
        scorers=["Cody Gakpo"],       # different scorer
    )
    r = _settle_scorer_market(
        d, "Mohamed Salah", "anytime goal scorer",
    )
    assert r == "lost"


def test_B5_played_zero_zero_returns_lost():
    """0-0 game, player played → LOST for anytime scorer."""
    from soccer_fotmob_settle import _settle_scorer_market
    d = _fake_detail(
        home_starters=[_fake_starter("Harry Kane")],
        scorers=[],   # no goals
    )
    r = _settle_scorer_market(
        d, "Harry Kane", "anytime goal scorer",
    )
    assert r == "lost"


def test_B6_unknown_participation_no_goals_returns_none():
    """When we can't verify participation AND there are no goals in
    the feed, abstain (None) so the caller can try another source."""
    from soccer_fotmob_settle import _settle_scorer_market
    d = _fake_detail(scorers=[])    # empty lineup, empty scorer list
    r = _settle_scorer_market(
        d, "Unknown Player", "anytime goal scorer",
    )
    assert r is None


def test_B7_first_goal_scorer_dnp_void():
    """First Goal Scorer must also VOID on DNP (not just Anytime)."""
    from soccer_fotmob_settle import _settle_scorer_market
    d = _fake_detail(
        home_starters=[_fake_starter("Other A")],
        scorers=["Other A"],
    )
    r = _settle_scorer_market(
        d, "Tobias Heintz", "tobias heintz first goal scorer",
    )
    assert r == "void"


# ═════════════════════════════════════════════════════════════════════
# C. check_fotmob_participation public helper signature
# ═════════════════════════════════════════════════════════════════════
def test_C1_check_fotmob_participation_signature():
    """The helper is publicly importable and returns None safely when
    called with garbage args (no network needed for shape test)."""
    from soccer_fotmob_settle import check_fotmob_participation
    import asyncio
    # Empty strings must short-circuit to None.
    r = asyncio.run(check_fotmob_participation("", "", None, ""))
    assert r is None


__all__: list[str] = []
