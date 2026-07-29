"""NBA Feature Builder (Phase 7, 2026-07-29).

Mirrors `ml.features.mlb` so the existing `trained_prediction_engine`
and `train_prop_model.py` treat NBA the same as MLB / NFL / Tennis.

Supported stats
  • points       (canonical market family)
  • rebounds
  • assists
  • threes_made  (mapped from "3-pointers made" market pattern)

Data source: `player_game_logs` where `sport == "nba"` — populated by
`services.nba_gamelog_ingest`.

Features (16 — matches MLB feature count for downstream homogeneity)
──────────────────────────────────────────────────────────────────────
  PLAYER    stat_last_3_avg, stat_last_5_avg, stat_last_10_avg,
            stat_last_20_avg, stat_last_10_median, stat_last_10_std,
            stat_last_5_max, stat_last_5_min, stat_last_10_cv,
            stat_season_to_date_avg, stat_last_10_hit_rate
  VOLUME    minutes_last_5_avg
  SITUATION is_home, rest_days_est, is_b2b
  EXPERIENCE games_played_ytd
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd


_NBA_STAT_ELIGIBLE = {"points", "rebounds", "assists", "threes_made",
                      "steals", "blocks"}


@dataclass
class TrainingFrame:
    features: pd.DataFrame
    target:   pd.Series
    row_meta: pd.DataFrame
    feature_names: list[str]
    stat:  str
    sport: str = "NBA"


def _grouped_rolling_prior(df, group_col, val_col, window, aggfunc="mean"):
    grp = df.groupby(group_col)[val_col]
    shifted = grp.shift(1)
    roll = shifted.groupby(df[group_col]).rolling(window=window,
                                                    min_periods=1)
    if aggfunc == "mean":     out = roll.mean()
    elif aggfunc == "median": out = roll.median()
    elif aggfunc == "std":    out = roll.std()
    elif aggfunc == "max":    out = roll.max()
    elif aggfunc == "min":    out = roll.min()
    else: raise ValueError(aggfunc)
    return out.reset_index(level=0, drop=True)


def _season_to_date_prior(df, val_col):
    grp = df.groupby("player_id")[val_col]
    shifted = grp.shift(1)
    return shifted.groupby(df["player_id"]).expanding().mean() \
                   .reset_index(level=0, drop=True)


def _feature_names() -> list[str]:
    return [
        "stat_last_3_avg", "stat_last_5_avg", "stat_last_10_avg",
        "stat_last_20_avg", "stat_last_10_median", "stat_last_10_std",
        "stat_last_5_max", "stat_last_5_min", "stat_last_10_cv",
        "minutes_last_5_avg", "games_played_ytd",
        "stat_season_to_date_avg", "stat_last_10_hit_rate",
        "is_home", "rest_days_est", "is_b2b",
    ]


def _empty_vec() -> dict[str, float]:
    return {n: float("nan") for n in _feature_names()}


# ═════════════════════════════════════════════════════════════════════
# Training frame
# ═════════════════════════════════════════════════════════════════════
def build_nba_training_frame(
    rows_df: pd.DataFrame,
    stat: str,
    min_prior_games: int = 5,
) -> TrainingFrame:
    if not isinstance(rows_df, pd.DataFrame) or rows_df.empty:
        return TrainingFrame(pd.DataFrame(), pd.Series(dtype=float),
                              pd.DataFrame(), _feature_names(),
                              stat, "NBA")
    if stat not in _NBA_STAT_ELIGIBLE:
        return TrainingFrame(pd.DataFrame(), pd.Series(dtype=float),
                              pd.DataFrame(), _feature_names(),
                              stat, "NBA")

    df = rows_df.copy()
    if stat not in df.columns:
        return TrainingFrame(pd.DataFrame(), pd.Series(dtype=float),
                              pd.DataFrame(), _feature_names(),
                              stat, "NBA")

    for col in (stat, "minutes", "rebounds", "assists", "points",
                 "threes_made", "steals", "blocks", "rest_days",
                 "is_b2b", "is_home"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=[stat, "player_id"]).copy()
    df[stat] = df[stat].astype(float)

    sort_key = "date" if "date" in df.columns and \
        df["date"].notna().any() else "game_id"
    df = df.sort_values(["player_id", sort_key], kind="mergesort") \
            .reset_index(drop=True)

    # ── Rolling player features ─────────────────────────────────
    df["stat_last_3_avg"]     = _grouped_rolling_prior(df, "player_id", stat, 3)
    df["stat_last_5_avg"]     = _grouped_rolling_prior(df, "player_id", stat, 5)
    df["stat_last_10_avg"]    = _grouped_rolling_prior(df, "player_id", stat, 10)
    df["stat_last_20_avg"]    = _grouped_rolling_prior(df, "player_id", stat, 20)
    df["stat_last_10_median"] = _grouped_rolling_prior(df, "player_id", stat, 10, "median")
    df["stat_last_10_std"]    = _grouped_rolling_prior(df, "player_id", stat, 10, "std")
    df["stat_last_5_max"]     = _grouped_rolling_prior(df, "player_id", stat, 5, "max")
    df["stat_last_5_min"]     = _grouped_rolling_prior(df, "player_id", stat, 5, "min")
    df["stat_last_10_cv"]     = df["stat_last_10_std"] / \
                                 (df["stat_last_10_avg"] + 1e-6)
    df["minutes_last_5_avg"]  = _grouped_rolling_prior(
        df, "player_id",
        "minutes" if "minutes" in df.columns else stat,
        5,
    )
    df["games_played_ytd"]    = df.groupby("player_id").cumcount()
    df["stat_season_to_date_avg"] = _season_to_date_prior(df, stat)
    df["_beat_avg_flag"] = (df[stat] > df["stat_last_10_avg"]).astype(float)
    df["stat_last_10_hit_rate"] = _grouped_rolling_prior(
        df, "player_id", "_beat_avg_flag", 10)

    # ── Situation features ─────────────────────────────────
    if "is_home" not in df.columns:
        df["is_home"] = 0.5
    if "rest_days" in df.columns:
        df["rest_days_est"] = df["rest_days"].fillna(2.0).clip(0, 10)
    else:
        df["rest_days_est"] = 2.0
    if "is_b2b" not in df.columns:
        df["is_b2b"] = 0.0
    df["is_home"] = df["is_home"].fillna(0.5).astype(float)
    df["is_b2b"] = df["is_b2b"].fillna(0.0).astype(float)

    feature_names = _feature_names()
    df = df[df["games_played_ytd"] >= min_prior_games].copy()
    if df.empty:
        return TrainingFrame(pd.DataFrame(), pd.Series(dtype=float),
                              pd.DataFrame(), feature_names, stat, "NBA")
    df = df.dropna(subset=["stat_last_5_avg"]).copy()
    for c in feature_names:
        if c not in df.columns:
            df[c] = np.nan
    X = df[feature_names].astype(float).reset_index(drop=True)
    y = df[stat].astype(float).reset_index(drop=True)
    meta_cols = [c for c in ("date", "player_id", "player", "team",
                              "opp_team_id", "game_id", "season")
                  if c in df.columns]
    meta = df[meta_cols].reset_index(drop=True)
    return TrainingFrame(features=X, target=y, row_meta=meta,
                          feature_names=feature_names, stat=stat,
                          sport="NBA")


# ═════════════════════════════════════════════════════════════════════
# Live inference
# ═════════════════════════════════════════════════════════════════════
async def build_nba_live_features(
    db,
    *,
    player_name: str,
    opponent_team: Optional[str] = None,
    stat: str,
    player_id: Optional[int] = None,
) -> tuple[dict[str, float], list[str], dict]:
    """Live inference: pull last-50 games and run same pipeline."""
    notes: list[str] = []
    meta = {"sport": "NBA", "stat": stat, "opponent": opponent_team,
             "player_name": player_name, "notes": notes}
    if stat not in _NBA_STAT_ELIGIBLE:
        notes.append(f"unsupported NBA stat: {stat!r}")
        return _empty_vec(), _feature_names(), meta

    q: dict = {"sport": "nba"}
    if player_id is not None:
        q["player_id"] = int(player_id)
    else:
        if not player_name:
            notes.append("no player id/name supplied")
            return _empty_vec(), _feature_names(), meta
        # Resolve espn_id via players collection (case-insensitive exact)
        try:
            row = await db.players.find_one(
                {"sport": "nba", "canonical_name":
                    (player_name or "").strip().lower()},
                {"espn_id": 1, "player_id": 1, "_id": 0},
            )
            if not row:
                # Try 'name' field fallback (case-insensitive regex)
                import re
                pat = re.compile(f"^{re.escape(player_name.strip())}$",
                                  re.I)
                row = await db.players.find_one(
                    {"sport": "nba", "name": pat},
                    {"espn_id": 1, "player_id": 1, "_id": 0},
                )
            pid = (row or {}).get("espn_id") or (row or {}).get("player_id")
            if not pid:
                notes.append(f"player_id not resolvable for {player_name!r}")
                return _empty_vec(), _feature_names(), meta
            q["player_id"] = int(pid)
        except Exception as e:
            notes.append(f"id lookup error: {e}")
            return _empty_vec(), _feature_names(), meta

    cursor = db.player_game_logs.find(q, {"_id": 0}) \
                                  .sort("date", -1).limit(50)
    rows = [r async for r in cursor]
    if not rows:
        notes.append("no historical rows")
        return _empty_vec(), _feature_names(), meta

    df = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    tf = build_nba_training_frame(df, stat=stat, min_prior_games=1)
    if tf.features.empty:
        notes.append("training-frame builder returned empty")
        return _empty_vec(), _feature_names(), meta

    last = tf.features.iloc[-1].to_dict()
    return last, tf.feature_names, meta


__all__ = [
    "build_nba_training_frame",
    "build_nba_live_features",
    "TrainingFrame",
]
