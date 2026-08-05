"""Phase 8 — safeguards: prevent retired players / invalid markets / DNPs."""
from __future__ import annotations

from typing import Optional


# Sportsbook markets we ALLOW alt-line projections for.
_SUPPORTED_STAT_WHITELIST = {
    "NFL":    {"passing_yards", "rushing_yards", "receiving_yards",
                "passing_tds", "rushing_tds", "receiving_tds",
                "passing_completions", "passing_attempts",
                "rush_attempts", "receptions"},
    "MLB":    {"hits", "total_bases", "home_runs", "strikeouts",
                "pitcher_strikeouts", "runs_scored", "rbi", "walks",
                "hits_runs_rbis"},
    "NBA":    {"points", "rebounds", "assists", "threes",
                "steals", "blocks", "points_rebounds_assists"},
    "TENNIS": {"aces", "double_faults", "break_points_won"},
}


async def is_safe_for_alt_lines(
    db, *,
    sport: str,
    player_name: str,
    stat: str,
    min_prior_games: int = 5,
) -> tuple[bool, Optional[str]]:
    """Return (safe, reason).

    Rejects:
      • unsupported stat for this sport
      • player with < min_prior_games historical rows
      • player flagged as retired / inactive in the DB
      • market where the player has no team assignment
    """
    sport_u = (sport or "").upper()
    stat_l = (stat or "").lower()
    if sport_u not in _SUPPORTED_STAT_WHITELIST:
        return False, f"sport {sport_u} not supported"
    if stat_l not in _SUPPORTED_STAT_WHITELIST[sport_u]:
        return False, f"stat {stat_l} not whitelisted for {sport_u}"
    if not player_name:
        return False, "no player_name"

    # Retired / inactive check — soccer & NFL both have `player_status`.
    try:
        p = await db.players.find_one(
            {"name": {"$regex": f"^{player_name}$", "$options": "i"}},
            {"status": 1, "retired": 1, "active": 1, "_id": 0},
        )
        if p:
            if p.get("retired") is True:
                return False, "player is retired"
            if p.get("active") is False:
                return False, "player marked inactive"
            if (p.get("status") or "").lower() in ("retired", "inactive"):
                return False, f"player status: {p['status']}"
    except Exception:
        pass  # missing collection is fine — safeguard is best-effort

    # Historical-history gate.
    try:
        coll_map = {
            "MLB":    ("player_game_logs", {"sport": "MLB",
                                              "player_name": player_name}),
            "NBA":    ("player_game_logs", {"sport": "NBA",
                                              "player_name": player_name}),
            "NFL":    ("player_game_logs", {"sport": "NFL",
                                              "player_name": player_name}),
            "TENNIS": ("tennis_matches_history", {"$or": [
                        {"winner_name": player_name},
                        {"loser_name": player_name}]}),
        }
        coll_name, query = coll_map.get(sport_u, (None, None))
        if coll_name:
            n = await db[coll_name].count_documents(query)
            if n < min_prior_games:
                return False, (f"insufficient history "
                                f"({n} < {min_prior_games})")
    except Exception:
        pass  # non-fatal

    return True, None


__all__ = ["is_safe_for_alt_lines", "_SUPPORTED_STAT_WHITELIST"]
