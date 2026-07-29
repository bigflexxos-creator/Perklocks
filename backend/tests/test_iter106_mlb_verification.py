"""MLB Production Verification Tests (Phase 7 Part 2, iter106, 2026-07-29).

Guards the wiring that connects MLB models to the live fusion pipeline.
NO models are retrained — this file only verifies existing artifacts
still load, route correctly, and reach fusion + Why-This-Pick.

Section map
───────────
  A. All 4 MLB `.meta.json` + `.pkl` files load through the registry.
  B. Market → stat detection produces canonical family names.
  C. Batter vs pitcher routing gates strikeouts correctly (position +
     threshold both work; batter Ks safe-fail; pitcher Ks route to
     `pitcher_strikeouts` model).
  D. Live prediction pipeline (`predict_player_prop`) fires for hits,
     home_runs, total_bases on a real batter and pitcher_strikeouts
     on a real pitcher.
  E. Fusion engine `ml.available=True` for supported MLB props.
  F. `top_factors` populated on the ML result so Why-This-Pick renders.
  G. Off-model MLB families (rbi, hits_runs_rbis, walks) safe-fail
     without an exception.
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


# ═════════════════════════════════════════════════════════════════════
# A. Model registry — all 4 MLB models
# ═════════════════════════════════════════════════════════════════════
_MLB_STATS = ("hits", "home_runs", "pitcher_strikeouts", "total_bases")


@pytest.mark.parametrize("stat", _MLB_STATS)
def test_mlb_model_meta_and_pkl_exist(stat):
    meta = MODELS / f"mlb_{stat}.meta.json"
    assert meta.exists(), f"missing meta.json for MLB/{stat}"
    m = json.loads(meta.read_text())
    assert m.get("sport") == "MLB"
    assert m.get("stat") == stat
    assert m.get("winner") in ("lgbm", "xgb")
    pkl = meta.with_name(f"mlb_{stat}_{m['winner']}.pkl")
    assert pkl.exists(), f"pkl missing: {pkl.name}"


@pytest.mark.parametrize("stat", _MLB_STATS)
def test_mlb_model_loads_through_registry(stat):
    from services.trained_prediction_engine import _load_model
    b = _load_model("MLB", stat)
    assert b is not None, f"MLB/{stat} bundle failed to load"
    assert b["booster"] is not None
    assert b["feature_names"], f"MLB/{stat} has no feature_names"


# ═════════════════════════════════════════════════════════════════════
# B. Market detector
# ═════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("market, expected", [
    ("Aaron Judge (NYY) Over 1.5 Hits",              "hits"),
    ("Cody Bellinger (NYY) Over 0.5 Home Runs",      "home_runs"),
    ("Aaron Judge (NYY) Over 1.5 Total Bases",       "total_bases"),
    ("Aaron Nola (PHI) Over 4.5 Strikeouts",         "strikeouts"),
    ("Aaron Judge (NYY) Over 0.5 Strikeouts",        "strikeouts"),
])
def test_mlb_market_detector(market, expected):
    from services.pick_matchup_wiring import _detect_stat
    assert _detect_stat("MLB", market) == expected


# ═════════════════════════════════════════════════════════════════════
# C. Batter vs pitcher routing
# ═════════════════════════════════════════════════════════════════════
def test_pitcher_k_routes_by_position_or_threshold():
    from services.trained_prediction_engine import _resolve_model_key
    # Pitcher position — routes even at line 0.5
    stat, notes = _resolve_model_key("MLB", "strikeouts", line=0.5,
                                        player_position="SP")
    assert stat == "pitcher_strikeouts"
    # High threshold — routes even when position unknown
    stat, notes = _resolve_model_key("MLB", "strikeouts", line=5.5,
                                        player_position=None)
    assert stat == "pitcher_strikeouts"
    # Batter position — NEVER promotes even at high threshold
    stat, notes = _resolve_model_key("MLB", "strikeouts", line=5.5,
                                        player_position="OF")
    assert stat == "strikeouts", (
        f"Batter K prop was misrouted to pitcher model — got {stat}")


def test_batter_ks_safe_fail_with_clear_reason():
    """Regression guard: a batter K prop must NEVER load the pitcher
    model and must return a supported=False with a clear reason."""
    from services.trained_prediction_engine import predict_player_prop
    client, db = _fresh_db()
    try:
        async def _q():
            return await predict_player_prop(
                db, sport="MLB", player="Aaron Judge",
                stat="strikeouts", opponent="BOS",
                line=1.5, player_position="OF",
            )
        r = _run(_q())
    finally:
        client.close()
    assert r["supported"] is False
    assert r["effective_stat"] == "strikeouts", (
        "Batter K resolved to pitcher model — critical routing bug")
    assert "batter" in r["reason"].lower()


# ═════════════════════════════════════════════════════════════════════
# D + E + F. Live end-to-end + fusion + Why-This-Pick
# ═════════════════════════════════════════════════════════════════════
def _live_batter_id_or_skip():
    """Pick a real MLB batter with recent game logs — skip when the
    DB isn't populated (fresh CI, etc.)."""
    from motor.motor_asyncio import AsyncIOMotorClient
    client = AsyncIOMotorClient(os.getenv("MONGO_URL"))
    try:
        async def _q():
            for pid in (592450, 660271, 665742, 545361):
                row = await client["lockscore_db"].player_game_logs.find_one(
                    {"sport": "mlb", "player_id": pid,
                     "at_bats": {"$gt": 0}},
                    {"_id": 0, "player_id": 1})
                if row:
                    p = await client["lockscore_db"].players.find_one(
                        {"sport": "mlb", "player_id": pid},
                        {"name": 1, "position": 1, "_id": 0}) or {}
                    return p.get("name"), p.get("position") or "OF"
            return None, None
        name, pos = _run(_q())
    finally:
        client.close()
    if not name:
        pytest.skip("no MLB batter game logs available in this env")
    return name, pos


def _live_pitcher_id_or_skip():
    from motor.motor_asyncio import AsyncIOMotorClient
    client = AsyncIOMotorClient(os.getenv("MONGO_URL"))
    try:
        async def _q():
            async for r in client["lockscore_db"].player_game_logs.find(
                {"sport": "mlb", "pitcher_strikeouts": {"$gte": 5}},
                {"_id": 0, "player_id": 1},
            ).limit(20):
                pid = r["player_id"]
                p = await client["lockscore_db"].players.find_one(
                    {"sport": "mlb", "player_id": pid},
                    {"name": 1, "position": 1, "_id": 0}) or {}
                pos = p.get("position") or ""
                if pos in ("P", "SP", "RP"):
                    return p["name"], pos
            return None, None
        name, pos = _run(_q())
    finally:
        client.close()
    if not name:
        pytest.skip("no MLB pitcher game logs available in this env")
    return name, pos


@pytest.mark.parametrize("stat, line", [("hits", 0.5),
                                          ("home_runs", 0.5),
                                          ("total_bases", 1.5)])
def test_live_batter_prediction(stat, line):
    from services.trained_prediction_engine import predict_player_prop
    name, pos = _live_batter_id_or_skip()
    client, db = _fresh_db()
    try:
        r = _run(predict_player_prop(
            db, sport="MLB", player=name, stat=stat,
            opponent="?", line=line, player_position=pos,
        ))
    finally:
        client.close()
    assert r.get("supported") is True, (
        f"MLB/{stat} live prediction unsupported: {r.get('reason')}")
    assert 0.0 <= r["prediction_probability"] <= 1.0
    assert isinstance(r["expected_value"], (int, float))
    assert r.get("top_factors"), f"MLB/{stat} missing top_factors"


def test_live_pitcher_strikeouts_end_to_end():
    from services.trained_prediction_engine import predict_player_prop
    name, pos = _live_pitcher_id_or_skip()
    client, db = _fresh_db()
    try:
        r = _run(predict_player_prop(
            db, sport="MLB", player=name, stat="strikeouts",
            opponent="?", line=5.5, player_position=pos,
        ))
    finally:
        client.close()
    assert r["supported"] is True, (
        f"Pitcher K prediction unsupported: {r.get('reason')}")
    assert r["effective_stat"] == "pitcher_strikeouts"
    assert "pitcher_strikeouts" in " ".join(r.get("routing_notes") or [])
    assert r.get("top_factors")


def test_fusion_engine_ml_available_for_mlb():
    """The audit's central regression — fusion ML component must fire
    on a real MLB prop, not silently return `available:false`."""
    from services.prediction_fusion_engine import fuse_prediction
    name, pos = _live_batter_id_or_skip()
    client, db = _fresh_db()
    try:
        r = _run(fuse_prediction(
            db, sport="MLB", player=name, stat="hits",
            opponent="?", threshold=0.5, persist=False,
        ))
    finally:
        client.close()
    ml = r.components["ml"].to_dict()
    assert ml["available"] is True, (
        f"MLB fusion ML component still not firing — {ml}")
    assert 0.0 <= ml["probability"] <= 1.0
    # Final probability must NOT be exactly 0.0 for a supported prop
    assert r.final_probability > 0.0


def test_why_this_pick_factors_populated_mlb():
    """Why-This-Pick renders the ML `top_factors` array. It must be
    non-empty for a supported MLB prop."""
    from services.trained_prediction_engine import predict_player_prop
    name, pos = _live_batter_id_or_skip()
    client, db = _fresh_db()
    try:
        r = _run(predict_player_prop(
            db, sport="MLB", player=name, stat="hits",
            opponent="?", line=0.5, player_position=pos,
        ))
    finally:
        client.close()
    factors = r.get("top_factors") or []
    assert factors, "Why-This-Pick has no factors on MLB/hits"
    top = factors[0]
    for k in ("feature", "value", "weight"):
        assert k in top, f"Why-This-Pick factor missing {k!r}: {top}"


# ═════════════════════════════════════════════════════════════════════
# G. Untrained MLB families still safe-fail
# ═════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("stat", ["rbi", "hits_runs_rbis", "walks"])
def test_untrained_mlb_stat_safe_fails(stat):
    """These families have no trained model — must return a clear
    supported=False (never load a foreign model or raise)."""
    from services.trained_prediction_engine import predict_player_prop
    client, db = _fresh_db()
    try:
        r = _run(predict_player_prop(
            db, sport="MLB", player="Aaron Judge", stat=stat,
            opponent="BOS", line=0.5, player_position="OF",
        ))
    finally:
        client.close()
    assert r["supported"] is False
    assert "no trained model" in r["reason"].lower()
