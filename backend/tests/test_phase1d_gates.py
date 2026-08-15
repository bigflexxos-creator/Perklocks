"""PERKLOCKS PHASE 1D — gate reconstruction (G1–G7) tests.

Run: EXPO_PUBLIC_BACKEND_URL=http://localhost:8001 python -m pytest -q \
     tests/test_phase1d_gates.py
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

import sports_engine as se  # noqa: E402
from services import funnel_telemetry as funnel  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_funnel():
    funnel.drain()
    yield
    funnel.drain()


def _mk(sport="NFL", model=0.60, odds=-110, lock=80.0, market="X Moneyline",
        **kw):
    return se._build_pick(
        sport=sport, league=sport, event="Away @ Home",
        event_time="2026-06-15T23:00:00Z", market=market, pick_side="X",
        model_win_prob=model, book_odds=odds, lock=lock,
        factors={}, insights=[], external_id=f"t-{sport}-{odds}-{model}",
        **kw)


# ── G1 — implied-probability floors retired / favorite-dog neutrality ─

class TestG1ImpliedFloors:
    def test_plus_money_underdog_with_strong_edge_passes(self):
        # +150 (40% implied), model 55% → edge +15 — old SPORT_IMPLIED_FLOOR
        # (0.50-0.56) and model floor (0.58) both killed this.
        p = _mk(model=0.55, odds=150)
        assert p is not None
        assert p["edge_percent"] > 10

    def test_short_favorite_with_weak_edge_fails(self):
        # -400 (80% implied), model 70% → edge -10 → EDGE_THRESHOLD.
        p = _mk(model=0.70, odds=-400)
        assert p is None
        recs = funnel.peek(reason="EDGE_THRESHOLD")
        assert recs, "edge rejection must be funnel-attributable"

    def test_no_automatic_favorite_floor_no_chalk_cap(self):
        # -600 chalk with genuine model edge passes (old -450 cap killed);
        # its lock score is whatever the model earned (no floor).
        p = _mk(model=0.92, odds=-600, lock=70.0)
        assert p is not None
        assert p["lock_score"] == 70.0

    def test_implied_floor_code_retired(self):
        src = inspect.getsource(se._build_pick)
        assert "SPORT_IMPLIED_FLOOR.get" not in src
        assert "book_implied < 0.42" not in src


# ── G2 — model-probability floors retired ────────────────────────────

class TestG2ModelFloors:
    def test_sub_58_model_prob_with_positive_edge_passes(self):
        # model 45% vs +200 (33.3% implied) → edge +11.7 — old universal
        # 0.58 floor rejected any sub-58% candidate regardless of value.
        p = _mk(model=0.45, odds=200)
        assert p is not None

    def test_mlb_62_floor_retired(self):
        p = _mk(sport="MLB", model=0.55, odds=-105)
        assert p is not None

    def test_truly_negative_edge_still_rejected(self):
        p = _mk(model=0.40, odds=-110)   # edge ≈ -12.4
        assert p is None


# ── G3 — score inflation removed ─────────────────────────────────────

class TestG3Inflation:
    def test_generation_booster_retired(self):
        # wp 72%, edge ≈ +19.6 — the old booster floored lock to 85-97.
        p = _mk(model=0.72, odds=-110, lock=64.0)
        assert p is not None
        assert p["lock_score"] == 64.0, (
            "lock score must be earned, not boosted at generation time")

    def test_compute_lock_score_ladder_retired(self):
        src = inspect.getsource(se.compute_lock_score)
        assert "floor = 98.0" not in src
        assert "floor = 95.0" not in src
        assert "floor = 90.0" not in src
        assert "floor = 85.0" not in src

    def test_no_hardcoded_85_floor_in_build_pick(self):
        src = inspect.getsource(se._build_pick)
        assert "_floor = 85.0" not in src


# ── G4 — single authoritative >=85 board rule ────────────────────────

class TestG4BoardRule:
    def _pick(self, score):
        return {"sport": "MLB", "book_odds": -120,
                "implied_probability": 54.5, "market": "X Moneyline",
                "lock_score": score, "published_lock_score": score}

    def test_8499_not_eligible(self):
        from services.main_board_eligibility import is_main_board_eligible
        assert is_main_board_eligible(self._pick(84.99)) is False

    def test_8500_eligible(self):
        from services.main_board_eligibility import is_main_board_eligible
        assert is_main_board_eligible(self._pick(85.00)) is True

    def test_8501_eligible(self):
        from services.main_board_eligibility import is_main_board_eligible
        assert is_main_board_eligible(self._pick(85.01)) is True

    def test_query_uses_gte(self):
        from services.main_board_eligibility import main_board_lock_score_query
        q = str(main_board_lock_score_query())
        assert "$gte" in q and "85" in q

    def test_no_sport_specific_publication_floor_remains(self):
        src = inspect.getsource(se._build_pick)
        assert "if lock < min_lock:\n        # PHASE 1D" in src or \
               "return None" not in src.split("if lock < min_lock:")[1][:200]


# ── G5 — de-vig market probability ───────────────────────────────────

class TestG5Devig:
    def test_two_way_moneyline(self):
        p = {"sport": "NFL", "market": "X Moneyline", "event": "A @ B",
             "book_odds": -110, "win_probability": 60.0}
        se._attach_devig(p, [-110])
        assert p["raw_implied_probability"] == 52.4
        assert p["devig_market_probability"] == 50.0
        assert p["devig_method"] == "2_way_normalization"
        assert p["devig_edge_percent"] == 10.0
        assert p["book_odds"] == -110  # raw odds preserved

    def test_three_way_soccer_normalization(self):
        p = {"sport": "Soccer", "market": "H Moneyline", "event": "A @ H",
             "book_odds": 120, "win_probability": 50.0}
        se._attach_devig(p, [230, 260])
        assert p["devig_method"] == "3_way_normalization"
        assert 0 < p["devig_market_probability"] < 100

    def test_missing_opposing_side_telemetried(self):
        p = {"sport": "Tennis", "market": "X Moneyline", "event": "A vs B",
             "book_odds": -150, "win_probability": 65.0}
        se._attach_devig(p, [])
        assert "raw_implied_probability" in p
        assert "devig_market_probability" not in p
        assert funnel.peek(reason="OPPOSING_SIDE_UNAVAILABLE")

    def test_wired_into_nfl_production_path(self):
        from tests.test_phase1b_runtime_wiring import _game, NFL_CTX_OK
        g = _game("americanfootball_nfl")
        g["_ctx"] = dict(NFL_CTX_OK)
        picks = se._picks_from_game("NFL", "NFL", g, "2026-06-15")
        assert picks
        for p in picks:
            assert "raw_implied_probability" in p
            assert "devig_market_probability" in p


# ── G6 — both-sides / favorite-underdog neutrality ───────────────────

class TestG6Neutrality:
    def test_model_not_odds_sign_chooses_ml_side(self):
        """Book favors HOME (-150) but the model expects the AWAY team
        to win — the emitted ML side must be the plus-money underdog."""
        from tests.test_phase1b_runtime_wiring import _game
        g = _game("americanfootball_nfl")
        g["_ctx"] = {"nfl_model_available": True,
                     "expected_margin_home": -9.0,   # away better by 9
                     "expected_total": 51.0}
        picks = se._picks_from_game("NFL", "NFL", g, "2026-06-15")
        ml = [p for p in picks if "Moneyline" in p["market"]]
        assert ml, "underdog ML must survive when the model warrants it"
        assert ml[0]["selection"] == g["away_team"]
        assert ml[0]["book_odds"] > 0    # plus-money side emitted

    def test_favorite_wins_when_model_warrants(self):
        from tests.test_phase1b_runtime_wiring import _game, NFL_CTX_OK
        g = _game("americanfootball_nfl")
        g["_ctx"] = dict(NFL_CTX_OK)     # home better by 9
        picks = se._picks_from_game("NFL", "NFL", g, "2026-06-15")
        ml = [p for p in picks if "Moneyline" in p["market"]]
        assert ml and ml[0]["selection"] == g["home_team"]

    def test_equivalent_edge_symmetry_in_build_pick(self):
        fav = _mk(model=0.66, odds=-150)     # implied 60 → edge +6
        dog = _mk(model=0.46, odds=150)      # implied 40 → edge +6
        assert fav is not None and dog is not None


# ── G7 + NFL evidence gate ───────────────────────────────────────────

class TestNFLEvidenceGate:
    def _platinum_pick(self, edge=3.0):
        return {"id": "x", "sport": "NFL", "event": "A @ B",
                "market": "B Moneyline", "selection": "B",
                "event_time": "2026-06-15T23:00:00Z", "book_odds": -110,
                "lock_score": 88, "factors": {}, "edge_percent": edge,
                "model_source": "platinum_nfl_game_sim",
                "platinum_game_sim": {"sim_probability": 0.64,
                                      "expected_margin_home": 6.5,
                                      "expected_total": 47.0}}

    def test_platinum_candidate_passes_evidence_gate(self):
        from board_validator import evidence_threshold
        kept, stats = evidence_threshold([self._platinum_pick(edge=3.0)])
        assert len(kept) == 1, stats

    def test_platinum_counts_at_most_two_categories(self):
        """Sim prob + ratings context = 2; a candidate with weak edge
        (< 1.5, no third signal) must still fail — no multi-counting."""
        from board_validator import evidence_threshold
        kept, stats = evidence_threshold([self._platinum_pick(edge=0.5)])
        assert kept == [], "2 platinum categories alone must not pass 3-of-6"

    def test_book_price_only_candidate_still_fails(self):
        from board_validator import evidence_threshold
        weak = {"id": "w", "sport": "NBA", "event": "A @ B",
                "market": "B Moneyline", "selection": "B",
                "event_time": "2026-06-15T23:00:00Z", "book_odds": -300,
                "lock_score": 90, "factors": {}, "edge_percent": 0.0}
        kept, _ = evidence_threshold([weak])
        assert kept == []
        assert funnel.peek(reason="EVIDENCE_THRESHOLD")

    def test_nfl_platinum_reaches_scoring_and_gate_end_to_end(self):
        from tests.test_phase1b_runtime_wiring import _game, NFL_CTX_OK
        from board_validator import evidence_threshold
        g = _game("americanfootball_nfl")
        g["_ctx"] = dict(NFL_CTX_OK)
        picks = se._picks_from_game("NFL", "NFL", g, "2026-06-15")
        assert picks, "candidates must reach scoring"
        for i, p in enumerate(picks):
            p["id"] = f"e2e-{i}"
        kept, stats = evidence_threshold(picks)
        strong = [p for p in kept
                  if p.get("model_source") == "platinum_nfl_game_sim"
                  and (p.get("edge_percent") or 0) >= 1.5]
        assert strong, (
            f"legitimate Platinum candidates must pass the evidence "
            f"gate; stats={stats}")
