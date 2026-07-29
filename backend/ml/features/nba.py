"""NBA feature builder (2026-07-28) — SCAFFOLD ONLY.

⚠️  DATA GAP:  `player_game_logs` has 0 rows for sport='nba' in the
current DB. Training therefore CANNOT run yet. This module exists so
that once historical NBA game logs are ingested (via `historical/nba.py`
or a similar backfill), the same architecture that trained NFL/MLB/
Tennis models is ready to consume them.

Supported stats (all scaffolded, will train when data lands)
  • points, rebounds, assists, pra, threes_made, steals, blocks

Feature blueprint (same layout as NFL/MLB, adapted for NBA)
  PLAYER
    stat_last_5_avg, stat_last_10_avg, stat_season_to_date_avg,
    stat_last_10_median, stat_last_10_cv, minutes_last_5_avg
  OPPONENT
    opp_allowed_avg_l1_season, opp_pace_rank, opp_def_rating_l10
  SITUATION
    is_home, rest_days_est, is_b2b, season_phase

Interface identical to `ml.features.mlb`. Public API:
    build_nba_training_frame(rows_df, stat, ...)
    build_nba_live_features(db, ...)
Both return `TrainingFrame` / empty payload when data is absent.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd


_NBA_STAT_ELIGIBLE = ("points", "rebounds", "assists", "pra",
                       "threes_made", "steals", "blocks")


@dataclass
class TrainingFrame:
    features: pd.DataFrame
    target:   pd.Series
    row_meta: pd.DataFrame
    feature_names: list[str]
    stat:  str
    sport: str = "NBA"


def _feature_names() -> list[str]:
    return [
        "stat_last_3_avg", "stat_last_5_avg", "stat_last_10_avg",
        "stat_season_to_date_avg", "stat_last_10_median",
        "stat_last_10_cv", "stat_last_10_std",
        "minutes_last_5_avg", "opp_allowed_avg_l1_season",
        "opp_pace_rank", "opp_def_rating_l10",
        "is_home", "rest_days_est", "is_b2b", "season_phase",
    ]


def build_nba_training_frame(
    rows_df: pd.DataFrame,
    stat: str,
    min_prior_games: int = 5,
) -> TrainingFrame:
    """Build NBA training frame. Returns empty frame when no data,
    with `feature_names` populated so downstream save/load logic works
    once data is ingested."""
    if not isinstance(rows_df, pd.DataFrame) or rows_df.empty:
        return TrainingFrame(pd.DataFrame(), pd.Series(dtype=float),
                              pd.DataFrame(), _feature_names(),
                              stat, "NBA")
    if stat not in _NBA_STAT_ELIGIBLE and stat + "s_made" not in _NBA_STAT_ELIGIBLE:
        return TrainingFrame(pd.DataFrame(), pd.Series(dtype=float),
                              pd.DataFrame(), _feature_names(),
                              stat, "NBA")
    # PRA composite (points + rebounds + assists).
    df = rows_df.copy()
    if stat == "pra" and "pra" not in df.columns:
        df["pra"] = (df.get("points", 0).fillna(0)
                     + df.get("rebounds", 0).fillna(0)
                     + df.get("assists", 0).fillna(0))
    if stat not in df.columns or df[stat].dropna().empty:
        return TrainingFrame(pd.DataFrame(), pd.Series(dtype=float),
                              pd.DataFrame(), _feature_names(),
                              stat, "NBA")
    # Real feature engineering would go here once the source table
    # exists. Punt with an empty frame so the trainer prints a clear
    # message and skips.
    return TrainingFrame(pd.DataFrame(), pd.Series(dtype=float),
                          pd.DataFrame(), _feature_names(),
                          stat, "NBA")


async def build_nba_live_features(
    db, *, player_name: str, opponent_team: Optional[str] = None,
    stat: str,
) -> tuple[dict[str, float], list[str], dict]:
    return (
        {n: float("nan") for n in _feature_names()},
        _feature_names(),
        {"sport": "NBA", "stat": stat, "opponent": opponent_team,
          "player_name": player_name,
          "notes": ["NBA game-log ingest pending — module scaffolded only"]},
    )


__all__ = [
    "build_nba_training_frame",
    "build_nba_live_features",
    "TrainingFrame",
]
