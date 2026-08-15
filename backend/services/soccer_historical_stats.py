"""Historical stats aggregator — Phase 2A.5D CLOSURE (2026-08).

DELTA-ONLY reuse layer over the existing `soccer_player_game_logs` and
`mls_player_matchup_history` collections.  Does NOT ingest new data;
does NOT rebuild player identity; does NOT modify existing storage.

For a player+season, aggregate their per-game log rows into a
season-level form dict compatible with the input contract of
`services.soccer_scorer_bridge.compute_soccer_scorer_factors_sync`
(``prior_form_row=...``).

For a player+opponent, return the pre-computed H2H record from
`mls_player_matchup_history` (existing store) with sample-size-aware
shrinkage.  Missing H2H is NEUTRAL, never negative.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("lockscore.soccer_historical_stats")


# H2H shrinkage — small samples must not dominate.
H2H_SHRINKAGE_MATCHES = 5
NEUTRAL_H2H_GOAL_RATE = 0.35     # league-avg-ish goals-per-match for an attacker


async def aggregate_player_season(
    db, *, player_name_canonical: str, season: str,
) -> Optional[dict[str, Any]]:
    """Aggregate `soccer_player_game_logs` into a season form dict.

    Returns None if no rows exist for that player+season.  Never fabricates.
    """
    if not player_name_canonical or not season:
        return None
    # `season` in `soccer_player_game_logs` is stored as int OR string.
    # Query for both to be safe.
    try:
        _s_int = int(season) if "-" not in str(season) else None
    except Exception:
        _s_int = None
    q_or: list[dict] = [{"season": season}]
    if _s_int is not None:
        q_or.append({"season": _s_int})
    q = {
        "name_canonical": player_name_canonical.lower(),
        "$or": q_or,
    }
    cursor = db.soccer_player_game_logs.find(q)
    minutes = games = starts = 0
    goals = xg = xa = shots = sot = assists = 0.0
    team = None
    league = None
    async for r in cursor:
        minutes += int(r.get("minutes") or 0)
        games += 1
        starts += int(r.get("starts") or 0) if isinstance(r.get("starts"), (int, bool)) else 0
        goals += float(r.get("goals") or 0)
        xg += float(r.get("xg") or 0)
        xa += float(r.get("xa") or 0)
        shots += float(r.get("shots") or 0)
        sot += float(r.get("shots_on_target") or 0)
        assists += float(r.get("assists") or 0)
        if not team:
            team = r.get("team_name")
        if not league:
            league = r.get("league")
    if games == 0:
        return None
    return {
        "name_canonical": player_name_canonical.lower(),
        "season": str(season),
        "minutes": minutes,
        "games": games,
        "starts": starts,
        "goals": round(goals, 3),
        "xg": round(xg, 3),
        "xa": round(xa, 3),
        "shots": round(shots, 3),
        "sot": round(sot, 3),
        "assists": round(assists, 3),
        "team": team,
        "league": league,
        "source": "soccer_player_game_logs_agg_v1",
    }


async def load_prior_season_form_row(
    db, *, player_name_canonical: str, competition: str,
) -> Optional[dict]:
    """Convenience: resolve prior season for competition + aggregate."""
    try:
        from services.soccer_season_resolver import resolve_prior_season
        prior = resolve_prior_season(competition)
    except Exception:
        return None
    return await aggregate_player_season(
        db, player_name_canonical=player_name_canonical, season=prior)


async def load_player_h2h(
    db, *, player_name: str, opponent_team_name: Optional[str] = None,
    opponent_team_id: Optional[str] = None,
) -> Optional[dict]:
    """Return the sample-shrunk H2H contribution for a player vs opponent.

    Reuses the EXISTING `mls_player_matchup_history` store.  Small
    samples are heavily shrunk; missing H2H returns None (treated as
    NEUTRAL by callers).
    """
    if not player_name:
        return None
    q = {"player_name_norm": player_name.lower().strip()}
    doc = await db.mls_player_matchup_history.find_one(q)
    if not doc:
        return None
    by_opp = doc.get("by_opponent") or []
    row: Optional[dict] = None
    for r in by_opp:
        name = str(r.get("opponent_name") or "").lower()
        _id = str(r.get("opponent_id") or "")
        if opponent_team_name and name == opponent_team_name.lower():
            row = r
            break
        if opponent_team_id and _id == str(opponent_team_id):
            row = r
            break
    if not row:
        return None
    matches = int(row.get("matches") or 0)
    goals = float(row.get("goals") or 0)
    assists = float(row.get("assists") or 0)
    shots = float(row.get("shots") or 0)
    if matches <= 0:
        return None
    # Sample-shrunk goal + assist rate per match.
    raw_gpm = goals / matches
    raw_apm = assists / matches
    w = matches / (matches + H2H_SHRINKAGE_MATCHES)
    shrunk_gpm = w * raw_gpm + (1.0 - w) * NEUTRAL_H2H_GOAL_RATE
    shrunk_apm = w * raw_apm + (1.0 - w) * NEUTRAL_H2H_GOAL_RATE / 2.0
    return {
        "player_name": player_name,
        "opponent": row.get("opponent_name"),
        "matches": matches,
        "goals": goals,
        "assists": assists,
        "shots": shots,
        "raw_goals_per_match": round(raw_gpm, 4),
        "shrunk_goals_per_match": round(shrunk_gpm, 4),
        "shrunk_assists_per_match": round(shrunk_apm, 4),
        "sample_weight": round(w, 3),
        "provenance": "PLAYER_H2H",
        "source": "mls_player_matchup_history",
    }


__all__ = [
    "aggregate_player_season",
    "load_prior_season_form_row",
    "load_player_h2h",
    "H2H_SHRINKAGE_MATCHES",
    "NEUTRAL_H2H_GOAL_RATE",
]
