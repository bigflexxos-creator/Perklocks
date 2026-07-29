"""Tests for `services.nfl_matchup_intelligence`.

Contract:
  1. Unknown player → empty NFLPlayerMatchup, confidence="none".
  2. QB vs opponent → passing_yards / passing_tds / attempts /
     completions / passing_ints / rushing_yards breakdowns with
     150/200/250/300 thresholds on passing_yards.
  3. WR vs opponent → receiving_yards / receptions / targets /
     receiving_tds with 25/50/75/100 thresholds on receiving_yards.
  4. Hit-rate math: `hits = sum(v >= threshold)`, `hit_rate = hits/games`.
  5. avg / median / min / max computed correctly.
  6. last_meeting is the MOST RECENT (season DESC, week DESC).
  7. Full team name resolves to nflverse code (e.g. "Kansas City Chiefs" → "KC").
  8. No sportsbook odds and no betting-line references anywhere in module.
"""
from __future__ import annotations

import asyncio, os, sys, uuid

os.environ.setdefault("MONGO_URL", os.environ.get("MONGO_URL", "mongodb://localhost:27017"))
os.environ.setdefault("DB_NAME", "lockscore_db")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _insert_qb_rows(db, player_name, opponent_code, rows_spec):
    docs = []
    for i, (season, week, py, tds, att, comp, rush) in enumerate(rows_spec):
        docs.append({
            "player_id": f"nfl_test_{uuid.uuid4().hex[:8]}_{i}",
            "player_name": player_name.replace(" ", "."),
            "player_display_name": player_name,
            "position": "QB",
            "opponent_team": opponent_code,
            "team": "TST",
            "season": season, "week": week,
            "passing_yards": py, "passing_tds": tds,
            "attempts": att, "completions": comp,
            "rushing_yards": rush,
            "passing_ints": 0,
        })
    return docs


def _insert_wr_rows(db, player_name, opponent_code, rows_spec):
    docs = []
    for i, (season, week, ry, rec, tgts, tds) in enumerate(rows_spec):
        docs.append({
            "player_id": f"nfl_wr_{uuid.uuid4().hex[:8]}_{i}",
            "player_name": player_name.replace(" ", "."),
            "player_display_name": player_name,
            "position": "WR",
            "opponent_team": opponent_code,
            "team": "TST",
            "season": season, "week": week,
            "receiving_yards": ry, "receptions": rec, "targets": tgts,
            "receiving_tds": tds,
        })
    return docs


def test_unknown_player_returns_empty_matchup():
    from motor.motor_asyncio import AsyncIOMotorClient
    from services.nfl_matchup_intelligence import get_nfl_matchup_intelligence

    async def _inner():
        client = AsyncIOMotorClient(os.environ["MONGO_URL"])
        db = client[os.environ["DB_NAME"]]
        r = await get_nfl_matchup_intelligence(
            db,
            player_name="Nobody Player " + uuid.uuid4().hex[:6],
            opponent_team="KC",
        )
        assert r.games_played == 0
        assert r.sample_confidence == "none"
        assert r.stat_lines == {}
        assert r.data_sources_used == []
        assert any("no nfl_player_weekly rows" in n for n in r.notes)

    asyncio.run(_inner())


def test_qb_vs_opponent_full_breakdown_with_hit_rates():
    from motor.motor_asyncio import AsyncIOMotorClient
    from services.nfl_matchup_intelligence import get_nfl_matchup_intelligence

    async def _inner():
        client = AsyncIOMotorClient(os.environ["MONGO_URL"])
        db = client[os.environ["DB_NAME"]]
        name = f"QB Fixture {uuid.uuid4().hex[:6]}"
        # 5 games vs KC — pass yards: 199, 220, 285, 310, 341
        rows = [
            (2023, 17, 341, 3, 36, 24, 12),
            (2022, 10, 220, 1, 30, 20, 8),
            (2022, 3,  199, 0, 28, 16, 3),
            (2021, 14, 285, 2, 33, 22, 11),
            (2021, 4,  310, 3, 40, 27, 5),
        ]
        docs = _insert_qb_rows(db, name, "KC", rows)
        try:
            await db.nfl_player_weekly.insert_many(docs)
            r = await get_nfl_matchup_intelligence(
                db,
                player_name=name,
                opponent_team="KC",
            )
            assert r.games_played == 5
            assert r.position == "QB"
            assert r.sample_confidence == "low"   # 5 games → low tier
            py = r.stat_lines.get("passing_yards")
            assert py is not None
            assert py.games == 5
            assert py.minimum == 199.0
            assert py.maximum == 341.0
            assert py.median == 285.0
            # avg = (199+220+285+310+341)/5 = 271.0
            assert abs(py.avg - 271.0) < 0.5
            # Thresholds
            assert py.thresholds[150.0].hits == 5   # all games >= 150
            assert py.thresholds[150.0].hit_rate == 1.0
            assert py.thresholds[200.0].hits == 4   # 220, 285, 310, 341
            assert py.thresholds[200.0].hit_rate == 0.8
            assert py.thresholds[250.0].hits == 3   # 285, 310, 341
            assert py.thresholds[250.0].hit_rate == 0.6
            assert py.thresholds[300.0].hits == 2   # 310, 341
            assert py.thresholds[300.0].hit_rate == 0.4
            # passing_tds should also be present
            tds = r.stat_lines.get("passing_tds")
            assert tds is not None
            assert tds.games == 5
            # attempts
            att = r.stat_lines.get("attempts")
            assert att is not None
            # Last meeting is 2023 W17 with 341 yards
            assert r.last_meeting["season"] == 2023
            assert r.last_meeting["week"] == 17
            assert r.last_meeting["passing_yards"] == 341
        finally:
            await db.nfl_player_weekly.delete_many(
                {"player_display_name": name}
            )

    asyncio.run(_inner())


def test_wr_vs_opponent_receiving_yards_thresholds():
    from motor.motor_asyncio import AsyncIOMotorClient
    from services.nfl_matchup_intelligence import get_nfl_matchup_intelligence

    async def _inner():
        client = AsyncIOMotorClient(os.environ["MONGO_URL"])
        db = client[os.environ["DB_NAME"]]
        name = f"WR Fixture {uuid.uuid4().hex[:6]}"
        # 4 games vs BUF — rec yards: 22, 55, 88, 110
        rows = [
            (2023, 12, 110, 8, 12, 1),
            (2023, 3,   88, 6, 10, 0),
            (2022, 7,   55, 4,  7, 0),
            (2022, 1,   22, 2,  4, 0),
        ]
        docs = _insert_wr_rows(db, name, "BUF", rows)
        try:
            await db.nfl_player_weekly.insert_many(docs)
            r = await get_nfl_matchup_intelligence(
                db,
                player_name=name,
                opponent_team="BUF",
            )
            assert r.games_played == 4
            assert r.position == "WR"
            ry = r.stat_lines["receiving_yards"]
            # 25+ = 3 (55, 88, 110), 50+ = 3, 75+ = 2, 100+ = 1
            assert ry.thresholds[25.0].hits == 3
            assert ry.thresholds[50.0].hits == 3
            assert ry.thresholds[75.0].hits == 2
            assert ry.thresholds[100.0].hits == 1
            assert ry.thresholds[100.0].hit_rate == 0.25
            # receptions + targets breakdowns present
            assert "receptions" in r.stat_lines
            assert "targets" in r.stat_lines
        finally:
            await db.nfl_player_weekly.delete_many(
                {"player_display_name": name}
            )

    asyncio.run(_inner())


def test_full_team_name_resolves_to_nfl_code():
    from services.nfl_matchup_intelligence import _resolve_opponent_code
    assert _resolve_opponent_code("Kansas City Chiefs") == "KC"
    assert _resolve_opponent_code("Buffalo Bills") == "BUF"
    assert _resolve_opponent_code("San Francisco 49ers") == "SF"
    assert _resolve_opponent_code("kc") == "KC"      # short-code passthrough
    assert _resolve_opponent_code("KC") == "KC"
    # Unknown name → passthrough
    assert _resolve_opponent_code("Fake Team Name") == "Fake Team Name"


def test_no_betting_line_references_in_module():
    """Purely-historical guarantee: module source must not USE betting
    data (book_odds, implied_prob, etc.). Docstring mentions of
    "sportsbook" / "betting line" are allowed as disclaimers — we
    only forbid actual code identifiers that would indicate a
    dependency on sportsbook data."""
    with open("/app/backend/services/nfl_matchup_intelligence.py") as f:
        src = f.read()
    # Actual code identifiers, not English words in comments.
    forbidden_code = [
        "book_odds",
        "book_implied",
        "implied_prob",
        "moneyline_odds",
        "over_odds",
        "under_odds",
        "the_odds_api",
        "from services.odds",
        "import odds",
    ]
    for term in forbidden_code:
        # Case-sensitive — we're looking for code, not English.
        assert term not in src, (
            f"Module illegally references betting-data identifier {term!r}"
        )


def test_result_serialises_to_dict_cleanly():
    from services.nfl_matchup_intelligence import (
        NFLPlayerMatchup, StatBreakdown, ThresholdHit
    )
    m = NFLPlayerMatchup(
        player_name="Test QB",
        position="QB",
        opponent_team="KC",
        games_played=3,
        sample_confidence="low",
        stat_lines={
            "passing_yards": StatBreakdown(
                stat_key="passing_yards",
                games=3, avg=280.0, median=275.0,
                minimum=220.0, maximum=340.0,
                values=[220, 275, 340],
                thresholds={
                    200.0: ThresholdHit(200.0, 3, 3, 1.0),
                    250.0: ThresholdHit(250.0, 2, 3, 0.6667),
                },
            )
        },
    )
    d = m.to_dict()
    assert d["player_name"] == "Test QB"
    assert d["stat_lines"]["passing_yards"]["games"] == 3
    # threshold keys stringified for JSON safety
    thr_keys = list(d["stat_lines"]["passing_yards"]["thresholds"].keys())
    assert all(isinstance(k, str) for k in thr_keys)
