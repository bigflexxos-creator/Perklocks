"""STEP 15 · Sport / Market reachability classifier — contract tests
======================================================================

Kills silent starvation — every zero-published count MUST classify to
exactly one of the 10 canonical reason codes the directive lists.
"""
from __future__ import annotations
import pytest, os, sys

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from services.sport_market_reachability import (
    classify_starvation, ReachabilityResult,
    NO_EVENTS, NO_REAL_MARKETS, NORMALIZATION_FAILURE, IDENTITY_FAILURE,
    MODEL_UNAVAILABLE_, INTEGRITY_REJECTED, LEGITIMATELY_BELOW_85,
    PUBLICATION_FAILURE, API_FAILURE, FRONTEND_FAILURE,
)
from services.universal_market_contract import Family


def test_step15_no_events_when_provider_returns_zero():
    r = classify_starvation("MLB", Family.HITTER_HITS, {})
    # No entry for hitter_hits in the "PROVIDER_UNAVAILABLE" state, so
    # this rolls through the funnel — first layer with 0 downstream
    # (provider_events) triggers NO_EVENTS.
    assert r.reason == NO_EVENTS


def test_step15_no_real_markets_when_events_present_but_markets_zero():
    r = classify_starvation("MLB", Family.HITTER_HITS, {
        "provider_events": 5,
        "provider_real_markets": 0,
    })
    assert r.reason == NO_REAL_MARKETS
    assert r.layer == "markets"


def test_step15_identity_failure_bucket():
    r = classify_starvation("MLB", Family.HITTER_HITS, {
        "provider_events": 5, "provider_real_markets": 40,
        "normalized_candidates": 30, "identity_resolved": 0,
    })
    assert r.reason == IDENTITY_FAILURE


def test_step15_model_unavailable_bucket():
    r = classify_starvation("MLB", Family.HITTER_HITS, {
        "provider_events": 5, "provider_real_markets": 40,
        "normalized_candidates": 30, "identity_resolved": 30,
        "model_evaluated": 0,
    })
    assert r.reason == MODEL_UNAVAILABLE_


def test_step15_legitimately_below_85_bucket():
    """A market that produces candidates but NONE reach 85 must NOT
    be classified as PUBLICATION_FAILURE. It's a legitimate below-85."""
    r = classify_starvation("MLB", Family.HITTER_HITS, {
        "provider_events": 5, "provider_real_markets": 40,
        "normalized_candidates": 30, "identity_resolved": 30,
        "model_evaluated": 30, "integrity_passed": 20,
        "below_85": 20, "at_or_above_85": 0, "published": 0,
    })
    assert r.reason == LEGITIMATELY_BELOW_85


def test_step15_publication_failure_only_when_above_85_nonzero():
    """If 85+ candidates exist and published == 0, THAT is a real
    publication failure and it should also flag contradiction with the
    ACTIVE market contract state."""
    r = classify_starvation("MLB", Family.HITTER_HITS, {
        "provider_events": 5, "provider_real_markets": 40,
        "normalized_candidates": 30, "identity_resolved": 30,
        "model_evaluated": 30, "integrity_passed": 25,
        "below_85": 20, "at_or_above_85": 5, "published": 0,
    })
    assert r.reason == PUBLICATION_FAILURE
    assert r.contradicts_market_contract is True


def test_step15_api_failure_bucket():
    r = classify_starvation("MLB", Family.HITTER_HITS, {
        "provider_events": 5, "provider_real_markets": 40,
        "normalized_candidates": 30, "identity_resolved": 30,
        "model_evaluated": 30, "integrity_passed": 25,
        "at_or_above_85": 8, "published": 8, "served_full_api": 0,
    })
    assert r.reason == API_FAILURE


def test_step15_frontend_failure_bucket():
    """FRONTEND_FAILURE distinctly requires the frontend layer to
    report ZERO renders when the API served rows successfully."""
    r = classify_starvation("MLB", Family.HITTER_HITS, {
        "provider_events": 5, "provider_real_markets": 40,
        "normalized_candidates": 30, "identity_resolved": 30,
        "model_evaluated": 30, "integrity_passed": 25,
        "at_or_above_85": 8, "published": 8, "served_full_api": 8,
        "served_lite_api": 8, "frontend_rendered": 0,
    })
    assert r.reason == FRONTEND_FAILURE


def test_step15_honours_market_contract_provider_unavailable():
    """If UniversalMarketContract already declares
    PROVIDER_UNAVAILABLE (NBA off-season, NHL off-season, ...) the
    classifier honours that and does NOT flag silent starvation."""
    # NBA points market is declared PROVIDER_UNAVAILABLE at contract load.
    r = classify_starvation("NBA", Family.NBA_POINTS, {
        "provider_events": 0,
    })
    assert r.reason == NO_EVENTS
    assert r.contradicts_market_contract is False


def test_step15_healthy_path_returns_healthy():
    r = classify_starvation("MLB", Family.HITTER_HITS, {
        "provider_events": 5, "provider_real_markets": 40,
        "normalized_candidates": 30, "identity_resolved": 30,
        "model_evaluated": 30, "integrity_passed": 25,
        "at_or_above_85": 8, "published": 8, "served_full_api": 8,
        "served_lite_api": 8,
    })
    assert r.reason == "HEALTHY"
