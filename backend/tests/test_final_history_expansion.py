"""Final History Expansion — deterministic tests."""
from __future__ import annotations
import asyncio, sys
import pytest
sys.path.insert(0, "/app/backend")
pytestmark = pytest.mark.unit

# Reuse fake DB from prior tests.
from tests.test_history_gap_closure import _DB, _run  # noqa: E402


def test_nfl_weekly_ingest_basic_and_identity():
    from services.team_history.final_expansion import backfill_nfl_from_player_weekly
    db = _DB()
    db["nfl_player_weekly"].docs.extend([
        {"_id":"a1","player_id":"p1","player_display_name":"Player A",
          "game_id":"2019_01_IND_LAC","opponent_team":"LAC",
          "passing_yards":267,"passing_tds":2,"passing_ints":1,
          "position":"QB"},
        {"_id":"a2","player_id":None,"game_id":"2019_01_X_Y"},   # id missing
        {"_id":"a3","player_id":"p2","game_id":"2019_02_KC_BAL",
          "passing_yards":None,"passing_tds":None,"rushing_yards":None},  # all None
    ])
    r = _run(backfill_nfl_from_player_weekly(db))
    assert r["examined"] == 3
    assert r["identity_unresolved"] == 1
    assert r["no_stats"] == 1
    assert r["accepted"] == 1
    row = db["player_game_actuals"].docs[0]
    assert row["canonical_player_id"] == "p1"
    assert row["actuals"]["pass_yds"] == 267.0
    assert row["actuals"]["pass_tds"] == 2.0
    assert row["season"] == 2019 and row["week"] == 1
    assert row["opponent"] == "LAC"
    assert row["source"] == "nfl_player_weekly"


def test_nfl_weekly_idempotent():
    from services.team_history.final_expansion import backfill_nfl_from_player_weekly
    db = _DB()
    db["nfl_player_weekly"].docs.append({
        "_id":"a1","player_id":"p1","game_id":"2019_01_A_B",
        "passing_yards":200,
    })
    r1 = _run(backfill_nfl_from_player_weekly(db))
    r2 = _run(backfill_nfl_from_player_weekly(db))
    assert r1["inserted"] == 1
    assert r2["inserted"] == 0 and r2["updated"] == 1
    assert len(db["player_game_actuals"].docs) == 1


def test_soccer_matches_ingest_home_away_perspective():
    from services.team_history.final_expansion import backfill_soccer_teams_from_soccer_matches
    db = _DB()
    db["soccer_matches"].docs.append({
        "_id":"m1","home_team":"Man United","away_team":"Fulham",
        "home_score":1,"away_score":0,"date":"2024-08-16",
        "league":"EPL","season":"2024-25","status":"finished",
    })
    r = _run(backfill_soccer_teams_from_soccer_matches(db))
    assert r["examined"] == 1 and r["accepted"] == 2
    rows = db["team_game_actuals"].docs
    mu = next(x for x in rows if x["canonical_team_id"] == "Man United")
    fu = next(x for x in rows if x["canonical_team_id"] == "Fulham")
    assert mu["home_away"] == "home" and fu["home_away"] == "away"
    assert mu["team_score"] == 1.0 and mu["opponent_score"] == 0.0
    assert fu["team_score"] == 0.0 and fu["opponent_score"] == 1.0
    assert mu["result"] == "WIN" and fu["result"] == "LOSS"
    assert mu["season"] == 2024  # derived from "2024-25"
    assert mu["competition"] == "EPL"


def test_soccer_matches_zero_zero_draw_preserved():
    from services.team_history.final_expansion import backfill_soccer_teams_from_soccer_matches
    db = _DB()
    db["soccer_matches"].docs.append({
        "_id":"m2","home_team":"A","away_team":"B",
        "home_score":0,"away_score":0,"date":"2024-05-01",
        "league":"EPL","season":"2023-24","status":"finished",
    })
    _run(backfill_soccer_teams_from_soccer_matches(db))
    rows = db["team_game_actuals"].docs
    assert rows[0]["team_score"] == 0.0  # real zero preserved
    assert rows[0]["result"] == "DRAW"


def test_soccer_matches_missing_score_rejected():
    from services.team_history.final_expansion import backfill_soccer_teams_from_soccer_matches
    db = _DB()
    db["soccer_matches"].docs.append({
        "_id":"m3","home_team":"A","away_team":"B",
        "home_score":None,"away_score":None,"status":"finished",
    })
    r = _run(backfill_soccer_teams_from_soccer_matches(db))
    assert r["missing_result"] == 1 and r["inserted"] == 0
