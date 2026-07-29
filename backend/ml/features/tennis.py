"""Tennis feature builder (2026-07-28).

Sources
  • `tennis_matches_history` (36k rows) — winner/loser IDs, surface,
    per-side stats (w_ace, l_ace, w_df, l_df, w_bpFaced, l_bpFaced,
    w_bpSaved, l_bpSaved, w_svGms, l_svGms, w_1stIn, w_1stWon, ...).
    Best training source — one row per completed match.

Stats supported
  • aces        — count per match (side-specific)
  • double_faults
  • total_games — sum of games played across sets
  • match_winner — binary classification proxy via regression on w-l flag
  • break_points_won — bpFaced - bpSaved (won by the returner)

We train per-player-side by "unpivoting" each match row into two
player-side rows (winner side + loser side) so the feature builder
can produce one training example per (player, match).

**No sportsbook odds. No betting lines.** Target = raw stat value.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger("lockscore.ml.features.tennis")


_TENNIS_STAT_COLUMN: dict[str, str] = {
    "aces":            "aces",
    "double_faults":   "double_faults",
    "total_games":     "total_games_match",
    "match_winner":    "won_flag",
    "break_points_won":"bp_won",
}


@dataclass
class TrainingFrame:
    features: pd.DataFrame
    target:   pd.Series
    row_meta: pd.DataFrame
    feature_names: list[str]
    stat:  str
    sport: str = "Tennis"


# ─────────────────────────────────────────────────────────────────────
# Match unpivoter — turn 1 match row into 2 player-side rows
# ─────────────────────────────────────────────────────────────────────
def _unpivot_matches(matches: pd.DataFrame) -> pd.DataFrame:
    """Convert winner/loser wide format into a long per-player table."""
    if matches.empty:
        return pd.DataFrame()
    # Winner side.
    w = pd.DataFrame({
        "date":            matches.get("date"),
        "match_id":        matches.index,
        "player_id":       matches.get("winner_id"),
        "player_name":     matches.get("winner_name"),
        "opponent_id":     matches.get("loser_id"),
        "opponent_name":   matches.get("loser_name"),
        "opponent_rank":   matches.get("loser_rank"),
        "self_rank":       matches.get("winner_rank"),
        "surface":         matches.get("surface"),
        "best_of":         matches.get("best_of"),
        "won_flag":        1.0,
        "aces":            matches.get("w_ace"),
        "double_faults":   matches.get("w_df"),
        "service_points":  matches.get("w_svpt"),
        "first_serve_in":  matches.get("w_1stIn"),
        "first_won":       matches.get("w_1stWon"),
        "second_won":      matches.get("w_2ndWon"),
        "service_games":   matches.get("w_SvGms"),
        "bp_faced":        matches.get("w_bpFaced"),
        "bp_saved":        matches.get("w_bpSaved"),
        "total_games_match": matches.get("total_games_match"),
    })
    l = pd.DataFrame({
        "date":            matches.get("date"),
        "match_id":        matches.index,
        "player_id":       matches.get("loser_id"),
        "player_name":     matches.get("loser_name"),
        "opponent_id":     matches.get("winner_id"),
        "opponent_name":   matches.get("winner_name"),
        "opponent_rank":   matches.get("winner_rank"),
        "self_rank":       matches.get("loser_rank"),
        "surface":         matches.get("surface"),
        "best_of":         matches.get("best_of"),
        "won_flag":        0.0,
        "aces":            matches.get("l_ace"),
        "double_faults":   matches.get("l_df"),
        "service_points":  matches.get("l_svpt"),
        "first_serve_in":  matches.get("l_1stIn"),
        "first_won":       matches.get("l_1stWon"),
        "second_won":      matches.get("l_2ndWon"),
        "service_games":   matches.get("l_SvGms"),
        "bp_faced":        matches.get("l_bpFaced"),
        "bp_saved":        matches.get("l_bpSaved"),
        "total_games_match": matches.get("total_games_match"),
    })
    long = pd.concat([w, l], ignore_index=True)
    long["bp_won"] = (long["bp_faced"].fillna(0)
                       - long["bp_saved"].fillna(0)).clip(lower=0)
    return long


# ─────────────────────────────────────────────────────────────────────
# Rolling helper — same shape as NFL / MLB
# ─────────────────────────────────────────────────────────────────────
def _grouped_rolling_prior(df, group_col, val_col, window, aggfunc="mean"):
    grp = df.groupby(group_col)[val_col]
    shifted = grp.shift(1)
    roll = shifted.groupby(df[group_col]).rolling(window=window,
                                                   min_periods=1)
    if aggfunc == "mean":   out = roll.mean()
    elif aggfunc == "median": out = roll.median()
    elif aggfunc == "std":  out = roll.std()
    else: raise ValueError(aggfunc)
    return out.reset_index(level=0, drop=True)


# ─────────────────────────────────────────────────────────────────────
# Training frame builder
# ─────────────────────────────────────────────────────────────────────
def build_tennis_training_frame(
    matches_df: pd.DataFrame,
    stat: str,
    min_prior_matches: int = 10,
    surface: Optional[str] = None,
) -> TrainingFrame:
    if not isinstance(matches_df, pd.DataFrame) or matches_df.empty:
        return TrainingFrame(pd.DataFrame(), pd.Series(dtype=float),
                              pd.DataFrame(), [], stat, "Tennis")
    long = _unpivot_matches(matches_df)
    if long.empty:
        return TrainingFrame(pd.DataFrame(), pd.Series(dtype=float),
                              pd.DataFrame(), [], stat, "Tennis")
    if surface:
        long = long[long["surface"] == surface].copy()

    target_col = _TENNIS_STAT_COLUMN.get(stat, stat)
    if target_col not in long.columns:
        return TrainingFrame(pd.DataFrame(), pd.Series(dtype=float),
                              pd.DataFrame(), [], stat, "Tennis")
    long = long.dropna(subset=[target_col, "player_id", "date"]).copy()
    long[target_col] = long[target_col].astype(float)
    long = long.sort_values(["player_id", "date"], kind="mergesort") \
                .reset_index(drop=True)

    # Rolling features on the target.
    long["stat_last_3_avg"]  = _grouped_rolling_prior(long, "player_id", target_col, 3)
    long["stat_last_5_avg"]  = _grouped_rolling_prior(long, "player_id", target_col, 5)
    long["stat_last_10_avg"] = _grouped_rolling_prior(long, "player_id", target_col, 10)
    long["stat_last_20_avg"] = _grouped_rolling_prior(long, "player_id", target_col, 20)
    long["stat_last_10_median"] = _grouped_rolling_prior(long, "player_id", target_col, 10, "median")
    long["stat_last_10_std"] = _grouped_rolling_prior(long, "player_id", target_col, 10, "std")

    # Rate features.
    long["ace_rate_l10"] = _grouped_rolling_prior(long, "player_id", "aces", 10)
    long["df_rate_l10"]  = _grouped_rolling_prior(long, "player_id", "double_faults", 10)
    long["first_in_rate_l10"] = _grouped_rolling_prior(long, "player_id",
                                                         "first_serve_in", 10)
    long["service_games_l10"] = _grouped_rolling_prior(long, "player_id",
                                                        "service_games", 10)

    # Opponent-side rolling — how many aces the opponent typically ALLOWS?
    long["opp_ace_allowed_l10"] = _grouped_rolling_prior(long, "opponent_id",
                                                          "aces", 10)
    # Rank diff (higher = tougher opp).
    long["rank_diff"] = (long["opponent_rank"].fillna(200)
                          - long["self_rank"].fillna(200))

    # Surface one-hot.
    for s in ("Hard", "Clay", "Grass", "Carpet"):
        long[f"surface_{s.lower()}"] = (long["surface"] == s).astype(float)

    # Best-of encoding.
    long["best_of_5"] = (long["best_of"] == 5).astype(float)

    long["matches_played_ytd"] = long.groupby("player_id").cumcount()

    feature_names = [
        "stat_last_3_avg", "stat_last_5_avg", "stat_last_10_avg",
        "stat_last_20_avg", "stat_last_10_median", "stat_last_10_std",
        "ace_rate_l10", "df_rate_l10", "first_in_rate_l10",
        "service_games_l10", "opp_ace_allowed_l10", "rank_diff",
        "surface_hard", "surface_clay", "surface_grass", "surface_carpet",
        "best_of_5", "matches_played_ytd",
    ]

    long = long[long["matches_played_ytd"] >= min_prior_matches].copy()
    if long.empty:
        return TrainingFrame(pd.DataFrame(), pd.Series(dtype=float),
                              pd.DataFrame(), feature_names, stat, "Tennis")
    long = long.dropna(subset=["stat_last_5_avg"]).copy()
    for c in feature_names:
        if c not in long.columns:
            long[c] = np.nan

    X = long[feature_names].astype(float).reset_index(drop=True)
    y = long[target_col].astype(float).reset_index(drop=True)
    meta = long[["date", "player_id", "player_name", "opponent_id",
                  "opponent_name", "surface"]].reset_index(drop=True)
    return TrainingFrame(features=X, target=y, row_meta=meta,
                          feature_names=feature_names, stat=stat, sport="Tennis")


async def build_tennis_live_features(
    db, *, player_name: str, opponent_team: Optional[str] = None,
    stat: str, surface: Optional[str] = None,
) -> tuple[dict[str, float], list[str], dict]:
    notes: list[str] = []
    meta = {"sport": "Tennis", "stat": stat, "opponent": opponent_team,
             "player_name": player_name, "notes": notes}

    # Pull all matches involving this player.
    q = {"$or": [{"winner_name": player_name}, {"loser_name": player_name}]}
    cursor = db.tennis_matches_history.find(q, {"_id": 0}).sort("date", 1).limit(200)
    rows = [r async for r in cursor]
    if not rows:
        notes.append("no historical matches for player")
        return _empty_vec(), _feature_names(), meta
    df = pd.DataFrame(rows)
    tf = build_tennis_training_frame(df, stat=stat, min_prior_matches=1,
                                        surface=surface)
    if tf.features.empty:
        notes.append("training frame empty")
        return _empty_vec(), _feature_names(), meta
    # Filter to rows where this was OUR player (unpivot creates both
    # winner + loser sides). Use the last row for this player_name.
    mask = (tf.row_meta["player_name"] == player_name)
    if mask.any():
        idx = tf.features.index[mask][-1]
        return tf.features.loc[idx].to_dict(), tf.feature_names, meta
    return tf.features.iloc[-1].to_dict(), tf.feature_names, meta


def _feature_names() -> list[str]:
    return [
        "stat_last_3_avg", "stat_last_5_avg", "stat_last_10_avg",
        "stat_last_20_avg", "stat_last_10_median", "stat_last_10_std",
        "ace_rate_l10", "df_rate_l10", "first_in_rate_l10",
        "service_games_l10", "opp_ace_allowed_l10", "rank_diff",
        "surface_hard", "surface_clay", "surface_grass", "surface_carpet",
        "best_of_5", "matches_played_ytd",
    ]


def _empty_vec() -> dict[str, float]:
    return {n: float("nan") for n in _feature_names()}


__all__ = [
    "build_tennis_training_frame",
    "build_tennis_live_features",
    "TrainingFrame",
]
