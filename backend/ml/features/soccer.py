"""Soccer feature builder (2026-06 · Phase 7 Part 4).

Source
──────
`soccer_player_game_logs` — one row per (player, match) populated by
`ml.ingestors.soccer_understat`. Contains per-match: goals, assists,
shots, shots_on_target, key_passes, xg, xa, xg_chain, xg_buildup,
minutes, starts, is_home, opponent context (team_goals_conceded,
opponent_xg), position, red/yellow cards.

Supported stats (all Over/Under player props)
  • goals             — count per match
  • assists           — count per match
  • shots             — count per match
  • shots_on_target   — count per match
  • xg                — cumulative xG per match (regression proxy)
  • goal_contributions — goals + assists (composite)

Feature philosophy — mirrors NFL / MLB / NBA / Tennis pipelines:
  • Rolling last-3 / last-5 / last-10 average of TARGET stat.
  • Rolling median & std over last-10 for stability signal.
  • Availability: last-5 minutes average, last-10 starts rate.
  • Attacking form (own team): xg per match last-10.
  • Opponent defensive form: goals conceded / xg conceded last-10.
  • Home flag.
  • Days rest since last match.
  • Position encoding (F=3, MF=2, DF=1, GK=0).

No fake confidence:
  • If a player has < 3 prior matches → feature vector is NaN-heavy;
    live builder returns `notes=["insufficient prior matches"]` and
    the fusion layer safe-fails with `supported=False`.
  • No priors from position averages or league averages leak into
    per-player features (would inflate confidence).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger("lockscore.ml.features.soccer")


# ─────────────────────────────────────────────────────────────────────
# Stat catalogue — target column in `soccer_player_game_logs`
# ─────────────────────────────────────────────────────────────────────
_SOCCER_STAT_COLUMN: dict[str, str] = {
    "goals":               "goals",
    "assists":             "assists",
    "shots":               "shots",
    "shots_on_target":     "shots_on_target",
    "xg":                  "xg",
    "goal_contributions":  "_goal_contributions",
}


@dataclass
class TrainingFrame:
    features: pd.DataFrame
    target:   pd.Series
    row_meta: pd.DataFrame
    feature_names: list[str]
    stat:  str
    sport: str = "Soccer"


# ─────────────────────────────────────────────────────────────────────
# Feature schema (16 dims)
# ─────────────────────────────────────────────────────────────────────
def _feature_names() -> list[str]:
    return [
        # Rolling target stat
        "stat_last3_avg",           # rolling avg over prior 3 matches
        "stat_last5_avg",           # rolling avg over prior 5 matches
        "stat_last10_avg",          # rolling avg over prior 10 matches
        "stat_last10_median",
        "stat_last10_std",
        # Availability / role
        "minutes_last5_avg",
        "starts_rate_last10",
        # Attacking form
        "xg_last5_avg",
        "xa_last5_avg",
        "shots_per_start_last10",
        # Opponent defense (context of THIS match)
        "opp_goals_conceded_last10_avg",
        "opp_xg_conceded_last10_avg",
        # Team attacking strength (context of THIS match)
        "team_goals_scored_last10_avg",
        # Match context
        "is_home",
        "days_rest",
        "position_encoded",
    ]


# ─────────────────────────────────────────────────────────────────────
# Position encoding
# ─────────────────────────────────────────────────────────────────────
_POS_ENCODE: dict[str, float] = {
    "FW": 3.0, "F": 3.0, "S": 3.0, "SS": 3.0,
    "AMR": 2.5, "AML": 2.5, "AMC": 2.5,
    "MF": 2.0, "M": 2.0, "MR": 2.0, "ML": 2.0, "MC": 2.0,
    "DMC": 1.5, "DMR": 1.5, "DML": 1.5, "DM": 1.5,
    "DF": 1.0, "D": 1.0, "DR": 1.0, "DL": 1.0, "DC": 1.0,
    "GK": 0.0, "G": 0.0,
    "SUB": 2.0,        # sub — neutral default
}


def _encode_position(pos: Any) -> float:
    if pos is None:
        return 2.0
    p = str(pos).strip().upper()
    if not p:
        return 2.0
    if p in _POS_ENCODE:
        return _POS_ENCODE[p]
    # Fallback: match on first two letters (e.g. "AMR" already handled)
    return _POS_ENCODE.get(p[:2], 2.0)


# ─────────────────────────────────────────────────────────────────────
# TRAINING FRAME — build one row per (player, match)
# ─────────────────────────────────────────────────────────────────────
def build_soccer_training_frame(
    rows_df: pd.DataFrame,
    stat: str,
    min_prior_matches: int = 3,
) -> TrainingFrame:
    """Build a per-match training frame with rolling-window features.

    `rows_df` must be a DataFrame of `soccer_player_game_logs` docs.
    The frame is chronologically sorted per-player then a **prior-only
    rolling window** produces features that are strictly leak-free
    (each row's features use only matches BEFORE that row's date).
    """
    empty = TrainingFrame(pd.DataFrame(), pd.Series(dtype=float),
                          pd.DataFrame(), _feature_names(), stat, "Soccer")
    if not isinstance(rows_df, pd.DataFrame) or rows_df.empty:
        return empty
    df = rows_df.copy()
    # Composite target.
    if stat == "goal_contributions":
        df["_goal_contributions"] = (df.get("goals", 0).fillna(0)
                                      + df.get("assists", 0).fillna(0))
    target_col = _SOCCER_STAT_COLUMN.get(stat, stat)
    if target_col not in df.columns:
        return empty

    # Ensure required numeric columns exist.
    for c in ("goals", "assists", "shots", "shots_on_target", "xg", "xa",
              "minutes", "starts", "team_goals_scored",
              "team_goals_conceded", "opponent_xg"):
        if c not in df.columns:
            df[c] = np.nan

    # Parse match_date and sort by (player_id, match_date).
    df["match_date_parsed"] = pd.to_datetime(df["match_date"], errors="coerce")
    df = df.sort_values(["player_id", "match_date_parsed"]).reset_index(drop=True)

    # ─── Build player-level rolling features (shift 1 → strictly prior) ──
    g_player = df.groupby("player_id", sort=False)

    def _prior_roll(col: str, window: int, agg: str = "mean") -> pd.Series:
        s = df[col].astype(float)
        rolled = g_player[col].apply(
            lambda x: x.astype(float)
                       .shift(1)
                       .rolling(window, min_periods=1)
                       .agg(agg)
        )
        # `apply` returns a MultiIndex; drop the outer level.
        if isinstance(rolled.index, pd.MultiIndex):
            rolled = rolled.reset_index(level=0, drop=True)
        return rolled.astype(float)

    df["stat_last3_avg"]        = _prior_roll(target_col, 3)
    df["stat_last5_avg"]        = _prior_roll(target_col, 5)
    df["stat_last10_avg"]       = _prior_roll(target_col, 10)
    df["stat_last10_median"]    = _prior_roll(target_col, 10, "median")
    df["stat_last10_std"]       = _prior_roll(target_col, 10, "std")
    df["minutes_last5_avg"]     = _prior_roll("minutes", 5)
    df["starts_rate_last10"]    = _prior_roll("starts", 10)
    df["xg_last5_avg"]          = _prior_roll("xg", 5)
    df["xa_last5_avg"]          = _prior_roll("xa", 5)
    # Shots per start (prior-10 shots / max(1, prior-10 starts))
    shots_l10 = _prior_roll("shots", 10, "sum")
    starts_l10 = _prior_roll("starts", 10, "sum").fillna(0)
    df["shots_per_start_last10"] = shots_l10 / starts_l10.clip(lower=1)

    # ─── Opponent-team defensive form (prior 10 matches at OPPONENT level).
    # Aggregate at (team_id, date) — team_goals_conceded per match.
    team_match = df.groupby(["team_id", "match_date_parsed"], as_index=False) \
                    .agg({"team_goals_conceded": "first",
                           "opponent_xg": "first"})
    team_match = team_match.sort_values(["team_id", "match_date_parsed"])
    team_match["team_gc_l10"] = team_match.groupby("team_id")["team_goals_conceded"] \
        .apply(lambda x: x.shift(1).rolling(10, min_periods=1).mean()) \
        .reset_index(level=0, drop=True)
    team_match["team_oppxg_l10"] = team_match.groupby("team_id")["opponent_xg"] \
        .apply(lambda x: x.shift(1).rolling(10, min_periods=1).mean()) \
        .reset_index(level=0, drop=True)
    # Team attacking form.
    team_match_scored = df.groupby(["team_id", "match_date_parsed"], as_index=False) \
                          .agg({"team_goals_scored": "first"})
    team_match_scored = team_match_scored.sort_values(["team_id", "match_date_parsed"])
    team_match_scored["team_gs_l10"] = team_match_scored.groupby("team_id")["team_goals_scored"] \
        .apply(lambda x: x.shift(1).rolling(10, min_periods=1).mean()) \
        .reset_index(level=0, drop=True)

    # Merge opponent defence (by OPPONENT team_id at same date).
    opp_view = team_match.rename(columns={
        "team_id": "opponent_team_id",
        "team_gc_l10": "opp_goals_conceded_last10_avg",
        "team_oppxg_l10": "opp_xg_conceded_last10_avg",
    })[["opponent_team_id", "match_date_parsed",
        "opp_goals_conceded_last10_avg", "opp_xg_conceded_last10_avg"]]
    df = df.merge(opp_view, on=["opponent_team_id", "match_date_parsed"],
                   how="left")

    own_view = team_match_scored.rename(columns={
        "team_gs_l10": "team_goals_scored_last10_avg",
    })[["team_id", "match_date_parsed", "team_goals_scored_last10_avg"]]
    df = df.merge(own_view, on=["team_id", "match_date_parsed"], how="left")

    # ─── Home flag + days-rest + position.
    df["is_home"] = df["is_home"].astype(float)
    df["days_rest"] = g_player["match_date_parsed"].apply(
        lambda x: x.diff().dt.days
    ).reset_index(level=0, drop=True)
    df["position_encoded"] = df["position"].apply(_encode_position) \
        if "position" in df.columns else 2.0

    # ─── Drop rows without enough prior history.
    prior_count = g_player.cumcount()
    df = df[prior_count >= int(min_prior_matches)].copy()
    if df.empty:
        return empty

    feature_names = _feature_names()
    for c in feature_names:
        if c not in df.columns:
            df[c] = np.nan
    X = df[feature_names].astype(float).reset_index(drop=True)
    y = df[target_col].astype(float).reset_index(drop=True)
    meta_cols = [c for c in
                 ("player_id", "player_name", "match_id", "match_date",
                  "team_name", "opponent_team_name", "league", "season")
                 if c in df.columns]
    meta = df[meta_cols].reset_index(drop=True)
    return TrainingFrame(features=X, target=y, row_meta=meta,
                          feature_names=feature_names,
                          stat=stat, sport="Soccer")


# ─────────────────────────────────────────────────────────────────────
# LIVE FEATURES — compute a single row for prediction
# ─────────────────────────────────────────────────────────────────────
async def _fetch_recent_matches(
    db, *, player_id: Optional[str] = None,
    name_canonical: Optional[str] = None,
    limit: int = 30,
) -> list[dict]:
    query: dict[str, Any] = {}
    if player_id:
        query["player_id"] = str(player_id)
    elif name_canonical:
        query["name_canonical"] = name_canonical
    else:
        return []
    cursor = db.soccer_player_game_logs.find(
        query, {"_id": 0}
    ).sort("match_date", -1).limit(limit)
    return [d async for d in cursor]


def _canonicalize_name(name: str) -> str:
    import re, unicodedata
    if not name:
        return ""
    nfkd = unicodedata.normalize("NFKD", name)
    d = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"\s+", " ",
                   re.sub(r"[\.\-'\"\u2019]", "", d).strip().lower())


async def build_soccer_live_features(
    db, *, player_name: str,
    opponent_team: Optional[str] = None,
    stat: str,
    is_home: Optional[bool] = None,
) -> tuple[dict[str, float], list[str], dict]:
    """Return (feature_vec, feature_names, meta_and_notes).

    Fails safely with `notes=["insufficient prior matches"]` when the
    player has fewer than 3 completed matches in `soccer_player_game_logs`.
    """
    feats = _feature_names()
    nan_vec = {n: float("nan") for n in feats}
    notes: list[str] = []
    meta: dict[str, Any] = {
        "sport":         "Soccer",
        "stat":          stat,
        "opponent":      opponent_team,
        "player_name":   player_name,
        "notes":         notes,
    }
    if stat not in _SOCCER_STAT_COLUMN:
        notes.append(f"unsupported stat: {stat}")
        return nan_vec, feats, meta

    name_c = _canonicalize_name(player_name)
    matches = await _fetch_recent_matches(db, name_canonical=name_c, limit=30)
    if len(matches) < 3:
        notes.append("insufficient prior matches (< 3)")
        return nan_vec, feats, meta

    df = pd.DataFrame(matches)
    if stat == "goal_contributions":
        df["_goal_contributions"] = (df.get("goals", 0).fillna(0)
                                      + df.get("assists", 0).fillna(0))
    target_col = _SOCCER_STAT_COLUMN.get(stat, stat)
    if target_col not in df.columns:
        notes.append("target column missing in logs")
        return nan_vec, feats, meta

    df["match_date_parsed"] = pd.to_datetime(df["match_date"], errors="coerce")
    df = df.sort_values("match_date_parsed").reset_index(drop=True)

    # Rolling helpers on the CHRONOLOGICAL window (ALL rows here are
    # prior — we're computing at "now" (before this match), no shift needed).
    def _tail_agg(col: str, n: int, fn: str = "mean") -> float:
        s = df[col].astype(float).dropna().tail(n)
        if s.empty:
            return float("nan")
        if fn == "mean":   return float(s.mean())
        if fn == "median": return float(s.median())
        if fn == "std":    return float(s.std(ddof=0)) if len(s) > 1 else 0.0
        if fn == "sum":    return float(s.sum())
        return float("nan")

    vec = dict(nan_vec)
    vec["stat_last3_avg"]     = _tail_agg(target_col, 3)
    vec["stat_last5_avg"]     = _tail_agg(target_col, 5)
    vec["stat_last10_avg"]    = _tail_agg(target_col, 10)
    vec["stat_last10_median"] = _tail_agg(target_col, 10, "median")
    vec["stat_last10_std"]    = _tail_agg(target_col, 10, "std")
    vec["minutes_last5_avg"]  = _tail_agg("minutes", 5)
    vec["starts_rate_last10"] = _tail_agg("starts", 10)
    vec["xg_last5_avg"]       = _tail_agg("xg", 5)
    vec["xa_last5_avg"]       = _tail_agg("xa", 5)
    shots_sum = _tail_agg("shots", 10, "sum")
    starts_sum = _tail_agg("starts", 10, "sum")
    vec["shots_per_start_last10"] = (shots_sum / max(1.0, starts_sum)
                                      if not np.isnan(shots_sum) else float("nan"))

    # Opponent defense — pull opponent's last-10 goals conceded.
    if opponent_team:
        opp_matches = await db.soccer_player_game_logs.find(
            {"team_name": opponent_team},
            {"_id": 0, "match_id": 1, "match_date": 1,
             "team_goals_conceded": 1, "opponent_xg": 1},
        ).sort("match_date", -1).limit(30).to_list(30)
        if opp_matches:
            # Deduplicate to one row per match.
            seen: set = set()
            unique: list[dict] = []
            for om in opp_matches:
                mid = om.get("match_id")
                if mid and mid not in seen:
                    seen.add(mid)
                    unique.append(om)
            gc_vals = [o["team_goals_conceded"] for o in unique[:10]
                       if o.get("team_goals_conceded") is not None]
            oxg_vals = [o["opponent_xg"] for o in unique[:10]
                        if o.get("opponent_xg") is not None]
            vec["opp_goals_conceded_last10_avg"] = (
                float(np.mean(gc_vals)) if gc_vals else float("nan"))
            vec["opp_xg_conceded_last10_avg"] = (
                float(np.mean(oxg_vals)) if oxg_vals else float("nan"))
        else:
            notes.append(f"no opponent logs for team '{opponent_team}'")

    # Team attacking form — last-10 team_goals_scored (use player's own team).
    team_id = df.iloc[-1].get("team_id") if "team_id" in df.columns else None
    if team_id:
        own_matches = await db.soccer_player_game_logs.find(
            {"team_id": team_id},
            {"_id": 0, "match_id": 1, "match_date": 1,
             "team_goals_scored": 1},
        ).sort("match_date", -1).limit(30).to_list(30)
        seen2: set = set()
        gs_vals: list[float] = []
        for om in own_matches:
            mid = om.get("match_id")
            if mid and mid not in seen2:
                seen2.add(mid)
                v = om.get("team_goals_scored")
                if v is not None:
                    gs_vals.append(float(v))
            if len(gs_vals) >= 10:
                break
        vec["team_goals_scored_last10_avg"] = (
            float(np.mean(gs_vals)) if gs_vals else float("nan"))

    vec["is_home"] = 1.0 if is_home is True else (
        0.0 if is_home is False else float("nan"))
    # Days rest since last match.
    dates_sorted = df["match_date_parsed"].dropna().sort_values()
    if len(dates_sorted) >= 1:
        last_date = dates_sorted.iloc[-1]
        vec["days_rest"] = float((pd.Timestamp.now(tz="UTC").tz_localize(None)
                                    - last_date).days) if not pd.isna(last_date) else float("nan")
    # Position — take most recent non-Sub position, else last row's.
    pos_last = None
    for p in reversed(df["position"].tolist() if "position" in df.columns else []):
        if p and str(p).strip().upper() != "SUB":
            pos_last = p
            break
    if pos_last is None and "position" in df.columns and not df.empty:
        pos_last = df["position"].iloc[-1]
    vec["position_encoded"] = _encode_position(pos_last)

    meta["prior_matches"] = int(len(df))
    return vec, feats, meta


__all__ = [
    "build_soccer_training_frame",
    "build_soccer_live_features",
    "TrainingFrame",
]
