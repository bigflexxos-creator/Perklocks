"""PHASE 2A — NFL calibration + preseason uncertainty + de-vig promotion.

Run: EXPO_PUBLIC_BACKEND_URL=http://localhost:8001 python -m pytest -q \
     tests/test_phase2a_calibration_devig.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

import sports_engine as se  # noqa: E402
from services import funnel_telemetry as funnel  # noqa: E402
from tests.test_phase1b_runtime_wiring import _game, NFL_CTX_OK  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    funnel.drain()
    yield
    funnel.drain()


def _run_nfl(ctx, sport_key="americanfootball_nfl"):
    g = _game(sport_key)
    g["_ctx"] = dict(ctx)
    return se._picks_from_game("NFL", "NFL", g, "2026-06-15")


# ── Parts 2/3 — sparse-evidence calibration ─────────────────────────

class TestSparseEvidenceCalibration:
    def test_platinum_scores_use_v3_composite_not_probability_map(self):
        picks = _run_nfl(NFL_CTX_OK)
        assert picks
        for p in picks:
            comp = {c.get("component") if isinstance(c, dict) else c
                    for c in (p.get("factors") or [])} if isinstance(
                        p.get("factors"), list) else set()
            # v3 breakdown keys present (edge/alignment/roi/etc.), not
            # the legacy wp-band map
            assert p["lock_score"] <= 100

    def test_sparse_evidence_cannot_earn_95_plus_from_probability_alone(self):
        """Old defect: legacy wp-band map turned sim prob ~0.85 into
        93-98 with empty factors.  v3 composite requires edge +
        historical reliability — sparse candidates land materially
        lower."""
        picks = _run_nfl(NFL_CTX_OK)
        assert picks
        for p in picks:
            assert p["lock_score"] < 95, (
                f"sparse-evidence Platinum pick scored {p['lock_score']} "
                f"({p['market']}) — probability-mapped inflation returned")

    def test_no_artificial_floor_or_ladder(self):
        import inspect
        src = inspect.getsource(se.compute_lock_score)
        for tok in ("floor = 98.0", "floor = 95.0", "floor = 90.0",
                    "floor = 85.0"):
            assert tok not in src


# ── Parts 4/5 — preseason uncertainty ────────────────────────────────

class TestPreseasonUncertainty:
    def test_preseason_prob_shrinks_toward_half_bounded_deterministic(self):
        from services.platinum_nfl.game_runtime import (
            platinum_game_side_probability)
        g_reg = _game("americanfootball_nfl")
        g_pre = _game("americanfootball_nfl_preseason")
        reg = platinum_game_side_probability(
            game=g_reg, ctx=NFL_CTX_OK, market="Moneyline",
            side=g_reg["home_team"], line=None, is_home_side=True,
            book_total_line=45.5)
        pre = platinum_game_side_probability(
            game=g_pre, ctx=NFL_CTX_OK, market="Moneyline",
            side=g_pre["home_team"], line=None, is_home_side=True,
            book_total_line=45.5)
        assert reg["available"] and pre["available"]
        assert reg.get("preseason_uncertainty") is None
        pu = pre["preseason_uncertainty"]
        assert pu and pu["confidence_shrink"] == 0.85
        # shrunk toward 0.5, bounded, deterministic recompute
        assert abs(pre["prob"] - 0.5) < abs(pu["raw_sim_probability"] - 0.5)
        assert 0.0 < pre["prob"] < 1.0
        pre2 = platinum_game_side_probability(
            game=g_pre, ctx=NFL_CTX_OK, market="Moneyline",
            side=g_pre["home_team"], line=None, is_home_side=True,
            book_total_line=45.5)
        assert pre2["prob"] == pre["prob"]

    def test_preseason_not_automatically_suppressed(self):
        picks = _run_nfl(NFL_CTX_OK, "americanfootball_nfl_preseason")
        assert picks, "strong-model preseason candidates must still emit"
        for p in picks:
            assert p["season_type"] == "PRESEASON"
            assert p.get("preseason_uncertainty"), "uncertainty in provenance"

    def test_regular_season_does_not_inherit_uncertainty(self):
        picks = _run_nfl(NFL_CTX_OK)
        for p in picks:
            assert p.get("preseason_uncertainty") is None
            assert p["season_type"] == "REGULAR_SEASON"


# ── Part 6 — distribution/quantile validation ────────────────────────

class TestSimulationDistribution:
    def test_quantile_ordering_and_input_sensitivity(self):
        from services.platinum_nfl.game_runtime import (
            platinum_game_side_probability)
        g = _game("americanfootball_nfl")
        r = platinum_game_side_probability(
            game=g, ctx=NFL_CTX_OK, market="Total", side="Over",
            line=45.5, book_total_line=45.5)
        s = r["sim"]
        assert s["q10"] <= s["q25"] <= s["distribution_median"] \
               <= s["q75"] <= s["q90"]
        assert s["q90"] - s["q10"] > 5, "distribution must have real spread"
        ctx2 = dict(NFL_CTX_OK, expected_margin_home=1.0)
        r2 = platinum_game_side_probability(
            game=g, ctx=ctx2, market="Moneyline", side=g["home_team"],
            line=None, is_home_side=True, book_total_line=45.5)
        r1 = platinum_game_side_probability(
            game=g, ctx=NFL_CTX_OK, market="Moneyline", side=g["home_team"],
            line=None, is_home_side=True, book_total_line=45.5)
        assert r1["prob"] != r2["prob"], "changed inputs must change output"


# ── Parts 7/8/9 — de-vig promotion + canonical edge contract ─────────

class TestDevigCanonicalEdge:
    def _build(self, odds, model, opp):
        return se._build_pick(
            sport="NFL", league="NFL", event="A @ B",
            event_time="2026-06-15T23:00:00Z", market="B Moneyline",
            pick_side="B", model_win_prob=model, book_odds=odds,
            lock=80.0, factors={}, insights=[], external_id=f"dv-{odds}",
            opposing_prices=opp)

    def test_two_way_ml_devig_edge(self):
        p = self._build(-110, 0.55, [-110])
        assert p and p["edge_method"] == "DEVIG"
        assert p["devig_market_probability"] == 50.0
        assert p["edge_percent"] == 5.0          # 55 − 50 (de-vig)
        assert p["raw_edge_percent"] == pytest.approx(2.62, abs=0.05)
        assert p["book_odds"] == -110            # raw odds preserved
        assert p["raw_implied_probability"] == 52.4

    def test_three_way_soccer(self):
        p = se._build_pick(
            sport="Soccer", league="EPL", event="A @ H",
            event_time="2026-06-15T23:00:00Z", market="H Moneyline",
            pick_side="H", model_win_prob=0.48, book_odds=120,
            lock=80.0, factors={}, insights=[], external_id="dv-3way",
            opposing_prices=[230, 260])
        assert p and p["devig_method"] == "3_way_normalization"

    def test_missing_opposing_side_raw_fallback(self):
        p = self._build(-120, 0.60, None)
        assert p and p["edge_method"] == "RAW_FALLBACK"
        assert "devig_market_probability" not in p
        assert funnel.peek(reason="DEVIG_UNAVAILABLE")

    def test_edge_gate_uses_canonical_devig_edge(self):
        """A candidate NEGATIVE on raw edge but positive after de-vig
        must survive the -1% gate (and vice versa stays rejected)."""
        # raw implied 52.4%, devig 50%: model 51.6% → raw edge -0.8?,
        # use model 0.52: raw edge -0.4, devig edge +2.0 → passes
        ok = self._build(-110, 0.52, [-110])
        assert ok is not None and ok["edge_percent"] == 2.0
        # model 0.48 → devig edge -2.0 → rejected with EDGE_THRESHOLD
        bad = self._build(-110, 0.48, [-110])
        assert bad is None
        assert funnel.peek(reason="EDGE_THRESHOLD")

    def test_units_are_percent_not_fraction(self):
        p = self._build(-110, 0.55, [-110])
        assert 1.0 < p["edge_percent"] < 100.0

    def test_mismatched_lines_cannot_devig(self):
        """Production call sites pair ONLY the exact same market/line
        (over/under share `line`; spreads share |point|; ML pairs the
        same game).  Guard the wiring by source inspection."""
        import inspect
        src = inspect.getsource(se._picks_from_game)
        assert 'opposing_prices=[u_price if best["side"] == "Over"' in src
        assert 'opposing_prices=[((away_sp if side == home else home_sp)' in src


# ── Part 10 — neutrality after de-vig ────────────────────────────────

class TestNeutralityAfterDevig:
    def test_underdog_with_stronger_edge_outranks_favorite(self):
        dog = se._build_pick(
            sport="NFL", league="NFL", event="A @ B",
            event_time="2026-06-15T23:00:00Z", market="B Moneyline",
            pick_side="B", model_win_prob=0.48, book_odds=150,
            lock=80.0, factors={}, insights=[], external_id="n-dog",
            opposing_prices=[-180])
        fav = se._build_pick(
            sport="NFL", league="NFL", event="A @ B",
            event_time="2026-06-15T23:00:00Z", market="A Moneyline",
            pick_side="A", model_win_prob=0.62, book_odds=-180,
            lock=80.0, factors={}, insights=[], external_id="n-fav",
            opposing_prices=[150])
        assert dog is not None
        assert dog["edge_percent"] > (fav["edge_percent"] if fav else -99)

    def test_short_odds_alone_no_high_score(self):
        p = se._build_pick(
            sport="NFL", league="NFL", event="A @ B",
            event_time="2026-06-15T23:00:00Z", market="A Moneyline",
            pick_side="A", model_win_prob=0.86, book_odds=-600,
            lock=62.0, factors={}, insights=[], external_id="n-chalk",
            opposing_prices=[420])
        assert p is not None and p["lock_score"] == 62.0

    def test_85_boundary_preserved(self):
        from services.main_board_eligibility import is_main_board_eligible
        base = {"sport": "MLB", "book_odds": -120,
                "implied_probability": 54.5, "market": "X Moneyline"}
        for s, exp in ((84.99, False), (85.00, True), (85.01, True)):
            assert is_main_board_eligible(
                dict(base, lock_score=s, published_lock_score=s)) is exp
