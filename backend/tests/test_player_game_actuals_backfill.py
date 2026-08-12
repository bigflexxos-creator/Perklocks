"""Player Game Actuals backfill — deterministic + smoke tests."""
from __future__ import annotations
import asyncio, sys
import pytest
sys.path.insert(0, "/app/backend")
pytestmark = pytest.mark.unit


class _Cur:
    def __init__(self, docs): self._d = list(docs)
    def limit(self, n): self._d = self._d[:n]; return self
    def sort(self, *a, **kw): return self
    def __aiter__(self): self._i = 0; return self
    async def __anext__(self):
        if self._i >= len(self._d): raise StopAsyncIteration
        d = self._d[self._i]; self._i += 1; return dict(d)


def _matches(doc, q):
    for k, v in q.items():
        if isinstance(v, dict):
            if "$lt" in v and not (doc.get(k) and doc.get(k) < v["$lt"]):
                return False
        elif doc.get(k) != v:
            return False
    return True


class _Coll:
    def __init__(self): self.docs = []
    def find(self, q, projection=None):
        return _Cur([d for d in self.docs if _matches(d, q)])
    async def find_one(self, q, projection=None):
        for d in self.docs:
            if _matches(d, q):
                return dict(d)
        return None
    async def insert_one(self, d): self.docs.append(dict(d))
    async def update_one(self, q, upd, upsert=False):
        for i, d in enumerate(self.docs):
            if _matches(d, q):
                self.docs[i].update(upd.get("$set", {})); return
        if upsert:
            m = dict(q); m.update(upd.get("$set", {})); self.docs.append(m)
    async def create_index(self, *a, **kw): pass


class _DB:
    def __init__(self): self._c = {}
    def __getitem__(self, n): return self._c.setdefault(n, _Coll())
    def __getattr__(self, n):
        if n.startswith("_"): raise AttributeError(n)
        return self.__getitem__(n)


def _run(coro): return asyncio.run(coro)


def test_backfill_mlb_preserves_raw_zeros_and_missing():
    from services.player_history.backfill import backfill_from_player_game_logs
    db = _DB()
    db["player_game_logs"].docs.extend([
        {"sport": "mlb", "player_id": "p1", "game_id": "g1",
          "date": "2026-05-01", "season": 2026, "team": "NYY",
          "hits": 3, "home_runs": 1, "rbi": 2, "runs": 0, "total_bases": 6},
        # Missing hits (None) — real zero on runs preserved.
        {"sport": "mlb", "player_id": "p1", "game_id": "g2",
          "date": "2026-04-28", "season": 2026, "team": "NYY",
          "hits": None, "home_runs": 0, "rbi": 0, "runs": 0},
    ])
    r = _run(backfill_from_player_game_logs(db, sport="mlb"))
    assert r["examined"] == 2
    assert r["accepted"] == 2
    assert r["inserted"] == 2
    rows = db["player_game_actuals"].docs
    # Row 1: hits=3 preserved.
    a = next(x for x in rows if x["event_id"] == "g1")
    assert a["actuals"]["h"] == 3.0
    # Row 2: hits=None (missing), hr=0 (real zero).
    b = next(x for x in rows if x["event_id"] == "g2")
    assert b["actuals"]["h"] is None
    assert b["actuals"]["hr"] == 0.0
    assert b["actuals"]["rbi"] == 0.0


def test_backfill_is_idempotent():
    from services.player_history.backfill import backfill_from_player_game_logs
    db = _DB()
    db["player_game_logs"].docs.append({
        "sport": "nba", "player_id": "p1", "game_id": "g1",
        "date": "2026-05-01", "season": 2026, "points": 30,
        "rebounds": 10, "assists": 8,
    })
    r1 = _run(backfill_from_player_game_logs(db, sport="nba"))
    r2 = _run(backfill_from_player_game_logs(db, sport="nba"))
    r3 = _run(backfill_from_player_game_logs(db, sport="nba"))
    assert r1["inserted"] == 1
    assert r2["inserted"] == 0 and r2["updated"] == 1
    assert r3["inserted"] == 0 and r3["updated"] == 1
    # Only ONE canonical row exists.
    assert len(db["player_game_actuals"].docs) == 1


def test_backfill_rejects_unresolved_identity():
    from services.player_history.backfill import backfill_from_player_game_logs
    db = _DB()
    db["player_game_logs"].docs.append({
        "sport": "nfl", "game_id": "g1", "date": "2025-11-01",
        "pass_yds": 300,   # NO player_id / canonical_player_id
    })
    r = _run(backfill_from_player_game_logs(db, sport="nfl"))
    assert r["identity_unresolved"] == 1
    assert r["inserted"] == 0
    assert len(db["player_game_actuals"].docs) == 0


def test_backfill_rejects_missing_event_id():
    from services.player_history.backfill import backfill_from_player_game_logs
    db = _DB()
    db["player_game_logs"].docs.append({
        "sport": "nba", "player_id": "p1",
        "date": "2026-05-01", "points": 25,  # no game_id
    })
    r = _run(backfill_from_player_game_logs(db, sport="nba"))
    assert r["event_unresolved"] == 1
    assert r["inserted"] == 0


def test_backfill_rejects_row_with_no_usable_stats():
    from services.player_history.backfill import backfill_from_player_game_logs
    db = _DB()
    db["player_game_logs"].docs.append({
        "sport": "mlb", "player_id": "p1", "game_id": "g1",
        "date": "2026-05-01", "hits": None, "home_runs": None,
        "rbi": None, "runs": None, "total_bases": None,
        "strikeouts": None, "pitcher_strikeouts": None,
    })
    r = _run(backfill_from_player_game_logs(db, sport="mlb"))
    assert r["missing_all_stats"] == 1
    assert r["inserted"] == 0


def test_backfill_provenance_retained():
    from services.player_history.backfill import (
        backfill_from_player_game_logs, BACKFILL_VERSION,
    )
    db = _DB()
    db["player_game_logs"].docs.append({
        "sport": "nba", "player_id": "p1", "game_id": "g1",
        "date": "2026-05-01", "points": 30,
    })
    _run(backfill_from_player_game_logs(db, sport="nba"))
    doc = db["player_game_actuals"].docs[0]
    assert doc["source"] == "legacy_player_game_logs"
    assert doc["source_record_id"] == "g1"
    assert doc["backfill_version"] == BACKFILL_VERSION
    assert doc["ingested_at"]


def test_mls_matchup_history_normalised_into_per_event_rows():
    from services.player_history.backfill import (
        backfill_from_mls_matchup_history,
    )
    db = _DB()
    db["mls_player_matchup_history"].docs.append({
        "player_id": "45843", "player_name": "Lionel Messi",
        "by_opponent": [
            {"opponent_id": "17606", "opponent_name": "NYC",
              "recent": [
                {"date": "2026-03-22T17:00Z", "goals": 1,
                  "assists": 0, "shots": 9, "season": 2026},
                {"date": "2025-11-29T23:00Z", "goals": 0,
                  "assists": 1, "shots": 0, "season": 2025},
              ]},
        ],
    })
    r = _run(backfill_from_mls_matchup_history(db))
    assert r["examined_players"] == 1
    assert r["accepted"] == 2
    rows = db["player_game_actuals"].docs
    assert len(rows) == 2
    assert all(r["sport"] == "soccer" for r in rows)
    assert all(r["canonical_player_id"] == "45843" for r in rows)


def test_player_history_stage2_reads_backfilled_rows_end_to_end():
    """Prove the Stage-2 NBA adapter reads real backfilled rows and
    performs an exact-threshold query with quantiles."""
    from services.player_history.backfill import backfill_from_player_game_logs
    from services.player_history import get_player_history
    db = _DB()
    for i, pts in enumerate([30, 22, 27, 19, 35, 24, 31, 18, 26, 33]):
        db["player_game_logs"].docs.append({
            "sport": "nba", "player_id": "cpid-lbj",
            "game_id": f"g-{i}",
            "date": f"2026-01-{10+i:02d}",
            "season": 2025,
            "points": pts, "rebounds": 8, "assists": 6,
        })
    _run(backfill_from_player_game_logs(db, sport="nba"))
    ev = _run(get_player_history(
        db, sport="NBA", canonical_player_id="cpid-lbj",
        market="player_points", threshold=24.5, direction="over",
        event_time="2026-06-01T00:00:00Z",
    ))
    # 10 rows normalised → source path proven end-to-end.
    assert ev.games_available == 10
    l10 = ev.last_10["result"]
    assert l10["sample_size"] == 10
    # Wins: pts > 24.5 → 30,27,35,31,26,33 = 6; losses: 4
    assert l10["wins"] == 6 and l10["losses"] == 4
    # Quantiles populated.
    assert l10["median"] is not None
    assert l10["q25"] is not None and l10["q75"] is not None
    assert l10["variance"] is not None


def test_as_of_still_excludes_future_backfill_rows():
    from services.player_history.backfill import backfill_from_player_game_logs
    from services.player_history import get_player_history
    db = _DB()
    db["player_game_logs"].docs.extend([
        {"sport": "nba", "player_id": "cpid-y", "game_id": "g-old",
          "date": "2026-03-01", "season": 2025, "points": 30,
          "rebounds": 8, "assists": 5},
        {"sport": "nba", "player_id": "cpid-y", "game_id": "g-new",
          "date": "2026-05-01", "season": 2025, "points": 5,
          "rebounds": 3, "assists": 1},
    ])
    _run(backfill_from_player_game_logs(db, sport="nba"))
    ev = _run(get_player_history(
        db, sport="NBA", canonical_player_id="cpid-y",
        market="player_points", threshold=15.5,
        event_time="2026-04-01T00:00:00Z",     # cutoff
    ))
    assert ev.games_available == 1
    assert ev.last_5["result"]["actual_values"] == [30.0]
