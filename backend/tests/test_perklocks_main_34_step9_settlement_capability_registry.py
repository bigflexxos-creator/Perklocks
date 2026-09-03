"""STEP 9 · SettlementCapabilityRegistry — contract tests
============================================================

Guarantees:
  1. Every ACTIVE (sport, canonical family) declares required actuals.
  2. `is_gradeable` NEVER returns True when a required actual is None.
  3. Reason codes cover the 4 canonical failure classes so callers
     never invent a LOSS / zero / VOID for missing data.
  4. Coverage matrix rows expose the 8 diagnostic counters.
"""
from __future__ import annotations
import os, sys
import pytest

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from services.settlement_capability_registry import (
    SettlementAuthority, register, get, all_registrations,
    is_gradeable, coverage_row,
    REASON_MISSING_ACTUAL, REASON_UNSUPPORTED_MARKET,
    REASON_EVENT_NOT_FINAL, REASON_IDENTITY_FAILURE,
)


def test_step9_registry_seeded_active_surface():
    """Core ACTIVE (sport, family) markets must be present."""
    for key in (
        ("MLB", "moneyline"), ("MLB", "run_line"), ("MLB", "hitter_hits"),
        ("MLB", "pitcher_strikeouts"),
        ("NFL", "wr_receiving_yards"), ("NFL", "wr_receptions"),
        ("Tennis", "tennis_match_winner"), ("Tennis", "tennis_total_games"),
        ("Soccer", "moneyline"), ("Soccer", "goalscorer_anytime"),
    ):
        assert get(*key) is not None, f"seed missing: {key}"


def test_step9_every_entry_declares_required_actuals_and_primary_authority():
    for (sport, fam), cap in all_registrations().items():
        assert cap.required_actual_fields, (
            f"{sport}/{fam} has empty required_actual_fields"
        )
        assert cap.primary_authority, (
            f"{sport}/{fam} has no primary_authority"
        )


def test_step9_is_gradeable_returns_unsupported_for_unknown_market():
    ok, reason = is_gradeable("XLeague", "some_family", True, True, {})
    assert not ok
    assert reason == REASON_UNSUPPORTED_MARKET


def test_step9_is_gradeable_returns_event_not_final_when_flag_false():
    ok, reason = is_gradeable("MLB", "hitter_hits", False, True,
                               {"player_hits": 2})
    assert not ok
    assert reason == REASON_EVENT_NOT_FINAL


def test_step9_is_gradeable_returns_identity_failure():
    ok, reason = is_gradeable("MLB", "hitter_hits", True, False,
                               {"player_hits": 2})
    assert not ok
    assert reason == REASON_IDENTITY_FAILURE


def test_step9_missing_actual_becomes_unresolved_not_loss_or_zero():
    """CRITICAL: is_gradeable must return False + MISSING_ACTUAL for
    ANY missing required field — the callers rely on this so no
    grader ever invents a LOSS or zero for missing data."""
    ok, reason = is_gradeable("MLB", "hitter_hits", True, True,
                               {"player_hits": None})
    assert not ok
    assert reason == REASON_MISSING_ACTUAL
    # Same test for a completely empty actuals dict.
    ok2, reason2 = is_gradeable("NFL", "wr_receiving_yards", True, True, {})
    assert not ok2
    assert reason2 == REASON_MISSING_ACTUAL
    # And for MLB moneyline requiring BOTH scores — one missing is enough.
    ok3, reason3 = is_gradeable("MLB", "moneyline", True, True,
                                 {"home_score": 5, "away_score": None})
    assert not ok3
    assert reason3 == REASON_MISSING_ACTUAL


def test_step9_is_gradeable_true_when_all_present():
    ok, reason = is_gradeable("MLB", "moneyline", True, True,
                               {"home_score": 5, "away_score": 3})
    assert ok is True
    assert reason == ""


def test_step9_coverage_row_shape_supports_matrix_output():
    row = coverage_row("MLB", "hitter_hits", {
        "published": 30, "gradeable": 28, "settled": 25,
        "unresolved": 3, "missing_actual": 3,
    })
    for k in (
        "sport", "canonical_market_family", "primary_authority",
        "fallback_authorities", "required_actuals",
        "published", "gradeable", "settled", "unresolved",
        "missing_actual", "identity_failure", "unsupported_rule",
        "provider_failure",
    ):
        assert k in row, f"coverage_row missing {k!r}"
    assert row["published"] == 30
    assert row["missing_actual"] == 3
    assert row["fallback_authorities"] == ["pitchapi"]


def test_step9_duplicate_registration_raises():
    with pytest.raises(ValueError):
        register(SettlementAuthority(
            "MLB", "moneyline", ("home_score", "away_score"), "dup", ()))
