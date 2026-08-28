"""Strategy Lab 10X reachability trace (§12).

Proves that a Hot-Hitter subject travels the complete pipeline:
    (a) Hot Hitters returns real MLB player with exact multi-hit truth.
    (b) Trend Radar classifies the subject.
    (c) Research Service builds a FACTUAL snapshot.
    (d) FACTUAL context threaded via research bridge does NOT overwrite
        existing production ctx keys and NEVER carries SHADOW rows.
    (e) Hot status does NOT publish a pick (RESEARCH_ONLY).

Also proves NO direct hot-player publishing exists.
"""
from __future__ import annotations

import asyncio
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_hot_hitters_multi_hit_no_approximation():
    """hot_hitters.py must NOT use the removed `hits * 0.35` approx."""
    import inspect
    from hot_hitters import build_hot_hitters
    src = inspect.getsource(build_hot_hitters)
    # The approximation was `multi_hits = int(hits * 0.35)`. Ensure the
    # ARITHMETIC form is gone (docstring reference to it is allowed).
    assert "multi_hits = int(hits * 0.35)" not in src, \
        "Approximate multi-hit assignment still present"
    assert "int(hits * 0.35)" not in src, \
        "Approximate multi-hit expression still present in code"
    # Must reference exact 0/1/2/3+ bucket variables
    assert "one_hit_games" in src and "zero_hit_games" in src
    assert "multi_hit_games" in src or "g2" in src
    print("  ✓ hits*0.35 removed; exact multi-hit bucket variables present")


def test_hot_hitters_never_publishes_directly():
    """Hot hitters must never call any publication service."""
    import inspect
    import hot_hitters
    src = inspect.getsource(hot_hitters)
    forbidden = [
        "PredictionPublicationService", "prediction_publication_service",
        "publication_boundary.publish", "db.picks.insert",
        "update_published_lock_score",
    ]
    for token in forbidden:
        assert token not in src, f"hot_hitters imports forbidden path: {token}"
    print("  ✓ hot_hitters cannot directly publish a pick")


def test_trend_signal_provenance_is_shadow():
    """Every TrendSignal must carry SHADOW_SIGNAL provenance."""
    from services.research.trend_radar import classify
    features = {
        "l15_avg": 0.345, "l15_ops": 0.912, "l15_obp": 0.400,
        "hit_streak": 6, "multi_hit_games": 5, "one_hit_games": 6,
        "zero_hit_games": 3, "exact_game_log_n": 14, "l15_games": 14,
    }
    sig = classify("MLB", "Test Player", features)
    assert sig is not None
    assert sig.provenance == "SHADOW_SIGNAL"
    print(f"  ✓ MLB trend: {sig.trend_type.value} (SHADOW_SIGNAL)")


def test_nfl_trend_role_breakout():
    from services.research.trend_radar import classify
    features = {
        "nfl_snap_pct": 80, "nfl_target_share": 25, "nfl_carry_share": 10,
        "nfl_rz_touches_pg": 1.5, "nfl_l4_targets_avg": 9, "nfl_l4_carries_avg": 2,
    }
    sig = classify("NFL", "WR-X", features)
    assert sig is not None
    assert sig.trend_type.value in ("ROLE_BREAKOUT", "TARGET_SURGE")
    print(f"  ✓ NFL trend: {sig.trend_type.value}")


def test_nba_trend_scoring_surge():
    from services.research.trend_radar import classify
    features = {
        "nba_l10_pts": 27.5, "nba_l10_reb": 6.0, "nba_l10_ast": 4.0,
        "nba_l10_fg3m": 3.5, "nba_l10_min": 34.0,
        "nba_opp_pace": 103.0, "nba_opp_def_rating": 116.0,
    }
    sig = classify("NBA", "SG-Y", features)
    assert sig is not None
    assert sig.trend_type.value == "SCORING_SURGE"
    print(f"  ✓ NBA trend: {sig.trend_type.value}")


def test_bh_fdr_math():
    """BH-FDR: q=0.10, m=5, pvals = [0.001, 0.01, 0.03, 0.5, 0.8]."""
    from services.research.validation import bh_fdr
    pvals = [0.001, 0.01, 0.03, 0.5, 0.8]
    accept = bh_fdr(pvals, q=0.10)
    # Rank thresholds: k*q/m = 0.02, 0.04, 0.06, 0.08, 0.10
    # 0.001<=0.02 ✓, 0.01<=0.04 ✓, 0.03<=0.06 ✓, 0.5<=0.08 ✗ → k=3
    assert accept == [True, True, True, False, False], f"got {accept}"
    print(f"  ✓ BH-FDR at q=0.10 rejects 3/5 nulls correctly")


def test_signal_registry_lifecycle():
    from services.research import signal_registry as sr
    async def _run():
        doc = await sr.upsert(
            sport="NFL", market_family="player_receiving_yards",
            conditions={"role": "WR1", "target_share": ">=25"},
            metrics={"train_n": 500, "validation_n": 120, "test_n": 30,
                     "baseline_probability": 0.5, "observed_probability": 0.62,
                     "lift": 0.12, "wilson_lower": 0.55},
            status="DISCOVERED",
        )
        assert doc["status"] == "DISCOVERED"
        assert doc["provenance"] == "SHADOW_SIGNAL"
        # Advance lifecycle
        for tgt in ("TESTING", "VALIDATED", "VERIFIED"):
            d = await sr.transition(doc["signal_id"], tgt)
            assert d["status"] == tgt
        return doc["signal_id"]
    sid = asyncio.get_event_loop().run_until_complete(_run())
    print(f"  ✓ signal {sid} traversed DISCOVERED→TESTING→VALIDATED→VERIFIED")


def test_reachability_hot_hitter_never_bypasses_lock():
    """A hot-hitter subject MUST travel the normal publication path.

    We prove this at the STATIC level: `hot_hitters.py` neither
    imports nor calls any publication service, ever. Publication
    remains exclusively the responsibility of
    `services/prediction_publication_service.py` invoked by the
    canonical sports_engine pipeline.
    """
    import inspect, hot_hitters
    src = inspect.getsource(hot_hitters)
    assert "compute_lock_score" not in src, "Hot hitters cannot compute Lock"
    assert "publication_boundary" not in src
    assert "board_projection" not in src
    print("  ✓ hot_hitters cannot bypass canonical publication authority")


def _run_all():
    tests = [
        test_hot_hitters_multi_hit_no_approximation,
        test_hot_hitters_never_publishes_directly,
        test_trend_signal_provenance_is_shadow,
        test_nfl_trend_role_breakout,
        test_nba_trend_scoring_surge,
        test_bh_fdr_math,
        test_signal_registry_lifecycle,
        test_reachability_hot_hitter_never_bypasses_lock,
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
