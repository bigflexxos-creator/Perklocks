"""Regression tests for `services/similar_matchup_engine`.

Focus: math correctness (z-score normalisation, similarity monotonic
in distance), sport dispatch, unsupported-sport / unknown-team error
paths, and end-to-end shape safety on a stubbed DB.
"""
from __future__ import annotations

import asyncio
import math
import pytest


# ─────────────────────────────────────────────────────────────────────
# Async DB stub
# ─────────────────────────────────────────────────────────────────────
class _Cursor:
    def __init__(self, rows): self._rows = list(rows)
    def sort(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def __aiter__(self):
        self._it = iter(self._rows); return self
    async def __anext__(self):
        try: return next(self._it)
        except StopIteration: raise StopAsyncIteration


class _Coll:
    def __init__(self, rows=None): self.rows = rows or []
    def find(self, *a, **k): return _Cursor(self.rows)
    def aggregate(self, *a, **k): return _Cursor(getattr(self, "agg", self.rows))


class _StubDB:
    def __init__(self):
        self.nfl_player_weekly = _Coll()
        self.mlb_team_k_splits = _Coll()
        self.player_game_logs  = _Coll()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ─────────────────────────────────────────────────────────────────────
# Math helpers
# ─────────────────────────────────────────────────────────────────────
def test_zscore_normalises_columns():
    from services.similar_matchup_engine import _zscore_normalize
    z = _zscore_normalize({
        "A": [0, 100],
        "B": [10, 200],
        "C": [20, 300],
    })
    # Column means should now be 0 in z-space
    col0 = [z["A"][0], z["B"][0], z["C"][0]]
    col1 = [z["A"][1], z["B"][1], z["C"][1]]
    assert abs(sum(col0)) < 1e-9
    assert abs(sum(col1)) < 1e-9


def test_zscore_handles_zero_variance_col():
    from services.similar_matchup_engine import _zscore_normalize
    z = _zscore_normalize({"A": [5, 100], "B": [5, 200]})
    # First col has 0 variance → both entries must be 0.
    assert z["A"][0] == 0.0
    assert z["B"][0] == 0.0


def test_similarity_monotonic_in_distance():
    from services.similar_matchup_engine import _similarity_from_dist
    assert _similarity_from_dist(0.0) == 1.0
    assert _similarity_from_dist(1.0) > _similarity_from_dist(2.0)
    assert 0.0 < _similarity_from_dist(10.0) < 0.1


def test_nearest_teams_selects_k_closest_and_excludes_self():
    from services.similar_matchup_engine import _find_nearest_teams
    profiles = {
        "A": [1.0, 1.0],
        "B": [1.1, 1.05],   # very close to A
        "C": [2.0, 2.0],    # further
        "D": [9.0, 9.0],    # way out
    }
    nn = _find_nearest_teams(profiles, "A", k=2)
    names = [n for n, _ in nn]
    assert "A" not in names             # excludes self
    assert names[0] == "B"              # closest first
    assert names == ["B", "C"]          # k=2


def test_nearest_teams_empty_when_target_missing():
    from services.similar_matchup_engine import _find_nearest_teams
    assert _find_nearest_teams({"A": [1.0]}, "Z", k=3) == []


def test_confidence_tiers():
    from services.similar_matchup_engine import _confidence
    assert _confidence(0)  == "none"
    assert _confidence(4)  == "none"
    assert _confidence(5)  == "low"
    assert _confidence(9)  == "low"
    assert _confidence(10) == "medium"
    assert _confidence(19) == "medium"
    assert _confidence(20) == "high"
    assert _confidence(50) == "high"


def test_grade_scales_with_sample_and_hit_rate():
    from services.similar_matchup_engine import _grade
    # High hit-rate, small sample → capped
    assert _grade(0.9, 4)  == "F"
    assert _grade(0.9, 20) in {"A+", "A"}
    # Perfect hit rate + big sample → A+
    assert _grade(1.0, 40) == "A+"
    # Low hit rate → D/F regardless
    assert _grade(0.2, 40) in {"D", "F"}


# ─────────────────────────────────────────────────────────────────────
# High-level dispatch
# ─────────────────────────────────────────────────────────────────────
def test_unsupported_sport_returns_empty_result():
    from services.similar_matchup_engine import (
        get_similar_matchup_intelligence,
    )
    db = _StubDB()
    r = _run(get_similar_matchup_intelligence(
        db, sport="NHL", player_name="X", stat="goals",
        opponent_team="TOR",
    ))
    assert r.n_similar_games == 0
    assert r.grade == "F"
    assert any("not supported" in n for n in r.notes)


def test_missing_opponent_returns_empty_result():
    from services.similar_matchup_engine import (
        get_similar_matchup_intelligence,
    )
    db = _StubDB()
    r = _run(get_similar_matchup_intelligence(
        db, sport="NFL", player_name="X", stat="passing_yards",
        opponent_team="",
    ))
    assert r.n_similar_games == 0
    assert any("required" in n for n in r.notes)


def test_result_dict_shape_is_stable():
    from services.similar_matchup_engine import (
        get_similar_matchup_intelligence, _reset_profile_caches,
    )
    _reset_profile_caches()
    db = _StubDB()   # no data — engine should still return a well-formed shape
    r = _run(get_similar_matchup_intelligence(
        db, sport="NFL", player_name="X", stat="passing_yards",
        opponent_team="KC",
    ))
    d = r.to_dict()
    for k in ("sport", "player_name", "target_opponent", "stat",
              "n_similar_games", "avg_stat_output", "hit_rate",
              "similar_opponents", "similarity_dimensions",
              "sample_confidence", "grade", "note",
              "data_sources_used", "notes"):
        assert k in d, f"missing key {k}"
    assert isinstance(d["similar_opponents"], list)
    assert isinstance(d["notes"], list)


def test_never_raises_on_broken_pick():
    """Even nonsense inputs must return a safe SimilarMatchupResult."""
    from services.similar_matchup_engine import (
        get_similar_matchup_intelligence, _reset_profile_caches,
    )
    _reset_profile_caches()
    db = _StubDB()
    r = _run(get_similar_matchup_intelligence(
        db, sport="", player_name="", stat="", opponent_team="",
    ))
    assert r.n_similar_games == 0
    assert r.grade == "F"
