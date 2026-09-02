"""Sport Model Authority Registry — Phase 5.

ONE canonical live authority PER (sport, market_family) — with
specialized engines preserved where they legitimately outperform
the generic per-sport model.  Different market families in the SAME
sport CAN and DO have different authorities (e.g. MLB pitcher
strikeouts uses `mlb_stuff_plus_k_model`, MLB game totals use
`mlb_shared_run_distribution_v1` — both are canonical for THEIR
family).

Rules encoded here:

  * `AUTHORITY[sport][market_family]` -> one canonical model tag
    (a `model_source` string a producer stamps on the pick).
  * A market family MAY have `preserved_specialists` — additional
    canonical tags accepted for narrower sub-families (e.g. NFL
    Platinum player-prop authority sits alongside NFL game-market
    Platinum authority).
  * `UNAVAILABLE` means fail-closed: NHL / UFC families that lack a
    legitimate authoritative model must NEVER emit picks; a
    telemetry-only sentinel is stamped instead.

The registry is READ-ONLY at runtime — the boundary imports it and
validates every published pick's `(sport, market_family, model_source)`
triple against it.  Producers that claim an authority they don't own
are rejected.
"""
from __future__ import annotations
from typing import Any


# Sentinel — market families that cannot currently emit an
# actionable Locks pick.  Producers may still generate research
# rows (blocked from Locks via `no_real_book_line=True` +
# main-board-eligibility filter).
UNAVAILABLE = "MODEL_UNAVAILABLE"


AUTHORITY: dict[str, dict[str, dict[str, Any]]] = {
    "MLB": {
        "moneyline":       {"canonical": "mlb_feature_engine_ml",
                             "preserved_specialists": ()},
        "run_line":        {"canonical": "mlb_shared_run_distribution_v1",
                             "preserved_specialists": ()},
        "total":           {"canonical": "mlb_shared_run_distribution_v1",
                             "preserved_specialists": ()},
        "team_total":      {"canonical": "mlb_shared_run_distribution_v1",
                             "preserved_specialists": ()},
        # Specialized props preserved.
        "pitcher_strikeouts": {"canonical": "mlb_stuff_plus_k_model",
                                "preserved_specialists": (
                                    "mlb_bvp",
                                )},
        "pitcher_outs":       {"canonical": "mlb_pitcher_outs_model",
                                "preserved_specialists": ()},
        "hitter_hits":        {"canonical": "mlb_hitter_intel",
                                "preserved_specialists": ("mlb_bvp",)},
        "hitter_home_runs":   {"canonical": "mlb_hitter_intel",
                                "preserved_specialists": ("mlb_bvp",)},
        "hitter_total_bases": {"canonical": "mlb_hitter_intel",
                                "preserved_specialists": ("mlb_bvp",)},
    },
    "NFL": {
        "moneyline":  {"canonical": "platinum_nfl_game_sim",
                        "preserved_specialists": ()},
        "spread":     {"canonical": "platinum_nfl_game_sim",
                        "preserved_specialists": ()},
        "total":      {"canonical": "platinum_nfl_game_sim",
                        "preserved_specialists": ()},
        # Player props: Platinum runtime preserved as canonical
        # (opportunity + share features, then platinum sim).
        "player_passing_yards":  {"canonical": "platinum_nfl_prop",
                                    "preserved_specialists": ()},
        "player_rushing_yards":  {"canonical": "platinum_nfl_prop",
                                    "preserved_specialists": ()},
        "player_receiving_yards":{"canonical": "platinum_nfl_prop",
                                    "preserved_specialists": ()},
        "player_receptions":     {"canonical": "platinum_nfl_prop",
                                    "preserved_specialists": ()},
        "player_touchdowns":     {"canonical": "platinum_nfl_atd",
                                    "preserved_specialists": ()},
    },
    "CFB": {
        "moneyline": {"canonical": "cfb_sp_game_model",
                       "preserved_specialists": ()},
        "spread":    {"canonical": "cfb_sp_game_model",
                       "preserved_specialists": ()},
        "total":     {"canonical": "cfb_sp_game_model",
                       "preserved_specialists": ()},
    },
    "NBA": {
        "moneyline":     {"canonical": "nba_feature_engine",
                           "preserved_specialists": ()},
        "spread":        {"canonical": "nba_feature_engine",
                           "preserved_specialists": ()},
        "total":         {"canonical": "nba_feature_engine",
                           "preserved_specialists": ()},
        "player_points": {"canonical": "nba_player_prop_intel",
                           "preserved_specialists": ()},
        "player_assists":{"canonical": "nba_player_prop_intel",
                           "preserved_specialists": ()},
        "player_rebounds":{"canonical": "nba_player_prop_intel",
                            "preserved_specialists": ()},
    },
    "NHL": {
        # Fail-closed until authoritative NHL simulator is wired end-
        # to-end.  Producers may generate research rows but must
        # stamp `no_real_book_line=True` so Locks eligibility blocks.
        "moneyline":  {"canonical": UNAVAILABLE,
                        "preserved_specialists": ()},
        "puck_line":  {"canonical": UNAVAILABLE,
                        "preserved_specialists": ()},
        "total":      {"canonical": UNAVAILABLE,
                        "preserved_specialists": ()},
    },
    "Soccer": {
        "moneyline":         {"canonical": "soccer_game_model",
                                "preserved_specialists": ()},
        "total":             {"canonical": "soccer_game_model",
                                "preserved_specialists": ()},
        "btts":              {"canonical": "soccer_game_model",
                                "preserved_specialists": ()},
        "double_chance":     {"canonical": "soccer_game_model",
                                "preserved_specialists": ()},
        "goal_scorer":       {"canonical": "sportdb_scorer_intel",
                                "preserved_specialists": (
                                    "csl_espn_leaderboard",
                                )},
    },
    "Tennis": {
        # Tennis authority is REAL-DATA ONLY (Sackmann / surface Elo
        # / hold %).  Player-name-hash predictive evidence has been
        # explicitly retired from authoritative scoring per Phase 5
        # master spec.  Sportsbook-implied baseline is NOT counted
        # as independent model evidence.
        "moneyline":  {"canonical": "tennis_sackmann_engine",
                        "preserved_specialists": (
                            "tennis_fair_odds_engine",
                        )},
        "spread":     {"canonical": "tennis_sackmann_engine",
                        "preserved_specialists": ()},
        "total":      {"canonical": "tennis_sackmann_engine",
                        "preserved_specialists": ()},
    },
    "UFC": {
        # Fail-closed for every family until authoritative UFC
        # pre-fight model (Elo/age/reach/striking/takedown defense)
        # is wired.
        "moneyline":  {"canonical": UNAVAILABLE,
                        "preserved_specialists": ()},
        "total":      {"canonical": UNAVAILABLE,
                        "preserved_specialists": ()},
    },
}


def get_authority(sport: str, market_family: str) -> dict[str, Any] | None:
    """Return `{canonical, preserved_specialists}` for a
    (sport, market_family) pair, or None when the pair is
    unregistered.  Unregistered pairs fail-open: the boundary
    doesn't block them but they are recorded for follow-up."""
    if not sport or not market_family:
        return None
    sport_map = AUTHORITY.get(sport)
    if not sport_map:
        return None
    return sport_map.get(market_family.lower())


def is_authoritative(sport: str, market_family: str,
                       model_source: str | None) -> bool:
    """Return True iff `model_source` is the canonical authority (or
    a preserved specialist) for the (sport, market_family) pair.

    Returns True on unregistered pairs (fail-open — new market
    surface).  Returns False when the pair is registered UNAVAILABLE
    (fail-closed for UFC / NHL).
    """
    if not model_source:
        return False
    entry = get_authority(sport, market_family)
    if entry is None:
        return True   # unregistered pair, fail-open
    canonical = entry.get("canonical")
    if canonical == UNAVAILABLE:
        return False
    if model_source == canonical:
        return True
    if model_source in (entry.get("preserved_specialists") or ()):
        return True
    return False


def is_unavailable(sport: str, market_family: str) -> bool:
    """True iff the pair is registered as MODEL_UNAVAILABLE (must
    fail-closed for actionable publication)."""
    entry = get_authority(sport, market_family)
    if not entry:
        return False
    return entry.get("canonical") == UNAVAILABLE


__all__ = [
    "AUTHORITY",
    "UNAVAILABLE",
    "get_authority",
    "is_authoritative",
    "is_unavailable",
]
