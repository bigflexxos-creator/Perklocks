"""Trained-prediction engine — loads pickled LightGBM/XGBoost models
and serves live P(exceed line) inferences for a (sport, player, stat,
opponent, line) query.

Contract
────────
    result = await predict_player_prop(
        db,
        sport="NFL",
        player="Joe Burrow",
        stat="passing_yards",
        opponent="KC",
        line=249.5,
    )

    → {
        "supported":              True,
        "sport":                  "NFL",
        "player":                 "Joe Burrow",
        "stat":                   "passing_yards",
        "opponent":               "KC",
        "line":                   249.5,
        "expected_value":         272.4,
        "residual_std":           82.5,
        "prediction_probability": 0.61,   # P(actual > line)
        "confidence":             "medium", # low|medium|high (based on features fill + sample)
        "model":                  "xgb",   # winning model tag
        "top_factors": [
            {"feature": "stat_last_10_avg", "value": 289.1, "weight": 0.34},
            ...
        ],
        "similar_games_used":     8,       # rows used in feature build
        "notes":                  [...],
      }

**Zero writes. No sportsbook odds. Never wired into pick generation.**
The model consumes only pre-game features from the DB. The market line
is used ONLY at inference to convert (predicted µ, residual σ) →
P(actual > line) via a normal CDF.

Missing model → returns `{"supported": False, "reason": "model not loaded"}`.
"""
from __future__ import annotations

import json
import logging
import math
import pickle
import threading
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from ml.feature_builder import build_nfl_live_features, _nfl_feature_names

logger = logging.getLogger("lockscore.services.trained_prediction_engine")

MODEL_DIR = Path(__file__).resolve().parent.parent / "models"

# ─────────────────────────────────────────────────────────────────────
# Model loader (lazy + cached)
# ─────────────────────────────────────────────────────────────────────
_MODEL_CACHE: dict[str, dict] = {}
_CACHE_LOCK = threading.Lock()


def _model_key(sport: str, stat: str) -> str:
    return f"{sport.lower()}_{stat.lower()}"


def _load_model(sport: str, stat: str) -> Optional[dict]:
    key = _model_key(sport, stat)
    with _CACHE_LOCK:
        if key in _MODEL_CACHE:
            return _MODEL_CACHE[key]

    meta_path = MODEL_DIR / f"{key}.meta.json"
    if not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text())
    except Exception as e:
        logger.error("failed to read %s: %s", meta_path, e)
        return None

    winner = meta.get("winner", "lgbm")
    pkl = MODEL_DIR / f"{key}_{winner}.pkl"
    if not pkl.exists():
        return None
    try:
        payload = pickle.loads(pkl.read_bytes())
    except Exception as e:
        logger.error("failed to unpickle %s: %s", pkl, e)
        return None

    bundle = {
        "meta":          meta,
        "booster":       payload.get("booster"),
        "feature_names": payload.get("feature_names") or [],
        "position":      payload.get("position"),
        "residual_std":  float(payload.get("residual_std")
                                or meta.get(winner, {}).get("residual_std")
                                or 0.0),
        "sport":         sport,
        "stat":          stat,
        "winner":        winner,
    }
    with _CACHE_LOCK:
        _MODEL_CACHE[key] = bundle
    logger.info("loaded model %s (%s)", key, winner)
    return bundle


def _reset_model_cache() -> None:
    with _CACHE_LOCK:
        _MODEL_CACHE.clear()


# ─────────────────────────────────────────────────────────────────────
# Confidence scoring
# ─────────────────────────────────────────────────────────────────────
def _confidence_score(
    feature_vec: dict[str, float],
    feature_names: list[str],
) -> tuple[str, dict]:
    """Score confidence based on how many features are non-NaN and how
    much pregame signal we have in `stat_last_5_avg` and
    `similar_def_n_games`."""
    filled = sum(1 for f in feature_names
                  if not (isinstance(feature_vec.get(f), float)
                          and math.isnan(feature_vec[f])))
    fill_rate = filled / max(1, len(feature_names))
    l5 = feature_vec.get("stat_last_5_avg", float("nan"))
    l10 = feature_vec.get("stat_last_10_avg", float("nan"))
    has_recent = not (isinstance(l5, float) and math.isnan(l5))
    n_sim = feature_vec.get("similar_def_n_games", 0)
    if isinstance(n_sim, float) and math.isnan(n_sim):
        n_sim = 0
    label = "high" if (fill_rate >= 0.85 and has_recent and n_sim >= 3) \
            else "medium" if (fill_rate >= 0.65 and has_recent) \
            else "low"
    return label, {
        "fill_rate": round(fill_rate, 3),
        "has_recent": has_recent,
        "similar_games_n": int(n_sim) if not isinstance(n_sim, float) or not math.isnan(n_sim) else 0,
    }


# ─────────────────────────────────────────────────────────────────────
# Feature attribution (top-K factors)
# ─────────────────────────────────────────────────────────────────────
def _top_factors(
    booster,
    winner: str,
    feature_vec: dict[str, float],
    feature_names: list[str],
    k: int = 5,
) -> list[dict]:
    """Return the top-k features by gain × current value magnitude.

    We combine global feature-importance (from training) with the
    current-row's value so features that are BOTH globally important
    AND present in this specific row rank higher.
    """
    try:
        if winner == "lgbm":
            gains = booster.feature_importance(importance_type="gain")
            imp = dict(zip(booster.feature_name(), gains))
        else:
            imp = booster.get_score(importance_type="gain")
    except Exception:
        imp = {}
    total = sum(imp.values()) or 1.0
    scored: list[tuple[str, float, float]] = []
    for f in feature_names:
        weight = imp.get(f, 0.0) / total
        val = feature_vec.get(f, float("nan"))
        if isinstance(val, float) and math.isnan(val):
            continue
        # score = normalized weight (feature always contributes)
        scored.append((f, val, weight))
    scored.sort(key=lambda t: t[2], reverse=True)
    return [{"feature": f, "value": round(float(v), 3), "weight": round(w, 4)}
            for f, v, w in scored[:k]]


# ─────────────────────────────────────────────────────────────────────
# Normal CDF (no scipy dep at inference)
# ─────────────────────────────────────────────────────────────────────
def _norm_sf(z: float) -> float:
    """P(Z > z) using math.erf for a stdlib-only inference path."""
    return 0.5 * math.erfc(z / math.sqrt(2.0))


# ─────────────────────────────────────────────────────────────────────
# Public prediction entry point
# ─────────────────────────────────────────────────────────────────────
async def predict_player_prop(
    db,
    *,
    sport: str,
    player: str,
    stat: str,
    opponent: str,
    line: Optional[float] = None,
) -> dict:
    """Predict P(player exceeds line) for the given prop.

    Zero raises — errors are folded into `notes` inside the payload.
    """
    sport_u = (sport or "").upper()
    if sport_u != "NFL":
        return {
            "supported": False,
            "reason": f"sport {sport_u} not yet supported by trained engine",
        }

    bundle = _load_model(sport_u, stat)
    if not bundle:
        return {
            "supported": False,
            "reason": f"no trained model for {sport_u}/{stat}",
        }

    # 1. Live feature vector.
    try:
        feat_dict, feat_order, feat_meta = await build_nfl_live_features(
            db,
            player_name=player,
            opponent_team=opponent,
            stat=stat,
            position=bundle.get("position"),
        )
    except Exception as e:
        logger.exception("live feature build failed: %s", e)
        return {"supported": False, "reason": f"feature build error: {e}"}

    # Align feature order to what the model was trained on.
    train_feat_names = bundle["feature_names"]
    row = np.array([[feat_dict.get(fn, float("nan"))
                       for fn in train_feat_names]], dtype=float)

    # 2. Model inference (predicted µ).
    booster = bundle["booster"]
    try:
        if bundle["winner"] == "lgbm":
            mu = float(booster.predict(row)[0])
        else:
            import xgboost as xgb
            dm = xgb.DMatrix(row, feature_names=train_feat_names)
            mu = float(booster.predict(dm)[0])
    except Exception as e:
        logger.exception("model.predict failed: %s", e)
        return {"supported": False, "reason": f"inference error: {e}"}

    sigma = bundle["residual_std"] or 1.0
    if not math.isfinite(sigma) or sigma <= 0:
        sigma = 1.0

    # 3. Threshold probability.
    if line is not None:
        z = (float(line) - mu) / sigma
        p_over = round(1.0 - _norm_sf(z) if z < 0 else _norm_sf(z) if False else 1.0 - _norm_sf(-z), 4)
        # ↑ simplified: P(X > line) = 1 - Φ((line - µ)/σ) = SF((line - µ)/σ)
        p_over = round(_norm_sf((float(line) - mu) / sigma), 4)
    else:
        p_over = None

    # 4. Confidence + factors.
    confidence, conf_debug = _confidence_score(feat_dict, train_feat_names)
    factors = _top_factors(booster, bundle["winner"], feat_dict,
                            train_feat_names, k=5)

    # 5. Similar-games count (from feature vec).
    sim_n = feat_dict.get("similar_def_n_games", 0)
    if isinstance(sim_n, float) and math.isnan(sim_n):
        sim_n = 0

    result = {
        "supported":               True,
        "sport":                   "NFL",
        "player":                  player,
        "stat":                    stat,
        "opponent":                opponent,
        "line":                    line,
        "expected_value":          round(mu, 3),
        "residual_std":            round(sigma, 3),
        "prediction_probability":  p_over,
        "confidence":              confidence,
        "confidence_debug":        conf_debug,
        "model":                   bundle["winner"],
        "model_meta": {
            "n_train":  bundle["meta"].get(bundle["winner"], {}).get("n_train"),
            "n_val":    bundle["meta"].get(bundle["winner"], {}).get("n_val"),
            "auc_p50":  bundle["meta"].get(bundle["winner"], {}).get("auc_by_thr", {}).get("p50"),
            "trained_at": bundle["meta"].get("trained_at"),
        },
        "top_factors":             factors,
        "similar_games_used":      int(sim_n),
        "notes":                   feat_meta.get("notes", []),
    }
    return result


__all__ = ["predict_player_prop", "_reset_model_cache"]
