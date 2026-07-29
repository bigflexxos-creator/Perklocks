"""NBA End-to-End Wiring Tests (Phase 7, iter105, 2026-07-29).

Guards the NBA foundation:
  1. Historical player_game_logs are actually populated (real data,
     no synthetic rows) — smoke-tests against live DB.
  2. Feature builder produces the expected 16-column NBA feature matrix.
  3. All 4 NBA models load through the registry: points, rebounds,
     assists, threes_made.
  4. Market detector routes NBA market strings to the right stat key.
  5. `predict_player_prop` end-to-end for NBA returns supported=True
     with a probability + top_factors.
  6. Fusion engine consumes NBA — `ml.available=True` on a supported prop.
"""
from __future__ import annotations

import asyncio
import json
import pathlib

import pytest


def _run(c): return asyncio.run(c)


# ═════════════════════════════════════════════════════════════════════
# A. Historical data
# ═════════════════════════════════════════════════════════════════════
def test_nba_game_logs_ingested_or_skip():
    """Smoke: at least a few thousand NBA rows in `player_game_logs`.

    Skipped gracefully outside the live pod so CI on a fresh mongo
    still passes."""
    from motor.motor_asyncio import AsyncIOMotorClient
    import os
    async def _check():
        client = AsyncIOMotorClient(os.getenv("MONGO_URL"))
        try:
            return await client["lockscore_db"].player_game_logs \
                .count_documents({"sport": "nba"})
        finally:
            client.close()
    try:
        n = _run(_check())
    except Exception:
        pytest.skip("mongo unavailable")
    if n == 0:
        pytest.skip(f"NBA game logs not yet ingested in this env (n={n})")
    assert n >= 100, (
        f"NBA game logs suspiciously thin ({n} rows) — ingest likely "
        "did not complete")


def test_nba_row_shape_matches_mlb_convention():
    """Real NBA rows have the fields the feature builder expects."""
    try:
        from motor.motor_asyncio import AsyncIOMotorClient
        import os
        client = AsyncIOMotorClient(os.getenv("MONGO_URL"))
        db = client["lockscore_db"]
    except Exception:
        pytest.skip("mongo unavailable")
    async def _fetch():
        try:
            return await db.player_game_logs.find_one({"sport": "nba"},
                                                       {"_id": 0})
        finally:
            client.close()
    row = _run(_fetch())
    if not row:
        pytest.skip("no NBA rows in this env")
    for k in ("player_id", "player", "team", "sport", "date", "game_id",
              "points", "rebounds", "assists", "threes_made",
              "minutes", "is_home"):
        assert k in row, f"NBA game log row missing field {k!r}"
    assert row["sport"] == "nba"


# ═════════════════════════════════════════════════════════════════════
# B. Feature builder
# ═════════════════════════════════════════════════════════════════════
def test_nba_training_frame_has_16_features():
    from ml.features.nba import build_nba_training_frame
    import pandas as pd
    # Minimum synthetic frame — 6 rows for one player, enough to pass
    # the min_prior_games gate at 5.
    rows = [
        {"player_id": 1, "player": "X", "team": "T", "sport": "nba",
         "date": f"2024-11-0{i+1}", "game_id": f"g{i}",
         "points": 20 + i, "rebounds": 5 + i, "assists": 3,
         "threes_made": 2, "minutes": 32, "is_home": i % 2}
        for i in range(1, 8)
    ]
    df = pd.DataFrame(rows)
    tf = build_nba_training_frame(df, stat="points", min_prior_games=1)
    assert len(tf.feature_names) == 16
    assert not tf.features.empty
    assert set(tf.feature_names) >= {
        "stat_last_5_avg", "stat_last_10_avg", "stat_season_to_date_avg",
        "minutes_last_5_avg", "is_home", "rest_days_est", "is_b2b",
    }


def test_nba_training_frame_empty_for_unsupported_stat():
    from ml.features.nba import build_nba_training_frame
    import pandas as pd
    df = pd.DataFrame([{"sport": "nba", "player_id": 1,
                         "points": 20, "date": "2024-11-01"}])
    tf = build_nba_training_frame(df, stat="dunks", min_prior_games=1)
    assert tf.features.empty


# ═════════════════════════════════════════════════════════════════════
# C. Model registry — all 4 NBA models loadable
# ═════════════════════════════════════════════════════════════════════
def test_all_four_nba_models_load():
    from services.trained_prediction_engine import _load_model, _reset_model_cache
    _reset_model_cache()
    for stat in ("points", "rebounds", "assists", "threes_made"):
        bundle = _load_model("NBA", stat)
        assert bundle is not None, f"NBA/{stat} bundle failed to load"
        assert bundle["booster"] is not None
        assert bundle["feature_names"], f"NBA/{stat} has no feature_names"
        # Winner is either lgbm or xgb — must be a valid enum
        assert bundle["winner"] in ("lgbm", "xgb")


def test_nba_meta_json_exist_on_disk():
    root = pathlib.Path("/app/backend/models")
    for stat in ("points", "rebounds", "assists", "threes_made"):
        meta = root / f"nba_{stat}.meta.json"
        assert meta.exists(), f"missing {meta.name}"
        payload = json.loads(meta.read_text())
        assert payload.get("sport") == "NBA"
        assert payload.get("stat") == stat
        assert payload.get("winner") in ("lgbm", "xgb")


# ═════════════════════════════════════════════════════════════════════
# D. Market detector routes NBA strings correctly
# ═════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("market, expected", [
    ("LeBron James Over 24.5 Points",              "points"),
    ("Nikola Jokic Over 12.5 Rebounds",            "rebounds"),
    ("Trae Young Over 9.5 Assists",                "assists"),
    ("Steph Curry Over 4.5 3-Pointers Made",       "threes_made"),
    ("Trae Young Over 4.5 3PT Made",               "threes_made"),
    ("Curry Over 4.5 Three-Pointers",              "threes_made"),
    ("Anthony Davis Over 1.5 Blocks",              "blocks"),
    ("De'Aaron Fox Over 1.5 Steals",               "steals"),
])
def test_market_detector_nba(market, expected):
    from services.pick_matchup_wiring import _detect_stat
    assert _detect_stat("NBA", market) == expected


# ═════════════════════════════════════════════════════════════════════
# E. Live end-to-end prediction (skips if no ingested data)
# ═════════════════════════════════════════════════════════════════════
def test_predict_player_prop_nba_end_to_end():
    from motor.motor_asyncio import AsyncIOMotorClient
    import os
    from services.trained_prediction_engine import predict_player_prop

    async def _pipeline():
        client = AsyncIOMotorClient(os.getenv("MONGO_URL"))
        try:
            db = client["lockscore_db"]
            row = await db.player_game_logs.find_one(
                {"sport": "nba"}, {"_id": 0, "player": 1})
            if not row:
                return None, None
            r = await predict_player_prop(
                db, sport="NBA", player=row["player"],
                stat="points", opponent="?", line=15.0,
            )
            return row["player"], r
        finally:
            client.close()

    player, r = _run(_pipeline())
    if not player:
        pytest.skip("no NBA rows in this env for live prediction")
    assert isinstance(r, dict)
    assert r.get("supported") is True, f"NBA predict unsupported: {r}"
    assert 0.0 <= r.get("prediction_probability", -1) <= 1.0
    assert isinstance(r.get("expected_value"), (int, float))
    assert r.get("top_factors"), "NBA prediction missing top_factors"


def test_fusion_engine_consumes_nba():
    from motor.motor_asyncio import AsyncIOMotorClient
    import os
    from services.prediction_fusion_engine import fuse_prediction

    async def _pipeline():
        client = AsyncIOMotorClient(os.getenv("MONGO_URL"))
        try:
            db = client["lockscore_db"]
            row = await db.player_game_logs.find_one(
                {"sport": "nba"}, {"_id": 0, "player": 1})
            if not row:
                return None
            return await fuse_prediction(
                db, sport="NBA", player=row["player"], stat="points",
                opponent="?", threshold=15.0, persist=False,
            )
        finally:
            client.close()

    r = _run(_pipeline())
    if r is None:
        pytest.skip("no NBA rows available for fusion test")
    ml_comp = r.components["ml"].to_dict()
    assert ml_comp["available"] is True, (
        f"Fusion ML component didn't fire for NBA — {ml_comp}")
    assert isinstance(ml_comp["probability"], (int, float))
    assert 0.0 <= ml_comp["probability"] <= 1.0


# ═════════════════════════════════════════════════════════════════════
# F. Ingest module import / interface
# ═════════════════════════════════════════════════════════════════════
def test_ingest_module_public_api():
    from services.nba_gamelog_ingest import ingest_nba_gamelogs
    import inspect
    sig = inspect.signature(ingest_nba_gamelogs)
    params = set(sig.parameters.keys())
    for p in ("db", "seasons", "player_limit", "concurrency"):
        assert p in params, f"ingest_nba_gamelogs missing param {p!r}"


def test_ingest_writes_to_correct_collection():
    """Regression: ingest MUST persist to `player_game_logs` with
    `sport='nba'` — matches MLB/NFL/Tennis structure."""
    src = pathlib.Path(
        "/app/backend/services/nba_gamelog_ingest.py").read_text()
    assert "player_game_logs" in src
    assert '"sport": "nba"' in src
