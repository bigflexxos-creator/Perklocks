"""Phase 23 registry gap closure — CFB.player_points / MLB.spread /
Soccer.player_assists / Soccer.other.

Master directive: If ANY required authority is missing → fail closed.
Do not fabricate placeholder authority to improve capability matrix.
"""
from __future__ import annotations
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest
from services.sport_model_authority import (
    get_authority, is_authoritative, is_unavailable, UNAVAILABLE,
)


def test_mlb_spread_registered_as_unavailable():
    """MLB uses RUN LINE (registered) — 'spread' label is
    research-only per Phase 23 gap review."""
    e = get_authority("MLB", "spread")
    assert e is not None, "MLB.spread must be explicitly registered"
    assert e["canonical"] == UNAVAILABLE
    assert is_unavailable("MLB", "spread") is True
    # Any producer stamping any model_source cannot pass authority.
    assert is_authoritative("MLB", "spread", "any_model") is False


def test_cfb_player_points_registered_as_unavailable():
    e = get_authority("CFB", "player_points")
    assert e is not None
    assert e["canonical"] == UNAVAILABLE
    assert is_unavailable("CFB", "player_points") is True


def test_soccer_player_assists_registered_as_unavailable():
    e = get_authority("Soccer", "player_assists")
    assert e is not None
    assert e["canonical"] == UNAVAILABLE
    assert is_unavailable("Soccer", "player_assists") is True


def test_soccer_other_bucket_stays_unregistered_fails_closed():
    """The Phase-23 parser 'other' bucket collects unclassified
    markets — deliberately NOT registered so Phase-5 fail-closed
    hardening (is_authoritative returns False on unregistered)
    denies production authority automatically."""
    e = get_authority("Soccer", "other")
    assert e is None
    # Phase 5 fail-closed hardening handles this via is_authoritative.
    assert is_authoritative("Soccer", "other", "any_model") is False


def test_authoritative_families_still_pass():
    """Guard against over-blocking: legitimate families must still
    pass is_authoritative."""
    assert is_authoritative("MLB", "total",
                              "mlb_shared_run_distribution_v1") is True
    assert is_authoritative("CFB", "spread", "cfb_sp_game_model") is True
    assert is_authoritative("Soccer", "total",
                              "soccer_game_model") is True
    assert is_authoritative("Soccer", "goal_scorer",
                              "sportdb_scorer_intel") is True
    assert is_authoritative("MLB", "run_line",
                              "mlb_shared_run_distribution_v1") is True


def test_boundary_rejects_gap_families_at_runtime():
    """Runtime proof: the Phase 5 wiring in the canonical
    publication boundary rejects the newly-unavailable families."""
    from services.canonical_publication_boundary import (
        evaluate_publication, PublicationState, RejectionReason,
    )
    # MLB.spread live-attempt
    p = {"id":"t","sport":"MLB","market":"Spread",
         "selection":"Yankees","line":-1.5,"book_odds":-110,
         "odds_source":"the_odds_api",
         "model_probability":0.55,"identity_class":"AUTHORITATIVE"}
    v = evaluate_publication(p)
    assert v.state == PublicationState.REJECTED
    assert RejectionReason.MODEL_LINE_NOT_REAL_OFFERING.value in v.reasons
    # CFB.player_points live-attempt
    p2 = dict(p) | {"sport":"CFB","market":"Player Points Over 15.5",
                     "line":15.5}
    v2 = evaluate_publication(p2)
    assert v2.state == PublicationState.REJECTED


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
