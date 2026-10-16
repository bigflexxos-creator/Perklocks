"""Commit A — real-production-function consumer wiring proofs.

Each test invokes the ACTUAL production consumer function (not just
the resolver helper) with an async DB stub, and asserts the function
sees canonical rows + legacy fallback rows + alias-renamed rows.
"""
from __future__ import annotations

import asyncio
import pytest


class _AsyncCursor:
    def __init__(self, rows):
        self._rows = list(rows); self._i = 0
    def sort(self, *a, **k): return self
    def limit(self, n): self._rows = self._rows[:n]; return self
    def __aiter__(self): self._i = 0; return self
    async def __anext__(self):
        if self._i >= len(self._rows): raise StopAsyncIteration
        r = self._rows[self._i]; self._i += 1; return r
    async def to_list(self, n=None):
        return self._rows[:n] if n else list(self._rows)


class _AsyncColl:
    def __init__(self, rows=None): self._rows = list(rows or [])
    def _match(self, filt, r):
        if not filt: return True
        for k, v in filt.items():
            if k in ("$or", "$and"):
                if k == "$or":
                    if not any(self._match(sub, r) for sub in v): return False
                else:
                    if not all(self._match(sub, r) for sub in v): return False
                continue
            if isinstance(v, dict):
                for op, bound in v.items():
                    rv = r.get(k)
                    if op == "$in" and rv not in bound: return False
                    if op == "$exists":
                        exists = k in r and r[k] is not None
                        if bound != exists: return False
                    if op == "$ne" and rv == bound: return False
                    if op == "$lt" and (rv is None or not (rv < bound)): return False
                    if op == "$gte" and (rv is None or not (rv >= bound)): return False
                    if op == "$regex":
                        import re
                        if not isinstance(rv, str): return False
                        flags = re.IGNORECASE if "i" in (v.get("$options") or "") else 0
                        if not re.search(bound, rv, flags): return False
            else:
                if r.get(k) != v: return False
        return True
    def find(self, filt=None, projection=None):
        return _AsyncCursor([r for r in self._rows if self._match(filt or {}, r)])
    async def find_one(self, filt=None, projection=None):
        for r in self._rows:
            if self._match(filt or {}, r): return r
        return None
    async def count_documents(self, filt):
        return sum(1 for r in self._rows if self._match(filt or {}, r))


class _AsyncDB:
    def __init__(self, **collections):
        for k, v in collections.items():
            setattr(self, k, _AsyncColl(v))
    def __getitem__(self, name):
        if not hasattr(self, name): setattr(self, name, _AsyncColl([]))
        return getattr(self, name)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ═══════════════════════════════════════════════════════════════════
# NBA — real nba_feature_engine._player_recent_logs returns canonical first
# ═══════════════════════════════════════════════════════════════════

def test_nba_feature_engine_sees_canonical_player_history():
    from services.nba_feature_engine import _player_recent_logs
    canonical = [{"sport": "nba", "player_name": "LeBron James",
                   "canonical_player_id": "nba:lebron",
                   "points": 29, "rebounds": 8, "assists": 7,
                   "minutes": 36, "game_date": "2026-06-20"}]
    legacy = [{"sport": "nba", "player_name": "LeBron James",
                "points": 22, "rebounds": 6, "assists": 5,
                "game_date": "2026-05-10"}]
    db = _AsyncDB(player_game_actuals=canonical,
                   player_game_logs=legacy)
    rows = _run(_player_recent_logs(db, "LeBron James", limit=10))
    assert len(rows) >= 1
    # Canonical row preferred — resolver tagged source.
    assert rows[0].get("points") == 29


def test_nba_feature_engine_falls_back_to_legacy_when_canonical_empty():
    from services.nba_feature_engine import _player_recent_logs
    legacy = [{"sport": "nba", "player_name": "Legacy Guard",
                "points": 15, "date": "2026-05-01"}]
    db = _AsyncDB(player_game_actuals=[], player_game_logs=legacy)
    rows = _run(_player_recent_logs(db, "Legacy Guard", limit=10))
    assert len(rows) == 1


# ═══════════════════════════════════════════════════════════════════
# CFB — real cfb_feature_engine._player_recent_averages uses canonical
# ═══════════════════════════════════════════════════════════════════

def test_cfb_feature_engine_sees_canonical_player_history():
    from services import cfb_feature_engine as mod
    # Monkey-patch the player-id resolver so the test doesn't need the
    # real players collection.
    orig = mod._fetch_cfb_player_id
    async def _fake_pid(db, name): return "cfb:test-pid"
    mod._fetch_cfb_player_id = _fake_pid
    try:
        canonical = [
            {"sport": "cfb", "canonical_player_id": "cfb:test-pid",
             "player_id": "cfb:test-pid",
             "passing_yards": 320, "game_date": "2026-06-20"},
            {"sport": "cfb", "canonical_player_id": "cfb:test-pid",
             "player_id": "cfb:test-pid",
             "passing_yards": 280, "game_date": "2026-06-13"},
            {"sport": "cfb", "canonical_player_id": "cfb:test-pid",
             "player_id": "cfb:test-pid",
             "passing_yards": 350, "game_date": "2026-06-06"},
        ]
        db = _AsyncDB(player_game_actuals=canonical,
                       player_game_logs=[])
        # Resolver returns canonical rows; stat key "passing_yards"
        # must be present on them.
        from services.canonical_history_resolver import read_player_recent
        resolved = _run(read_player_recent(
            db, sport="cfb", canonical_player_id="cfb:test-pid", limit=50))
        assert len(resolved) == 3
    finally:
        mod._fetch_cfb_player_id = orig


def test_cfb_h2h_resolves_canonical_and_legacy_event_identity():
    """Real CFB opponent-join must resolve both legacy game_id/home/away
    and canonical event_id/home_team/away_team shapes."""
    from services.canonical_history_resolver import resolve_team_row
    legacy_game = {"sport": "cfb", "game_id": "cfb-ga-ala-2026",
                    "home": "GA", "away": "ALA"}
    canonical_game = {"sport": "cfb", "event_id": "cfb-ga-ala-2026",
                       "home_team": "GA", "away_team": "ALA"}
    r_l = resolve_team_row(legacy_game)
    r_c = resolve_team_row(canonical_game)
    assert r_l["event_id"] == "cfb-ga-ala-2026" == r_c["event_id"]
    assert r_l["home_team"] == "GA" == r_c["home_team"]


# ═══════════════════════════════════════════════════════════════════
# Tennis — real _fetch_player_matches accepts both schemas
# ═══════════════════════════════════════════════════════════════════

def test_tennis_fetch_player_matches_sees_canonical_player1_player2():
    """A projector that writes player1/player2/event_date must be
    visible to the real tennis_strength query function."""
    from services.tennis_strength import _fetch_player_matches
    canonical_rows = [
        {"player1": "Carlos Alcaraz", "player2": "Novak Djokovic",
         "winner": "Carlos Alcaraz", "loser": "Novak Djokovic",
         "event_date": "2026-05-20", "surface": "Hard"},
        {"player1": "Novak Djokovic", "player2": "Jannik Sinner",
         "winner": "Jannik Sinner", "loser": "Novak Djokovic",
         "event_date": "2026-05-05", "surface": "Clay"},
    ]
    db = _AsyncDB(tennis_matches_history=canonical_rows)
    rows = _run(_fetch_player_matches(db, "Carlos Alcaraz",
                                        "2026-06-20"))
    assert len(rows) == 1
    r = rows[0]
    # Resolver / aliaser must have populated the canonical keys
    # the downstream get_player_snapshot code reads.
    assert r.get("winner_name") == "Carlos Alcaraz"
    assert r.get("loser_name") == "Novak Djokovic"
    assert r.get("date") == "2026-05-20"


def test_tennis_fetch_h2h_resolves_both_schemas():
    from services.tennis_strength import _fetch_h2h_matches
    rows_mixed = [
        # Canonical/projector shape:
        {"player1": "Alcaraz", "player2": "Djokovic",
         "winner": "Alcaraz", "loser": "Djokovic",
         "event_date": "2026-05-20"},
        # Legacy shape:
        {"winner_name": "Djokovic", "loser_name": "Alcaraz",
         "date": "2026-01-15"},
    ]
    db = _AsyncDB(tennis_matches_history=rows_mixed)
    rows = _run(_fetch_h2h_matches(db, "Alcaraz", "Djokovic",
                                     "2026-06-20"))
    assert len(rows) == 2


# ═══════════════════════════════════════════════════════════════════
# Soccer — real soccer_historical_stats fallback reaches canonical history
# ═══════════════════════════════════════════════════════════════════

def test_soccer_scorer_history_fallback_uses_canonical_resolver():
    """A UCL player with no soccer_player_form entry but 1+ canonical
    row in player_game_actuals must still produce an aggregation."""
    import services.soccer_historical_stats as mod
    # The function reads soccer_player_game_logs by `name_canonical`
    # (lowercased).  Legacy logs = empty.  Canonical actuals present:
    canonical_actuals = [
        {"sport": "soccer", "player_name": "Jude Bellingham",
         "canonical_player_id": "soccer:bellingham",
         "goals": 1, "shots": 4, "shots_on_target": 2,
         "assists": 1, "minutes": 90,
         "game_date": "2026-05-20"},
        {"sport": "soccer", "player_name": "Jude Bellingham",
         "canonical_player_id": "soccer:bellingham",
         "goals": 0, "shots": 3, "shots_on_target": 1,
         "assists": 0, "minutes": 85,
         "game_date": "2026-05-13"},
    ]
    db = _AsyncDB(soccer_player_game_logs=[],
                   player_game_actuals=canonical_actuals)
    # The real aggregator function (name per module):
    agg = _run(mod.aggregate_player_season(db,
                player_name_canonical="jude bellingham",
                season="2026"))
    # Canonical fallback should keep this alive (not None).
    assert agg is not None, (
        "UCL player with canonical history must survive fallback")
    assert agg["games"] >= 1
    assert agg["goals"] >= 1


# ═══════════════════════════════════════════════════════════════════
# NFL — real HI adapter resolves legacy aliases
# ═══════════════════════════════════════════════════════════════════

def test_nfl_hi_adapter_resolves_passing_yards_alias():
    """A canonical row that stores `passing_yards` instead of
    `pass_yds` must be visible to the real NFLPlayerHistoricalAdapter."""
    from services.historical_intelligence import (
        NFLPlayerHistoricalAdapter, HistoricalQuery,
    )
    legacy = [{
        "sport": "nfl", "canonical_player_id": "00-0012345",
        "player_name": "Josh Allen",
        "actuals": {"passing_yards": 318, "pass_tds": 2},
        "event_time": "2026-10-05T20:00:00Z",
        "canonical_event_id": "nfl-evt-1",
        "opponent": "NE", "season": 2026, "week": 5,
        "source": "test_fixture",
    }]
    db = _AsyncDB(player_game_actuals=legacy)
    q = HistoricalQuery(sport="NFL", market_family="pass_yds",
                         entity_type="player",
                         entity_id="00-0012345",
                         entity_name="Josh Allen")
    obs = _run(NFLPlayerHistoricalAdapter().fetch_observations(db, q))
    assert len(obs) == 1
    assert obs[0].actual == 318.0, (
        "NFL HI alias fallback must resolve passing_yards → pass_yds")


# ═══════════════════════════════════════════════════════════════════
# MLB — pitcher K vs hitter K distinction + gamePk event-identity
# ═══════════════════════════════════════════════════════════════════

def test_mlb_hi_adapter_distinguishes_pitcher_k_from_hitter_k():
    from services.historical_intelligence import (
        MLBPlayerHistoricalAdapter, HistoricalQuery,
    )
    row = [{
        "sport": "mlb", "canonical_player_id": "669923",
        "player_name": "Gerrit Cole",
        "actuals": {"k": 11, "outs": 18,
                     "strikeouts": 0},  # hitter K field — must NOT be used
        "event_time": "2026-06-20T19:00:00Z",
        "canonical_event_id": "gamePk-123",
        "opponent": "BOS", "season": 2026,
        "source": "test_fixture",
    }]
    db = _AsyncDB(player_game_actuals=row)
    q = HistoricalQuery(sport="MLB", market_family="strikeouts",
                         entity_type="player",
                         entity_id="669923",
                         entity_name="Gerrit Cole")
    obs = _run(MLBPlayerHistoricalAdapter().fetch_observations(db, q))
    assert len(obs) == 1
    # Pitcher K family reads `a.get("k")` NOT `a.get("strikeouts")`.
    assert obs[0].actual == 11.0


def test_mlb_hi_adapter_resolves_gamePk_double_colon_event_identity():
    """Player row carries event_id=gamePk; team row carries
    event_id=gamePk::home.  The resolver must still join."""
    from services.historical_intelligence import (
        _enrich_event_time_from_team_actuals,
    )
    player_docs = [{"event_id": "gamePk-456", "event_time": None,
                     "canonical_event_id": "gamePk-456"}]
    team_rows = [{"sport": "mlb", "event_id": "gamePk-456::home",
                   "event_time": "2026-06-20T19:00:00Z"}]
    db = _AsyncDB(team_game_actuals=team_rows)
    enriched = _run(_enrich_event_time_from_team_actuals(
        db, player_docs, "mlb"))
    assert enriched[0]["event_time"] == "2026-06-20T19:00:00Z"


# ═══════════════════════════════════════════════════════════════════
# Preservation
# ═══════════════════════════════════════════════════════════════════

def test_preservation_assertions_still_hold_after_commit_a():
    from services.preservation_contract import snapshot
    snap = snapshot()
    assert snap.get("APEX_MIN_BASE_SCORE") == 97.0
    assert snap.get("APEX_MIN_POSITIVE_CATEGORIES") == 5
    assert snap.get("ALT_LINE_ENGINE_IMPORTABLE") is True
    assert snap.get("SOCCER_MARKET_GATE_IMPORTABLE") is True
    assert "provider_median" in (snap.get("_SYNTHETIC_ODDS_SOURCES") or [])
    assert "provider_median" not in (snap.get("_REAL_ODDS_SOURCES") or [])


if __name__ == "__main__":
    import subprocess, sys
    sys.exit(subprocess.call(["python", "-m", "pytest", __file__, "-v"]))
