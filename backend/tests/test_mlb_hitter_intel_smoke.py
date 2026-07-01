"""Smoke test for the MLB Hitter Intel separated-scoring refactor.

Runs entirely on synthetic inputs (no MLB API calls). Verifies:
  1. HitterMatchup exposes three distinct sub-scores.
  2. lean_and_edge routes each market label to the right score.
  3. HitterContextMissing fires when pitcher / lineup / Vegas are absent.
  4. to_rationale() surfaces market_scores + vegas_context.
"""
from __future__ import annotations

import asyncio
import sys
import traceback
from pathlib import Path

# Make backend importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import mlb_hitter_intel as mhi  # noqa: E402


def _mk_matchup() -> mhi.HitterMatchup:
    bs = mhi.BatterSplits(
        season_avg=0.285, last5_avg=0.320, avg_vs_r=0.290,
        obp=0.360, iso=0.190, bat_side="L",
        pa_vs_l=80, pa_vs_r=400, last5_ab=20, last5_hits=6,
    )
    pp = mhi.PitcherProfile(
        era=4.60, whip=1.35, k_per_9=7.5, bb_per_9=3.4, h_per_9=9.0,
        avg_against_l=0.270, throw_hand="R",
        k_pct=0.19, bb_pct=0.09, bf_l=200, bf_r=400,
    )
    m = mhi.HitterMatchup(
        batter_id=1, pitcher_id=2,
        batter_name="Test Batter", pitcher_name="Test Pitcher",
        batter=bs, pitcher=pp,
        ballpark="yankee stadium", is_home=True, batting_order=3,
    )
    m.base_form = mhi._base_form(bs)
    m.final_hit_prob = m.base_form * 1.10
    m.team_implied_runs = 5.2
    m.obp_in_front = 0.345
    m.p_hit_score = mhi._score_hit(m)
    m.p_rbi_score = mhi._score_rbi(m)
    m.p_run_score = mhi._score_run(m)
    m.summary = "smoke-test"
    return m


def test_three_distinct_scores():
    m = _mk_matchup()
    assert 0.30 <= m.p_hit_score <= 0.90, m.p_hit_score
    assert 0.10 <= m.p_rbi_score <= 0.72, m.p_rbi_score
    assert 0.10 <= m.p_run_score <= 0.72, m.p_run_score
    assert m.p_hit_score != m.p_rbi_score != m.p_run_score, "scores collapsed to one value"
    print(f"  ✓ p_hit={m.p_hit_score:.3f}  p_rbi={m.p_rbi_score:.3f}  p_run={m.p_run_score:.3f}")


def test_lean_routes_by_market():
    m = _mk_matchup()
    hit  = mhi.lean_and_edge(m, market_implied_prob=0.55, line=0.5, market="Hits O 0.5")
    rbi  = mhi.lean_and_edge(m, market_implied_prob=0.35, line=0.5, market="RBIs O 0.5")
    run  = mhi.lean_and_edge(m, market_implied_prob=0.40, line=0.5, market="Runs O 0.5")
    combo = mhi.lean_and_edge(m, market_implied_prob=0.65, line=0.5, market="Hits+Runs+RBIs O 1.5")
    assert abs(hit["model_prob"]  - m.p_hit_score)  < 1e-4, hit
    assert abs(rbi["model_prob"]  - m.p_rbi_score)  < 1e-4, rbi
    assert abs(run["model_prob"]  - m.p_run_score)  < 1e-4, run
    assert combo["model_prob"] > max(m.p_hit_score, m.p_rbi_score, m.p_run_score), combo
    print(f"  ✓ Hit lean={hit['lean']} edge={hit['edge_pct_points']}pp")
    print(f"  ✓ RBI lean={rbi['lean']} edge={rbi['edge_pct_points']}pp")
    print(f"  ✓ Run lean={run['lean']} edge={run['edge_pct_points']}pp")
    print(f"  ✓ H+R+RBI union p={combo['model_prob']:.3f}")


def test_rationale_exposes_market_scores():
    m = _mk_matchup()
    r = m.to_rationale()
    assert "market_scores" in r
    assert "vegas_context" in r
    ms = r["market_scores"]
    assert set(ms.keys()) == {"p_hit_pct", "p_rbi_pct", "p_run_pct"}
    assert r["vegas_context"]["team_implied_runs"] == 5.2
    print(f"  ✓ rationale.market_scores = {ms}")
    print(f"  ✓ rationale.vegas_context = {r['vegas_context']}")


def test_strict_gate_fires():
    # No real DB needed — build_matchup will short-circuit on the strict
    # guard before touching Mongo.
    async def run():
        class FakeDB:
            class FakeColl:
                async def find_one(self, *a, **k): return None
                async def update_one(self, *a, **k): return None
            mlb_hitter_intel_cache = FakeColl()
        db = FakeDB()
        # Missing team_implied_runs → should raise
        try:
            await mhi.build_matchup(
                db, batter_id=1, pitcher_id=2,
                batter_name="X", pitcher_name="Y",
                batting_order=3, team_implied_runs=None,
                strict=True,
            )
        except mhi.HitterContextMissing as e:
            print(f"  ✓ strict gate raised: {e}")
            return
        raise AssertionError("HitterContextMissing did NOT fire")
    asyncio.run(run())


if __name__ == "__main__":
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                print(f"▶ {name}")
                fn()
            except Exception as e:
                failed += 1
                print(f"  ✗ {name}: {e}")
                traceback.print_exc()
    print(f"\n{'PASS' if failed == 0 else 'FAIL'} — {failed} failures")
    sys.exit(1 if failed else 0)
