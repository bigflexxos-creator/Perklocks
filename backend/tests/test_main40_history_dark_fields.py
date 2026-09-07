"""Item P0-D — Historical Intelligence Completion.

Verifies that after Round 3 patches, the standard player-history
contract exposes:

  1. NHL wired into the universal dispatcher.
  2. Atomic per-game rows on the evidence contract.
  3. H2H source games surfaced when an opponent is supplied.
  4. Streak (current consecutive HIT/MISS vs the exact threshold).
  5. days_since_last_game (relative to history_as_of, never future
     leakage).
  6. vs_opponent_recent aggregate (trailing-5 H2H).

Uses in-memory fake DB — no network.
"""
from __future__ import annotations

import asyncio
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


class _FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)
    def sort(self, *a, **k):
        # sort by event_time desc as expected by loader
        try:
            self._docs.sort(key=lambda d: d.get("event_time", ""), reverse=True)
        except Exception:
            pass
        return self
    def limit(self, n):
        self._docs = self._docs[:n]
        return self
    def __aiter__(self):
        self._i = 0
        return self
    async def __anext__(self):
        if self._i >= len(self._docs):
            raise StopAsyncIteration
        d = self._docs[self._i]
        self._i += 1
        return d


class _FakeColl:
    def __init__(self, docs):
        self._docs = docs
    def find(self, query, projection=None):
        pid = query.get("player_id") or query.get("canonical_player_id")
        cutoff = None
        for k in ("event_time", "date"):
            v = query.get(k)
            if isinstance(v, dict) and "$lt" in v:
                cutoff = v["$lt"]
        docs = [d for d in self._docs if d.get("player_id") == pid or d.get("canonical_player_id") == pid]
        if cutoff:
            docs = [d for d in docs if str(d.get("event_time") or d.get("date") or "") < cutoff]
        return _FakeCursor(docs)


class _FakeDB:
    def __init__(self, actuals):
        self.player_game_actuals = _FakeColl(actuals)
        self.player_game_logs = _FakeColl([])


def _row(days_ago: int, hits: float, opponent: str = "OPP",
         home: bool = True, season: int = 2026,
         player_id: str = "p1", canonical: str = "canon-1"):
    from datetime import datetime, timezone, timedelta
    dt = (datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc)
          - timedelta(days=days_ago)).isoformat()
    return {
        "sport": "nba",
        "player_id": player_id,
        "canonical_player_id": canonical,
        "event_time": dt,
        "game_id": f"g-{days_ago}",
        "opponent": opponent,
        "team": "TEAM",
        "home_away": "home" if home else "away",
        "season": season,
        "points": hits,  # generic scorer
    }


def test_nhl_now_wired_into_universal_dispatcher():
    """Before this round NHL returned SPORT_NOT_SUPPORTED; it must
    now route to populate_nhl_evidence."""
    from services.player_history.service import get_player_history

    class _NoDB:
        def __getattr__(self, _n):
            return self
        def find(self, *a, **k):
            return _FakeCursor([])
        async def find_one(self, *a, **k):
            return None
    async def _run():
        ev = await get_player_history(
            _NoDB(), sport="NHL", player_name="Auston Matthews",
            market="Shots on goal", threshold=3.5,
        )
        # NHL adapter runs → source should NOT be SPORT_NOT_SUPPORTED
        # (may be UNAVAILABLE due to no data, but the dispatcher ran).
        assert ev.source != "SPORT_NOT_SUPPORTED", (
            f"NHL dispatcher not wired: source={ev.source}")
    asyncio.get_event_loop().run_until_complete(_run())


def test_standard_populate_exposes_dark_fields():
    """Feed an NBA-shaped fake set of rows through the shared
    populate and verify atomic_games, streak, h2h_source_games,
    days_since_last_game, vs_opponent_recent are all populated."""
    from services.player_history._shared import populate_standard_evidence
    from services.player_history.models import PlayerHistoryEvidence

    rows = [
        _row(1,  22, opponent="OPP"),   # recent vs OPP, hit
        _row(3,  28, opponent="XYZ"),   # non-H2H, hit
        _row(5,  10, opponent="OPP"),   # H2H miss
        _row(7,  25, opponent="OPP"),   # H2H hit
        _row(10, 26, opponent="OPP"),   # H2H hit
        _row(14, 30, opponent="OPP"),   # H2H hit
    ]
    db = _FakeDB(rows)
    ev = PlayerHistoryEvidence(
        sport="NBA", market="points", threshold=20.5,
        direction="over",
        history_as_of="2026-06-06T12:00:00+00:00",
    )
    def _extract(market, r):
        return r.get("points")

    async def _run():
        result = await populate_standard_evidence(
            db, ev,
            sport="nba",
            player_id="p1",
            canonical_player_id="canon-1",
            opponent="OPP",
            home_away=None,
            market_extractor=_extract,
        )
        # 1. Atomic games surfaced.
        assert result.atomic_games and len(result.atomic_games) == 6
        assert result.atomic_games[0]["actual"] == 22
        assert result.atomic_games[0]["opponent"] == "OPP"
        # 2. Streak = HIT x 1 (most recent is >20.5 vs OPP).
        assert result.streak is not None
        assert result.streak.startswith("HIT")
        # 3. days_since_last_game = 1.
        assert result.days_since_last_game == 1
        # 4. H2H source games: 4 vs OPP (indexes 1,3,4,5 in reverse-time).
        assert result.h2h_source_games is not None
        assert len(result.h2h_source_games) == 5  # game -1d, -5d, -7d, -10d, -14d
        # 5. vs_opponent_recent populated.
        assert result.vs_opponent_recent is not None
        assert result.vs_opponent_recent["games_used"] > 0
    asyncio.get_event_loop().run_until_complete(_run())


def test_streak_ignores_missing_actuals():
    """A None actual in the sequence must not break the streak."""
    from services.player_history._shared import populate_standard_evidence
    from services.player_history.models import PlayerHistoryEvidence
    rows = [
        _row(1,  22),
        _row(3,  28),
        _row(5,  25),
    ]
    # Corrupt one row to have no actual.
    rows[1]["points"] = None
    db = _FakeDB(rows)
    ev = PlayerHistoryEvidence(
        sport="NBA", market="points", threshold=20.5, direction="over",
        history_as_of="2026-06-06T12:00:00+00:00",
    )
    async def _run():
        result = await populate_standard_evidence(
            db, ev, sport="nba",
            player_id="p1", canonical_player_id="canon-1",
            opponent=None, home_away=None,
            market_extractor=lambda m, r: r.get("points"),
        )
        assert result.streak == "HIT x 2"  # first + third row hit; middle skipped
    asyncio.get_event_loop().run_until_complete(_run())


if __name__ == "__main__":
    test_nhl_now_wired_into_universal_dispatcher()
    test_standard_populate_exposes_dark_fields()
    test_streak_ignores_missing_actuals()
    print("OK — historical intelligence dark fields verified.")
