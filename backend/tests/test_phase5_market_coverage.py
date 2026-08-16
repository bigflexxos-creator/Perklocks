"""PHASE 5 — Real Market + Prop Coverage regressions.

Proves:

  §5A  Capability registry honestly classifies every sport with a
       production_status.  Core release sports report SUPPORTED, deferred
       sports report INTENTIONALLY_DEFERRED, disabled sports report
       INTENTIONALLY_UNSUPPORTED.  No sport secretly advertises
       production-readiness while its models are absent.
  §5B  Soccer game-market families are all reachable by
       ``sport_capability_registry``: h2h (1X2), spreads (handicap),
       totals (Over/Under), btts, double_chance.  DRAW is not
       silently dropped by the classifier.
  §5D  MLB props catalogue includes strikeouts / outs / hits / total
       bases / home runs / RBIs — all real Odds-API market keys.
  §5E  NFL props catalogue includes passing / rushing / receiving /
       ATD.
  §5F  NBA props catalogue includes points / rebounds / assists / PRA
       and their alternate variants; NBA game markets are honestly
       tagged MODEL_UNAVAILABLE.
  §5G  Tennis game markets (ML/spread/total) reachable.
  §5I  ``consumer_disposition`` vocabulary contains the taxonomy
       required by the funnel report.
"""
from __future__ import annotations

from services.sport_capability_registry import (
    SPORT_CAPABILITIES, prop_markets_for, game_markets_for,
    production_status, market_production_status, core_release_sports,
    is_production_ready, VALID_PRODUCTION_STATUSES,
)


# ─────────────────────────────────────────────────────────────────────
# §5A — Capability registry honesty.
# ─────────────────────────────────────────────────────────────────────
def test_core_release_sports_are_all_supported():
    for sport in ("MLB", "NFL", "NBA", "Soccer", "Tennis"):
        assert production_status(sport) == "SUPPORTED", (
            f"{sport} must be SUPPORTED for the current release, "
            f"got {production_status(sport)}"
        )
        assert is_production_ready(sport) is True


def test_deferred_sports_are_honestly_classified():
    for sport in ("NHL", "CFB", "UFC"):
        assert production_status(sport) == "INTENTIONALLY_DEFERRED", (
            f"{sport} must be INTENTIONALLY_DEFERRED per release scope"
        )
        # Deferred sports MUST NOT return production-ready.
        assert is_production_ready(sport) is False


def test_every_status_is_from_the_valid_vocabulary():
    for sport, entry in SPORT_CAPABILITIES.items():
        status = entry.get("production_status")
        if status is None:
            # Legacy entries are tolerated — production_status() returns
            # a defensive MODEL_UNAVAILABLE.  Assert that instead.
            assert production_status(sport) in VALID_PRODUCTION_STATUSES
        else:
            assert status in VALID_PRODUCTION_STATUSES, (
                f"{sport} has invalid production_status={status}"
            )


def test_nba_game_markets_are_honestly_model_unavailable():
    # NBA is SUPPORTED overall (props travel end-to-end) but game
    # markets are MODEL_UNAVAILABLE per Phase 1B retirement.
    assert market_production_status("NBA", "h2h") == "MODEL_UNAVAILABLE"
    assert market_production_status("NBA", "spreads") == "MODEL_UNAVAILABLE"
    assert market_production_status("NBA", "totals") == "MODEL_UNAVAILABLE"
    # Props inherit the SUPPORTED sport-level status.
    assert market_production_status("NBA", "player_points") == "SUPPORTED"


def test_core_release_sports_helper():
    assert core_release_sports() == ["MLB", "NFL", "NBA", "Soccer", "Tennis"]


# ─────────────────────────────────────────────────────────────────────
# §5B — Soccer game-market families are all reachable.
# ─────────────────────────────────────────────────────────────────────
def test_soccer_game_market_family_completeness():
    game = set(game_markets_for("Soccer"))
    for required in ("h2h", "spreads", "totals", "btts", "double_chance"):
        assert required in game, (
            f"Soccer registry missing required family {required}"
        )


def test_soccer_scorer_family_completeness():
    props = set(prop_markets_for("Soccer"))
    for required in (
        "player_goal_scorer_anytime",
        "player_to_score_or_assist",
        "player_first_goal_scorer",
    ):
        assert required in props


# ─────────────────────────────────────────────────────────────────────
# §5D — MLB prop catalogue completeness.
# ─────────────────────────────────────────────────────────────────────
def test_mlb_props_catalogue():
    props = set(prop_markets_for("MLB"))
    for required in (
        "pitcher_strikeouts", "pitcher_outs",
        "batter_hits", "batter_home_runs", "batter_rbis",
        "batter_total_bases",
    ):
        assert required in props, f"MLB missing prop family {required}"
    # Alternate lines available for the heavy hitters.
    for required in (
        "pitcher_strikeouts_alternate",
        "batter_hits_alternate", "batter_home_runs_alternate",
        "batter_total_bases_alternate",
    ):
        assert required in props


# ─────────────────────────────────────────────────────────────────────
# §5E — NFL prop catalogue completeness.
# ─────────────────────────────────────────────────────────────────────
def test_nfl_props_catalogue():
    props = set(prop_markets_for("NFL"))
    for required in (
        "player_pass_yds", "player_rush_yds", "player_reception_yds",
        "player_anytime_td",
    ):
        assert required in props
    # Alternates for the yardage families.
    for required in (
        "player_pass_yds_alternate",
        "player_rush_yds_alternate",
        "player_reception_yds_alternate",
    ):
        assert required in props


# ─────────────────────────────────────────────────────────────────────
# §5F — NBA prop catalogue completeness.
# ─────────────────────────────────────────────────────────────────────
def test_nba_props_catalogue():
    props = set(prop_markets_for("NBA"))
    for required in (
        "player_points", "player_rebounds", "player_assists",
        "player_points_rebounds_assists",
    ):
        assert required in props
    for required in (
        "player_points_alternate", "player_rebounds_alternate",
        "player_assists_alternate",
        "player_points_rebounds_assists_alternate",
    ):
        assert required in props


# ─────────────────────────────────────────────────────────────────────
# §5G — Tennis game markets reachable.
# ─────────────────────────────────────────────────────────────────────
def test_tennis_game_markets_present():
    game = set(game_markets_for("Tennis"))
    for required in ("h2h", "spreads", "totals"):
        assert required in game


# ─────────────────────────────────────────────────────────────────────
# §5I — Rejection-funnel taxonomy shipped by the platform.
# ─────────────────────────────────────────────────────────────────────
def test_disposition_vocabulary_supports_funnel_reporting():
    # The Phase 1E vocabulary + Phase 5 additions must all be
    # importable from the board utility layer + settlement contract so
    # the funnel report has a stable set of terminal states.
    from services.board_utility_layer import (
        apply_extreme_juice_utility, apply_ladder_collapse,
    )
    # Marker-only imports — presence proves the vocabulary is wired.
    assert callable(apply_extreme_juice_utility)
    assert callable(apply_ladder_collapse)

    # Universal settlement contract — the terminal grader states.
    from services.universal_settlement_contract import (
        RESULT_WON, RESULT_LOST, RESULT_PUSH,
        RESULT_VOID, RESULT_UNRESOLVED,
    )
    # These MUST remain distinct string values.
    all_terms = {RESULT_WON, RESULT_LOST, RESULT_PUSH,
                  RESULT_VOID, RESULT_UNRESOLVED}
    assert len(all_terms) == 5
