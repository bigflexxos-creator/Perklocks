"""PERKLOCKS-MAIN 35 · P1-8 — CAPABILITY ALIGNMENT (NBA/NHL/UFC/Soccer).

Locks in the aligned capability states across BOTH registries:
  * `services.universal_market_contract` — sport × canonical family
  * `services.sport_capability_registry` — sport × provider market key

Rules:
  * Two registries must NEVER disagree on the classification of the
    same (sport, market) combination.
  * NBA h2h / points: MODEL_UNAVAILABLE (provider wired, no NBA model).
  * NHL h2h: MODEL_UNAVAILABLE (provider wired, no NHL model).
  * UFC h2h: MODEL_UNAVAILABLE (provider wired, no UFC model).
  * Soccer h2h / totals / goalscorer_anytime: ACTIVE.
  * Tennis match / totals / handicap: ACTIVE.
  * No sport claims ACTIVE without a model wired.
"""
from __future__ import annotations

import pytest


def test_nba_family_states_are_model_unavailable():
    from services.universal_market_contract import get, Family, MODEL_UNAVAILABLE

    ml = get("NBA", Family.MONEYLINE)
    pts = get("NBA", Family.NBA_POINTS)
    assert ml is not None and ml.capability_state == MODEL_UNAVAILABLE
    assert pts is not None and pts.capability_state == MODEL_UNAVAILABLE


def test_nhl_family_state_is_model_unavailable():
    from services.universal_market_contract import get, Family, MODEL_UNAVAILABLE

    ml = get("NHL", Family.MONEYLINE)
    assert ml is not None and ml.capability_state == MODEL_UNAVAILABLE


def test_ufc_family_state_is_model_unavailable():
    from services.universal_market_contract import get, Family, MODEL_UNAVAILABLE

    ml = get("UFC", Family.MONEYLINE)
    assert ml is not None and ml.capability_state == MODEL_UNAVAILABLE


def test_soccer_active_families_are_active():
    from services.universal_market_contract import get, Family, ACTIVE

    for fam in (Family.MONEYLINE, Family.GAME_TOTAL, Family.GOALSCORER_ANY):
        e = get("Soccer", fam)
        assert e is not None and e.capability_state == ACTIVE, fam


def test_tennis_active_families_are_active():
    from services.universal_market_contract import get, Family, ACTIVE

    for fam in (
        Family.TENNIS_MATCH_WIN,
        Family.TENNIS_TOTAL_GAMES,
        Family.TENNIS_GAME_HANDICAP,
    ):
        e = get("Tennis", fam)
        assert e is not None and e.capability_state == ACTIVE, fam


def test_two_registries_do_not_disagree_on_nba_nhl_ufc():
    """The Universal Market Contract and the Sport Capability Registry
    both track (sport × market) support state.  For our audited surface
    the two registries must NEVER report contradictory states — e.g.
    one saying PROVIDER_UNAVAILABLE while the other says
    MODEL_UNAVAILABLE for the same sport."""
    from services.universal_market_contract import (
        get, Family, MODEL_UNAVAILABLE,
    )
    from services.sport_capability_registry import SPORT_CAPABILITIES as _SR

    # For NBA / NHL / UFC h2h game markets, sport_capability_registry
    # records MODEL_UNAVAILABLE. The universal contract must agree.
    for sport in ("NBA", "NHL", "UFC"):
        umc_state = get(sport, Family.MONEYLINE).capability_state
        sr_state = _SR.get(sport, {}).get("market_status", {}).get("h2h")
        assert umc_state == MODEL_UNAVAILABLE, (sport, umc_state)
        # sport_capability_registry uses the same enum vocabulary.
        assert sr_state == MODEL_UNAVAILABLE, (sport, sr_state)


def test_active_state_requires_a_model_authority():
    """Regression guard: a sport/market claiming ACTIVE must declare a
    real `model_authority` string. No fake ACTIVE support."""
    from services.universal_market_contract import all_entries, ACTIVE

    for (sport, fam), e in all_entries().items():
        if e.capability_state == ACTIVE:
            assert e.model_authority, (
                f"{sport}.{fam} is ACTIVE without a model_authority — "
                "fake support flag"
            )


def test_capability_states_are_a_finite_honest_set():
    """No new invented state labels."""
    from services.universal_market_contract import (
        all_entries,
        ACTIVE, RESEARCH_ONLY, MODEL_UNAVAILABLE,
        PROVIDER_UNAVAILABLE, SETTLEMENT_UNAVAILABLE,
    )
    valid = {
        ACTIVE, RESEARCH_ONLY, MODEL_UNAVAILABLE,
        PROVIDER_UNAVAILABLE, SETTLEMENT_UNAVAILABLE,
    }
    for (_, _), e in all_entries().items():
        assert e.capability_state in valid, e.capability_state
