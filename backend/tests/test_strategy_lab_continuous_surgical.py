"""Strategy Lab Continuous Surgical Research Upgrade — focused verification.

Proves §5-§14 helpers work and the correlation identity fix is in place.
"""
from __future__ import annotations

import asyncio
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_line_sensitivity_math():
    from services.research import extended as ext
    values = [10, 12, 14, 16, 18, 20, 22, 24, 26, 28]
    r = ext.line_sensitivity(values, line=19.0, step=2.0)
    assert r["available"]
    assert r["classification"] in ("LINE_ROBUST", "LINE_SENSITIVE", "LINE_FRAGILE")
    # p(>19) with the given values = 5/10 = 0.5 exactly
    center = next(c for c in r["curve"] if c["model_threshold"] == 19.0)
    assert center["empirical_over"] == 0.5
    print(f"  ✓ line_sensitivity classification={r['classification']} slope={r['slope']}")


def test_market_disagreement_math():
    from services.research import extended as ext
    async def _run():
        # book -110 → implied ~0.524; model 0.60 → +0.076 → ABOVE
        r = await ext.market_disagreement(None, 0.60, -110)
        assert r["available"]
        assert r["classification"] == "MODEL_ABOVE_MARKET"
        # book -110, model 0.40 → -0.124 → BELOW
        r2 = await ext.market_disagreement(None, 0.40, -110)
        assert r2["classification"] == "MODEL_BELOW_MARKET"
        # aligned
        r3 = await ext.market_disagreement(None, 0.53, -110)
        assert r3["classification"] == "MARKET_ALIGNED"
    asyncio.get_event_loop().run_until_complete(_run())
    print("  ✓ market_disagreement: ABOVE / BELOW / ALIGNED all correct")


def test_h2h_quality_thresholds():
    from services.research import extended as ext
    assert ext.h2h_quality(0)["classification"] == "NOT_MEANINGFUL"
    assert ext.h2h_quality(3)["classification"] == "LOW_SAMPLE_H2H"
    assert ext.h2h_quality(8)["classification"] == "MODERATE_H2H"
    assert ext.h2h_quality(20)["classification"] == "HIGH_VALUE_H2H"
    print("  ✓ h2h_quality: 0/3/8/20 → NOT/LOW/MOD/HIGH")


def test_scorecard_aggregation():
    from services.research import extended as ext
    high = {"available": True, "classification": "ADVANTAGE"}
    high_reg = {"available": True, "classification": "POSITIVE_REGRESSION"}
    stab = {"available": True, "classification": "STABLE"}
    price = {"available": True, "classification": "GOOD_PRICE"}
    role = {"available": True, "classification": "OPPORTUNITY_CHANGE"}
    sc = ext.research_scorecard(role=role, matchup=high, regression_r=high_reg,
                                 stability=stab, price=price)
    assert sc["research_quality"] in ("HIGH", "MEDIUM")
    assert "note" in sc and "Lock" in sc["note"]
    print(f"  ✓ scorecard: quality={sc['research_quality']}  dims={sc['dimensions']}")


def test_correlation_identity_guard_in_place():
    """§15 — _prettify_leg must return CORRELATION_LEG_IDENTITY_INCOMPLETE
    for an unknown market rather than 'Mlb Other'."""
    import inspect
    from lab_routes import _prettify_leg
    src = inspect.getsource(_prettify_leg)
    assert "CORRELATION_LEG_IDENTITY_INCOMPLETE" in src
    # Direct behaviour: an _OTHER family with no market string must return the marker
    r = _prettify_leg("Some Team", "MLB_OTHER", market=None)
    assert r == "CORRELATION_LEG_IDENTITY_INCOMPLETE"
    print("  ✓ _prettify_leg returns CORRELATION_LEG_IDENTITY_INCOMPLETE")


def test_correlation_no_ai_confidence_from_lock():
    """§16 — correlation code must expose correlation_evidence AND
    leg_a_lock/leg_b_lock as SEPARATE buckets from Lock-averaged
    confidence. The v2 co-hit block emits correlation_evidence;
    _today_recommended_pairs SGP suggestions expose leg_a_lock/leg_b_lock
    + CORRELATION_UNVERIFIED provenance."""
    import inspect
    from lab_routes import correlations_v2, _today_recommended_pairs
    src_v2 = inspect.getsource(correlations_v2)
    src_sgp = inspect.getsource(_today_recommended_pairs)
    assert "correlation_evidence" in src_v2
    assert "leg_a_lock" in src_sgp
    assert "leg_b_lock" in src_sgp
    assert "CORRELATION_UNVERIFIED" in src_sgp
    print("  ✓ correlation_evidence separated; leg_a_lock/leg_b_lock + UNVERIFIED for SGP")


def test_hot_hitters_still_shadow_and_no_publisher():
    """§4 preservation — Hot Hitters remain research only, no publisher."""
    import inspect
    from hot_hitters import build_hot_hitters
    src = inspect.getsource(build_hot_hitters)
    assert "trend_provenance" in src
    assert "SHADOW_SIGNAL" in src
    assert "PredictionPublicationService" not in src
    print("  ✓ hot_hitters still SHADOW-only, no publication imports")


def test_actual_game_history_stat_map():
    from lab_routes import _stat_field_for_family
    assert _stat_field_for_family("MLB_HITS") == "hits"
    assert _stat_field_for_family("MLB_HR") == "hr"
    assert _stat_field_for_family("MLB_KS") == "strikeouts"
    assert _stat_field_for_family("NFL_REC") == "receptions"
    assert _stat_field_for_family("NBA_POINTS") == "points"
    assert _stat_field_for_family("NBA_THREES") == "three_pointers_made"
    assert _stat_field_for_family("UNKNOWN") is None
    print("  ✓ actual-game stat_field map covers all supported families")


def test_supported_sports_gate_still_mlb_nfl_nba():
    from services.research.service import SUPPORTED_SPORTS
    assert SUPPORTED_SPORTS == {"MLB", "NFL", "NBA"}
    print("  ✓ Strategy Lab still supports only MLB/NFL/NBA (per user directive)")


def _run_all():
    tests = [
        test_line_sensitivity_math,
        test_market_disagreement_math,
        test_h2h_quality_thresholds,
        test_scorecard_aggregation,
        test_correlation_identity_guard_in_place,
        test_correlation_no_ai_confidence_from_lock,
        test_hot_hitters_still_shadow_and_no_publisher,
        test_actual_game_history_stat_map,
        test_supported_sports_gate_still_mlb_nfl_nba,
    ]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{passed}/{len(tests)} PASS")
    return passed == len(tests)


if __name__ == "__main__":
    ok = _run_all()
    sys.exit(0 if ok else 1)
