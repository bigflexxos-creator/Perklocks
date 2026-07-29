"""Soccer Prediction + Fusion Wiring Tests (Phase 7 Part 4c, iter109,
2026-06).

Verifies the fully-wired soccer pipeline:
  A. Interim model registry — all 6 soccer models load through the
     shared registry with `interim: true` stamped in their meta json.
  B. Market → stat routing (7 canonical soccer market shapes).
  C. Match-level markets safe-skip (moneyline, spreads, totals, BTTS).
  D. Threshold inference for implicit ≥1 markets (Anytime / First Goal
     Scorer / To Score or Assist → 0.5 line).
  E. Soccer opponent resolver — uses `soccer_player_game_logs` to look
     up the player's team and pick the OPPOSITE side of the fixture.
  F. `predict_player_prop("SOCCER", …)` returns real probabilities +
     projections for supported stats.
  G. `enrich_pick_with_fusion` produces `ml.available=True` for
     supported soccer picks.
  H. Player H2H component uses `soccer_player_game_logs` (not the
     mixed `player_game_logs` collection).
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib

import pytest


def _run(c): return asyncio.run(c)


def _fresh_db():
    from motor.motor_asyncio import AsyncIOMotorClient
    return AsyncIOMotorClient(os.getenv("MONGO_URL"))["lockscore_db"]


MODELS = pathlib.Path("/app/backend/models")

_SOCCER_TRAINED_STATS = ("goals", "assists", "shots",
                          "shots_on_target", "xg", "goal_contributions")


# ═════════════════════════════════════════════════════════════════════
# A. Interim model registry
# ═════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("stat", _SOCCER_TRAINED_STATS)
def test_A1_soccer_model_files_present(stat):
    meta = MODELS / f"soccer_{stat}.meta.json"
    assert meta.exists(), f"missing meta: {meta}"
    j = json.loads(meta.read_text())
    assert j.get("winner") in ("lgbm", "xgb")
    pkl = MODELS / f"soccer_{stat}_{j['winner']}.pkl"
    assert pkl.exists(), f"missing model pickle: {pkl}"


@pytest.mark.parametrize("stat", _SOCCER_TRAINED_STATS)
def test_A2_soccer_models_load_via_registry(stat):
    from services.trained_prediction_engine import (
        _load_model, _reset_model_cache,
    )
    _reset_model_cache()
    bundle = _load_model("SOCCER", stat)
    assert bundle is not None, f"soccer/{stat} did not load"
    assert bundle["booster"] is not None
    assert len(bundle["feature_names"]) == 16
    assert float(bundle["residual_std"]) > 0
    assert bundle["winner"] in ("lgbm", "xgb")


@pytest.mark.parametrize("stat", _SOCCER_TRAINED_STATS)
def test_A3_interim_flag_stamped(stat):
    """Every soccer model must be flagged `interim=true` until the
    full retrain script (`train_soccer_full.py`) runs."""
    meta = json.loads((MODELS / f"soccer_{stat}.meta.json").read_text())
    assert meta.get("interim") is True, (
        f"soccer/{stat} meta missing interim=true flag"
    )
    assert "EPL 2024-25" in (meta.get("interim_reason") or ""), (
        f"soccer/{stat} interim_reason not stamped"
    )


# ═════════════════════════════════════════════════════════════════════
# B. Market → stat detection
# ═════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("market,expected", [
    # Player props → real stats
    ("Mohamed Salah Anytime Goal Scorer",         "goals"),
    ("Cole Palmer First Goal Scorer",              "goals"),
    ("Player X Player To Score",                   "goals"),
    ("Bukayo Saka To Score or Assist",             "goal_contributions"),
    ("Player X Goals + Assists Over 0.5",          "goal_contributions"),
    ("Bruno Fernandes Anytime Assist",             "assists"),
    ("Kylian Mbappe Shots On Target Over 1.5",     "shots_on_target"),
    ("Cody Gakpo Total Shots Over 2.5",            "shots"),
    ("Erling Haaland xG Over 0.5",                 "xg"),
])
def test_B1_soccer_market_detection(market, expected):
    from services.pick_matchup_wiring import _detect_stat
    assert _detect_stat("Soccer", market) == expected


# ═════════════════════════════════════════════════════════════════════
# C. Match-level markets safe-skip
# ═════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("market", [
    "Arsenal Moneyline",
    "Liverpool Moneyline",
    "Total Goals Over 2.5",
    "Total Goals Under 3.5",
    "Both Teams To Score",
    "BTTS",
    "Correct Score 2-1",
    "Double Chance",
    "Asian Handicap -1.5",
])
def test_C1_soccer_match_level_safe_skip(market):
    from services.pick_matchup_wiring import _detect_stat
    assert _detect_stat("Soccer", market) is None, market


def test_C2_soccer_moneyline_safe_skip_in_fusion_parser():
    from services.pick_fusion_decorator import _parse_pick
    pick = {"sport": "Soccer", "market": "Arsenal Moneyline",
             "selection": "Arsenal",
             "event": "Liverpool @ Arsenal", "id": "ml1"}
    assert _parse_pick(pick) is None


# ═════════════════════════════════════════════════════════════════════
# D. Threshold inference for implicit ≥1 markets
# ═════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("market", [
    "Mohamed Salah Anytime Goal Scorer",
    "Cole Palmer First Goal Scorer",
    "Bukayo Saka To Score or Assist",
    "Player X Anytime Assist",
])
def test_D1_infer_soccer_threshold_half(market):
    from services.pick_matchup_wiring import _infer_soccer_threshold
    assert _infer_soccer_threshold(market) == 0.5


def test_D2_infer_soccer_threshold_none_for_explicit_line():
    """Markets that DO have a numeric line should return None from the
    inferrer (the explicit `_parse_threshold` catches those)."""
    from services.pick_matchup_wiring import _infer_soccer_threshold
    # Shots Over 2.5 has an explicit line — no inference needed.
    assert _infer_soccer_threshold("Player X Shots Over 2.5") is None


def test_D3_parse_pick_includes_inferred_threshold():
    from services.pick_fusion_decorator import _parse_pick
    p = _parse_pick({
        "sport": "Soccer",
        "market": "Mohamed Salah Anytime Goal Scorer",
        "selection": "Mohamed Salah",
        "event": "Liverpool @ Arsenal",
    })
    assert p["threshold"] == 0.5


# ═════════════════════════════════════════════════════════════════════
# E. Soccer opponent resolver
# ═════════════════════════════════════════════════════════════════════
def test_E1_soccer_opponent_left_side_player():
    """Player is on the LEFT (home) side — opponent is RIGHT (away)."""
    from services.pick_matchup_wiring import _parse_opponent_soccer
    async def go():
        db = _fresh_db()
        opp = await _parse_opponent_soccer(
            db, "Liverpool @ Arsenal", "Mohamed Salah",
        )
        assert opp == "Arsenal"
    _run(go())


def test_E2_soccer_opponent_right_side_player():
    """Player is on the RIGHT (away) side — opponent is LEFT (home)."""
    from services.pick_matchup_wiring import _parse_opponent_soccer
    async def go():
        db = _fresh_db()
        opp = await _parse_opponent_soccer(
            db, "Manchester City @ Liverpool", "Mohamed Salah",
        )
        assert opp == "Manchester City"
    _run(go())


def test_E3_soccer_opponent_no_player_fallback():
    """No player name → returns 2nd side (best-effort)."""
    from services.pick_matchup_wiring import _parse_opponent_soccer
    async def go():
        db = _fresh_db()
        opp = await _parse_opponent_soccer(db, "A @ B", None)
        assert opp == "B"
    _run(go())


# ═════════════════════════════════════════════════════════════════════
# F. predict_player_prop for SOCCER returns real projections
# ═════════════════════════════════════════════════════════════════════
def test_F1_predict_soccer_supports_all_6_stats():
    from services.trained_prediction_engine import (
        predict_player_prop, _reset_model_cache,
    )
    async def go():
        _reset_model_cache()
        db = _fresh_db()
        for stat, line in (
            ("goals",              0.5),
            ("assists",            0.5),
            ("shots",              2.5),
            ("shots_on_target",    1.5),
            ("xg",                 0.5),
            ("goal_contributions", 0.5),
        ):
            r = await predict_player_prop(
                db, sport="SOCCER", player="Mohamed Salah",
                stat=stat, opponent="Arsenal", line=line,
            )
            if not r.get("supported"):
                reason = (r.get("reason") or "").lower()
                assert "no trained model" not in reason, (
                    f"soccer/{stat}: {reason}"
                )
            else:
                assert r["prediction_probability"] is not None
                assert 0.0 <= r["prediction_probability"] <= 1.0
                assert r["expected_value"] is not None
    _run(go())


def test_F2_predict_soccer_unsupported_stat_safe_fail():
    from services.trained_prediction_engine import predict_player_prop
    async def go():
        db = _fresh_db()
        r = await predict_player_prop(
            db, sport="SOCCER", player="X", stat="tackles",
            opponent="Y", line=1.5,
        )
        assert r["supported"] is False
        assert "no trained model" in (r.get("reason") or "").lower()
    _run(go())


# ═════════════════════════════════════════════════════════════════════
# G. Fusion end-to-end for soccer picks
# ═════════════════════════════════════════════════════════════════════
def test_G1_fusion_ml_available_for_anytime_scorer():
    from services.pick_fusion_decorator import enrich_pick_with_fusion
    async def go():
        db = _fresh_db()
        p = {
            "sport":     "Soccer",
            "market":    "Mohamed Salah Anytime Goal Scorer",
            "selection": "Mohamed Salah",
            "event":     "Liverpool @ Arsenal",
            "id":        "iter109-g1",
        }
        r = await enrich_pick_with_fusion(db, p, persist=False)
        assert r["fusion"]["supported"] is True
        ml = r["fusion"]["components"]["ml"]
        assert ml.get("available") is True, ml
        joined = " ".join(ml.get("notes", []))
        assert "no trained model" not in joined.lower(), joined
    _run(go())


def test_G2_fusion_ml_available_for_to_score_or_assist():
    from services.pick_fusion_decorator import enrich_pick_with_fusion
    async def go():
        db = _fresh_db()
        p = {
            "sport":     "Soccer",
            "market":    "Cole Palmer To Score or Assist",
            "selection": "Cole Palmer",
            "event":     "Chelsea @ Fulham",
            "id":        "iter109-g2",
        }
        r = await enrich_pick_with_fusion(db, p, persist=False)
        assert r["fusion"]["supported"] is True
        ml = r["fusion"]["components"]["ml"]
        assert ml.get("available") is True, ml
    _run(go())


def test_G3_fusion_ml_available_for_shots():
    from services.pick_fusion_decorator import enrich_pick_with_fusion
    async def go():
        db = _fresh_db()
        p = {
            "sport":     "Soccer",
            "market":    "Erling Haaland Total Shots Over 3.5",
            "selection": "Erling Haaland",
            "event":     "Manchester City @ Liverpool",
            "id":        "iter109-g3",
        }
        r = await enrich_pick_with_fusion(db, p, persist=False)
        assert r["fusion"]["supported"] is True
        ml = r["fusion"]["components"]["ml"]
        assert ml.get("available") is True, ml
    _run(go())


def test_G4_fusion_skips_moneyline():
    from services.pick_fusion_decorator import enrich_pick_with_fusion
    async def go():
        db = _fresh_db()
        p = {
            "sport":     "Soccer",
            "market":    "Arsenal Moneyline",
            "selection": "Arsenal",
            "event":     "Liverpool @ Arsenal",
            "id":        "iter109-g4",
        }
        r = await enrich_pick_with_fusion(db, p, persist=False)
        assert r["fusion"]["supported"] is False
    _run(go())


def test_G5_fusion_skips_total_goals():
    from services.pick_fusion_decorator import enrich_pick_with_fusion
    async def go():
        db = _fresh_db()
        p = {
            "sport":     "Soccer",
            "market":    "Total Goals Over 2.5",
            "selection": "Over",
            "event":     "Liverpool @ Arsenal",
            "id":        "iter109-g5",
        }
        r = await enrich_pick_with_fusion(db, p, persist=False)
        assert r["fusion"]["supported"] is False
    _run(go())


# ═════════════════════════════════════════════════════════════════════
# H. Player H2H lookup uses soccer_player_game_logs
# ═════════════════════════════════════════════════════════════════════
def test_H1_soccer_h2h_uses_soccer_player_game_logs():
    """Player-H2H component must resolve rows from the soccer
    collection (not the mixed `player_game_logs`)."""
    from services.player_matchup_intelligence import (
        _lookup_soccer_recent, _lookup_soccer_vs_opponent,
    )
    async def go():
        db = _fresh_db()
        recent = await _lookup_soccer_recent(
            db, "Mohamed Salah", "goals", limit=10,
        )
        # Salah has ingested rows → some historical values expected.
        assert isinstance(recent, list)
        vs_opp = await _lookup_soccer_vs_opponent(
            db, "Mohamed Salah", "Arsenal", "goals", limit=10,
        )
        assert isinstance(vs_opp, list)
    _run(go())


def test_H2_soccer_matchup_intelligence_returns_sample():
    """`get_matchup_intelligence` for soccer must return non-zero
    sample_size when the player has rows in the DB."""
    from services.player_matchup_intelligence import (
        get_matchup_intelligence,
    )
    async def go():
        db = _fresh_db()
        n = await db.soccer_player_game_logs.count_documents(
            {"name_canonical": "mohamed salah"})
        if n < 5:
            pytest.skip(f"Salah not fully ingested yet (n={n})")
        r = await get_matchup_intelligence(
            db, sport="Soccer", player_name="Mohamed Salah",
            stat="goals", opponent_team="Arsenal", threshold=0.5,
        )
        assert r.sample_size > 0
        assert "soccer_player_game_logs" in r.data_sources_used
    _run(go())


__all__: list[str] = []
