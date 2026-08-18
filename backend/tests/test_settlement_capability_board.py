"""P2 μ-closure focused test:
Publication boundary must reject picks whose market is classified
SETTLEMENT_UNSUPPORTED. An unsettleable market MUST never become an
actionable Board pick.
"""
from __future__ import annotations

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.canonical_publication_boundary import (
    evaluate_publication, PublicationState, RejectionReason,
)


def _base_valid_pick(**overrides):
    p = {
        "id": "test-pick-1",
        "sport": "MLB",
        "league": "mlb",
        "market_key": "moneyline",  # SUPPORTED game line, not a player prop
        "book_odds": +150,
        "odds_source": "the_odds_api",
        "model_probability": 0.42,
        "identity_class": "MAPPED",
        "no_real_book_line": False,
        "edge_percent": 3.4,
    }
    p.update(overrides)
    return p


def test_supported_market_publishes():
    v = evaluate_publication(_base_valid_pick())
    assert v.state == PublicationState.PUBLISHED, (
        f"Expected PUBLISHED, got {v.state} reasons={v.reasons}"
    )


def test_unsupported_soccer_market_rejects():
    # soccer_shots is in the deny-list per settlement_capability.py
    pick = _base_valid_pick(sport="Soccer", league="epl",
                            market_key="player_shots")
    v = evaluate_publication(pick)
    assert v.state == PublicationState.REJECTED
    assert RejectionReason.SETTLEMENT_UNSUPPORTED.value in v.reasons


def test_unsupported_soccer_cards_market_rejects():
    pick = _base_valid_pick(sport="Soccer", league="epl",
                            market_key="player_cards_over_under")
    v = evaluate_publication(pick)
    assert v.state == PublicationState.REJECTED
    assert RejectionReason.SETTLEMENT_UNSUPPORTED.value in v.reasons


def test_unknown_market_still_permitted():
    # Unknown markets stay in UNKNOWN state — fail-open by design
    # so new market surfaces aren't blocked before registry updates.
    pick = _base_valid_pick(sport="Tennis", market_key="tennis_first_set_winner")
    v = evaluate_publication(pick)
    # This particular market is SUPPORTED via generic tennis allow-list;
    # even if not, UNKNOWN must not carry SETTLEMENT_UNSUPPORTED reason.
    assert RejectionReason.SETTLEMENT_UNSUPPORTED.value not in v.reasons


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
