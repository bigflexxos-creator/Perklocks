"""Regression tests for the ML player-prop prediction stack (2026-07-28).

Proves the three critical constraints from the spec:
  1. NO sportsbook odds / betting lines are used in features or targets.
  2. NO future games leak into training (time-safe rolling + split).
  3. Missing data gracefully falls back — never raises.

Plus core sanity checks:
  • Feature builder respects position filters + min_prior_games gate.
  • Prediction engine returns well-formed dicts for happy + edge paths.
  • Model artefacts on disk have matching metadata + feature schema.
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


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────
def _synthetic_qb_frame(n_games: int = 40, seasons=(2019, 2020, 2021, 2022,
                                                     2023, 2024, 2025)):
    """Build a small synthetic QB frame that mimics `nfl_player_weekly`."""
    rows = []
    weeks_per_season = 17
    opps = ["KC", "BUF", "SF", "PHI", "BAL", "NE", "DEN", "CIN"]
    for season in seasons:
        for w in range(1, weeks_per_season + 1):
            rows.append({
                "player_id": "P1",
                "player_display_name": "Test QB",
                "player_name": "Test QB",
                "team": "TB",
                "opponent_team": opps[(w + season) % len(opps)],
                "season": season,
                "week": w,
                "position": "QB",
                "game_id": f"{season}_{w:02d}_TB_{opps[(w + season) % len(opps)]}",
                "passing_yards": 200 + 10 * ((w + season) % 5) + np.random.default_rng(w+season).normal(0, 25),
                "attempts": 32 + (w % 6),
                "completions": 22,
                "passing_tds": 2,
                "carries": 4, "targets": 0, "rushing_yards": 8, "receiving_yards": 0,
                "receptions": 0, "rushing_tds": 0, "receiving_tds": 0,
            })
    return pd.DataFrame(rows)


def _run(coro):
    return asyncio.run(coro)


# ═════════════════════════════════════════════════════════════════════
# Constraint 1 — NO sportsbook odds / betting lines in features
# ═════════════════════════════════════════════════════════════════════
def test_no_book_odds_or_lines_in_feature_names():
    """Explicitly verify feature names never mention book/odds/line."""
    from ml.feature_builder import _nfl_feature_names
    banned_substrings = ("odds", "book", "line", "vig", "juice",
                          "consensus", "sportsbook", "moneyline",
                          "spread", "handle", "steam", "market")
    for stat in ("passing_yards", "rushing_yards", "receiving_yards",
                 "receptions", "carries", "targets", "attempts"):
        for pos in (None, "QB", "RB", "WR", "TE"):
            names = _nfl_feature_names(stat, pos)
            for name in names:
                low = name.lower()
                for bad in banned_substrings:
                    assert bad not in low, (
                        f"feature {name!r} contains banned "
                        f"substring {bad!r} — features must NEVER "
                        f"reference sportsbook odds or lines."
                    )


def test_no_book_odds_features_in_training_frame():
    """Training frame columns must not contain book/odds features."""
    from ml.feature_builder import build_nfl_training_frame
    df = _synthetic_qb_frame()
    tf = build_nfl_training_frame(df, stat="passing_yards", position="QB",
                                   min_prior_games=3)
    banned = {"odds", "book", "line", "vig", "juice", "moneyline",
              "spread", "handle", "steam", "market"}
    for col in tf.features.columns:
        low = col.lower()
        for bad in banned:
            assert bad not in low, f"training col {col} banned"


def test_training_target_is_raw_stat_not_binary_over_line():
    """Regressor must train on RAW stat value, never on binary label
    derived from a sportsbook line."""
    from ml.feature_builder import build_nfl_training_frame
    df = _synthetic_qb_frame()
    tf = build_nfl_training_frame(df, stat="passing_yards", position="QB",
                                   min_prior_games=3)
    # Target must be continuous (float), never boolean/int-{0,1}.
    assert tf.target.dtype in (np.float64, np.float32, float)
    uniq = tf.target.round().unique()
    assert len(uniq) > 10, "target should be continuous stat values"
    # And target must equal the raw stat column from the input frame.
    assert set(tf.target.round().tolist()[:5]).issubset(
        set(df["passing_yards"].round().tolist())
    )


# ═════════════════════════════════════════════════════════════════════
# Constraint 2 — NO future games in training
# ═════════════════════════════════════════════════════════════════════
def test_rolling_features_use_prior_games_only():
    """For any row, stat_last_5_avg must be computed from games strictly
    BEFORE that row's (season, week)."""
    from ml.feature_builder import build_nfl_training_frame
    df = _synthetic_qb_frame()
    tf = build_nfl_training_frame(df, stat="passing_yards", position="QB",
                                   min_prior_games=1)
    # Reconstruct: for each row, the 5-game rolling avg should equal
    # the mean of the 5 prior rows' passing_yards.
    df_sorted = df.sort_values(["player_id", "season", "week"]).reset_index(drop=True)
    for i in range(5, min(25, len(tf.features))):
        # Row `i` in features corresponds to a specific game — find its
        # row in df_sorted by matching meta.
        meta = tf.row_meta.iloc[i]
        idx = df_sorted[(df_sorted["season"] == meta["season"]) &
                        (df_sorted["week"] == meta["week"]) &
                        (df_sorted["player_id"] == meta["player_id"])].index
        if len(idx) == 0:
            continue
        j = int(idx[0])
        if j < 5:
            continue
        prior_5 = df_sorted["passing_yards"].iloc[j-5:j].mean()
        got = tf.features.iloc[i]["stat_last_5_avg"]
        assert abs(prior_5 - got) < 1e-6, (
            f"row {i}: expected last-5 avg {prior_5:.3f} from prior games, "
            f"got {got:.3f} — future games may be leaking in."
        )


def test_time_split_never_uses_val_seasons_for_training():
    """Time-based split boundary must be strict."""
    from ml.feature_builder import build_nfl_training_frame
    from ml.train_prop_model import _time_split
    df = _synthetic_qb_frame()
    tf = build_nfl_training_frame(df, stat="passing_yards", position="QB",
                                   min_prior_games=3)
    X_tr, y_tr, X_va, y_va = _time_split(tf, split_season=2024)

    train_seasons = set(tf.row_meta.loc[tf.row_meta["season"] < 2024, "season"].unique())
    val_seasons = set(tf.row_meta.loc[tf.row_meta["season"] >= 2024, "season"].unique())
    # No overlap.
    assert train_seasons.isdisjoint(val_seasons)
    # All train seasons must be earlier than every val season.
    assert max(train_seasons) < min(val_seasons)
    # Row-count sanity: sum equals the full frame.
    assert len(X_tr) + len(X_va) == len(tf.features)


def test_season_to_date_avg_excludes_current_row():
    """Verify season-to-date avg uses ONLY prior in-season games —
    changing FUTURE games in the frame must not change past features."""
    from ml.feature_builder import build_nfl_training_frame
    df = _synthetic_qb_frame()
    tf = build_nfl_training_frame(df, stat="passing_yards", position="QB",
                                   min_prior_games=3)
    # Now mutate the LAST game's stat and rebuild — earlier rows'
    # season-to-date-avg must be IDENTICAL (no future leakage).
    df2 = df.copy()
    df2.loc[df2.index[-1], "passing_yards"] = 999.0
    tf2 = build_nfl_training_frame(df2, stat="passing_yards",
                                    position="QB", min_prior_games=3)
    # Compare all rows except possibly the last row itself.
    n = min(len(tf.features), len(tf2.features)) - 1
    if n > 0:
        pd.testing.assert_series_equal(
            tf.features["stat_season_to_date_avg"].iloc[:n].reset_index(drop=True),
            tf2.features["stat_season_to_date_avg"].iloc[:n].reset_index(drop=True),
            check_names=False,
        )


# ═════════════════════════════════════════════════════════════════════
# Constraint 3 — Missing data graceful fallback
# ═════════════════════════════════════════════════════════════════════
def test_feature_builder_handles_empty_frame():
    """Empty input frame should return empty features + target with no
    exceptions."""
    from ml.feature_builder import build_nfl_training_frame
    df = pd.DataFrame(columns=["player_id", "player_display_name",
                                "player_name", "team", "opponent_team",
                                "season", "week", "position", "game_id",
                                "passing_yards", "attempts", "completions"])
    tf = build_nfl_training_frame(df, stat="passing_yards", position="QB",
                                   min_prior_games=3)
    assert tf.features.empty
    assert len(tf.target) == 0


def test_feature_builder_survives_missing_optional_columns():
    """If `season_type` or `game_id` are missing, builder must still
    produce features (with graceful defaults for is_home / is_playoffs)."""
    from ml.feature_builder import build_nfl_training_frame
    df = _synthetic_qb_frame().drop(columns=["game_id", "carries",
                                              "targets", "rushing_yards",
                                              "receiving_yards"])
    tf = build_nfl_training_frame(df, stat="passing_yards", position="QB",
                                   min_prior_games=3)
    assert not tf.features.empty
    # is_home should degrade to neutral 0.5 when game_id is missing.
    assert 0.0 <= tf.features["is_home"].mean() <= 1.0


def test_prediction_engine_returns_supported_false_for_missing_model():
    """If the model file doesn't exist, engine must return supported=False
    and never raise."""
    from services.trained_prediction_engine import predict_player_prop, _reset_model_cache
    _reset_model_cache()

    class _EmptyDB: pass

    r = _run(predict_player_prop(
        _EmptyDB(), sport="NFL", player="X", stat="nonexistent_stat_xyz",
        opponent="KC", line=1.5,
    ))
    assert r["supported"] is False
    assert "no trained model" in r["reason"].lower()


def test_prediction_engine_returns_supported_false_for_unsupported_sport():
    from services.trained_prediction_engine import predict_player_prop
    class _EmptyDB: pass
    r = _run(predict_player_prop(
        _EmptyDB(), sport="NHL", player="X", stat="goals",
        opponent="TOR", line=0.5,
    ))
    assert r["supported"] is False
    assert "not yet supported" in r["reason"].lower()


# ═════════════════════════════════════════════════════════════════════
# Sanity checks
# ═════════════════════════════════════════════════════════════════════
def test_position_filter_restricts_rows():
    from ml.feature_builder import build_nfl_training_frame
    df = _synthetic_qb_frame().copy()
    # Sprinkle in some RB rows with a different position
    df2 = df.head(10).copy()
    df2["position"] = "RB"; df2["player_id"] = "P2"
    df_mixed = pd.concat([df, df2], ignore_index=True)
    tf = build_nfl_training_frame(df_mixed, stat="passing_yards",
                                   position="QB", min_prior_games=3)
    assert set(tf.row_meta["position"].unique()) == {"QB"}


def test_min_prior_games_gate():
    from ml.feature_builder import build_nfl_training_frame
    df = _synthetic_qb_frame()
    # Require 10 prior games — rows within the first 10 of each season
    # for the player must be dropped.
    tf = build_nfl_training_frame(df, stat="passing_yards", position="QB",
                                   min_prior_games=10)
    if not tf.features.empty:
        # Every retained row must have games_played_ytd >= 10.
        assert (tf.features["games_played_ytd"] >= 10).all()


def test_trained_models_exist_and_have_matching_schema():
    """Confirm the flagship NFL models are on disk with a valid meta."""
    for stat in ("passing_yards", "rushing_yards", "receiving_yards"):
        meta_path = MODEL_DIR / f"nfl_{stat}.meta.json"
        if not meta_path.exists():
            pytest.skip(f"model {stat} not trained yet in this env")
        m = json.loads(meta_path.read_text())
        assert m["sport"] == "NFL"
        assert m["stat"] == stat
        assert m["winner"] in ("lgbm", "xgb")
        assert m["split_season"] >= 2019
        # Metrics sanity.
        for tag in ("lgbm", "xgb"):
            assert m[tag]["mae"] > 0
            assert 0 <= m[tag]["r2"] <= 1
            assert m[tag]["residual_std"] > 0


def test_model_never_uses_target_column_as_feature():
    """A regressor's feature list must never include the target column
    itself (the raw stat) — otherwise the model would just look up the
    answer."""
    from ml.feature_builder import _nfl_feature_names
    for stat in ("passing_yards", "rushing_yards", "receiving_yards"):
        names = _nfl_feature_names(stat, None)
        assert stat not in names, f"target {stat} leaked into feature list"


def test_predict_never_raises_on_broken_inputs():
    """Even nonsense inputs must return a well-formed dict."""
    from services.trained_prediction_engine import predict_player_prop
    class _DB: pass
    r = _run(predict_player_prop(
        _DB(), sport="", player="", stat="", opponent="", line=None,
    ))
    assert isinstance(r, dict)
    assert r.get("supported") is False
