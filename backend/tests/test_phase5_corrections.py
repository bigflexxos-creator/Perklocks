"""PHASE 5 CORRECTION regressions (2026-06).

Locks in the three surgical fixes required by the conditional
acceptance:

  FIX 1 — First / Last goal scorer must be INTENTIONALLY_UNSUPPORTED.
  FIX 2 — PROVIDER_UNAVAILABLE must never be used when a provider row
          exists (below-threshold candidates get BELOW_SCORE_THRESHOLD).
  FIX 3 — NBA props remain SUPPORTED; NBA game markets remain
          MODEL_UNAVAILABLE — no consumer surface may advertise game
          markets as production-ready.
"""
from __future__ import annotations


# ─────────────────────────────────────────────────────────────────────
# FIX 1 — First / Last goal scorer are INTENTIONALLY_UNSUPPORTED.
# ─────────────────────────────────────────────────────────────────────
def test_first_goal_scorer_is_intentionally_unsupported():
    from services.sport_capability_registry import (
        market_production_status, prop_markets_for,
    )
    # Must NOT appear in the SUPPORTED props catalogue.
    assert "player_first_goal_scorer" not in set(prop_markets_for("Soccer"))
    assert "player_last_goal_scorer"  not in set(prop_markets_for("Soccer"))
    # Must be honestly labelled unsupported.
    assert market_production_status(
        "Soccer", "player_first_goal_scorer",
    ) == "INTENTIONALLY_UNSUPPORTED"
    assert market_production_status(
        "Soccer", "player_last_goal_scorer",
    ) == "INTENTIONALLY_UNSUPPORTED"


def test_first_scorer_not_in_acquisition_market_list():
    # alt_lines_feed.SOCCER_MARKETS is the authoritative acquisition
    # list — first-goal-scorer MUST NOT be present.
    from alt_lines_feed import SOCCER_MARKETS
    assert "player_first_goal_scorer" not in SOCCER_MARKETS
    assert "player_last_goal_scorer" not in SOCCER_MARKETS


def test_anytime_and_score_or_assist_remain_supported():
    from services.sport_capability_registry import (
        market_production_status, prop_markets_for,
    )
    props = set(prop_markets_for("Soccer"))
    assert "player_goal_scorer_anytime" in props
    assert "player_to_score_or_assist" in props
    assert market_production_status(
        "Soccer", "player_goal_scorer_anytime",
    ) == "SUPPORTED"
    assert market_production_status(
        "Soccer", "player_to_score_or_assist",
    ) == "SUPPORTED"


# ─────────────────────────────────────────────────────────────────────
# FIX 2 — PROVIDER_UNAVAILABLE vs BELOW_SCORE_THRESHOLD taxonomy.
# ─────────────────────────────────────────────────────────────────────
def test_provider_row_present_below_threshold_is_below_score_threshold():
    """The exact regression required by the conditional acceptance:
    provider row exists + LS<85 ≠ PROVIDER_UNAVAILABLE."""
    from services.funnel_terminal_states import (
        classify_terminal_state,
        PROVIDER_UNAVAILABLE, BELOW_SCORE_THRESHOLD,
    )
    label = classify_terminal_state(
        provider_row_present=True,   # ← key: provider offered the line
        lock_score=78.5,
        lock_floor=85.0,
    )
    assert label != PROVIDER_UNAVAILABLE, (
        "provider row exists + LS<85 MUST NOT be tagged "
        "PROVIDER_UNAVAILABLE — this is the exact confusion "
        "FIX 2 forbids"
    )
    assert label == BELOW_SCORE_THRESHOLD


def test_no_provider_row_is_provider_unavailable():
    """The ONLY correct use of PROVIDER_UNAVAILABLE."""
    from services.funnel_terminal_states import (
        classify_terminal_state, PROVIDER_UNAVAILABLE,
    )
    label = classify_terminal_state(provider_row_present=False)
    assert label == PROVIDER_UNAVAILABLE


def test_provider_row_present_survives_all_gates_is_visible():
    from services.funnel_terminal_states import (
        classify_terminal_state, VISIBLE,
    )
    label = classify_terminal_state(
        provider_row_present=True,
        lock_score=91.5, lock_floor=85.0,
    )
    assert label == VISIBLE


def test_model_unavailable_takes_precedence_over_threshold():
    # If the sport has no model wired (e.g. NBA game markets today),
    # the terminal state is MODEL_UNAVAILABLE — not
    # BELOW_SCORE_THRESHOLD (which requires the model priced the row).
    from services.funnel_terminal_states import (
        classify_terminal_state, MODEL_UNAVAILABLE,
    )
    label = classify_terminal_state(
        provider_row_present=True,
        model_available=False,
        lock_score=None,
    )
    assert label == MODEL_UNAVAILABLE


def test_terminal_state_vocabulary_is_complete():
    from services.funnel_terminal_states import VALID_TERMINAL_STATES
    # Exactly the 10 states required by FIX 2.
    required = {
        "PROVIDER_UNAVAILABLE", "IDENTITY_UNRESOLVED",
        "HISTORY_UNAVAILABLE", "INPUT_QUALITY_INSUFFICIENT",
        "MODEL_UNAVAILABLE", "BELOW_SCORE_THRESHOLD",
        "NO_POSITIVE_EDGE", "CANONICAL_REJECTED",
        "DISPLAY_REJECTED", "VISIBLE",
    }
    assert required.issubset(VALID_TERMINAL_STATES)


# ─────────────────────────────────────────────────────────────────────
# FIX 3 — NBA props SUPPORTED, game markets MODEL_UNAVAILABLE.
# ─────────────────────────────────────────────────────────────────────
def test_nba_props_are_supported():
    from services.sport_capability_registry import market_production_status
    for prop in (
        "player_points", "player_rebounds", "player_assists",
        "player_points_rebounds_assists",
        "player_points_alternate", "player_rebounds_alternate",
        "player_assists_alternate",
        "player_points_rebounds_assists_alternate",
    ):
        assert market_production_status("NBA", prop) == "SUPPORTED", (
            f"NBA prop {prop} must remain SUPPORTED"
        )


def test_nba_game_markets_are_model_unavailable():
    from services.sport_capability_registry import (
        market_production_status, is_production_ready,
    )
    # Overall sport SUPPORTED (props travel end-to-end).
    assert is_production_ready("NBA") is True
    # But EVERY game market MUST report MODEL_UNAVAILABLE.
    for game in ("h2h", "spreads", "totals"):
        status = market_production_status("NBA", game)
        assert status == "MODEL_UNAVAILABLE", (
            f"NBA {game} must be MODEL_UNAVAILABLE — audit found "
            f"{status} (would falsely advertise game markets as "
            f"production-ready)"
        )
