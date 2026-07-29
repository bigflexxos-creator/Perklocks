"""Soccer feature builder (2026-07-28) — SCAFFOLD.

⚠️  DATA CAVEAT: Soccer per-game logs are not stored in
`player_game_logs`. Available signal:
  • `soccer_player_form` (2774 rows) — SEASON aggregates only:
    goals, assists, shots, key_passes, npxg, goals_over_xg, minutes,
    goals_per_90, shots_per_90.
  • `mls_player_matchup_history` (81 rows) — MLS only.

Training on season aggregates is fundamentally weaker than per-game
data (no rolling recency, no matchup-specific splits, no home/away).
This module trains on the season-level target using recent-season
priors as pregame features — a coarse baseline that will improve
dramatically when per-match logs are ingested.

Supported stats (season totals)
  • goals, assists, shots, shots_on_target, goal_contributions
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd


_SOCCER_STAT_COLUMN: dict[str, str] = {
    "goals":               "goals",
    "assists":             "assists",
    "shots":               "shots",
    "shots_on_target":     "shots",   # proxy — real SOT not stored
    "goal_contributions":  "_goal_contributions",   # composite
}


@dataclass
class TrainingFrame:
    features: pd.DataFrame
    target:   pd.Series
    row_meta: pd.DataFrame
    feature_names: list[str]
    stat:  str
    sport: str = "Soccer"


def _feature_names() -> list[str]:
    return [
        "prior_season_avg", "prior_season_per90",
        "minutes_prior_season", "shots_per90_prior",
        "npxg_per90_prior",
        "position_encoded",
    ]


def build_soccer_training_frame(
    rows_df: pd.DataFrame,
    stat: str,
    min_prior_seasons: int = 1,
) -> TrainingFrame:
    if not isinstance(rows_df, pd.DataFrame) or rows_df.empty:
        return TrainingFrame(pd.DataFrame(), pd.Series(dtype=float),
                              pd.DataFrame(), _feature_names(),
                              stat, "Soccer")
    df = rows_df.copy()
    if stat == "goal_contributions" and "_goal_contributions" not in df.columns:
        df["_goal_contributions"] = df.get("goals", 0).fillna(0) \
                                     + df.get("assists", 0).fillna(0)
    target_col = _SOCCER_STAT_COLUMN.get(stat, stat)
    if target_col not in df.columns or df[target_col].dropna().empty:
        return TrainingFrame(pd.DataFrame(), pd.Series(dtype=float),
                              pd.DataFrame(), _feature_names(),
                              stat, "Soccer")
    # Pair each (player, season) row with their PRIOR (player, season-1)
    # row so we get prior-season features → current-season target.
    df = df.sort_values(["name_canonical", "season"]).reset_index(drop=True)
    df["prior_season_avg"] = df.groupby("name_canonical")[target_col].shift(1)
    df["prior_season_per90"] = df.groupby("name_canonical").shift(1) \
        .apply(lambda r: r.get(target_col, np.nan) / max(r.get("minutes", 90) / 90.0, 0.5)
                if not pd.isna(r.get(target_col, np.nan)) else np.nan, axis=1) \
        if False else df.groupby("name_canonical")[target_col].shift(1)
    # (Kept simple — the `apply` above would be per-row; using shifted target
    #  as a stand-in per90 baseline.)
    df["minutes_prior_season"] = df.groupby("name_canonical")["minutes"].shift(1)
    df["shots_per90_prior"] = df.groupby("name_canonical")["shots_per_90"].shift(1) \
        if "shots_per_90" in df.columns else np.nan
    df["npxg_per90_prior"] = df.groupby("name_canonical")["npxg_per_90"].shift(1) \
        if "npxg_per_90" in df.columns else np.nan
    # Position encoded: F=3, M=2, D=1, G=0
    pos_map = {"F": 3.0, "FW": 3.0, "AM": 2.5, "M": 2.0, "MF": 2.0,
                "D": 1.0, "DF": 1.0, "G": 0.0, "GK": 0.0}
    df["position_encoded"] = df.get("position", "").astype(str).str.upper() \
                              .map(pos_map).fillna(2.0)

    df = df.dropna(subset=["prior_season_avg"]).copy()
    if df.empty:
        return TrainingFrame(pd.DataFrame(), pd.Series(dtype=float),
                              pd.DataFrame(), _feature_names(),
                              stat, "Soccer")

    feature_names = _feature_names()
    for c in feature_names:
        if c not in df.columns:
            df[c] = np.nan
    X = df[feature_names].astype(float).reset_index(drop=True)
    y = df[target_col].astype(float).reset_index(drop=True)
    meta = df[["name_canonical", "season", "team", "league"]] \
             .reset_index(drop=True) \
             if "team" in df.columns else \
             df[["name_canonical", "season"]].reset_index(drop=True)
    return TrainingFrame(features=X, target=y, row_meta=meta,
                          feature_names=feature_names, stat=stat, sport="Soccer")


async def build_soccer_live_features(
    db, *, player_name: str, opponent_team: Optional[str] = None,
    stat: str,
) -> tuple[dict[str, float], list[str], dict]:
    notes: list[str] = []
    meta = {"sport": "Soccer", "stat": stat, "opponent": opponent_team,
             "player_name": player_name, "notes": notes}
    doc = await db.soccer_player_form.find_one(
        {"$or": [{"player_name": player_name},
                  {"name_canonical": player_name.lower()}]},
        {"_id": 0},
    )
    if not doc:
        notes.append("no soccer_player_form row for player")
        return {n: float("nan") for n in _feature_names()}, _feature_names(), meta
    stat_col = _SOCCER_STAT_COLUMN.get(stat, stat)
    prior_avg = doc.get(stat_col) or 0.0
    if stat == "goal_contributions":
        prior_avg = (doc.get("goals") or 0) + (doc.get("assists") or 0)
    vec = {
        "prior_season_avg":       float(prior_avg or 0.0),
        "prior_season_per90":     float(doc.get(f"{stat_col}_per_90")
                                        or doc.get("goals_per_90") or 0.0),
        "minutes_prior_season":   float(doc.get("minutes") or 0.0),
        "shots_per90_prior":      float(doc.get("shots_per_90") or 0.0),
        "npxg_per90_prior":       float(doc.get("npxg_per_90") or 0.0),
        "position_encoded":       2.0,
    }
    return vec, _feature_names(), meta


__all__ = [
    "build_soccer_training_frame",
    "build_soccer_live_features",
    "TrainingFrame",
]
