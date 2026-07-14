"""Tennis multi-source ingest — Phase 3 of the data-gap roadmap.

Primary source: Tennismylife's TML-Database on GitHub — a re-scrape of
Jeff Sackmann's tennis_atp/tennis_wta format with 1968-2025 coverage
(Sackmann's original repos were removed from GitHub in 2025). Files:
    https://raw.githubusercontent.com/Tennismylife/TML-Database/master/YYYY.csv

Schema per row (Sackmann-compatible):
    tourney_id, tourney_name, surface, draw_size, tourney_level, indoor,
    tourney_date, match_num,
    winner_id, winner_name, winner_hand, winner_ht, winner_ioc,
    winner_age, winner_rank, winner_rank_points,
    loser_* (same),
    score, best_of, round, minutes,
    w_ace, w_df, w_svpt, w_1stIn, w_1stWon, w_2ndWon, w_SvGms,
    w_bpSaved, w_bpFaced,
    l_* (same),
    retirement (RET), walkover (W-O)

Storage:
    tennis_matches_history — one doc per historical match
    tennis_player_stats    — per-player rolling aggregates (52-week window)

Public API:
    from services.tennis import (
        refresh_tennis_history,     # bulk ingest
        get_player_stats,           # lookup rolling averages
        get_h2h,                    # career + surface-specific
        get_recent_matches,         # last N matches for a player
    )
"""
from services.tennis.fallback import (
    get_h2h,
    get_player_stats,
    get_recent_matches,
    refresh_tennis_history,
)

__all__ = [
    "refresh_tennis_history",
    "get_player_stats",
    "get_h2h",
    "get_recent_matches",
]
