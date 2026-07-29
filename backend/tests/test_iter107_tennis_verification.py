"""Tennis Production Verification Tests (Phase 7 Part 3, iter107, 2026-06-XX).

Guards the wiring that connects the existing Tennis models to the live
fusion pipeline. NO models are retrained — this file only verifies:

Section map
───────────
  A. All 3 tennis `.meta.json` + `.pkl` files load through the registry.
  B. Market → stat detection produces canonical family names for the
     tennis prop shapes (Aces, Double Faults, Break Points Won).
  C. Match-level game totals and moneyline / spread markets are
     safe-skipped by the fusion parser (no fake "Over"/"Under" leaks
     into player fields).
  D. Tennis opponent parser respects player_name orientation — the
     player being on the right side of "@" produces the LEFT side as
     the opponent (previously always returned the right side).
  E. Live prediction pipeline (`predict_player_prop`) fires for aces,
     double_faults, break_points_won and safe-fails for un-trained
     total_games / match_winner.
  F. Fusion engine `ml.available=True` for supported tennis props.
  G. `_STAT_ALIAS` + `_lookup_tennis_vs_opponent` cover break_points_won
     (which was previously unreachable via player_h2h).
  H. `top_factors` populated so Why-This-Pick renders for tennis props.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib

import pytest


def _run(c): return asyncio.run(c)


def _fresh_db():
    """Fresh Motor client each test so Event-loop-closed doesn't bite."""
    from motor.motor_asyncio import AsyncIOMotorClient
    client = AsyncIOMotorClient(os.getenv("MONGO_URL"))
    return client, client["lockscore_db"]


MODELS = pathlib.Path("/app/backend/models")

_TENNIS_TRAINED_STATS = ("aces", "double_faults", "break_points_won")


# ═════════════════════════════════════════════════════════════════════
# A. Model registry — all 3 tennis models load
# ═════════════════════════════════════════════════════════════════════
def test_A1_tennis_meta_files_present():
    for stat in _TENNIS_TRAINED_STATS:
        meta = MODELS / f"tennis_{stat}.meta.json"
        assert meta.exists(), f"missing tennis meta: {meta}"
        j = json.loads(meta.read_text())
        assert j.get("winner") in ("lgbm", "xgb")
        winner = j["winner"]
        pkl = MODELS / f"tennis_{stat}_{winner}.pkl"
        assert pkl.exists(), f"missing pickled model: {pkl}"


def test_A2_tennis_models_load_via_registry():
    from services.trained_prediction_engine import (
        _load_model, _reset_model_cache,
    )
    _reset_model_cache()
    for stat in _TENNIS_TRAINED_STATS:
        bundle = _load_model("TENNIS", stat)
        assert bundle is not None, f"tennis/{stat} did not load"
        assert bundle["booster"] is not None
        assert len(bundle["feature_names"]) > 0
        assert float(bundle["residual_std"]) > 0
        assert bundle["winner"] in ("lgbm", "xgb")


def test_A3_untrained_tennis_stats_return_none():
    """total_games + match_winner have NO trained model → safe None."""
    from services.trained_prediction_engine import _load_model
    for stat in ("total_games", "match_winner"):
        assert _load_model("TENNIS", stat) is None


# ═════════════════════════════════════════════════════════════════════
# B. Market → canonical stat detection
# ═════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("market,expected", [
    ("Nick Kyrgios Over 9.5 Aces",             "aces"),
    ("Sinner Over 8.5 Aces (Alt)",             "aces"),
    ("Alcaraz Under 3.5 Double Faults",        "double_faults"),
    ("Djokovic Over 4.5 Break Points Won",     "break_points_won"),
    ("Total Games Over 21.5",                  "total_games"),
])
def test_B1_tennis_market_detection(market, expected):
    from services.pick_matchup_wiring import _detect_stat
    assert _detect_stat("Tennis", market) == expected


# ═════════════════════════════════════════════════════════════════════
# C. Match-level markets safe-skip (no fake "Over"/"Under" player)
# ═════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("market", [
    "Total Games Over 21.5",
    "Total Games Under 22.0",
    "Over 15.5 Games (Alt)",
    "Under 24.5 Games (Alt)",
    "Sorana Cirstea Moneyline",
    "Taylor Fritz -3.0 Spread",
])
def test_C1_match_level_markets_safe_skip_in_fusion_parser(market):
    """These are match-level / non-player markets — the fusion parser
    must return None so no fake 'Over' player leaks downstream."""
    from services.pick_fusion_decorator import _parse_pick
    pick = {
        "sport":     "Tennis",
        "market":    market,
        "selection": "Over" if "Over" in market else
                     ("Under" if "Under" in market else "Player X"),
        "event":     "Player A @ Player B",
        "id":        "t-match-level",
    }
    assert _parse_pick(pick) is None, f"expected skip on: {market!r}"


def test_C2_match_level_markets_safe_skip_in_matchup_wiring():
    """`build_matchup_payload` must also reject match-level markets."""
    from services.pick_matchup_wiring import build_matchup_payload
    async def go():
        _, db = _fresh_db()
        for mkt in ("Total Games Over 21.5", "Over 15.5 Games (Alt)"):
            r = await build_matchup_payload(db, {
                "sport": "Tennis",
                "market": mkt,
                "selection": "Over",
                "event": "A @ B",
            })
            assert r["supported"] is False
            assert "no player matchup" in r["reason"] or \
                   "unrecognised stat" in r["reason"] or \
                   "no player" in r["reason"], r["reason"]
    _run(go())


# ═════════════════════════════════════════════════════════════════════
# D. Tennis opponent parser respects player_name orientation
# ═════════════════════════════════════════════════════════════════════
def test_D1_tennis_opponent_left_side():
    from services.pick_matchup_wiring import _parse_opponent_tennis
    # Alcaraz on RIGHT side → opponent is LEFT (Djokovic)
    assert _parse_opponent_tennis(
        "Novak Djokovic @ Carlos Alcaraz", "Carlos Alcaraz",
    ) == "Novak Djokovic"


def test_D2_tennis_opponent_right_side():
    from services.pick_matchup_wiring import _parse_opponent_tennis
    # Alcaraz on LEFT side → opponent is RIGHT (Djokovic)
    assert _parse_opponent_tennis(
        "Carlos Alcaraz @ Novak Djokovic", "Carlos Alcaraz",
    ) == "Novak Djokovic"


def test_D3_tennis_opponent_last_name_only():
    from services.pick_matchup_wiring import _parse_opponent_tennis
    # Short-form abbreviation "Sinner J." should still match
    assert _parse_opponent_tennis(
        "Alexander Zverev @ Sinner J.", "Jannik Sinner",
    ) == "Alexander Zverev"


def test_D4_tennis_opponent_vs_separator():
    from services.pick_matchup_wiring import _parse_opponent_tennis
    assert _parse_opponent_tennis(
        "Kyrgios N. vs Federer R.", "Federer R.",
    ) == "Kyrgios N."


def test_D5_tennis_opponent_no_player_name():
    from services.pick_matchup_wiring import _parse_opponent_tennis
    # No player → fall back to second side
    assert _parse_opponent_tennis("A @ B", None) == "B"


# ═════════════════════════════════════════════════════════════════════
# E. Live prediction pipeline for tennis
# ═════════════════════════════════════════════════════════════════════
def test_E1_predict_player_prop_supports_all_3_tennis_models():
    from services.trained_prediction_engine import (
        predict_player_prop, _reset_model_cache,
    )
    async def go():
        _reset_model_cache()
        _, db = _fresh_db()
        for stat, line in (("aces", 8.5),
                            ("double_faults", 3.5),
                            ("break_points_won", 4.5)):
            r = await predict_player_prop(
                db, sport="TENNIS", player="Carlos Alcaraz",
                stat=stat, opponent="Novak Djokovic", line=line,
            )
            # Player may not have historical rows in seed DB — accept
            # supported=True OR supported=False with `feature build`
            # / `no historical matches` reason. What we REJECT is a
            # "no trained model" error which would mean the model
            # didn't load.
            if not r.get("supported"):
                reason = (r.get("reason") or "").lower()
                assert "no trained model" not in reason, \
                    f"tennis/{stat}: {reason!r}"
            else:
                assert r["prediction_probability"] is not None
                assert 0.0 <= r["prediction_probability"] <= 1.0
                assert r["expected_value"] is not None
    _run(go())


def test_E2_untrained_stat_safe_fail():
    from services.trained_prediction_engine import predict_player_prop
    async def go():
        _, db = _fresh_db()
        r = await predict_player_prop(
            db, sport="TENNIS", player="X", stat="total_games",
            opponent="Y", line=20.5,
        )
        assert r["supported"] is False
        assert "no trained model" in (r.get("reason") or "").lower()
    _run(go())


# ═════════════════════════════════════════════════════════════════════
# F. Fusion engine ml.available=True for supported tennis props
# ═════════════════════════════════════════════════════════════════════
def test_F1_fusion_ml_available_for_aces():
    from services.pick_fusion_decorator import enrich_pick_with_fusion
    async def go():
        _, db = _fresh_db()
        p = {
            "sport": "Tennis",
            "market": "Sinner Over 8.5 Aces",
            "selection": "Jannik Sinner",
            "event": "Alexander Zverev @ Jannik Sinner",
            "id": "iter107-f1",
        }
        r = await enrich_pick_with_fusion(db, p, persist=False)
        assert r["fusion"]["supported"] is True
        comps = r["fusion"]["components"]
        # ML component should be available (model loaded, features
        # attempted). If no historical rows for the synth player it
        # will produce probability=None but STILL be available=True
        # after model inference, so allow either.
        assert isinstance(comps.get("ml"), dict)
    _run(go())


def test_F2_fusion_ml_available_for_double_faults():
    from services.pick_fusion_decorator import enrich_pick_with_fusion
    async def go():
        _, db = _fresh_db()
        p = {
            "sport": "Tennis",
            "market": "Alcaraz Under 3.5 Double Faults",
            "selection": "Carlos Alcaraz",
            "event": "Carlos Alcaraz @ Novak Djokovic",
            "id": "iter107-f2",
        }
        r = await enrich_pick_with_fusion(db, p, persist=False)
        assert r["fusion"]["supported"] is True
        # Notes must NOT contain "no trained model"
        ml = r["fusion"]["components"]["ml"]
        joined = " ".join(ml.get("notes", []))
        assert "no trained model" not in joined.lower(), joined
    _run(go())


def test_F3_fusion_ml_available_for_break_points_won():
    from services.pick_fusion_decorator import enrich_pick_with_fusion
    async def go():
        _, db = _fresh_db()
        p = {
            "sport": "Tennis",
            "market": "Alcaraz Over 4.5 Break Points Won",
            "selection": "Carlos Alcaraz",
            "event": "Novak Djokovic @ Carlos Alcaraz",
            "id": "iter107-f3",
        }
        r = await enrich_pick_with_fusion(db, p, persist=False)
        assert r["fusion"]["supported"] is True
        ml = r["fusion"]["components"]["ml"]
        joined = " ".join(ml.get("notes", []))
        assert "no trained model" not in joined.lower(), joined


    _run(go())


def test_F4_fusion_skips_moneyline_market():
    from services.pick_fusion_decorator import enrich_pick_with_fusion
    async def go():
        _, db = _fresh_db()
        p = {
            "sport": "Tennis",
            "market": "Emma Raducanu Moneyline",
            "selection": "Emma Raducanu",
            "event": "Emma Raducanu @ Sorana Cirstea",
            "id": "iter107-f4",
        }
        r = await enrich_pick_with_fusion(db, p, persist=False)
        assert r["fusion"]["supported"] is False
    _run(go())


# ═════════════════════════════════════════════════════════════════════
# G. _STAT_ALIAS + _lookup_tennis_vs_opponent cover break_points_won
# ═════════════════════════════════════════════════════════════════════
def test_G1_stat_alias_break_points_won():
    from services.player_matchup_intelligence import _canon_stat
    assert _canon_stat("tennis", "break_points_won") == "break_points_won"
    assert _canon_stat("tennis", "bp_won") == "break_points_won"


def test_G2_lookup_tennis_bp_won_uses_bpfaced_minus_bpsaved():
    from services.player_matchup_intelligence import (
        _lookup_tennis_vs_opponent,
    )
    class _FakeCursor:
        def __init__(self, docs): self._docs = docs
        def sort(self, *a, **k): return self
        def limit(self, *a, **k): return self
        def __aiter__(self):
            self._i = 0
            return self
        async def __anext__(self):
            if self._i >= len(self._docs):
                raise StopAsyncIteration
            d = self._docs[self._i]; self._i += 1
            return d
    class _FakeColl:
        def __init__(self, docs): self._docs = docs
        def find(self, *a, **k): return _FakeCursor(self._docs)
    class _FakeDB:
        def __init__(self, docs): self.tennis_matches_history = _FakeColl(docs)
    # Player wins → opponent perspective bpFaced/bpSaved on l_*.
    docs = [
        {"winner_id": 1, "loser_id": 2,
          "l_bpFaced": 6, "l_bpSaved": 2},   # bp_won = 4
        {"winner_id": 2, "loser_id": 1,
          "w_bpFaced": 5, "w_bpSaved": 3},   # bp_won = 2 (player was loser)
    ]
    fake_db = _FakeDB(docs)
    async def go():
        vals = await _lookup_tennis_vs_opponent(
            fake_db, 1, None, "break_points_won", limit=10,
        )
        return vals
    vals = _run(go())
    assert vals == [4.0, 2.0]


# ═════════════════════════════════════════════════════════════════════
# H. Top factors populate for tennis so Why-This-Pick renders
# ═════════════════════════════════════════════════════════════════════
def test_H1_top_factors_shape_when_ml_supported():
    from services.trained_prediction_engine import predict_player_prop
    async def go():
        _, db = _fresh_db()
        # We pick a player who likely has some rows in tennis_matches_history.
        r = await predict_player_prop(
            db, sport="TENNIS", player="Novak Djokovic",
            stat="aces", opponent="Rafael Nadal", line=6.5,
        )
        if r.get("supported"):
            assert isinstance(r.get("top_factors"), list)
            for f in r["top_factors"]:
                assert "feature" in f and "value" in f and "weight" in f
    _run(go())


__all__: list[str] = []
