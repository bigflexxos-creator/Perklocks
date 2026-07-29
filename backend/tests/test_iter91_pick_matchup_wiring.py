"""Regression tests for `services/pick_matchup_wiring.build_matchup_payload`.

Focus: parsing correctness (market → stat, threshold, opponent, player),
support/unsupported branches, and that all sport routes return a
well-formed payload without raising, even on cold caches.

No HTTP calls. Uses an in-memory Motor DB via `mongomock_motor` when
available; falls back to a hand-rolled async stub otherwise so this
test file is CI-safe on any environment.
"""
from __future__ import annotations

import asyncio
import types
import pytest


# ─────────────────────────────────────────────────────────────────────
# Minimal async DB stub
# ─────────────────────────────────────────────────────────────────────
class _AsyncCursor:
    def __init__(self, rows: list[dict]):
        self._rows = list(rows)

    def sort(self, *_a, **_kw):
        return self

    def limit(self, *_a, **_kw):
        return self

    def __aiter__(self):
        self._it = iter(self._rows)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


class _AsyncCollection:
    def __init__(self, rows: list[dict] | None = None):
        self.rows = rows or []

    async def find_one(self, q: dict, *_a, **_kw):
        for r in self.rows:
            if all(r.get(k) == v for k, v in q.items()
                   if not isinstance(v, dict)):
                return dict(r)
        return None

    def find(self, *_a, **_kw):
        return _AsyncCursor(self.rows)

    async def count_documents(self, *_a, **_kw):
        return len(self.rows)


class _StubDB:
    def __init__(self):
        self.props_history = _AsyncCollection()
        self.player_game_logs = _AsyncCollection()
        self.nfl_player_weekly = _AsyncCollection()
        self.tennis_matches_history = _AsyncCollection()
        self.picks = _AsyncCollection()


# ─────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────
def _run(coro):
    return asyncio.run(coro)


def test_moneyline_pick_returns_unsupported():
    from services.pick_matchup_wiring import build_matchup_payload
    db = _StubDB()
    pick = {
        "id": "abc",
        "sport": "MLB",
        "market": "Miami Marlins Moneyline",
        "selection": "Miami Marlins",
        "event": "Arizona Diamondbacks @ Miami Marlins",
    }
    r = _run(build_matchup_payload(db, pick))
    assert r["supported"] is False
    assert r["sport"] == "MLB"
    assert "moneyline" in r["reason"].lower()


def test_mlb_strikeout_market_parsed_correctly():
    from services.pick_matchup_wiring import build_matchup_payload
    db = _StubDB()
    pick = {
        "id": "abc",
        "sport": "MLB",
        "market": "Zack Wheeler (PHI) Over 6.5 Strikeouts  · ALT LOCK",
        "selection": "Zack Wheeler",
        "event": "Philadelphia Phillies @ Miami Marlins",
    }
    r = _run(build_matchup_payload(db, pick))
    assert r["supported"] is True
    assert r["player_name"] == "Zack Wheeler"
    assert r["stat"] == "pitcher_strikeouts"
    assert r["threshold"] == 6.5
    # opponent should be resolved to the OPPOSING team, not the pitcher's
    assert r["opponent_team"] == "Miami Marlins"


def test_mlb_batter_hits_market():
    from services.pick_matchup_wiring import build_matchup_payload
    db = _StubDB()
    pick = {
        "id": "abc",
        "sport": "MLB",
        "market": "Aaron Judge Over 1.5 Hits",
        "selection": "Aaron Judge",
        "event": "New York Yankees @ Boston Red Sox",
    }
    r = _run(build_matchup_payload(db, pick))
    assert r["supported"] is True
    assert r["player_name"] == "Aaron Judge"
    assert r["stat"] == "hits"
    assert r["threshold"] == 1.5


def test_mlb_hits_runs_rbis_composite():
    from services.pick_matchup_wiring import build_matchup_payload
    db = _StubDB()
    pick = {
        "id": "abc",
        "sport": "MLB",
        "market": "Freddie Freeman Over 1.5 Hits + Runs + RBIs",
        "selection": "Freddie Freeman",
        "event": "LA Dodgers @ Colorado Rockies",
    }
    r = _run(build_matchup_payload(db, pick))
    assert r["supported"] is True
    assert r["stat"] == "hits_runs_rbis"
    assert r["threshold"] == 1.5


def test_nfl_passing_yards_market():
    from services.pick_matchup_wiring import build_matchup_payload
    db = _StubDB()
    pick = {
        "id": "abc",
        "sport": "NFL",
        "market": "Joe Burrow Over 249.5 Passing Yards",
        "selection": "Joe Burrow",
        "event": "Cincinnati Bengals @ Kansas City Chiefs",
    }
    r = _run(build_matchup_payload(db, pick))
    assert r["supported"] is True
    assert r["stat"] == "passing_yards"
    assert r["threshold"] == 249.5
    assert r["sport"] == "NFL"
    # Even with no data rows the shape must be sane.
    assert r["matchup_grade"] in {"A+", "A", "B", "C", "D", "F", None}


def test_tennis_aces_market():
    from services.pick_matchup_wiring import build_matchup_payload
    db = _StubDB()
    pick = {
        "id": "abc",
        "sport": "Tennis",
        "market": "Carlos Alcaraz Over 4.5 Aces",
        "selection": "Carlos Alcaraz",
        "event": "Carlos Alcaraz @ Novak Djokovic",
    }
    r = _run(build_matchup_payload(db, pick))
    assert r["supported"] is True
    assert r["stat"] == "aces"
    assert r["threshold"] == 4.5


def test_unrecognised_stat_returns_unsupported():
    from services.pick_matchup_wiring import build_matchup_payload
    db = _StubDB()
    pick = {
        "id": "abc",
        "sport": "MLB",
        "market": "Some Player Over 999.5 Unknown Metric",
        "selection": "Some Player",
        "event": "Team A @ Team B",
    }
    r = _run(build_matchup_payload(db, pick))
    assert r["supported"] is False
    assert "unrecognised stat" in r["reason"]


def test_never_raises_on_missing_fields():
    """Even a nearly-empty pick doc must return a safe payload."""
    from services.pick_matchup_wiring import build_matchup_payload
    db = _StubDB()
    pick = {"id": "abc"}   # no sport, market, event, ...
    r = _run(build_matchup_payload(db, pick))
    assert r["supported"] is False
    assert "sport" in r["reason"]
