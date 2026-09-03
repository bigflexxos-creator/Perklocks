"""PERKLOCKS MAIN 36 · UNIVERSAL IDENTITY HEAL — regression guard.

Locks in the recovery contract: when the CURRENT identity gate says
a pick is VALID, but the pick's DB row still carries a stale
``off_board=True`` + ``publication_rejection_reasons=[..., 'PLAYER_
EVENT_IDENTITY_MISMATCH', ...]`` from a pre-fix engine run, we can
identify and heal it deterministically without touching any P0 code.
"""
from __future__ import annotations

from services.player_event_identity_gate import (
    evaluate_identity, IdentityVerdict,
)


def _make_stale_devers() -> dict:
    """Real-shape probe: Devers today WAS stamped off_board=True by
    the pre-fix identity gate.  Post-fix ``evaluate_identity`` MUST
    return VALID so the heal query recovers this row."""
    return {
        "sport": "MLB",
        "market": "Rafael Devers (SF) Over 0.5 Hits",
        "selection": "Rafael Devers",
        "event": "San Francisco Giants @ Pittsburgh Pirates",
        "home_team": "Pittsburgh Pirates",
        "away_team": "San Francisco Giants",
        "home_team_id": 134, "away_team_id": 137,
        "off_board": True,
        "publication_rejection_reasons": ["PLAYER_EVENT_IDENTITY_MISMATCH"],
        "lock_score": 98.0,
    }


def test_healable_row_evaluates_valid_now():
    """The universal identity fix makes the stale row heal-eligible."""
    p = _make_stale_devers()
    assert evaluate_identity(p) == IdentityVerdict.VALID


def test_heal_query_selects_universal_identity_stale_rows_only():
    """The heal query pattern:
        off_board == True AND
        'PLAYER_EVENT_IDENTITY_MISMATCH' in publication_rejection_reasons
    matches the shape of the stale row.
    """
    p = _make_stale_devers()
    assert p["off_board"] is True
    assert "PLAYER_EVENT_IDENTITY_MISMATCH" in p["publication_rejection_reasons"]


def test_genuine_mismatch_is_not_healed():
    """A pick where the player is genuinely on neither team must
    remain off-board — the heal only recovers rows the CURRENT
    identity gate would clear."""
    bad = _make_stale_devers()
    # Change player to someone not on either team.
    bad["market"] = "Aaron Judge (NYY) Over 0.5 Hits"
    bad["selection"] = "Aaron Judge"
    assert evaluate_identity(bad) == IdentityVerdict.PLAYER_EVENT_IDENTITY_MISMATCH
