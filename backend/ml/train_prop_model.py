"""Trainer for player-prop prediction models (2026-07-28).

Contract
────────
  • Regressor (LightGBM + XGBoost) predicting the RAW stat value.
  • Time-based split ONLY. Never shuffle.
      train: seasons ≤ split_year - 1
      val:   seasons ≥ split_year
  • No sportsbook odds. No betting line. No consensus.
  • Metrics: MAE, RMSE, AUC & Brier at THREE synthetic thresholds
    (25/50/75 percentiles of the train target), calibration table.
  • Winner = model with lower val MAE. Both are saved for audit.

CLI
───
    python -m ml.train_prop_model \\
        --sport NFL --stat passing_yards --position QB \\
        --split-season 2024 --seasons-min 2019

Outputs
───────
  /app/backend/models/{sport}_{stat}_{model}.pkl        (booster)
  /app/backend/models/{sport}_{stat}.meta.json          (metrics + schema)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import pickle
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

from ml.feature_builder import build_nfl_training_frame, TrainingFrame

load_dotenv()
logger = logging.getLogger("lockscore.ml.train_prop_model")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")


MODEL_DIR = Path(__file__).resolve().parent.parent / "models"
MODEL_DIR.mkdir(parents=True, exist_ok=True)


# ─────────────────────────────────────────────────────────────────────
# Data loaders
# ─────────────────────────────────────────────────────────────────────
async def _load_nfl_rows(seasons_min: int) -> pd.DataFrame:
    client = AsyncIOMotorClient(os.getenv("MONGO_URL"))
    db = client["lockscore_db"]
    q = {"season": {"$gte": seasons_min}}
    proj = {
        "_id": 0,
        "player_id": 1, "player_display_name": 1, "player_name": 1,
        "team": 1, "opponent_team": 1, "season": 1, "week": 1,
        "position": 1, "game_id": 1, "season_type": 1,
        "passing_yards": 1, "passing_tds": 1, "attempts": 1,
        "completions": 1, "passing_ints": 1,
        "rushing_yards": 1, "rushing_tds": 1, "carries": 1,
        "receiving_yards": 1, "receiving_tds": 1, "receptions": 1,
        "targets": 1,
    }
    logger.info("loading NFL rows season>=%d ...", seasons_min)
    t0 = time.time()
    rows = [r async for r in
             db.nfl_player_weekly.find(q, proj).sort([("season", 1),
                                                       ("week", 1)])]
    dur = time.time() - t0
    logger.info("loaded %d NFL rows in %.1fs", len(rows), dur)
    return pd.DataFrame(rows)


# ─────────────────────────────────────────────────────────────────────
# Model trainers
# ─────────────────────────────────────────────────────────────────────
@dataclass
class ModelMetrics:
    n_train:      int
    n_val:        int
    mae:          float
    rmse:         float
    r2:           float
    auc_by_thr:   dict[str, float]      # key = "p25", "p50", "p75"
    brier_by_thr: dict[str, float]
    calibration:  list[dict]            # decile-based table
    top_features: list[tuple[str, float]]
    residual_std: float                 # for P(exceed line) at inference

    def to_dict(self) -> dict:
        return asdict(self)


def _time_split(tf: TrainingFrame, split_season: int) -> tuple[pd.DataFrame, pd.Series,
                                                                pd.DataFrame, pd.Series]:
    mask_train = tf.row_meta["season"] < split_season
    mask_val   = tf.row_meta["season"] >= split_season
    X_tr = tf.features.loc[mask_train].reset_index(drop=True)
    y_tr = tf.target.loc[mask_train].reset_index(drop=True)
    X_va = tf.features.loc[mask_val].reset_index(drop=True)
    y_va = tf.target.loc[mask_val].reset_index(drop=True)
    return X_tr, y_tr, X_va, y_va


def _evaluate(preds: np.ndarray, y_val: pd.Series,
              y_train: pd.Series) -> ModelMetrics:
    from sklearn.metrics import (
        mean_absolute_error, mean_squared_error, r2_score,
        roc_auc_score, brier_score_loss,
    )

    mae = float(mean_absolute_error(y_val, preds))
    rmse = float(np.sqrt(mean_squared_error(y_val, preds)))
    r2 = float(r2_score(y_val, preds))
    residuals = y_val.values - preds
    resid_std = float(np.std(residuals, ddof=1)) if len(residuals) > 1 else 0.0

    # Synthetic thresholds — 25/50/75 percentiles of training target.
    auc_by, brier_by = {}, {}
    for pctile, key in ((25, "p25"), (50, "p50"), (75, "p75")):
        thr = float(np.percentile(y_train, pctile))
        y_bin = (y_val > thr).astype(int).values
        if y_bin.sum() in (0, len(y_bin)):
            continue
        # P(exceed thr) = 1 - Φ((thr - pred) / resid_std)
        from scipy.stats import norm    # scipy comes with sklearn
        proba = 1.0 - norm.cdf((thr - preds) / max(resid_std, 1e-6))
        try:
            auc_by[key] = float(roc_auc_score(y_bin, proba))
            brier_by[key] = float(brier_score_loss(y_bin, proba))
        except ValueError:
            continue

    # Calibration table — 10 buckets on p50 probabilities.
    calibration: list[dict] = []
    from scipy.stats import norm
    thr50 = float(np.percentile(y_train, 50))
    y_bin = (y_val > thr50).astype(int).values
    proba50 = 1.0 - norm.cdf((thr50 - preds) / max(resid_std, 1e-6))
    if 0 < y_bin.sum() < len(y_bin):
        buckets = np.quantile(proba50, np.linspace(0, 1, 11))
        for i in range(10):
            lo, hi = buckets[i], buckets[i + 1]
            in_bucket = (proba50 >= lo) & (proba50 <= hi if i == 9
                                             else proba50 < hi)
            n = int(in_bucket.sum())
            if n == 0:
                continue
            calibration.append({
                "bucket": f"{lo:.2f}-{hi:.2f}",
                "n": n,
                "expected_pct": round(float(proba50[in_bucket].mean() * 100), 2),
                "observed_pct": round(float(y_bin[in_bucket].mean() * 100), 2),
                "delta": round(float((y_bin[in_bucket].mean()
                                        - proba50[in_bucket].mean()) * 100), 2),
            })

    return ModelMetrics(
        n_train=len(y_train), n_val=len(y_val),
        mae=round(mae, 3), rmse=round(rmse, 3), r2=round(r2, 4),
        auc_by_thr={k: round(v, 4) for k, v in auc_by.items()},
        brier_by_thr={k: round(v, 4) for k, v in brier_by.items()},
        calibration=calibration,
        top_features=[],   # filled below by caller
        residual_std=round(resid_std, 3),
    )


def _train_lightgbm(X_tr, y_tr, X_va, y_va, feature_names) -> tuple:
    import lightgbm as lgb
    logger.info("training LightGBM ...")
    train_set = lgb.Dataset(X_tr, label=y_tr, feature_name=feature_names)
    val_set   = lgb.Dataset(X_va, label=y_va, feature_name=feature_names,
                             reference=train_set)
    params = {
        "objective": "regression",
        "metric":    "l1",
        "learning_rate": 0.05,
        "num_leaves":    31,
        "min_data_in_leaf": 20,
        "feature_fraction": 0.85,
        "bagging_fraction": 0.85,
        "bagging_freq":     5,
        "verbose": -1,
        "seed": 42,
    }
    t0 = time.time()
    booster = lgb.train(
        params, train_set, num_boost_round=1200,
        valid_sets=[val_set],
        callbacks=[lgb.early_stopping(50, verbose=False),
                   lgb.log_evaluation(0)],
    )
    logger.info("LightGBM done in %.1fs (best_iter=%d)",
                time.time() - t0, booster.best_iteration)
    preds = booster.predict(X_va, num_iteration=booster.best_iteration)
    importance = booster.feature_importance(importance_type="gain")
    top_feats = sorted(zip(feature_names, importance),
                        key=lambda t: t[1], reverse=True)
    return booster, preds, [(n, float(v)) for n, v in top_feats[:10]]


def _train_xgboost(X_tr, y_tr, X_va, y_va, feature_names) -> tuple:
    import xgboost as xgb
    logger.info("training XGBoost ...")
    dtr = xgb.DMatrix(X_tr, label=y_tr, feature_names=feature_names,
                       enable_categorical=False)
    dva = xgb.DMatrix(X_va, label=y_va, feature_names=feature_names,
                       enable_categorical=False)
    params = {
        "objective":     "reg:squarederror",
        "eval_metric":   "mae",
        "learning_rate": 0.05,
        "max_depth":     6,
        "subsample":     0.85,
        "colsample_bytree": 0.85,
        "seed": 42,
        "verbosity": 0,
    }
    t0 = time.time()
    booster = xgb.train(
        params, dtr, num_boost_round=1200,
        evals=[(dva, "val")],
        early_stopping_rounds=50, verbose_eval=False,
    )
    logger.info("XGBoost done in %.1fs (best_iter=%d)",
                time.time() - t0, booster.best_iteration)
    preds = booster.predict(dva, iteration_range=(0, booster.best_iteration + 1))
    imp_dict = booster.get_score(importance_type="gain")
    top_feats = sorted(imp_dict.items(), key=lambda t: t[1], reverse=True)
    return booster, preds, [(n, float(v)) for n, v in top_feats[:10]]


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────
async def train_nfl(stat: str, position: Optional[str],
                    split_season: int, seasons_min: int,
                    limit_rows: Optional[int] = None) -> dict:
    df = await _load_nfl_rows(seasons_min)
    if limit_rows:
        df = df.head(limit_rows)
    tf = build_nfl_training_frame(
        df, stat=stat, position=position, min_prior_games=3,
        seasons_min=seasons_min,
    )
    logger.info("training frame: X=%s, y=%s",
                tf.features.shape, tf.target.shape)
    if tf.features.empty:
        raise SystemExit("training frame empty — check filters")

    X_tr, y_tr, X_va, y_va = _time_split(tf, split_season)
    logger.info("time-split: train=%d, val=%d", len(y_tr), len(y_va))
    if len(y_tr) < 500 or len(y_va) < 100:
        raise SystemExit(
            f"insufficient rows post-split: train={len(y_tr)}, val={len(y_va)}"
        )

    booster_lgb, preds_lgb, top_lgb = _train_lightgbm(
        X_tr, y_tr, X_va, y_va, tf.feature_names,
    )
    metrics_lgb = _evaluate(preds_lgb, y_va, y_tr)
    metrics_lgb.top_features = top_lgb

    booster_xgb, preds_xgb, top_xgb = _train_xgboost(
        X_tr, y_tr, X_va, y_va, tf.feature_names,
    )
    metrics_xgb = _evaluate(preds_xgb, y_va, y_tr)
    metrics_xgb.top_features = top_xgb

    # Winner = lower val MAE.
    winner = "lgbm" if metrics_lgb.mae <= metrics_xgb.mae else "xgb"
    logger.info("winner=%s | LGB MAE=%.3f  XGB MAE=%.3f",
                winner, metrics_lgb.mae, metrics_xgb.mae)

    # Persist both models + metadata.
    tag = f"nfl_{stat}"
    with open(MODEL_DIR / f"{tag}_lgbm.pkl", "wb") as f:
        pickle.dump({
            "booster": booster_lgb,
            "feature_names": tf.feature_names,
            "sport": "NFL", "stat": stat, "position": position,
            "residual_std": metrics_lgb.residual_std,
        }, f)
    with open(MODEL_DIR / f"{tag}_xgb.pkl", "wb") as f:
        pickle.dump({
            "booster": booster_xgb,
            "feature_names": tf.feature_names,
            "sport": "NFL", "stat": stat, "position": position,
            "residual_std": metrics_xgb.residual_std,
        }, f)

    meta = {
        "sport": "NFL",
        "stat": stat,
        "position": position,
        "split_season": split_season,
        "seasons_min": seasons_min,
        "trained_at": time.strftime("%Y-%m-%d %H:%M:%S UTC",
                                     time.gmtime()),
        "winner": winner,
        "feature_names": tf.feature_names,
        "lgbm": metrics_lgb.to_dict(),
        "xgb":  metrics_xgb.to_dict(),
        "row_counts_by_season": tf.row_meta.groupby("season").size().to_dict(),
    }
    with open(MODEL_DIR / f"{tag}.meta.json", "w") as f:
        json.dump(meta, f, indent=2, default=str)
    logger.info("saved models to %s", MODEL_DIR)
    return meta


# ─────────────────────────────────────────────────────────────────────
# Multi-sport data loaders + trainers (2026-07-28 extension)
# ─────────────────────────────────────────────────────────────────────
async def _load_mlb_rows() -> pd.DataFrame:
    client = AsyncIOMotorClient(os.getenv("MONGO_URL"))
    db = client["lockscore_db"]
    logger.info("loading MLB rows ...")
    t0 = time.time()
    rows = [r async for r in
             db.player_game_logs.find({"sport": "mlb"}, {"_id": 0}).sort("date", 1)]
    logger.info("loaded %d MLB rows in %.1fs", len(rows), time.time() - t0)
    return pd.DataFrame(rows)


async def _load_tennis_matches() -> pd.DataFrame:
    client = AsyncIOMotorClient(os.getenv("MONGO_URL"))
    db = client["lockscore_db"]
    logger.info("loading Tennis matches ...")
    t0 = time.time()
    rows = [r async for r in
             db.tennis_matches_history.find({}, {"_id": 0}).sort("date", 1)]
    logger.info("loaded %d Tennis matches in %.1fs", len(rows), time.time() - t0)
    return pd.DataFrame(rows)


async def train_mlb(stat: str, split_date: str = "2025-01-01") -> dict:
    from ml.features.mlb import build_mlb_training_frame
    df = await _load_mlb_rows()
    tf = build_mlb_training_frame(df, stat=stat, min_prior_games=5)
    if tf.features.empty:
        raise SystemExit(f"MLB training frame empty for stat={stat}")
    logger.info("MLB training frame: X=%s, y=%s",
                tf.features.shape, tf.target.shape)
    # Time-based split — prefer `date`; fall back to game_id percentile.
    if "date" in tf.row_meta.columns and \
       tf.row_meta["date"].notna().any():
        mask_tr = (tf.row_meta["date"] < split_date) | tf.row_meta["date"].isna()
        mask_va = tf.row_meta["date"] >= split_date
    else:
        # Split by game_id — take the last 15 % as validation.
        gids = tf.row_meta["game_id"].astype(float)
        threshold = float(gids.quantile(0.85))
        mask_tr = gids <= threshold
        mask_va = gids > threshold
        logger.info("MLB date column empty — splitting on game_id > %s (15%% val)",
                    threshold)
    X_tr = tf.features.loc[mask_tr].reset_index(drop=True)
    y_tr = tf.target.loc[mask_tr].reset_index(drop=True)
    X_va = tf.features.loc[mask_va].reset_index(drop=True)
    y_va = tf.target.loc[mask_va].reset_index(drop=True)
    if len(y_tr) < 500 or len(y_va) < 50:
        raise SystemExit(
            f"MLB split too thin: train={len(y_tr)}, val={len(y_va)}"
        )
    return _train_dual("mlb", stat, tf, X_tr, y_tr, X_va, y_va,
                        {"split_date": split_date, "position": None})


async def _load_soccer_rows() -> pd.DataFrame:
    """Load all soccer_player_game_logs rows sorted by match_date."""
    client = AsyncIOMotorClient(os.getenv("MONGO_URL"))
    db = client["lockscore_db"]
    logger.info("loading Soccer rows ...")
    t0 = time.time()
    rows = [r async for r in
             db.soccer_player_game_logs.find({}, {"_id": 0}).sort("match_date", 1)]
    logger.info("loaded %d Soccer rows in %.1fs", len(rows), time.time() - t0)
    return pd.DataFrame(rows)


async def train_soccer(
    stat: str,
    split_date: str = "2025-01-01",
    min_prior_matches: int = 5,
) -> dict:
    """Train a dual (LightGBM + XGBoost) Soccer prop model for `stat`.

    Uses the per-match rolling features from
    `ml.features.soccer.build_soccer_training_frame`.  Time-based split
    on `match_date`.  Fails loudly (SystemExit) if fewer than 500
    training rows or 50 validation rows are available.
    """
    from ml.features.soccer import build_soccer_training_frame
    df = await _load_soccer_rows()
    tf = build_soccer_training_frame(df, stat=stat,
                                       min_prior_matches=min_prior_matches)
    if tf.features.empty:
        raise SystemExit(f"Soccer training frame empty for stat={stat}")
    logger.info("Soccer training frame: X=%s, y=%s",
                tf.features.shape, tf.target.shape)
    # Time-based split — prefer `match_date`.
    if "match_date" in tf.row_meta.columns and \
       tf.row_meta["match_date"].notna().any():
        dates = pd.to_datetime(tf.row_meta["match_date"], errors="coerce")
        cutoff = pd.to_datetime(split_date)
        mask_tr = (dates < cutoff) | dates.isna()
        mask_va = dates >= cutoff
    else:
        # Fallback: last-15 % percentile split.
        n = len(tf.target)
        cut = int(n * 0.85)
        mask_tr = pd.Series([True] * cut + [False] * (n - cut))
        mask_va = ~mask_tr
        logger.info("Soccer match_date missing — split-idx %s / %s", cut, n)
    X_tr = tf.features.loc[mask_tr].reset_index(drop=True)
    y_tr = tf.target.loc[mask_tr].reset_index(drop=True)
    X_va = tf.features.loc[mask_va].reset_index(drop=True)
    y_va = tf.target.loc[mask_va].reset_index(drop=True)
    if len(y_tr) < 500 or len(y_va) < 50:
        raise SystemExit(
            f"Soccer split too thin: train={len(y_tr)}, val={len(y_va)}"
        )
    return _train_dual("soccer", stat, tf, X_tr, y_tr, X_va, y_va,
                        {"split_date": split_date,
                          "min_prior_matches": min_prior_matches})


async def _load_nba_rows() -> pd.DataFrame:
    """Load all NBA player_game_logs rows sorted by date ascending."""
    client = AsyncIOMotorClient(os.getenv("MONGO_URL"))
    db = client["lockscore_db"]
    logger.info("loading NBA rows ...")
    t0 = time.time()
    rows = [r async for r in
             db.player_game_logs.find({"sport": "nba"}, {"_id": 0}).sort("date", 1)]
    logger.info("loaded %d NBA rows in %.1fs", len(rows), time.time() - t0)
    return pd.DataFrame(rows)


async def train_nba(stat: str, split_date: str = "2025-01-01") -> dict:
    """Train a dual (LightGBM + XGBoost) NBA prop model for `stat`.

    Uses the same training pipeline as MLB / Tennis. Time-based split
    honours the `date` column; falls back to game_id quantile split if
    dates are missing."""
    from ml.features.nba import build_nba_training_frame
    df = await _load_nba_rows()
    tf = build_nba_training_frame(df, stat=stat, min_prior_games=5)
    if tf.features.empty:
        raise SystemExit(f"NBA training frame empty for stat={stat}")
    logger.info("NBA training frame: X=%s, y=%s",
                tf.features.shape, tf.target.shape)
    if "date" in tf.row_meta.columns and \
       tf.row_meta["date"].notna().any():
        mask_tr = (tf.row_meta["date"] < split_date) | tf.row_meta["date"].isna()
        mask_va = tf.row_meta["date"] >= split_date
    else:
        gids = tf.row_meta["game_id"].astype(float)
        threshold = float(gids.quantile(0.85))
        mask_tr = gids <= threshold
        mask_va = gids > threshold
        logger.info("NBA date column empty — splitting on game_id > %s (15%% val)",
                    threshold)
    X_tr = tf.features.loc[mask_tr].reset_index(drop=True)
    y_tr = tf.target.loc[mask_tr].reset_index(drop=True)
    X_va = tf.features.loc[mask_va].reset_index(drop=True)
    y_va = tf.target.loc[mask_va].reset_index(drop=True)
    if len(y_tr) < 500 or len(y_va) < 50:
        raise SystemExit(
            f"NBA split too thin: train={len(y_tr)}, val={len(y_va)}"
        )
    return _train_dual("nba", stat, tf, X_tr, y_tr, X_va, y_va,
                        {"split_date": split_date, "position": None})


async def train_tennis(stat: str, split_date: str = "2024-01-01",
                        surface: Optional[str] = None) -> dict:
    from ml.features.tennis import build_tennis_training_frame
    df = await _load_tennis_matches()
    tf = build_tennis_training_frame(df, stat=stat,
                                       min_prior_matches=10,
                                       surface=surface)
    if tf.features.empty:
        raise SystemExit(f"Tennis training frame empty for stat={stat}")
    logger.info("Tennis training frame: X=%s, y=%s",
                tf.features.shape, tf.target.shape)
    mask_tr = tf.row_meta["date"] < split_date
    mask_va = tf.row_meta["date"] >= split_date
    X_tr = tf.features.loc[mask_tr].reset_index(drop=True)
    y_tr = tf.target.loc[mask_tr].reset_index(drop=True)
    X_va = tf.features.loc[mask_va].reset_index(drop=True)
    y_va = tf.target.loc[mask_va].reset_index(drop=True)
    if len(y_tr) < 500 or len(y_va) < 50:
        raise SystemExit(
            f"Tennis split too thin: train={len(y_tr)}, val={len(y_va)}"
        )
    return _train_dual("tennis", stat, tf, X_tr, y_tr, X_va, y_va,
                        {"split_date": split_date, "surface": surface})


def _train_dual(sport_tag: str, stat: str, tf,
                 X_tr, y_tr, X_va, y_va, extra_meta: dict) -> dict:
    """Shared LightGBM + XGBoost trainer + persister for MLB / Tennis."""
    booster_lgb, preds_lgb, top_lgb = _train_lightgbm(
        X_tr, y_tr, X_va, y_va, tf.feature_names,
    )
    metrics_lgb = _evaluate(preds_lgb, y_va, y_tr)
    metrics_lgb.top_features = top_lgb
    booster_xgb, preds_xgb, top_xgb = _train_xgboost(
        X_tr, y_tr, X_va, y_va, tf.feature_names,
    )
    metrics_xgb = _evaluate(preds_xgb, y_va, y_tr)
    metrics_xgb.top_features = top_xgb
    winner = "lgbm" if metrics_lgb.mae <= metrics_xgb.mae else "xgb"
    logger.info("[%s/%s] winner=%s | LGB MAE=%.3f XGB MAE=%.3f",
                sport_tag.upper(), stat, winner,
                metrics_lgb.mae, metrics_xgb.mae)

    tag = f"{sport_tag}_{stat}"
    with open(MODEL_DIR / f"{tag}_lgbm.pkl", "wb") as f:
        pickle.dump({
            "booster": booster_lgb,
            "feature_names": tf.feature_names,
            "sport": tf.sport, "stat": stat,
            "residual_std": metrics_lgb.residual_std,
        }, f)
    with open(MODEL_DIR / f"{tag}_xgb.pkl", "wb") as f:
        pickle.dump({
            "booster": booster_xgb,
            "feature_names": tf.feature_names,
            "sport": tf.sport, "stat": stat,
            "residual_std": metrics_xgb.residual_std,
        }, f)
    meta = {
        "sport": tf.sport,
        "stat": stat,
        "trained_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        "winner": winner,
        "feature_names": tf.feature_names,
        "lgbm": metrics_lgb.to_dict(),
        "xgb":  metrics_xgb.to_dict(),
        **extra_meta,
    }
    with open(MODEL_DIR / f"{tag}.meta.json", "w") as f:
        json.dump(meta, f, indent=2, default=str)
    logger.info("saved %s models to %s", tag, MODEL_DIR)
    return meta


def main():
    ap = argparse.ArgumentParser(description="Train a player-prop model.")
    ap.add_argument("--sport", default="NFL",
                     choices=["NFL", "MLB", "Tennis", "NBA", "Soccer"])
    ap.add_argument("--stat", required=True)
    ap.add_argument("--position", default=None)
    ap.add_argument("--split-season", type=int, default=2024)
    ap.add_argument("--split-date", default=None,
                     help="For MLB/Tennis — ISO date splitting train vs val")
    ap.add_argument("--surface", default=None,
                     help="Tennis only: filter to one surface")
    ap.add_argument("--seasons-min", type=int, default=2019)
    ap.add_argument("--limit-rows", type=int, default=None)
    args = ap.parse_args()

    if args.sport == "NFL":
        meta = asyncio.run(train_nfl(
            stat=args.stat, position=args.position,
            split_season=args.split_season,
            seasons_min=args.seasons_min,
            limit_rows=args.limit_rows,
        ))
    elif args.sport == "MLB":
        meta = asyncio.run(train_mlb(
            stat=args.stat,
            split_date=args.split_date or "2025-01-01",
        ))
    elif args.sport == "Tennis":
        meta = asyncio.run(train_tennis(
            stat=args.stat,
            split_date=args.split_date or "2024-01-01",
            surface=args.surface,
        ))
    elif args.sport == "NBA":
        meta = asyncio.run(train_nba(
            stat=args.stat,
            split_date=args.split_date or "2025-01-01",
        ))
    elif args.sport == "Soccer":
        meta = asyncio.run(train_soccer(
            stat=args.stat,
            split_date=args.split_date or "2025-01-01",
        ))
    else:
        raise SystemExit(f"sport {args.sport} not yet supported by trainer")

    print(json.dumps({
        "sport":       meta["sport"],
        "stat":        meta["stat"],
        "winner":      meta["winner"],
        "lgbm_mae":    meta["lgbm"]["mae"],
        "xgb_mae":     meta["xgb"]["mae"],
        "lgbm_auc_p50":meta["lgbm"]["auc_by_thr"].get("p50"),
        "xgb_auc_p50": meta["xgb"]["auc_by_thr"].get("p50"),
    }, indent=2))


if __name__ == "__main__":
    main()
