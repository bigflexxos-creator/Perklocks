"""Soccer Foundation Regression Tests (Phase 7 Part 4, iter108, 2026-06).

Verifies the Understat per-match ingestor + new per-match feature
builder ONLY. No models are trained and no fusion wiring is touched
in this phase.

Sections
────────
  A. Ingestor unit tests (row shape, SOT derivation, sub detection).
  B. `soccer_player_game_logs` collection contract (indexes, shape).
  C. Live-feature builder (16 dims, safe-fail on insufficient data).
  D. Training-frame builder (rolling windows, prior-only, no leakage).
  E. Coverage sanity check on ingested EPL 2024-25 season.
  F. No-fake-confidence contract: fewer than 3 prior matches → NaN
     feature vector + explicit note.

External network calls: NONE.  All HTTP fetching is mocked via
fixtures that inject sample Understat payloads.
"""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

import numpy as np
import pandas as pd
import pytest


def _run(c): return asyncio.run(c)


def _fresh_db():
    from motor.motor_asyncio import AsyncIOMotorClient
    return AsyncIOMotorClient(os.getenv("MONGO_URL"))["lockscore_db"]


# ═════════════════════════════════════════════════════════════════════
# A. Ingestor unit tests — pure functions, no network
# ═════════════════════════════════════════════════════════════════════
def test_A1_canonicalize_name():
    from ml.ingestors.soccer_understat import _canonicalize_name
    assert _canonicalize_name("Erling Haaland") == "erling haaland"
    assert _canonicalize_name("Lisandro Martínez") == "lisandro martinez"
    assert _canonicalize_name("N'Golo Kanté") == "ngolo kante"
    assert _canonicalize_name("") == ""


def test_A2_sot_derivation_from_shots():
    from ml.ingestors.soccer_understat import _derive_sot_from_shots
    shots_side = [
        {"player_id": "8044", "result": "Goal"},
        {"player_id": "8044", "result": "SavedShot"},
        {"player_id": "8044", "result": "MissedShots"},
        {"player_id": "8044", "result": "BlockedShot"},
        {"player_id": "9999", "result": "Goal"},        # different player
    ]
    assert _derive_sot_from_shots(shots_side, "8044") == 2
    assert _derive_sot_from_shots(shots_side, "9999") == 1
    assert _derive_sot_from_shots(shots_side, "0000") == 0
    assert _derive_sot_from_shots([], "8044") == 0


def test_A3_player_row_start_vs_sub_flag():
    """Position=='Sub' → starts=0; everything else with minutes → 1."""
    from ml.ingestors.soccer_understat import _build_player_row
    common = dict(
        match_id="26602", match_date="2024-08-16 19:00:00",
        league="EPL", season=2024,
        home_team_id="89", home_team_name="Manchester United",
        away_team_id="102", away_team_name="Fulham",
        home_goals=1, away_goals=0, home_xg=1.2, away_xg=0.9,
        shots_h=[], shots_a=[],
    )
    starter = _build_player_row(
        **common, player_id="1001",
        player_stats={"player": "Bruno Fernandes", "h_a": "h",
                       "team_id": "89", "position": "AMC",
                       "time": 90, "goals": 0, "shots": 3, "xG": 0.4,
                       "roster_in": 0, "roster_out": 0},
    )
    sub = _build_player_row(
        **common, player_id="8044",
        player_stats={"player": "Joshua Zirkzee", "h_a": "h",
                       "team_id": "89", "position": "Sub",
                       "time": 28, "goals": 1, "shots": 1, "xG": 0.09,
                       "roster_in": 0, "roster_out": 666177},
    )
    assert starter["starts"] == 1
    assert sub["starts"] == 0
    assert sub["minutes"] == 28
    assert starter["is_home"] is True
    assert starter["opponent_team_name"] == "Fulham"
    assert starter["team_goals_scored"] == 1
    assert starter["team_goals_conceded"] == 0


def test_A4_player_row_derives_sot_from_shots_side():
    from ml.ingestors.soccer_understat import _build_player_row
    row = _build_player_row(
        match_id="1", match_date="2024-08-01 15:00:00",
        league="EPL", season=2024,
        home_team_id="A", home_team_name="A",
        away_team_id="B", away_team_name="B",
        home_goals=2, away_goals=1, home_xg=1.5, away_xg=1.0,
        player_id="pid1",
        player_stats={"player": "P", "h_a": "h", "team_id": "A",
                       "position": "F", "time": 90, "goals": 1,
                       "shots": 4, "xG": 0.8, "xA": 0.2,
                       "key_passes": 3, "xGChain": 1.1, "xGBuildup": 0.5,
                       "yellow_card": 0, "red_card": 0,
                       "own_goals": 0, "roster_in": 0, "roster_out": 0,
                       "positionOrder": 11},
        shots_h=[
            {"player_id": "pid1", "result": "Goal"},
            {"player_id": "pid1", "result": "SavedShot"},
            {"player_id": "pid1", "result": "MissedShots"},
            {"player_id": "pid1", "result": "BlockedShot"},
        ],
        shots_a=[],
    )
    # Goal + SavedShot → 2 SOT
    assert row["shots_on_target"] == 2
    assert row["goals"] == 1
    assert row["shots"] == 4
    assert row["is_home"] is True
    assert row["source"] == "understat"
    assert row["name_canonical"] == "p"


# ═════════════════════════════════════════════════════════════════════
# B. Collection contract
# ═════════════════════════════════════════════════════════════════════
def test_B1_ensure_indexes_creates_all_indexes():
    from ml.ingestors.soccer_understat import ensure_indexes, COLLECTION_NAME
    async def go():
        db = _fresh_db()
        await ensure_indexes(db)
        idx = await db[COLLECTION_NAME].index_information()
        # Unique index on (match_id, player_id)
        assert "uniq_match_player" in idx
        assert idx["uniq_match_player"].get("unique") is True
        assert "name_date_desc" in idx
        assert "pid_date_desc" in idx
        assert "league_season" in idx
    _run(go())


def test_B2_row_shape_matches_contract():
    """Every ingested row must have the documented schema fields."""
    async def go():
        db = _fresh_db()
        doc = await db.soccer_player_game_logs.find_one({}, {"_id": 0})
        if doc is None:
            pytest.skip("no ingested rows yet")
        required = {
            "match_id", "match_date", "season", "league",
            "player_id", "player_name", "name_canonical",
            "team_id", "team_name",
            "is_home", "opponent_team_id", "opponent_team_name",
            "position",
            "team_goals_scored", "team_goals_conceded",
            "team_xg", "opponent_xg", "home_goals", "away_goals",
            "minutes", "starts",
            "goals", "assists", "own_goals", "shots", "shots_on_target",
            "key_passes", "xg", "xa", "xg_chain", "xg_buildup",
            "yellow_card", "red_card",
            "source", "ingested_at",
        }
        missing = required - set(doc.keys())
        assert not missing, f"missing fields on ingested row: {missing}"
        assert doc["source"] == "understat"
        assert isinstance(doc["is_home"], bool)
    _run(go())


# ═════════════════════════════════════════════════════════════════════
# C. Live feature builder — 16 dims, safe-fail contract
# ═════════════════════════════════════════════════════════════════════
def test_C1_feature_names_are_16():
    from ml.features.soccer import _feature_names
    names = _feature_names()
    assert len(names) == 16
    # No duplicates.
    assert len(set(names)) == 16


def test_C2_live_feature_stub_returns_nan_when_no_history():
    from ml.features.soccer import build_soccer_live_features
    async def go():
        db = _fresh_db()
        vec, names, meta = await build_soccer_live_features(
            db, player_name="Definitely Not A Real Player",
            opponent_team="Somebody", stat="goals", is_home=True,
        )
        assert len(vec) == 16
        assert all(np.isnan(v) for v in vec.values())
        assert "insufficient prior matches" in meta["notes"][0]
    _run(go())


def test_C3_live_feature_unsupported_stat_safe_fail():
    from ml.features.soccer import build_soccer_live_features
    async def go():
        db = _fresh_db()
        vec, names, meta = await build_soccer_live_features(
            db, player_name="Anyone", stat="tackles_won",  # not supported
        )
        assert all(np.isnan(v) for v in vec.values())
        assert any("unsupported" in n for n in meta["notes"])
    _run(go())


def test_C4_live_features_populated_for_known_player():
    """Uses ingested EPL 2024 data — skips if not yet ingested."""
    from ml.features.soccer import build_soccer_live_features
    async def go():
        db = _fresh_db()
        n = await db.soccer_player_game_logs.count_documents(
            {"league": "EPL", "season": 2024})
        if n < 100:
            pytest.skip(f"ingestion still in progress (n={n})")
        for player, opp in (
            ("Mohamed Salah",  "Arsenal"),
            ("Erling Haaland", "Liverpool"),
            ("Cole Palmer",    "Fulham"),
        ):
            vec, _, meta = await build_soccer_live_features(
                db, player_name=player, opponent_team=opp,
                stat="shots", is_home=True,
            )
            if meta.get("prior_matches", 0) < 3:
                continue        # player not yet in DB
            defined = sum(1 for v in vec.values() if not np.isnan(v))
            assert defined >= 12, (
                f"{player}: only {defined}/16 dims defined"
            )
            # Basic sanity: rolling stats non-negative and finite
            for k in ("stat_last3_avg", "stat_last5_avg",
                      "stat_last10_avg"):
                assert vec[k] >= 0
                assert np.isfinite(vec[k])
    _run(go())


# ═════════════════════════════════════════════════════════════════════
# D. Training frame builder — rolling windows + leak-freeness
# ═════════════════════════════════════════════════════════════════════
def _make_synthetic_logs(n_players: int = 5, n_matches: int = 12) -> pd.DataFrame:
    """Deterministic synthetic per-match logs for 5 players."""
    import itertools
    rows = []
    for pid in range(1, n_players + 1):
        for m in range(n_matches):
            rows.append({
                "player_id":            f"p{pid}",
                "player_name":          f"Player {pid}",
                "name_canonical":       f"player {pid}",
                "match_id":             f"m{m:02d}_p{pid}",
                "match_date":           f"2024-01-{(m+1):02d} 15:00:00",
                "team_id":              f"t{(pid-1) % 2 + 1}",
                "team_name":            f"Team {(pid-1) % 2 + 1}",
                "opponent_team_id":     f"t{pid % 2 + 1}",
                "opponent_team_name":   f"Team {pid % 2 + 1}",
                "is_home":              m % 2 == 0,
                "position":             "FW",
                "minutes":              90,
                "starts":               1,
                "goals":                (pid + m) % 3,
                "assists":              m % 2,
                "shots":                2 + m % 4,
                "shots_on_target":      1 + m % 2,
                "xg":                   0.3 + 0.05 * (m % 5),
                "xa":                   0.1 + 0.02 * (m % 5),
                "team_goals_scored":    1 + m % 3,
                "team_goals_conceded":  1 + (pid + m) % 3,
                "opponent_xg":          1.0 + 0.1 * m,
                "league":               "EPL",
                "season":               2024,
            })
    return pd.DataFrame(rows)


def test_D1_training_frame_shape():
    from ml.features.soccer import build_soccer_training_frame
    df = _make_synthetic_logs(5, 12)
    tf = build_soccer_training_frame(df, stat="goals", min_prior_matches=3)
    assert not tf.features.empty
    assert len(tf.feature_names) == 16
    assert tf.features.shape[1] == 16
    assert tf.features.shape[0] == len(tf.target)
    # min_prior_matches=3 with 12 matches × 5 players → 45 rows
    assert tf.features.shape[0] == 45


def test_D2_training_frame_leak_free_prior_only():
    """First-N-prior-matches rows are dropped; remaining rows contain
    ONLY features computed from strictly-prior data."""
    from ml.features.soccer import build_soccer_training_frame
    df = _make_synthetic_logs(3, 15)
    tf = build_soccer_training_frame(df, stat="shots", min_prior_matches=5)
    # For the first row emitted per player (their 6th match), the
    # stat_last3_avg should equal mean of their previous 3 shots values.
    # We take one player and verify manually.
    meta = tf.row_meta.reset_index(drop=True)
    feat = tf.features.reset_index(drop=True)
    y = tf.target.reset_index(drop=True)
    # Find first row for player p1.
    idx = meta.index[meta["player_id"] == "p1"].min()
    p1_matches = df[df["player_id"] == "p1"].sort_values("match_date")
    # The idx-th p1 row corresponds to p1's 6th match (index 5 in sorted).
    p1_at_this = p1_matches.iloc[5]
    assert y.iloc[idx] == p1_at_this["shots"], "target aligned"
    prev_3_shots = p1_matches.iloc[2:5]["shots"].mean()
    assert abs(feat.iloc[idx]["stat_last3_avg"] - prev_3_shots) < 1e-6


def test_D3_training_frame_target_multi_stats():
    from ml.features.soccer import build_soccer_training_frame
    df = _make_synthetic_logs(4, 10)
    for stat in ("goals", "shots", "shots_on_target", "xg",
                  "goal_contributions"):
        tf = build_soccer_training_frame(df, stat=stat, min_prior_matches=3)
        assert not tf.features.empty
        assert tf.stat == stat
        assert tf.sport == "Soccer"


def test_D4_training_frame_empty_input():
    from ml.features.soccer import build_soccer_training_frame
    tf = build_soccer_training_frame(pd.DataFrame(), stat="goals")
    assert tf.features.empty
    assert tf.target.empty


def test_D5_training_frame_unknown_stat_returns_empty():
    from ml.features.soccer import build_soccer_training_frame
    df = _make_synthetic_logs(3, 6)
    tf = build_soccer_training_frame(df, stat="tackles")  # not in map
    # target_col=stat and column doesn't exist → empty frame
    assert tf.features.empty


# ═════════════════════════════════════════════════════════════════════
# E. Coverage sanity check on ingested EPL 2024-25
# ═════════════════════════════════════════════════════════════════════
def test_E1_ingested_data_has_diverse_teams_players():
    async def go():
        db = _fresh_db()
        n = await db.soccer_player_game_logs.count_documents(
            {"league": "EPL", "season": 2024})
        if n < 100:
            pytest.skip(f"ingestion still in progress (n={n})")

        team_ids = await db.soccer_player_game_logs.distinct(
            "team_id", {"league": "EPL", "season": 2024})
        # EPL has 20 teams — after >100 matches we should see all/most.
        assert len(team_ids) >= 15, f"only {len(team_ids)} teams seen"

        # xG values must span a realistic range.
        pipe = [
            {"$match": {"league": "EPL", "season": 2024}},
            {"$group": {"_id": None, "mean_xg": {"$avg": "$xg"},
                         "max_xg": {"$max": "$xg"}}},
        ]
        stats = None
        async for doc in db.soccer_player_game_logs.aggregate(pipe):
            stats = doc
        assert stats is not None
        assert 0.0 < stats["mean_xg"] < 0.5,   f"mean_xg={stats['mean_xg']}"
        assert stats["max_xg"] > 1.0,           f"max_xg={stats['max_xg']}"
    _run(go())


def test_E2_ingested_data_starts_flag_realistic():
    """Roughly ~55-70% of ingested player-rows should be starts."""
    async def go():
        db = _fresh_db()
        n = await db.soccer_player_game_logs.count_documents(
            {"league": "EPL", "season": 2024})
        if n < 500:
            pytest.skip(f"ingestion still in progress (n={n})")
        n_starts = await db.soccer_player_game_logs.count_documents(
            {"league": "EPL", "season": 2024, "starts": 1})
        rate = n_starts / n
        assert 0.45 < rate < 0.75, (
            f"starts_rate={rate:.3f} outside expected 0.45-0.75"
        )
    _run(go())


# ═════════════════════════════════════════════════════════════════════
# F. No-fake-confidence contract
# ═════════════════════════════════════════════════════════════════════
def test_F1_insufficient_prior_returns_nan_vector():
    """When < 3 prior matches, live builder must return NaN vector
    with an explicit note — never fabricate values."""
    from ml.features.soccer import build_soccer_live_features
    async def go():
        db = _fresh_db()
        vec, names, meta = await build_soccer_live_features(
            db, player_name="Definitely Nobody 12345XYZ",
            stat="goals",
        )
        # ALL 16 dims must be NaN
        assert all(np.isnan(v) for v in vec.values())
        # note must mention insufficient data
        joined = " ".join(meta["notes"])
        assert "insufficient" in joined.lower()
    _run(go())


def test_F2_min_prior_matches_gate_in_training_frame():
    """min_prior_matches=N drops rows with fewer than N prior."""
    from ml.features.soccer import build_soccer_training_frame
    df = _make_synthetic_logs(1, 10)   # single player, 10 matches
    tf_gate5 = build_soccer_training_frame(df, stat="goals",
                                            min_prior_matches=5)
    tf_gate3 = build_soccer_training_frame(df, stat="goals",
                                            min_prior_matches=3)
    # With 10 matches: gate=3 keeps 10-3=7, gate=5 keeps 10-5=5
    assert len(tf_gate5.features) == 5
    assert len(tf_gate3.features) == 7


__all__: list[str] = []
