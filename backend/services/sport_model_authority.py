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
        # Phase 23 gap closure (2026-09-02): MLB does not use "spread"
        # as an authoritative production family — the canonical MLB
        # spread analogue is the RUN LINE (registered above).  Any
        # producer emitting a market labelled "Spread" for MLB is
        # research-only and must NOT auto-become production
        # authoritative.  Fail-closed here.
        "spread":          {"canonical": UNAVAILABLE,
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
        # Phase 23 gap closure (2026-09-02): no authoritative CFB
        # player-points model exists (opportunity + share features
        # not wired for CFB; identity resolution incomplete).
        # Fail-closed for production authority; research/Lab may
        # still inspect.
        "player_points": {"canonical": UNAVAILABLE,
                           "preserved_specialists": ()},
    },
    # P1.6 NBA ROOT CLOSURE / STEP C ACTIVATION 2026-10-20 — NBA now
    # has two real authoritative models:
    #   • `nba_game_model` (services/nba_game_model.py) — real
    #     distribution-based moneyline / spread cover / total model
    #     seeded from the historical ``team_game_actuals`` collection
    #     (5,544 rows across 2024-25).  Rolling L30 per-team offensive
    #     and defensive ratings + NBA historical σ_margin≈12,
    #     σ_total≈20 feed a normal-distribution scoring model.
    #   • `nba_simulator` (brain/sim_nba.py + nba_feature_engine.py) —
    #     real 20,000-run Monte Carlo per-stat player prop simulator
    #     seeded from ``player_game_logs`` L10 with stats stamped
    #     EMPIRICAL_INDEPENDENT whenever enough real rows survive.
    # Both authorities are now registered as the canonical model_source
    # for their families.  Families with NO provider rows today
    # (`nba_threes`, `nba_turnovers`, `nba_blocks_steals`,
    # `nba_double_double`, `nba_triple_double`) retain UNAVAILABLE —
    # model path is wired but auto-surfaces only when the provider
    # emits rows.
    "NBA": {
        # Game markets
        "moneyline":           {"canonical": "nba_game_model",
                                 "preserved_specialists": ()},
        "spread":              {"canonical": "nba_game_model",
                                 "preserved_specialists": ()},
        "point_spread":        {"canonical": "nba_game_model",
                                 "preserved_specialists": ()},
        "total":               {"canonical": "nba_game_model",
                                 "preserved_specialists": ()},
        "game_total":          {"canonical": "nba_game_model",
                                 "preserved_specialists": ()},
        # Player prop families — canonical (nba_simulator) +
        # specialists for legacy stamp paths (`nba_sim`,
        # `distribution_monte_carlo`, `brain_sim_nba`).
        "nba_points":          {"canonical": "nba_simulator",
                                 "preserved_specialists": (
                                     "brain_sim_nba",
                                     "distribution_monte_carlo",
                                     "nba_sim",
                                 )},
        "nba_rebounds":        {"canonical": "nba_simulator",
                                 "preserved_specialists": (
                                     "brain_sim_nba",
                                     "distribution_monte_carlo",
                                     "nba_sim",
                                 )},
        "nba_assists":         {"canonical": "nba_simulator",
                                 "preserved_specialists": (
                                     "brain_sim_nba",
                                     "distribution_monte_carlo",
                                     "nba_sim",
                                 )},
        "nba_pra":             {"canonical": "nba_simulator",
                                 "preserved_specialists": (
                                     "brain_sim_nba",
                                     "distribution_monte_carlo",
                                     "nba_sim",
                                 )},
        "nba_pts_reb":         {"canonical": "nba_simulator",
                                 "preserved_specialists": (
                                     "brain_sim_nba",
                                     "distribution_monte_carlo",
                                     "nba_sim",
                                 )},
        "nba_pts_ast":         {"canonical": "nba_simulator",
                                 "preserved_specialists": (
                                     "brain_sim_nba",
                                     "distribution_monte_carlo",
                                     "nba_sim",
                                 )},
        "nba_reb_ast":         {"canonical": "nba_simulator",
                                 "preserved_specialists": (
                                     "brain_sim_nba",
                                     "distribution_monte_carlo",
                                     "nba_sim",
                                 )},
        "nba_steals":          {"canonical": "nba_simulator",
                                 "preserved_specialists": (
                                     "brain_sim_nba",
                                     "distribution_monte_carlo",
                                     "nba_sim",
                                 )},
        "nba_blocks":          {"canonical": "nba_simulator",
                                 "preserved_specialists": (
                                     "brain_sim_nba",
                                     "distribution_monte_carlo",
                                     "nba_sim",
                                 )},
        # Provider-zero families (no rows today): keep UNAVAILABLE —
        # auto-surfaces when provider returns them AND we add a
        # dedicated model path.  Fail-closed for publication authority.
        "nba_threes":          {"canonical": UNAVAILABLE,
                                 "preserved_specialists": ()},
        "nba_turnovers":       {"canonical": UNAVAILABLE,
                                 "preserved_specialists": ()},
        "nba_blocks_steals":   {"canonical": UNAVAILABLE,
                                 "preserved_specialists": ()},
        "nba_double_double":   {"canonical": UNAVAILABLE,
                                 "preserved_specialists": ()},
        "nba_triple_double":   {"canonical": UNAVAILABLE,
                                 "preserved_specialists": ()},
        # Legacy family-name aliases (defensive — some older producers
        # stamp `market_family` as `player_points` / `player_assists` /
        # `player_rebounds` instead of the canonical `nba_points` /
        # `nba_assists` / `nba_rebounds`).  Keep them routed to the
        # same canonical authority rather than fail-closed.
        "player_points":       {"canonical": "nba_simulator",
                                 "preserved_specialists": (
                                     "brain_sim_nba",
                                     "distribution_monte_carlo",
                                     "nba_sim",
                                 )},
        "player_assists":      {"canonical": "nba_simulator",
                                 "preserved_specialists": (
                                     "brain_sim_nba",
                                     "distribution_monte_carlo",
                                     "nba_sim",
                                 )},
        "player_rebounds":     {"canonical": "nba_simulator",
                                 "preserved_specialists": (
                                     "brain_sim_nba",
                                     "distribution_monte_carlo",
                                     "nba_sim",
                                 )},
    },
    "NHL": {
        # 2026-10-02 — NHL authority wired to brain.sim_nhl.
        # Pipeline:  services/nhl_feature_engine.build_nhl_sim_context
        # (real db.games + player_game_logs evidence) →
        # brain/sim_nhl.simulate (20k-run Poisson game model /
        # per-stat player model) → CAUSAL_INDEPENDENT provenance
        # with decision_valid=True → anchored Lock Score. Settlement
        # authorities registered for all 7 families in
        # services/settlement_capability_registry.py. Game pipeline
        # end-to-end verified against the 19 live icehockey_nhl
        # bulk-odds events.
        "moneyline":          {"canonical": "brain_sim_nhl",
                                "preserved_specialists": ()},
        "puck_line":          {"canonical": "brain_sim_nhl",
                                "preserved_specialists": ()},
        "total":              {"canonical": "brain_sim_nhl",
                                "preserved_specialists": ()},
        "game_total":         {"canonical": "brain_sim_nhl",
                                "preserved_specialists": ()},
        "nhl_goals":          {"canonical": "brain_sim_nhl",
                                "preserved_specialists": ()},
        "nhl_sog":            {"canonical": "brain_sim_nhl",
                                "preserved_specialists": ()},
        "nhl_assists":        {"canonical": "brain_sim_nhl",
                                "preserved_specialists": ()},
        "nhl_points":         {"canonical": "brain_sim_nhl",
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
        # Session 10.3 (2026-09-16) Soccer Player-Prop Final Live
        # Publication Closure — register the previously-missing
        # market families so real sportsbook Shots / SOT / Score-or-
        # Assist / Assists rows can publish alongside ATG rather than
        # dying to PUBLICATION_FILTER because the family isn't in the
        # authority registry.
        "goal_scorer":       {"canonical": "sportdb_scorer_intel",
                                "preserved_specialists": (
                                    "csl_espn_leaderboard",
                                    "soccer_player_authority_v1",
                                )},
        "score_or_assist":   {"canonical": "soccer_player_authority_v1",
                                "preserved_specialists": ()},
        "player_shots":      {"canonical": "soccer_player_authority_v1",
                                "preserved_specialists": ()},
        "player_shots_on_target": {"canonical": "soccer_player_authority_v1",
                                    "preserved_specialists": ()},
        "player_assists":    {"canonical": "soccer_player_authority_v1",
                                "preserved_specialists": ()},
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

    FAIL-CLOSED semantics (Phase 5 hardening, 2026-06):
      * Returns False on unregistered (sport, market_family) — a
        typoed / legacy / synthetic / unsupported family MUST NOT
        automatically receive production publication authority just
        because it is absent from the registry.
      * Returns False when the registered pair is UNAVAILABLE
        (UFC / NHL).
      * Returns True only when `model_source` matches the canonical
        tag or a preserved specialist.
    Research / Lab surfaces may still inspect unregistered pairs as
    SHADOW; only PRODUCTION publication authority is denied here.
    """
    if not model_source:
        return False
    entry = get_authority(sport, market_family)
    if entry is None:
        return False   # fail-closed — unregistered pair is NOT authoritative
    canonical = entry.get("canonical")
    if canonical == UNAVAILABLE:
        return False
    if model_source == canonical:
        return True
    if model_source in (entry.get("preserved_specialists") or ()):
        return True
    return False


def is_registered(sport: str, market_family: str) -> bool:
    """True iff (sport, market_family) is declared in the registry."""
    return get_authority(sport, market_family) is not None


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
    "is_registered",
    "is_unavailable",
]
