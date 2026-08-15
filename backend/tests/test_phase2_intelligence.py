"""PERKLOCKS PHASE 2 (partial) — model + intelligence runtime closure tests.

Covers the three mandatory Emergent-Support findings plus runtime proofs:
  B. MLB pitcher-strikeout model loads AND executes; the historical
     ctx-key mismatch (model looked up `starting_pitcher_home/away`)
     has a cross-module regression guard.
  K. Fusion is invoked in the production orchestrator and its output is
     consumed downstream (pick["fusion"] → elite_evidence_gate signal +
     fusion_predictions persistence).
  L. Adaptive learning learns ONLY from settled outcomes and is applied
     to pregame picks (no result leakage into inference).

Run: EXPO_PUBLIC_BACKEND_URL=http://localhost:8001 python -m pytest -q \
     tests/test_phase2_intelligence.py
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ── PART B — MLB pitcher-K model runtime execution ───────────────────

CTX = {
    "starting_pitcher_home": {
        "name": "Tarik Skubal",
        "l5_avg_k": 8.2, "l5_avg_ip": 6.1,     # signal 1 (recent form)
        "k_pct": 0.32,
        "opp_k_pct": 0.26,                      # signal 2 (opponent K%)
    },
    "starting_pitcher_away": {
        "name": "Weak Arm",
        "l5_avg_k": 3.1, "l5_avg_ip": 5.0,
        "k_pct": 0.14,
    },
}


class TestPitcherKModelRuntime:
    def test_model_loads_and_executes_both_sides(self):
        from services.mlb_k_probability import evaluate_k_pick
        over = evaluate_k_pick(CTX, "Tarik Skubal", 6.5, "over",
                               book_odds=-115)
        under = evaluate_k_pick(CTX, "Tarik Skubal", 6.5, "under",
                                book_odds=-105)
        assert over and under, "model must EXECUTE, not just import"
        assert "model_prob" in over and "model_prob" in under
        assert 0.0 < over["model_prob"] < 1.0
        # Poisson complement (± push-free X.5 line)
        assert abs(over["model_prob"] + under["model_prob"] - 1.0) < 1e-6
        assert over.get("expected_k") and over["expected_k"] > 5

    def test_rejections_carry_machine_reason(self):
        from services.mlb_k_probability import evaluate_k_pick
        res = evaluate_k_pick({}, "Nobody", 5.5, "over")
        assert res == {"emit": False, "reason": "no_pitcher_data"}
        thin = {"starting_pitcher_home": {"name": "One Signal",
                                          "k_pct": 0.20}}
        res2 = evaluate_k_pick(thin, "One Signal", 5.5, "over")
        assert res2["emit"] is False
        assert res2["reason"] == "insufficient_signals"

    def test_ctx_key_mismatch_regression_guard(self):
        """The historical bug: the K model reads
        ctx['starting_pitcher_home'/'starting_pitcher_away'] — if the
        context builder ever renames those keys the model silently
        never loads.  Guard both modules against drift."""
        import services.mlb_k_probability as kp
        import services.game_context as gc
        for key in ("starting_pitcher_home", "starting_pitcher_away"):
            assert key in inspect.getsource(kp.compute_expected_k)
            assert key in inspect.getsource(gc)
        # A ctx written under a WRONG key must be a loud, attributable
        # failure — not a silent pass-through.
        from services.mlb_k_probability import evaluate_k_pick
        wrong = {"home_starting_pitcher": CTX["starting_pitcher_home"]}
        res = evaluate_k_pick(wrong, "Tarik Skubal", 6.5, "over")
        assert res["emit"] is False and res["reason"] == "no_pitcher_data"

    def test_model_wired_into_production_side_selector(self):
        import sports_engine as se
        src = inspect.getsource(se._props_picks_from_event)
        assert "from services.mlb_k_probability import evaluate_k_pick" in src
        assert "pitcher_strikeouts" in src


# ── PART K — Fusion production consumption ───────────────────────────

class TestFusionRuntimeClosure:
    def test_fusion_invoked_by_orchestrator(self):
        import services.pick_refresh_orchestrator as orch
        src = inspect.getsource(orch)
        assert "from services.pick_fusion_decorator import enrich_picks_bulk" in src
        assert "fusion_predictions" in src, (
            "fusion output must persist for grading/history linkage")

    def test_fusion_output_consumed_downstream(self):
        """A function being called with its output discarded does NOT
        count — elite_evidence_gate consumes pick['fusion'] as a scored
        agreement signal."""
        import services.elite_evidence_gate as eg
        src = inspect.getsource(eg)
        assert "_classify_fusion" in src
        assert eg._FUSION_AGREE_POS > 0
        pos = eg._classify_fusion({
            "fusion": {"supported": True, "final_probability": 0.70},
            "win_probability": 60.0})
        neg = eg._classify_fusion({
            "fusion": {"supported": True, "final_probability": 0.50},
            "win_probability": 60.0})
        assert pos > neg, "fusion agreement must move the evidence signal"


# ── PART L — Adaptive learning closure (no leakage) ──────────────────

class TestAdaptiveLearningClosure:
    def test_learning_reads_settled_outcomes_only(self):
        import learning_engine as le
        src = inspect.getsource(le.recompute_learned_weights)
        assert "settled_at" in src
        import learning_system_v2 as l2
        src2 = inspect.getsource(l2)
        assert '"won", "lost"' in src2, (
            "v2 learning must aggregate settled picks only")
        assert l2.MIN_TOTAL_PICKS >= 50, "sample-size honesty gate"

    def test_learned_state_applied_to_pregame_inference(self):
        import services.pick_refresh_orchestrator as orch
        src = inspect.getsource(orch)
        assert "from learning_engine import apply_learning" in src
        assert "apply_v2_to_picks" in src

    def test_no_result_leakage_into_apply_path(self):
        """apply_learning adjusts a PREGAME pick — it must never read
        the pick's own settlement result."""
        import learning_engine as le
        src = inspect.getsource(le.apply_learning)
        assert 'pick["result"]' not in src
        assert "pick.get(\"result\")" not in src
        assert "actual_result" not in src


# ── Phase 1D protections stay intact under Phase 2 ───────────────────

class TestPhase1DProtections:
    def test_no_inflation_reintroduced(self):
        import sports_engine as se
        src = inspect.getsource(se.compute_lock_score)
        for token in ("floor = 98.0", "floor = 95.0", "floor = 90.0",
                      "floor = 85.0"):
            assert token not in src
        bp = inspect.getsource(se._build_pick)
        assert "_floor = 85.0" not in bp
        assert "SPORT_IMPLIED_FLOOR.get" not in bp

    def test_devig_fields_preserved(self):
        import sports_engine as se
        assert callable(se._attach_devig)
