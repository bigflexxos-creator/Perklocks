"""Feature builder for player-prop prediction models (2026-07-28).

Design principles
─────────────────
1. **Time-safe** — every feature for row `t` is computed from data
   strictly before `t`. No leakage. No shuffle-safe hacks.
2. **Market-independent** — no sportsbook odds. No betting line. No
   consensus lean. Model predicts player performance only.
3. **Pure pandas** — vectorised group-by rolling operations so we
   can process ~130 k rows in <20 s.
4. **Sport-pluggable** — one builder function per sport. All builders
   return a (features DataFrame, target Series) pair with a common
   schema so `train_prop_model.py` doesn't care about the sport.

Public API
──────────
    features, target, meta = build_nfl_training_frame(
        rows_df,             # raw nfl_player_weekly rows as a DF
        stat="passing_yards",
        position="QB",       # restrict to a role
        min_prior_games=3,   # require ≥3 pregame samples per row
    )

    live_features = build_nfl_live_features(
        db, player_name, opponent_team, stat, position,
    )

Feature groups
──────────────
  PLAYER
    stat_last_5_avg          — rolling mean of last 5 games (this stat)
    stat_last_10_avg         — rolling mean of last 10 games
    stat_season_to_date_avg  — mean across current season prior to row
    stat_last_10_median      — median of last 10 games
    stat_last_10_std         — rolling std (consistency proxy)
    stat_last_10_cv          — std / (mean+eps) (coeff of variation)
    stat_last_10_hit_avg     — how often player exceeded own last-10 avg
    stat_last_3_avg          — very-recent form
    stat_last_5_max          — recent ceiling
    stat_last_5_min          — recent floor
    games_played_ytd         — experience / mileage proxy
    volume_last_5_avg        — snap/usage volume (position-specific)

  OPPONENT
    opp_allowed_avg_l1_season — this opp's per-game allowance for the
                                stat, based on the PRIOR completed season
    opp_allowed_avg_ytd       — allowance this season, only games BEFORE
                                the target row's week
    opp_allowed_rank_l1       — 1 (best defense) .. 32 (worst) from prior
                                season allowance

  SIMILAR-DEFENSE (proxy for "matchups like this one")
    similar_def_avg           — player's average vs teams whose PRIOR-
                                SEASON allowance was within ±20 % of this
                                opp's PRIOR-SEASON allowance
    similar_def_n_games       — sample size feeding similar_def_avg

  SITUATION
    is_home                   — 1 if player's team == home team
    rest_days_est             — 7 * (this_week - last_played_week)
                                capped at 14 (bye weeks)
    season_phase              — this_week / 22 (regular + playoffs)
    is_early_season           — 1 if week ≤ 4
    is_late_season            — 1 if week ≥ 15
    is_playoffs               — 1 if season_type == "POST"

  POSITION ENCODING (one-hot, only if position filter is None)
    pos_QB, pos_RB, pos_WR, pos_TE

**No** market features. **No** book odds. **No** line. **No** consensus.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger("lockscore.ml.feature_builder")

# ─────────────────────────────────────────────────────────────────────
# NFL stat → volume column (usage proxy)
# ─────────────────────────────────────────────────────────────────────
_NFL_STAT_VOLUME_COL: dict[str, str] = {
    "passing_yards":   "attempts",
    "passing_tds":     "attempts",
    "attempts":        "attempts",
    "completions":     "attempts",
    "passing_ints":    "attempts",
    "rushing_yards":   "carries",
    "rushing_tds":     "carries",
    "carries":         "carries",
    "receiving_yards": "targets",
    "receiving_tds":   "targets",
    "receptions":      "targets",
    "targets":         "targets",
}

# Position filter → allowed positions for this stat
_NFL_STAT_ELIGIBLE_POS: dict[str, tuple[str, ...]] = {
    "passing_yards":   ("QB",),
    "passing_tds":     ("QB",),
    "attempts":        ("QB",),
    "completions":     ("QB",),
    "passing_ints":    ("QB",),
    "rushing_yards":   ("RB", "QB", "WR"),
    "rushing_tds":     ("RB", "QB"),
    "carries":         ("RB",),
    "receiving_yards": ("WR", "TE", "RB"),
    "receiving_tds":   ("WR", "TE", "RB"),
    "receptions":      ("WR", "TE", "RB"),
    "targets":         ("WR", "TE", "RB"),
}


@dataclass
class TrainingFrame:
    features:  pd.DataFrame          # X — feature matrix
    target:    pd.Series             # y — the raw stat value (regression target)
    row_meta:  pd.DataFrame          # (season, week, player_display_name, opponent_team)
    feature_names: list[str]
    stat:      str
    sport:     str


# ═════════════════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════════════════
def _safe_div(a: pd.Series, b: pd.Series, eps: float = 1e-6) -> pd.Series:
    return a / (b + eps)


def _grouped_rolling_prior(
    df: pd.DataFrame,
    group_col: str,
    val_col: str,
    window: int,
    aggfunc: str = "mean",
) -> pd.Series:
    """Rolling window over prior rows only (shift(1) to exclude current row).

    Sorted ordering is the caller's responsibility. Groups are per player.
    """
    grp = df.groupby(group_col)[val_col]
    shifted = grp.shift(1)                              # exclude current row
    roll = shifted.groupby(df[group_col]).rolling(window=window,
                                                    min_periods=1)
    if aggfunc == "mean":
        out = roll.mean()
    elif aggfunc == "median":
        out = roll.median()
    elif aggfunc == "std":
        out = roll.std()
    elif aggfunc == "max":
        out = roll.max()
    elif aggfunc == "min":
        out = roll.min()
    else:
        raise ValueError(f"unsupported aggfunc {aggfunc}")
    out = out.reset_index(level=0, drop=True)
    return out


def _season_to_date_prior(df: pd.DataFrame,
                          group_cols: list[str],
                          val_col: str) -> pd.Series:
    """Season-to-date mean, excluding the current row (uses expanding shift)."""
    grp = df.groupby(group_cols)[val_col]
    shifted = grp.shift(1)
    return shifted.groupby([df[c] for c in group_cols]).expanding().mean() \
                  .reset_index(level=list(range(len(group_cols))), drop=True)


def _games_played_ytd(df: pd.DataFrame,
                       group_cols: list[str]) -> pd.Series:
    """1-indexed game number within the current season (i.e. games PRIOR
    + 1 = this game). We return games PRIOR (0 for opener)."""
    return df.groupby(group_cols).cumcount()


# ═════════════════════════════════════════════════════════════════════
# NFL — training frame builder
# ═════════════════════════════════════════════════════════════════════
def build_nfl_training_frame(
    rows_df: pd.DataFrame,
    stat: str,
    position: Optional[str] = None,
    min_prior_games: int = 3,
    seasons_min: int = 2019,
) -> TrainingFrame:
    """Build (X, y, meta) for NFL prop-prediction training.

    Args
    ────
      rows_df:        Raw nfl_player_weekly rows as pandas DataFrame.
                       Must contain: player_id, player_display_name,
                       team, opponent_team, season, week, position,
                       season_type (optional), and the `stat` column.
      stat:           Target stat column name (e.g. "passing_yards").
      position:       Restrict to a single position (e.g. "QB").
                       If None, adds one-hot position features and
                       includes every eligible position for the stat.
      min_prior_games: Drop rows where the player has fewer than N
                       prior games this season+ (avoids cold-start).
      seasons_min:    Discard rows before this season.

    Returns
    ───────
      TrainingFrame with feature matrix, raw stat target, row meta.
    """
    if stat not in rows_df.columns:
        raise ValueError(f"stat {stat!r} not in DataFrame columns")

    df = rows_df.copy()
    # ── Filters ──────────────────────────────────────────────────────
    df = df[df["season"] >= seasons_min].copy()
    if position:
        df = df[df["position"] == position].copy()
    else:
        eligible = _NFL_STAT_ELIGIBLE_POS.get(stat, ())
        if eligible:
            df = df[df["position"].isin(eligible)].copy()
    # Drop rows where target is NaN — those are missed games.
    df = df.dropna(subset=[stat]).copy()
    df[stat] = df[stat].astype(float)

    # Ensure sort order for all rolling ops.
    df = df.sort_values(["player_id", "season", "week"], kind="mergesort").reset_index(drop=True)

    # ── Player features ──────────────────────────────────────────────
    df["stat_last_3_avg"]  = _grouped_rolling_prior(df, "player_id", stat, 3, "mean")
    df["stat_last_5_avg"]  = _grouped_rolling_prior(df, "player_id", stat, 5, "mean")
    df["stat_last_10_avg"] = _grouped_rolling_prior(df, "player_id", stat, 10, "mean")
    df["stat_last_10_median"] = _grouped_rolling_prior(df, "player_id", stat, 10, "median")
    df["stat_last_10_std"]    = _grouped_rolling_prior(df, "player_id", stat, 10, "std")
    df["stat_last_5_max"]  = _grouped_rolling_prior(df, "player_id", stat, 5, "max")
    df["stat_last_5_min"]  = _grouped_rolling_prior(df, "player_id", stat, 5, "min")
    df["stat_season_to_date_avg"] = _season_to_date_prior(df, ["player_id", "season"], stat)

    # Consistency: coefficient of variation (lower = more consistent)
    df["stat_last_10_cv"] = _safe_div(df["stat_last_10_std"], df["stat_last_10_avg"])

    # Rate at which player exceeded own rolling avg in the last 10 games.
    df["_beat_avg_flag"] = ((df[stat] > df["stat_last_10_avg"]).astype(float))
    df["stat_last_10_hit_avg"] = _grouped_rolling_prior(df, "player_id",
                                                        "_beat_avg_flag", 10, "mean")

    # Volume proxy (position-specific).
    vol_col = _NFL_STAT_VOLUME_COL.get(stat)
    if vol_col and vol_col in df.columns:
        df["volume_last_5_avg"] = _grouped_rolling_prior(df, "player_id",
                                                          vol_col, 5, "mean")
    else:
        df["volume_last_5_avg"] = np.nan

    df["games_played_ytd"] = _games_played_ytd(df, ["player_id", "season"])

    # ── Opponent features ────────────────────────────────────────────
    #
    # OPP ALLOWED PRIOR SEASON — one number per (opp, season). We
    # aggregate the FULL prior season's allowed-per-game across all
    # opponents, then join back onto the current row.
    def _opp_allowance_by_season() -> pd.DataFrame:
        # Sum stat per (opp, season, week) across players first (so a
        # game with 2 QBs isn't double-weighted per player).
        pg = df.groupby(["opponent_team", "season", "week"],
                        as_index=False)[stat].sum()
        opp_ss = pg.groupby(["opponent_team", "season"])[stat].mean().reset_index()
        opp_ss = opp_ss.rename(columns={stat: "opp_allowed_avg"})
        opp_ss["rank"] = (opp_ss.groupby("season")["opp_allowed_avg"]
                                .rank(method="average", ascending=True))
        return opp_ss

    opp_ss = _opp_allowance_by_season()
    # Prior season lookup: match season == season - 1
    opp_prior = opp_ss.assign(next_season=lambda d: d["season"] + 1)[
        ["opponent_team", "next_season", "opp_allowed_avg", "rank"]
    ].rename(columns={"next_season": "season",
                       "opp_allowed_avg": "opp_allowed_avg_l1_season",
                       "rank": "opp_allowed_rank_l1"})
    df = df.merge(opp_prior, on=["opponent_team", "season"], how="left")

    # Season-to-date opponent allowance (uses only games before the
    # target row's week within the same season).
    def _opp_allowance_ytd() -> pd.Series:
        # For each (opp, season, week), average allowed stat over all
        # earlier weeks in that season.
        pg = df.groupby(["opponent_team", "season", "week"],
                        as_index=False)[stat].sum()
        pg = pg.sort_values(["opponent_team", "season", "week"], kind="mergesort")
        pg["opp_allowed_ytd"] = (pg.groupby(["opponent_team", "season"])[stat]
                                     .shift(1)
                                     .groupby([pg["opponent_team"], pg["season"]])
                                     .expanding().mean()
                                     .reset_index(level=[0, 1], drop=True))
        return pg[["opponent_team", "season", "week", "opp_allowed_ytd"]]

    opp_ytd = _opp_allowance_ytd()
    df = df.merge(opp_ytd, on=["opponent_team", "season", "week"], how="left")

    # ── Similar-defense features ─────────────────────────────────────
    # For each row, compute the player's historical average against
    # opponents whose PRIOR-SEASON allowance is within ±20% of the
    # target opponent's PRIOR-SEASON allowance. All from data BEFORE
    # this row (using cumulative running mean).
    df = df.sort_values(["player_id", "season", "week"],
                        kind="mergesort").reset_index(drop=True)
    df["_opp_l1_bucket_low"]  = df["opp_allowed_avg_l1_season"] * 0.80
    df["_opp_l1_bucket_high"] = df["opp_allowed_avg_l1_season"] * 1.20
    # This is expensive if computed row-by-row. We approximate by
    # bucketing opponents into 4 tiers based on opp_allowed_rank_l1
    # (top-8, 9-16, 17-24, 25-32) and taking the player's prior-average
    # WITHIN that same tier. This is time-safe (shift(1) + expanding).
    df["_opp_tier"] = pd.cut(df["opp_allowed_rank_l1"],
                             bins=[-0.1, 8.5, 16.5, 24.5, 33.0],
                             labels=[0, 1, 2, 3]).astype("float")
    grp = df.groupby(["player_id", "_opp_tier"])[stat]
    shifted = grp.shift(1)
    df["similar_def_avg"] = (shifted.groupby([df["player_id"], df["_opp_tier"]])
                                    .expanding().mean()
                                    .reset_index(level=[0, 1], drop=True))
    df["similar_def_n_games"] = (shifted.groupby([df["player_id"], df["_opp_tier"]])
                                        .expanding().count()
                                        .reset_index(level=[0, 1], drop=True))

    # ── Situation features ───────────────────────────────────────────
    #
    # For NFL nflverse data, `team` is the player's team and there's no
    # direct home/away column. We use the game_id shape "SEASON_WEEK_AWAY_HOME"
    # to infer if this player's team was home.
    def _infer_home(row) -> float:
        gid = row.get("game_id") or ""
        team = row.get("team") or ""
        if not (isinstance(gid, str) and team):
            return 0.5           # unknown → neutral
        parts = gid.split("_")
        if len(parts) >= 4:
            away, home = parts[-2], parts[-1]
            if team == home: return 1.0
            if team == away: return 0.0
        return 0.5

    df["is_home"] = df.apply(_infer_home, axis=1)

    # Rest days — weeks between consecutive appearances × 7. Bye = 14.
    df["_prev_week"] = df.groupby(["player_id", "season"])["week"].shift(1)
    df["rest_days_est"] = ((df["week"] - df["_prev_week"]) * 7.0).clip(upper=14).fillna(7.0)

    df["season_phase"]    = df["week"] / 22.0
    df["is_early_season"] = (df["week"] <= 4).astype(float)
    df["is_late_season"]  = (df["week"] >= 15).astype(float)
    df["is_playoffs"]     = (df.get("season_type", "REG") == "POST").astype(float) \
                              if "season_type" in df.columns else 0.0

    # Position one-hot (only when caller didn't already filter).
    if not position:
        for pos in ("QB", "RB", "WR", "TE"):
            df[f"pos_{pos}"] = (df["position"] == pos).astype(float)

    # ── Assemble feature matrix ──────────────────────────────────────
    feature_names = [
        "stat_last_3_avg", "stat_last_5_avg", "stat_last_10_avg",
        "stat_last_10_median", "stat_last_10_std", "stat_last_5_max",
        "stat_last_5_min", "stat_season_to_date_avg", "stat_last_10_cv",
        "stat_last_10_hit_avg", "volume_last_5_avg", "games_played_ytd",
        "opp_allowed_avg_l1_season", "opp_allowed_rank_l1",
        "opp_allowed_ytd", "similar_def_avg", "similar_def_n_games",
        "is_home", "rest_days_est", "season_phase",
        "is_early_season", "is_late_season", "is_playoffs",
    ]
    if not position:
        feature_names.extend([f"pos_{p}" for p in ("QB", "RB", "WR", "TE")])

    # Require enough prior data.
    df = df[df["games_played_ytd"] >= min_prior_games].copy()
    # Drop rows where every prior-based feature is NaN (deep cold-start).
    df = df.dropna(subset=["stat_last_5_avg", "stat_season_to_date_avg"], how="all")

    # Fill remaining NaNs with column median — GBM libraries handle NaN
    # natively but LightGBM's native handling is slightly better with
    # missing features. We still keep NaN in some columns intentionally.
    for c in feature_names:
        if c not in df.columns:
            df[c] = np.nan
    # opp_allowed features can be NaN in the first year — leave as NaN
    # so the model learns "unknown".

    X = df[feature_names].astype(float)
    y = df[stat].astype(float)
    meta = df[["season", "week", "player_id", "player_display_name",
               "team", "opponent_team", "position"]].reset_index(drop=True)
    X = X.reset_index(drop=True)
    y = y.reset_index(drop=True)

    return TrainingFrame(
        features=X, target=y, row_meta=meta,
        feature_names=feature_names, stat=stat, sport="NFL",
    )


# ═════════════════════════════════════════════════════════════════════
# Live-inference feature builder
# ═════════════════════════════════════════════════════════════════════
async def build_nfl_live_features(
    db,
    *,
    player_name: str,
    opponent_team: str,
    stat: str,
    position: Optional[str] = None,
    n_recent_seasons: int = 3,
) -> tuple[dict[str, float], list[str], dict[str, any]]:
    """Async live feature vector for the trained prediction engine.

    Pulls the player's last-N seasons of game logs + opponent's per-game
    allowance, then computes the SAME features used at training time
    for the *most recent* row (i.e. inference on the "next game").

    Returns
    ───────
      (feature_dict, feature_order, meta_dict)
      • feature_dict: name → float (NaN allowed, engine handles)
      • feature_order: list of feature names in canonical order
      • meta_dict: player_id, opponent_team, sport, stat, notes[]
    """
    from ml.feature_builder import _NFL_STAT_VOLUME_COL, _NFL_STAT_ELIGIBLE_POS  # self-ref safe

    notes: list[str] = []
    meta = {"player_name": player_name, "opponent_team": opponent_team,
             "stat": stat, "sport": "NFL", "notes": notes}

    # 1. Player rows — last N seasons.
    seasons_cursor = db.nfl_player_weekly.distinct("season")
    seasons_list = sorted(await seasons_cursor if hasattr(seasons_cursor, "__await__")
                          else seasons_cursor)
    latest = max(seasons_list) if seasons_list else 2025
    seasons_min = max(2019, latest - n_recent_seasons + 1)

    name_or = [{"player_display_name": player_name},
               {"player_name": player_name}]
    q = {
        "$and": [
            {"$or": name_or},
            {"season": {"$gte": seasons_min}},
        ]
    }
    proj = {"_id": 0, "player_id": 1, "player_display_name": 1, "team": 1,
             "opponent_team": 1, "season": 1, "week": 1, "position": 1,
             "game_id": 1, "season_type": 1}
    for col in (stat, "attempts", "carries", "targets"):
        proj[col] = 1
    rows = [r async for r in
             db.nfl_player_weekly.find(q, proj)
                                  .sort([("season", 1), ("week", 1)])]

    if not rows:
        notes.append("no player rows found")
        return _empty_feature_vec(stat, position), _nfl_feature_names(stat, position), meta

    df = pd.DataFrame(rows)
    if stat not in df.columns or df[stat].dropna().empty:
        notes.append(f"stat {stat} missing for player")
        return _empty_feature_vec(stat, position), _nfl_feature_names(stat, position), meta

    # 2. Build a synthetic "next game" row and prepend the target
    # opponent so the same feature pipeline computes the features
    # PRE-game. We do this by:
    #   a. Running the training frame builder against the historical
    #      rows to get rolling averages.
    #   b. Reading the LAST row's feature vector (that IS the "prior-
    #      to-next-game" state we want).
    tf = build_nfl_training_frame(
        df, stat=stat, position=position, min_prior_games=1,
        seasons_min=seasons_min,
    )
    if tf.features.empty:
        notes.append("training frame empty for this player")
        return _empty_feature_vec(stat, position), _nfl_feature_names(stat, position), meta

    last_row = tf.features.iloc[-1].to_dict()
    last_meta = tf.row_meta.iloc[-1].to_dict()
    meta["player_id"] = str(last_meta.get("player_id") or "")
    meta["last_game"] = {
        "season": int(last_meta.get("season") or 0),
        "week":   int(last_meta.get("week") or 0),
        "opponent": last_meta.get("opponent_team"),
    }

    # 3. Override the OPPONENT-specific features with the TARGET
    # opponent (not the last-played opponent).
    opp_up = (opponent_team or "").upper()
    opp_pipeline = [
        {"$match": {"opponent_team": opp_up,
                     "season": {"$gte": seasons_min}}},
        {"$group": {"_id": {"season": "$season", "week": "$week"},
                     "sum_stat": {"$sum": {"$ifNull": [f"${stat}", 0]}}}},
        {"$group": {"_id": "$_id.season",
                     "per_game_avg": {"$avg": "$sum_stat"},
                     "n_games":      {"$sum": 1}}},
        {"$sort": {"_id": -1}},
    ]
    per_season = [r async for r in db.nfl_player_weekly.aggregate(opp_pipeline,
                                                                    allowDiskUse=True)]
    if per_season:
        # Prior-season allowance = the most recent COMPLETED season.
        prior = per_season[0]  # already sorted desc
        last_row["opp_allowed_avg_l1_season"] = float(prior.get("per_game_avg") or np.nan)
        # For rank_l1, compute all opps' per-game avg for that season, rank.
        rank_pipeline = [
            {"$match": {"season": prior["_id"],
                         "opponent_team": {"$ne": None}}},
            {"$group": {"_id": {"opp": "$opponent_team",
                                 "week": "$week"},
                        "sum_stat": {"$sum": {"$ifNull": [f"${stat}", 0]}}}},
            {"$group": {"_id": "$_id.opp",
                        "per_game_avg": {"$avg": "$sum_stat"}}},
            {"$sort": {"per_game_avg": 1}},
        ]
        ranked = [r async for r in
                   db.nfl_player_weekly.aggregate(rank_pipeline, allowDiskUse=True)]
        rank = next((i + 1 for i, r in enumerate(ranked)
                     if r["_id"] == opp_up), None)
        last_row["opp_allowed_rank_l1"] = float(rank) if rank else np.nan
    else:
        notes.append(f"no prior data for opponent {opp_up}")

    # 4. Season-to-date allowance for target opp — use the latest season.
    ytd_pipeline = [
        {"$match": {"opponent_team": opp_up, "season": latest}},
        {"$group": {"_id": {"season": "$season", "week": "$week"},
                     "sum_stat": {"$sum": {"$ifNull": [f"${stat}", 0]}}}},
        {"$group": {"_id": None,
                     "per_game_avg": {"$avg": "$sum_stat"}}},
    ]
    ytd_agg = [r async for r in db.nfl_player_weekly.aggregate(ytd_pipeline,
                                                                allowDiskUse=True)]
    if ytd_agg:
        last_row["opp_allowed_ytd"] = float(ytd_agg[0].get("per_game_avg") or np.nan)

    return last_row, _nfl_feature_names(stat, position), meta


def _nfl_feature_names(stat: str, position: Optional[str]) -> list[str]:
    """Canonical feature order — must match training pipeline exactly."""
    base = [
        "stat_last_3_avg", "stat_last_5_avg", "stat_last_10_avg",
        "stat_last_10_median", "stat_last_10_std", "stat_last_5_max",
        "stat_last_5_min", "stat_season_to_date_avg", "stat_last_10_cv",
        "stat_last_10_hit_avg", "volume_last_5_avg", "games_played_ytd",
        "opp_allowed_avg_l1_season", "opp_allowed_rank_l1",
        "opp_allowed_ytd", "similar_def_avg", "similar_def_n_games",
        "is_home", "rest_days_est", "season_phase",
        "is_early_season", "is_late_season", "is_playoffs",
    ]
    if not position:
        base.extend([f"pos_{p}" for p in ("QB", "RB", "WR", "TE")])
    return base


def _empty_feature_vec(stat: str, position: Optional[str]) -> dict[str, float]:
    return {name: float("nan") for name in _nfl_feature_names(stat, position)}


__all__ = [
    "build_nfl_training_frame",
    "build_nfl_live_features",
    "TrainingFrame",
]
