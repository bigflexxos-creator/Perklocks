"""Team Game Actuals Backfill — deterministic tests."""
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


def _seed_game(db, sport, gid, home, away, hs, aws, date="2026-05-01T20:00:00Z", season=2026, week=None):
    d = {"sport": sport, "game_id": gid, "date": date, "home": home,
          "away": away, "result": {"home": hs, "away": aws}, "status": "Final",
          "season": season}
    if week is not None: d["week"] = week
    db["games"].docs.append(d)


def test_backfill_writes_two_perspective_rows_per_game():
    from services.team_history.backfill import backfill_from_games_collection
    db = _DB()
    _seed_game(db, "mlb", "g1", "NYY", "BOS", 7, 4)
    r = _run(backfill_from_games_collection(db, sport="mlb"))
    assert r["examined"] == 1
    assert r["accepted"] == 2   # two perspectives
    assert r["inserted"] == 2
    rows = db["team_game_actuals"].docs
    assert len(rows) == 2
    nyy = next(x for x in rows if x["canonical_team_id"] == "NYY")
    bos = next(x for x in rows if x["canonical_team_id"] == "BOS")
    # Home/away correctness — NYY was HOME, BOS was AWAY.
    assert nyy["home_away"] == "home"
    assert bos["home_away"] == "away"
    # Perspective correctness — scores flipped.
    assert nyy["team_score"] == 7.0 and nyy["opponent_score"] == 4.0
    assert bos["team_score"] == 4.0 and bos["opponent_score"] == 7.0
    # Opponent identity preserved.
    assert nyy["canonical_opponent_id"] == "BOS"
    assert bos["canonical_opponent_id"] == "NYY"
    # Result strings correct.
    assert nyy["result"] == "WIN" and bos["result"] == "LOSS"


def test_backfill_zero_zero_draw_preserved_not_nulled():
    from services.team_history.backfill import backfill_from_games_collection
    db = _DB()
    _seed_game(db, "mlb", "g2", "TOR", "TB", 0, 0)
    _run(backfill_from_games_collection(db, sport="mlb"))
    rows = db["team_game_actuals"].docs
    tor = next(x for x in rows if x["canonical_team_id"] == "TOR")
    assert tor["team_score"] == 0.0        # real zero preserved
    assert tor["opponent_score"] == 0.0
    assert tor["result"] == "DRAW"


def test_backfill_missing_score_stays_none_no_row_emitted():
    from services.team_history.backfill import backfill_from_games_collection
    db = _DB()
    db["games"].docs.append({
        "sport": "mlb", "game_id": "g3", "date": "2026-05-01T20:00:00Z",
        "home": "NYY", "away": "BOS", "status": "Final",
        "result": {"home": None, "away": None},
    })
    r = _run(backfill_from_games_collection(db, sport="mlb"))
    assert r["missing_result"] == 1
    assert r["inserted"] == 0
    assert len(db["team_game_actuals"].docs) == 0


def test_backfill_rejects_missing_team_identity():
    from services.team_history.backfill import backfill_from_games_collection
    db = _DB()
    db["games"].docs.append({
        "sport": "nfl", "game_id": "g4", "date": "2026-01-01T20:00:00Z",
        "home": None, "away": "KC", "status": "Final",
        "result": {"home": 20, "away": 24},
    })
    r = _run(backfill_from_games_collection(db, sport="nfl"))
    assert r["identity_unresolved"] == 1
    assert r["inserted"] == 0


def test_backfill_is_idempotent():
    from services.team_history.backfill import backfill_from_games_collection
    db = _DB()
    _seed_game(db, "mlb", "g5", "LAD", "SFG", 5, 2)
    r1 = _run(backfill_from_games_collection(db, sport="mlb"))
    r2 = _run(backfill_from_games_collection(db, sport="mlb"))
    r3 = _run(backfill_from_games_collection(db, sport="mlb"))
    assert r1["inserted"] == 2
    assert r2["inserted"] == 0 and r2["updated"] == 2
    assert r3["inserted"] == 0 and r3["updated"] == 2
    # Only two rows exist.
    assert len(db["team_game_actuals"].docs) == 2


def test_backfill_provenance_retained():
    from services.team_history.backfill import (
        backfill_from_games_collection, BACKFILL_VERSION,
    )
    db = _DB()
    _seed_game(db, "nhl", "g6", "TOR", "MTL", 3, 2)
    _run(backfill_from_games_collection(db, sport="nhl"))
    doc = db["team_game_actuals"].docs[0]
    assert doc["source"] == "legacy_games"
    assert doc["source_record_id"] == "g6"
    assert doc["backfill_version"] == BACKFILL_VERSION
    assert doc["ingested_at"]


def test_team_history_stage3_reads_backfilled_rows_end_to_end():
    from services.team_history.backfill import backfill_from_games_collection
    from services.team_history import get_team_history
    db = _DB()
    # 10 games — NYY 6-4 record
    for i in range(10):
        hs, aws = (7, 4) if i % 2 == 0 else (3, 5)
        _seed_game(db, "mlb", f"g{i}", "NYY", "BOS", hs, aws,
                     date=f"2026-05-{10+i:02d}T20:00:00Z")
    _run(backfill_from_games_collection(db, sport="mlb"))
    ev = _run(get_team_history(
        db, sport="MLB", canonical_team_id="NYY",
        as_of="2026-06-15T00:00:00Z",
    ))
    l10 = ev.last_10
    assert l10["sample_size"] == 10
    # 5 games at 7 (home wins), 5 at 3 (home losses).
    assert l10["wins"] == 5 and l10["losses"] == 5
    # Distribution populated.
    assert l10["scored_median"] == 5.0    # median of [7,3,7,3,7,3,7,3,7,3]
    # Threshold — different lines from same raw actuals.
    from services.player_history.threshold_engine import evaluate_threshold
    over_4 = evaluate_threshold(l10["scored_values"], 4.0, "over")
    over_6 = evaluate_threshold(l10["scored_values"], 6.0, "over")
    assert over_4.wins == 5 and over_4.losses == 5    # 3 < 4 < 7
    assert over_6.wins == 5 and over_6.losses == 5    # 7 > 6, 3 < 6
    # Different lines → different hit-rates from SAME raw actuals.


def test_h2h_perspective_and_small_sample_honesty():
    from services.team_history.backfill import backfill_from_games_collection
    from services.team_history import get_h2h_history
    db = _DB()
    _seed_game(db, "mlb", "g1", "NYY", "BOS", 7, 4)
    _seed_game(db, "mlb", "g2", "BOS", "NYY", 6, 3,
                 date="2026-04-01T20:00:00Z")
    _run(backfill_from_games_collection(db, sport="mlb"))
    # NYY perspective: 1 win (7-4), 1 loss (3-6)
    h_nyy = _run(get_h2h_history(
        db, sport="MLB", canonical_team_id="NYY",
        canonical_opponent_id="BOS",
        as_of="2026-06-01T00:00:00Z",
    ))
    assert h_nyy.sample_size == 2
    assert h_nyy.wins == 1 and h_nyy.losses == 1
    # BOS perspective — mirror.
    h_bos = _run(get_h2h_history(
        db, sport="MLB", canonical_team_id="BOS",
        canonical_opponent_id="NYY",
        as_of="2026-06-01T00:00:00Z",
    ))
    assert h_bos.sample_size == 2
    assert h_bos.wins == 1 and h_bos.losses == 1
    # Small sample — quantiles NOT inflated.
    assert h_nyy.scored_median is None    # < 3 samples


def test_as_of_prevents_future_leakage_on_team_backfill():
    from services.team_history.backfill import backfill_from_games_collection
    from services.team_history import get_team_history
    db = _DB()
    _seed_game(db, "mlb", "g-old", "NYY", "BOS", 7, 4,
                 date="2026-03-01T20:00:00Z")
    _seed_game(db, "mlb", "g-new", "NYY", "BOS", 2, 10,
                 date="2026-05-01T20:00:00Z")
    _run(backfill_from_games_collection(db, sport="mlb"))
    # as_of BETWEEN the two games.
    ev = _run(get_team_history(
        db, sport="MLB", canonical_team_id="NYY",
        as_of="2026-04-01T00:00:00Z",
    ))
    assert ev.last_5["sample_size"] == 1
    assert ev.last_5["scored_values"] == [7.0]     # old game only


def test_soccer_fixtures_missing_scores_report_missing_result():
    from services.team_history.backfill import backfill_soccer_from_fixtures
    db = _DB()
    db["soccer_fixtures"].docs.append({
        "status": "FINISHED",
        "home_team": "A", "away_team": "B",
        "utc_kickoff": "2026-08-07T18:00:00Z",
        "home_score": None, "away_score": None, "full_time": None,
    })
    r = _run(backfill_soccer_from_fixtures(db))
    assert r["examined"] == 1
    assert r["missing_result"] == 1
    assert r["inserted"] == 0
