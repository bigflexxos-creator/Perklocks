"""History gap-closure — deterministic tests."""
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
            if "$exists" in v: continue
            if "$lt" in v and not (doc.get(k) and doc.get(k) < v["$lt"]): return False
        elif doc.get(k) != v: return False
    return True


class _Coll:
    def __init__(self): self.docs = []
    def find(self, q, projection=None):
        return _Cur([d for d in self.docs if _matches(d, q)])
    async def find_one(self, q, projection=None):
        for d in self.docs:
            if _matches(d, q): return dict(d)
        return None
    async def insert_one(self, d): self.docs.append(dict(d))
    async def update_one(self, q, upd, upsert=False):
        for i, d in enumerate(self.docs):
            if _matches(d, q):
                self.docs[i].update(upd.get("$set", {})); return
    async def create_index(self, *a, **kw): pass


class _DB:
    def __init__(self): self._c = {}
    def __getitem__(self, n): return self._c.setdefault(n, _Coll())
    def __getattr__(self, n):
        if n.startswith("_"): raise AttributeError(n)
        return self.__getitem__(n)


def _run(coro): return asyncio.run(coro)


def test_mlb_season_derived_from_date_deterministic():
    from services.team_history.gap_closure import _derive_mlb_season
    assert _derive_mlb_season("2026-05-01T20:00:00Z") == 2026
    assert _derive_mlb_season("2022-03-17T17:05:00Z") == 2022
    assert _derive_mlb_season(None) is None
    assert _derive_mlb_season("") is None
    assert _derive_mlb_season("badstr") is None


def test_mlb_season_backfill_updates_missing_rows():
    from services.team_history.gap_closure import apply_mlb_season_backfill
    db = _DB()
    db["team_game_actuals"].docs.extend([
        {"sport": "mlb", "canonical_team_id": "NYY", "event_id": "g1",
          "event_time": "2026-05-01T20:00:00Z", "season": None},
        {"sport": "mlb", "canonical_team_id": "BOS", "event_id": "g1",
          "event_time": "2026-05-01T20:00:00Z", "season": None},
        {"sport": "mlb", "canonical_team_id": "LAD", "event_id": "g2",
          "event_time": "2022-06-15T19:00:00Z", "season": None},
    ])
    r = _run(apply_mlb_season_backfill(db))
    assert r["updated"] == 3
    assert 2026 in r["seasons"] and 2022 in r["seasons"]
    for d in db["team_game_actuals"].docs:
        assert d["season"] in (2026, 2022)


def test_tennis_history_ingests_both_perspectives():
    from services.team_history.gap_closure import (
        backfill_tennis_from_matches_history,
    )
    db = _DB()
    db["tennis_matches_history"].docs.append({
        "tourney_id": "2024-339", "tourney_name": "Brisbane",
        "date": "2024-01-01",
        "winner_id": "W1", "winner_name": "Player A",
        "loser_id":  "L1", "loser_name":  "Player B",
        "surface":   "Hard",
        "w_ace": 8, "w_df": 2, "w_1stIn": 45,
        "l_ace": 3, "l_df": 4, "l_1stIn": 40,
        "round": "F",
    })
    r = _run(backfill_tennis_from_matches_history(db))
    assert r["examined"] == 1
    assert r["accepted"] == 2
    rows = db["player_game_actuals"].docs
    assert len(rows) == 2
    w = next(x for x in rows if x["canonical_player_id"] == "W1")
    l = next(x for x in rows if x["canonical_player_id"] == "L1")
    assert w["result"] == "WIN" and l["result"] == "LOSS"
    assert w["surface"] == "hard" and l["surface"] == "hard"
    assert w["actuals"]["aces"] == 8.0
    assert l["actuals"]["aces"] == 3.0
    # Opponent identity mirrored correctly.
    assert w["opponent"] == "L1" and l["opponent"] == "W1"


def test_tennis_gap_closure_rejects_missing_identity():
    from services.team_history.gap_closure import (
        backfill_tennis_from_matches_history,
    )
    db = _DB()
    db["tennis_matches_history"].docs.append({
        "tourney_id": "x", "date": "2024-01-01",
        "winner_id": "W1",   # loser_id missing
    })
    r = _run(backfill_tennis_from_matches_history(db))
    assert r["identity_unresolved"] == 1
    assert r["inserted"] == 0


def test_soccer_settlement_events_ingest():
    from services.team_history.gap_closure import (
        backfill_soccer_teams_from_settlement_events,
    )
    db = _DB()
    db["settlement_events"].docs.append({
        "event_id": "ev-1", "result": "won",
        "settled_at": "2026-08-06T22:44:35.442928+00:00",
        "actual_result": {"final_score": {"Fenerbahce": "2",
                                            "SK Sturm Graz": "0"}},
    })
    r = _run(backfill_soccer_teams_from_settlement_events(db))
    assert r["examined"] == 1
    assert r["accepted"] == 2
    rows = db["team_game_actuals"].docs
    fen = next(x for x in rows if x["canonical_team_id"] == "Fenerbahce")
    stg = next(x for x in rows if x["canonical_team_id"] == "SK Sturm Graz")
    # Home perspective is FIRST key in dict — deterministic in Py3.7+
    assert fen["team_score"] == 2.0 and fen["opponent_score"] == 0.0
    assert stg["team_score"] == 0.0 and stg["opponent_score"] == 2.0
    assert fen["result"] == "WIN" and stg["result"] == "LOSS"
    assert fen["source"] == "settlement_events"


def test_gap_closure_missing_final_score_rejected():
    from services.team_history.gap_closure import (
        backfill_soccer_teams_from_settlement_events,
    )
    db = _DB()
    db["settlement_events"].docs.append({
        "event_id": "ev-2", "result": "won",
        "settled_at": "2026-08-06T22:44:35Z",
        "actual_result": {"final_score": {}},  # empty
    })
    r = _run(backfill_soccer_teams_from_settlement_events(db))
    assert r["missing_result"] == 1
    assert r["inserted"] == 0


def test_gap_closure_is_idempotent():
    """Rerun the tennis + soccer gap closures — no duplicates."""
    from services.team_history.gap_closure import (
        backfill_tennis_from_matches_history,
    )
    db = _DB()
    db["tennis_matches_history"].docs.append({
        "tourney_id": "T1", "date": "2024-01-01",
        "winner_id": "W", "loser_id": "L", "w_ace": 5, "l_ace": 3,
    })
    r1 = _run(backfill_tennis_from_matches_history(db))
    r2 = _run(backfill_tennis_from_matches_history(db))
    assert r1["inserted"] == 2
    assert r2["inserted"] == 0 and r2["updated"] == 2
    assert len(db["player_game_actuals"].docs) == 2
