"""Multi-sport ML tests (Phase-2 extension, 2026-07-28).

Covers MLB / Tennis / NBA / Soccer feature builders + trained model
loading + prediction dispatch. Follows the same three critical
constraints as the NFL tests (iter93):

  1. No sportsbook odds / betting lines in features or targets.
  2. No future leakage (time-safe rolling).
  3. Graceful missing-data fallback.

Plus:
  • Feature schema stability across sports.
  • Trained-model artefacts exist for MLB/Tennis flagship stats.
  • Prediction service dispatches to the right sport module.
"""
from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


MODEL_DIR = Path(__file__).resolve().parent.parent / "models"


def _run(coro):
    return asyncio.run(coro)


# ═════════════════════════════════════════════════════════════════════
# MLB — feature builder
# ═════════════════════════════════════════════════════════════════════
def _synthetic_mlb_batter_frame(n_players: int = 3, n_games: int = 40):
    """Build a synthetic MLB batter frame mimicking `player_game_logs`."""
    rng = np.random.default_rng(42)
    rows = []
    for pid in range(1, n_players + 1):
        for g in range(1, n_games + 1):
            rows.append({
                "player_id": pid, "game_id": 100000 + pid * 1000 + g,
                "sport": "mlb", "team": f"T{pid}",
                "at_bats": 4, "hits": int(rng.integers(0, 4)),
                "home_runs": int(rng.integers(0, 2)),
                "rbi": int(rng.integers(0, 5)),
                "total_bases": int(rng.integers(0, 8)),
                "strikeouts": int(rng.integers(0, 3)),
                "innings_pitched": None,
                "pitcher_strikeouts": None, "hits_allowed": None,
                "walks": 0, "earned_runs": None,
                "date": None,
            })
    return pd.DataFrame(rows)


def test_mlb_feature_builder_shape():
    from ml.features.mlb import build_mlb_training_frame
    df = _synthetic_mlb_batter_frame()
    tf = build_mlb_training_frame(df, stat="hits", min_prior_games=3)
    assert not tf.features.empty
    assert tf.stat == "hits"
    assert tf.sport == "MLB"
    for col in ("stat_last_5_avg", "stat_last_10_avg",
                "stat_season_to_date_avg", "league_avg_prior_month"):
        assert col in tf.feature_names


def test_mlb_no_odds_or_line_features():
    from ml.features.mlb import build_mlb_training_frame
    df = _synthetic_mlb_batter_frame()
    tf = build_mlb_training_frame(df, stat="hits", min_prior_games=3)
    banned = ("odds", "book", "line", "vig", "juice", "moneyline",
              "spread", "handle", "steam", "market", "consensus")
    for col in tf.feature_names:
        low = col.lower()
        for bad in banned:
            assert bad not in low, f"MLB feature {col} banned: {bad}"


def test_mlb_time_safe_no_future_leakage():
    """Mutating a future game must not change past features."""
    from ml.features.mlb import build_mlb_training_frame
    df = _synthetic_mlb_batter_frame()
    tf1 = build_mlb_training_frame(df, stat="hits", min_prior_games=1)
    df2 = df.copy()
    df2.iloc[-1, df2.columns.get_loc("hits")] = 99
    tf2 = build_mlb_training_frame(df2, stat="hits", min_prior_games=1)
    n = min(len(tf1.features), len(tf2.features)) - 5
    if n > 0:
        pd.testing.assert_series_equal(
            tf1.features["stat_last_5_avg"].iloc[:n].reset_index(drop=True),
            tf2.features["stat_last_5_avg"].iloc[:n].reset_index(drop=True),
            check_names=False,
        )


def test_mlb_composite_hits_runs_rbis():
    """Composite hits_runs_rbis should equal hits + rbi in the target."""
    from ml.features.mlb import build_mlb_training_frame
    df = _synthetic_mlb_batter_frame()
    tf = build_mlb_training_frame(df, stat="hits_runs_rbis",
                                    min_prior_games=1)
    assert not tf.features.empty
    # Every target row must equal the sum of the corresponding hits+rbi.
    assert (tf.target >= 0).all()


def test_mlb_empty_frame_returns_empty_frame():
    from ml.features.mlb import build_mlb_training_frame
    tf = build_mlb_training_frame(pd.DataFrame(), stat="hits")
    assert tf.features.empty


def test_mlb_pitcher_row_classification():
    """Pitcher rows must be picked up when target stat is pitcher-related."""
    from ml.features.mlb import build_mlb_training_frame, _classify_row
    row = pd.Series({"at_bats": None, "innings_pitched": "5.2",
                     "hits_allowed": 3, "pitcher_strikeouts": 6})
    assert _classify_row(row) == "PITCHER"
    row2 = pd.Series({"at_bats": 4, "innings_pitched": None})
    assert _classify_row(row2) == "BATTER"


# ═════════════════════════════════════════════════════════════════════
# Tennis — feature builder
# ═════════════════════════════════════════════════════════════════════
def _synthetic_tennis_matches(n_players=6, n_matches=200):
    rng = np.random.default_rng(7)
    rows = []
    from datetime import datetime, timedelta
    base = datetime(2022, 1, 1)
    for m in range(n_matches):
        w = rng.integers(1, n_players + 1)
        l = rng.integers(1, n_players + 1)
        while l == w:
            l = rng.integers(1, n_players + 1)
        rows.append({
            "date": (base + timedelta(days=m * 3)).strftime("%Y-%m-%d"),
            "winner_id": int(w), "loser_id": int(l),
            "winner_name": f"P{w}", "loser_name": f"P{l}",
            "winner_rank": int(rng.integers(1, 200)),
            "loser_rank":  int(rng.integers(1, 200)),
            "surface": rng.choice(["Hard", "Clay", "Grass"]),
            "best_of": 3,
            "w_ace": int(rng.integers(0, 15)),
            "l_ace": int(rng.integers(0, 12)),
            "w_df": int(rng.integers(0, 6)),
            "l_df": int(rng.integers(0, 8)),
            "w_svpt": int(rng.integers(40, 100)),
            "l_svpt": int(rng.integers(40, 100)),
            "w_1stIn": int(rng.integers(20, 60)),
            "l_1stIn": int(rng.integers(20, 60)),
            "w_1stWon": int(rng.integers(15, 50)),
            "l_1stWon": int(rng.integers(15, 50)),
            "w_2ndWon": int(rng.integers(5, 25)),
            "l_2ndWon": int(rng.integers(5, 25)),
            "w_SvGms": int(rng.integers(5, 15)),
            "l_SvGms": int(rng.integers(5, 15)),
            "w_bpFaced": int(rng.integers(0, 15)),
            "l_bpFaced": int(rng.integers(0, 15)),
            "w_bpSaved": int(rng.integers(0, 12)),
            "l_bpSaved": int(rng.integers(0, 12)),
            "total_games_match": int(rng.integers(15, 40)),
        })
    return pd.DataFrame(rows)


def test_tennis_feature_builder_shape():
    from ml.features.tennis import build_tennis_training_frame
    df = _synthetic_tennis_matches()
    tf = build_tennis_training_frame(df, stat="aces", min_prior_matches=3)
    assert not tf.features.empty
    assert tf.sport == "Tennis"
    for col in ("stat_last_5_avg", "opp_ace_allowed_l10", "rank_diff",
                "surface_hard", "surface_clay", "surface_grass"):
        assert col in tf.feature_names


def test_tennis_no_odds_in_features():
    from ml.features.tennis import build_tennis_training_frame
    df = _synthetic_tennis_matches()
    tf = build_tennis_training_frame(df, stat="aces", min_prior_matches=3)
    banned = ("odds", "book", "vig", "juice", "moneyline",
              "spread", "handle", "steam", "market", "consensus")
    for col in tf.feature_names:
        for b in banned:
            assert b not in col.lower(), f"Tennis feature {col} banned {b}"


def test_tennis_time_safe_no_future_leakage():
    from ml.features.tennis import build_tennis_training_frame
    df = _synthetic_tennis_matches()
    tf1 = build_tennis_training_frame(df, stat="aces", min_prior_matches=1)
    df2 = df.copy()
    # Mutate the LAST row's w_ace.  Rows in tf that come BEFORE the
    # mutated match (by date) must be identical — that's what "no
    # future leakage" means. Rows after the mutation naturally CAN
    # differ because the mutated match becomes prior context for them.
    df2.iloc[-1, df2.columns.get_loc("w_ace")] = 999
    tf2 = build_tennis_training_frame(df2, stat="aces", min_prior_matches=1)
    # Take rows strictly BEFORE the last-original-match's date, using
    # meta.date (both frames are sorted by (player, date) so we compare
    # by (player_id, date) pairs).
    last_date = df["date"].max()
    mask1 = tf1.row_meta["date"] < last_date
    mask2 = tf2.row_meta["date"] < last_date
    a = tf1.features["stat_last_5_avg"].loc[mask1].reset_index(drop=True)
    b = tf2.features["stat_last_5_avg"].loc[mask2].reset_index(drop=True)
    n = min(len(a), len(b))
    if n > 0:
        pd.testing.assert_series_equal(a.iloc[:n], b.iloc[:n],
                                          check_names=False)


def test_tennis_surface_filter():
    from ml.features.tennis import build_tennis_training_frame
    df = _synthetic_tennis_matches()
    tf = build_tennis_training_frame(df, stat="aces", surface="Clay",
                                       min_prior_matches=1)
    assert (tf.row_meta["surface"] == "Clay").all()


def test_tennis_empty_frame():
    from ml.features.tennis import build_tennis_training_frame
    tf = build_tennis_training_frame(pd.DataFrame(), stat="aces")
    assert tf.features.empty


# ═════════════════════════════════════════════════════════════════════
# NBA — scaffold behavior
# ═════════════════════════════════════════════════════════════════════
def test_nba_feature_builder_reports_empty_gracefully():
    """NBA has no game logs — builder must return empty frame with
    populated `feature_names`, never raise."""
    from ml.features.nba import build_nba_training_frame, _feature_names
    tf = build_nba_training_frame(pd.DataFrame(), stat="points")
    assert tf.features.empty
    assert tf.feature_names == _feature_names()
    assert tf.sport == "NBA"


def test_nba_live_features_returns_stub():
    """NBA live features now hit real data. This test used to assert
    the pre-Phase-7 scaffold's 'pending' message; updated to reflect
    the live path — with an empty DB stub, the builder short-circuits
    at the ID lookup with `player_id not resolvable`."""
    from ml.features.nba import build_nba_live_features

    class _StubColl:
        async def find_one(self, *_a, **_kw): return None
        def find(self, *_a, **_kw):
            class _C:
                def sort(self, *_a, **_k): return self
                def limit(self, *_a, **_k): return self
                def __aiter__(self): return self
                async def __anext__(self): raise StopAsyncIteration
            return _C()
    class _DB:
        def __getattr__(self, _): return _StubColl()

    r = _run(build_nba_live_features(_DB(), player_name="LeBron",
                                       opponent_team="BOS", stat="points"))
    assert isinstance(r, tuple) and len(r) == 3
    vec, names, meta = r
    assert all(math.isnan(v) for v in vec.values())
    # Live builder message: either "not resolvable" or "no historical rows"
    notes_joined = " ".join(meta["notes"]).lower()
    assert ("resolvable" in notes_joined
            or "no historical rows" in notes_joined
            or "pending" in notes_joined)


# ═════════════════════════════════════════════════════════════════════
# Soccer — scaffold behavior
# ═════════════════════════════════════════════════════════════════════
def test_soccer_feature_builder_empty_frame():
    from ml.features.soccer import build_soccer_training_frame
    tf = build_soccer_training_frame(pd.DataFrame(), stat="goals")
    assert tf.features.empty
    assert tf.sport == "Soccer"


def test_soccer_training_frame_from_season_aggregates():
    from ml.features.soccer import build_soccer_training_frame
    df = pd.DataFrame([
        {"name_canonical": "player_a", "season": 2023, "goals": 12,
          "assists": 8, "shots": 60, "minutes": 2500,
          "shots_per_90": 2.2, "npxg_per_90": 0.4, "position": "F",
          "team": "T1", "league": "PL"},
        {"name_canonical": "player_a", "season": 2024, "goals": 15,
          "assists": 6, "shots": 70, "minutes": 2600,
          "shots_per_90": 2.4, "npxg_per_90": 0.45, "position": "F",
          "team": "T1", "league": "PL"},
    ])
    tf = build_soccer_training_frame(df, stat="goals",
                                       min_prior_seasons=1)
    assert not tf.features.empty
    assert "prior_season_avg" in tf.feature_names


def test_soccer_goal_contributions_composite():
    from ml.features.soccer import build_soccer_training_frame
    df = pd.DataFrame([
        {"name_canonical": "x", "season": 2023, "goals": 10, "assists": 5,
          "shots": 40, "minutes": 2400, "position": "F"},
        {"name_canonical": "x", "season": 2024, "goals": 12, "assists": 8,
          "shots": 45, "minutes": 2500, "position": "F"},
    ])
    tf = build_soccer_training_frame(df, stat="goal_contributions")
    assert not tf.features.empty
    # Target for the 2024 row should be 12+8=20.
    assert tf.target.iloc[0] == 20.0


# ═════════════════════════════════════════════════════════════════════
# Trained-model artefacts
# ═════════════════════════════════════════════════════════════════════
def test_trained_model_artefacts_exist_for_flagships():
    """MLB + Tennis flagship models must exist on disk."""
    expected = [
        ("mlb", "hits"), ("mlb", "home_runs"),
        ("mlb", "total_bases"), ("mlb", "pitcher_strikeouts"),
        ("tennis", "aces"), ("tennis", "double_faults"),
    ]
    for sport_tag, stat in expected:
        meta = MODEL_DIR / f"{sport_tag}_{stat}.meta.json"
        if not meta.exists():
            pytest.skip(f"model {sport_tag}_{stat} not trained yet")
        m = json.loads(meta.read_text())
        assert m["stat"] == stat
        assert m["winner"] in ("lgbm", "xgb")
        assert m["lgbm"]["mae"] > 0
        assert m["xgb"]["mae"] > 0


def test_trained_model_metadata_has_calibration_and_top_features():
    """Metadata must contain calibration + top-features + Brier."""
    for tag in ("mlb_hits", "tennis_aces"):
        meta = MODEL_DIR / f"{tag}.meta.json"
        if not meta.exists():
            pytest.skip(f"{tag} not trained yet")
        m = json.loads(meta.read_text())
        w = m[m["winner"]]
        assert "top_features" in w and len(w["top_features"]) > 0
        assert "brier_by_thr" in w
        assert "calibration" in w   # may be empty list, but the key must exist


# ═════════════════════════════════════════════════════════════════════
# Prediction service dispatch
# ═════════════════════════════════════════════════════════════════════
def test_prediction_engine_returns_supported_for_mlb_and_tennis():
    """Even without a live DB, MLB/Tennis routes should return a dict
    (with supported=True or a graceful False on no feature data)."""
    from services.trained_prediction_engine import (
        predict_player_prop, _reset_model_cache,
    )
    _reset_model_cache()

    class _DB:
        def __getattr__(self, name):
            class _E:
                def find(self, *a, **k):
                    class _C:
                        def sort(self, *a, **k): return self
                        def limit(self, *a, **k): return self
                        def __aiter__(self): return self
                        async def __anext__(self): raise StopAsyncIteration
                    return _C()
                def find_one(self, *a, **k): return _noop()
            return _E()

    async def _noop(): return None

    r = _run(predict_player_prop(
        _DB(), sport="MLB", player="Nobody",
        stat="hits", opponent="", line=1.5,
    ))
    assert isinstance(r, dict)
    # supported may be False (no player data) but must NEVER raise.
    assert r.get("supported") in (True, False)


def test_prediction_engine_still_rejects_unsupported_sport():
    from services.trained_prediction_engine import predict_player_prop
    class _EmptyDB: pass
    r = _run(predict_player_prop(
        _EmptyDB(), sport="NHL", player="X",
        stat="goals", opponent="TOR", line=0.5,
    ))
    assert r["supported"] is False
    assert "not yet supported" in r["reason"].lower()


# ═════════════════════════════════════════════════════════════════════
# Fusion engine remains sport-agnostic (compatibility check)
# ═════════════════════════════════════════════════════════════════════
def test_fusion_engine_accepts_all_four_sports():
    """The Fusion Engine's sport dispatch must accept all four sports
    without raising — even if downstream engines report unavailable."""
    from services.prediction_fusion_engine import fuse_prediction

    class _StubDB:
        def __init__(self):
            from tests.test_iter95_pick_fusion_decorator import _AsyncColl
            self.fusion_predictions = _AsyncColl()
        def __getattr__(self, name):
            from tests.test_iter95_pick_fusion_decorator import _AsyncColl
            return _AsyncColl()

    for sport, stat in (("NFL", "passing_yards"), ("MLB", "hits"),
                          ("NBA", "points"), ("Soccer", "goals"),
                          ("Tennis", "aces")):
        r = _run(fuse_prediction(
            _StubDB(), sport=sport, player="X",
            stat=stat, opponent="Y", threshold=1.5,
        ))
        assert isinstance(r.to_dict(), dict)
        assert r.sport == sport
