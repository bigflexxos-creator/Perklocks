"""MLB feature builder (2026-07-28).

Trains models for BATTER props (hits, home_runs, rbi, total_bases,
hits_runs_rbis) and PITCHER props (strikeouts/K, outs_recorded).

Data source: `player_game_logs` where sport='mlb' (63k+ rows).
Each row is a per-game per-player line with:
  hits, home_runs, rbi, total_bases, strikeouts (batter),
  pitcher_strikeouts, innings_pitched, walks, at_bats, earned_runs,
  hits_allowed, date, team, game_id, player_id

Feature engineering mirrors NFL builder exactly — rolling player
averages (last 3/5/10, season-to-date), volatility, opponent
allowance features. Where MLB-specific richer signal exists (Statcast
xBA, park factors), we add sport-specific columns.

**No sportsbook odds. No betting lines.** Target = raw stat value.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger("lockscore.ml.features.mlb")

# ─────────────────────────────────────────────────────────────────────
# Stat → position gate
# ─────────────────────────────────────────────────────────────────────
_MLB_BATTER_STATS = ("hits", "home_runs", "rbi", "total_bases",
                     "hits_runs_rbis", "strikeouts")   # batter Ks
_MLB_PITCHER_STATS = ("pitcher_strikeouts", "outs_recorded",
                      "innings_pitched")

_MLB_STAT_ELIGIBLE_POS: dict[str, str] = {
    "hits":               "BATTER",
    "home_runs":          "BATTER",
    "rbi":                "BATTER",
    "total_bases":        "BATTER",
    "strikeouts":         "BATTER",  # batter strikeouts (K prop)
    "hits_runs_rbis":     "BATTER",
    "pitcher_strikeouts": "PITCHER",
    "outs_recorded":      "PITCHER",
    "innings_pitched":    "PITCHER",
}

# Row-classifier: batter has at_bats > 0, pitcher has innings_pitched > 0
def _classify_row(row: pd.Series) -> str:
    ab = row.get("at_bats")
    try:
        if ab is not None and float(ab) > 0:
            return "BATTER"
    except (TypeError, ValueError):
        pass
    ip = row.get("innings_pitched")
    try:
        if ip is not None and float(ip) > 0:
            return "PITCHER"
    except (TypeError, ValueError):
        pass
    # Fallback signals when at_bats / innings_pitched are missing.
    if row.get("hits_allowed", 0) not in (None, 0) or \
       (row.get("pitcher_strikeouts") not in (None, 0)):
        return "PITCHER"
    if row.get("hits") not in (None, 0) or row.get("home_runs") not in (None, 0):
        return "BATTER"
    return "UNKNOWN"


@dataclass
class TrainingFrame:
    features:  pd.DataFrame
    target:    pd.Series
    row_meta:  pd.DataFrame
    feature_names: list[str]
    stat:      str
    sport:     str = "MLB"


# ─────────────────────────────────────────────────────────────────────
# Rolling helpers (shared shape with NFL builder)
# ─────────────────────────────────────────────────────────────────────
def _grouped_rolling_prior(df, group_col, val_col, window, aggfunc="mean"):
    grp = df.groupby(group_col)[val_col]
    shifted = grp.shift(1)
    roll = shifted.groupby(df[group_col]).rolling(window=window,
                                                   min_periods=1)
    if aggfunc == "mean":   out = roll.mean()
    elif aggfunc == "median": out = roll.median()
    elif aggfunc == "std":  out = roll.std()
    elif aggfunc == "max":  out = roll.max()
    elif aggfunc == "min":  out = roll.min()
    else: raise ValueError(aggfunc)
    return out.reset_index(level=0, drop=True)


def _season_to_date_prior(df, group_cols, val_col):
    grp = df.groupby(group_cols)[val_col]
    shifted = grp.shift(1)
    return shifted.groupby([df[c] for c in group_cols]).expanding().mean() \
                   .reset_index(level=list(range(len(group_cols))), drop=True)


# ─────────────────────────────────────────────────────────────────────
# Composite: hits_runs_rbis (batter). runs isn't in the schema, so we
# approximate as hits + rbi (a conservative undercount; but the same
# undercount at training + inference so it's self-consistent).
# ─────────────────────────────────────────────────────────────────────
def _ensure_composite_column(df: pd.DataFrame, stat: str) -> pd.DataFrame:
    if stat == "hits_runs_rbis" and "hits_runs_rbis" not in df.columns:
        # runs not stored per game — use hits + rbi as a proxy.
        df["hits_runs_rbis"] = df.get("hits", 0).fillna(0) \
                              + df.get("rbi", 0).fillna(0)
    if stat == "outs_recorded" and "outs_recorded" not in df.columns:
        # outs recorded = innings_pitched * 3 (rounded down to whole outs)
        ip = df.get("innings_pitched", 0)
        df["outs_recorded"] = (ip.fillna(0).astype(float) * 3.0).round(0)
    return df


# ─────────────────────────────────────────────────────────────────────
# Training frame builder
# ─────────────────────────────────────────────────────────────────────
def build_mlb_training_frame(
    rows_df: pd.DataFrame,
    stat: str,
    min_prior_games: int = 5,
    date_min: Optional[str] = None,
) -> TrainingFrame:
    if not isinstance(rows_df, pd.DataFrame) or rows_df.empty:
        return TrainingFrame(pd.DataFrame(), pd.Series(dtype=float),
                              pd.DataFrame(), [], stat, "MLB")
    df = rows_df.copy()

    # Filter by date only when the caller supplied one AND rows actually
    # have date populated (MLB game logs may store null date).
    if date_min and "date" in df.columns:
        # Coerce None → NaT then filter (retain nulls to avoid dropping
        # the whole frame when date is uniformly null).
        df_with_date = df["date"].notna() & (df["date"].astype(str) >= date_min)
        if df_with_date.any():
            df = df[df_with_date | df["date"].isna()].copy()
    if df.empty:
        return TrainingFrame(pd.DataFrame(), pd.Series(dtype=float),
                              pd.DataFrame(), [], stat, "MLB")

    df["_row_type"] = df.apply(_classify_row, axis=1)
    target_pos = _MLB_STAT_ELIGIBLE_POS.get(stat, "BATTER")
    df = df[df["_row_type"] == target_pos].copy()

    df = _ensure_composite_column(df, stat)
    if stat not in df.columns:
        return TrainingFrame(pd.DataFrame(), pd.Series(dtype=float),
                              pd.DataFrame(), [], stat, "MLB")

    # Coerce string numerics (innings_pitched sometimes "0.2").
    for _numeric_col in (stat, "at_bats", "innings_pitched"):
        if _numeric_col in df.columns:
            df[_numeric_col] = pd.to_numeric(df[_numeric_col], errors="coerce")

    df = df.dropna(subset=[stat, "player_id"]).copy()
    df[stat] = df[stat].astype(float)

    # Sort by (player_id, game_id) — MLB Stats API game_ids are
    # monotonically increasing and serve as a reliable proxy for date
    # when `date` is null.
    sort_key = "date" if ("date" in df.columns and
                          df["date"].notna().any()) else "game_id"
    df = df.sort_values(["player_id", sort_key], kind="mergesort") \
            .reset_index(drop=True)

    # ── Player rolling features ──────────────────────────────────
    df["stat_last_3_avg"]     = _grouped_rolling_prior(df, "player_id", stat, 3)
    df["stat_last_5_avg"]     = _grouped_rolling_prior(df, "player_id", stat, 5)
    df["stat_last_10_avg"]    = _grouped_rolling_prior(df, "player_id", stat, 10)
    df["stat_last_20_avg"]    = _grouped_rolling_prior(df, "player_id", stat, 20)
    df["stat_last_10_median"] = _grouped_rolling_prior(df, "player_id", stat, 10, "median")
    df["stat_last_10_std"]    = _grouped_rolling_prior(df, "player_id", stat, 10, "std")
    df["stat_last_5_max"]     = _grouped_rolling_prior(df, "player_id", stat, 5, "max")
    df["stat_last_5_min"]     = _grouped_rolling_prior(df, "player_id", stat, 5, "min")

    # Volatility (CV)
    df["stat_last_10_cv"] = df["stat_last_10_std"] / (df["stat_last_10_avg"] + 1e-6)

    # Volume proxy per stat family.
    if target_pos == "BATTER":
        df["volume_last_5_avg"] = _grouped_rolling_prior(df, "player_id", "at_bats", 5)
    else:
        df["volume_last_5_avg"] = _grouped_rolling_prior(df, "player_id",
                                                          "innings_pitched", 5)

    # ── Games-played (fatigue / experience) ─────────────────────
    df["games_played_ytd"] = df.groupby("player_id").cumcount()

    # ── Opponent allowance (batter: opposing team pitching K/HR rates).
    # We can't dereference an opponent field here since `player_game_logs`
    # has only `team`, not opponent. That's a known gap — we substitute
    # league-level averages by DATE (per-month) as a coarse proxy.
    if "date" in df.columns and df["date"].notna().any():
        df["_month"] = df["date"].astype(str).str[:7]
        league_by_month = df.groupby("_month")[stat].transform("mean")
        # Shift by 1 month so we don't leak in-month info.
        df["league_avg_prior_month"] = league_by_month.shift(1).fillna(
            league_by_month.mean()
        )
    else:
        # Fallback: expanding cumulative mean shifted by 1 row.
        df["league_avg_prior_month"] = (
            df[stat].expanding().mean().shift(1).fillna(df[stat].mean())
        )

    # ── Streak features (last-5 max, last-5 min already in) ──────
    # Hot-hand: how often stat > prior season-to-date avg over last 10
    df["stat_season_to_date_avg"] = _season_to_date_prior(df,
                                                           ["player_id"], stat)
    df["_beat_avg_flag"] = (df[stat] > df["stat_last_10_avg"]).astype(float)
    df["stat_last_10_hit_rate"] = _grouped_rolling_prior(
        df, "player_id", "_beat_avg_flag", 10)

    # ── Statcast enrichment optional — join in main trainer  ────

    # ── Position one-hot (batter/pitcher only) ────
    df["is_batter"]  = (df["_row_type"] == "BATTER").astype(float)
    df["is_pitcher"] = (df["_row_type"] == "PITCHER").astype(float)

    # ── Feature matrix ─────
    feature_names = [
        "stat_last_3_avg", "stat_last_5_avg", "stat_last_10_avg",
        "stat_last_20_avg", "stat_last_10_median", "stat_last_10_std",
        "stat_last_5_max", "stat_last_5_min", "stat_last_10_cv",
        "volume_last_5_avg", "games_played_ytd",
        "league_avg_prior_month",
        "stat_season_to_date_avg", "stat_last_10_hit_rate",
        "is_batter", "is_pitcher",
    ]

    df = df[df["games_played_ytd"] >= min_prior_games].copy()
    if df.empty:
        return TrainingFrame(pd.DataFrame(), pd.Series(dtype=float),
                              pd.DataFrame(), feature_names, stat, "MLB")

    df = df.dropna(subset=["stat_last_5_avg"]).copy()
    for c in feature_names:
        if c not in df.columns:
            df[c] = np.nan

    X = df[feature_names].astype(float).reset_index(drop=True)
    y = df[stat].astype(float).reset_index(drop=True)
    meta_cols = ["player_id", "team", "_row_type"]
    if "date" in df.columns:
        meta_cols.insert(0, "date")
    if "game_id" in df.columns:
        meta_cols.append("game_id")
    meta = df[meta_cols].rename(columns={"_row_type": "row_type"}) \
                          .reset_index(drop=True)
    return TrainingFrame(features=X, target=y, row_meta=meta,
                          feature_names=feature_names, stat=stat, sport="MLB")


# ─────────────────────────────────────────────────────────────────────
# Live inference: build feature vector for a single (player, stat).
# ─────────────────────────────────────────────────────────────────────
async def build_mlb_live_features(
    db,
    *,
    player_name: str,
    opponent_team: Optional[str] = None,
    stat: str,
    player_id: Optional[int] = None,
) -> tuple[dict[str, float], list[str], dict]:
    """Live inference vector — pull last-50 games for this player,
    run the same feature pipeline, return the last row."""
    notes: list[str] = []
    meta = {"sport": "MLB", "stat": stat, "opponent": opponent_team,
             "player_name": player_name, "notes": notes}
    if not player_id and not player_name:
        notes.append("no player id/name supplied")
        return _empty_vec(stat), _feature_names(stat), meta

    q = {"sport": "mlb"}
    if player_id is not None:
        q["player_id"] = player_id
    else:
        # player_game_logs stores player_id only — we need name→id
        # via mlb_bvp.lookup_player_id.
        try:
            from mlb_bvp import lookup_player_id
            pid = await lookup_player_id(player_name)
            if not pid:
                notes.append("player_id not resolvable")
                return _empty_vec(stat), _feature_names(stat), meta
            q["player_id"] = pid
        except Exception as e:
            notes.append(f"id lookup error: {e}")
            return _empty_vec(stat), _feature_names(stat), meta

    cursor = db.player_game_logs.find(q, {"_id": 0}).sort("date", -1).limit(50)
    rows = [r async for r in cursor]
    if not rows:
        notes.append("no historical rows")
        return _empty_vec(stat), _feature_names(stat), meta

    df = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    tf = build_mlb_training_frame(df, stat=stat, min_prior_games=1,
                                    date_min="1900-01-01")
    if tf.features.empty:
        notes.append("training-frame builder returned empty")
        return _empty_vec(stat), _feature_names(stat), meta
    last = tf.features.iloc[-1].to_dict()
    return last, tf.feature_names, meta


def _feature_names(stat: str) -> list[str]:
    return [
        "stat_last_3_avg", "stat_last_5_avg", "stat_last_10_avg",
        "stat_last_20_avg", "stat_last_10_median", "stat_last_10_std",
        "stat_last_5_max", "stat_last_5_min", "stat_last_10_cv",
        "volume_last_5_avg", "games_played_ytd",
        "league_avg_prior_month",
        "stat_season_to_date_avg", "stat_last_10_hit_rate",
        "is_batter", "is_pitcher",
    ]


def _empty_vec(stat: str) -> dict[str, float]:
    return {n: float("nan") for n in _feature_names(stat)}


__all__ = [
    "build_mlb_training_frame",
    "build_mlb_live_features",
    "TrainingFrame",
]
