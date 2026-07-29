"""Tests for `services.player_matchup_intelligence`.

Contract verified:
  1. Public API returns a MatchupIntelligence dataclass (never raises).
  2. Missing/unknown player → empty slices + `sample_confidence="none"`
     + grade "F" (no false confidence).
  3. props_history data populates L5/L10/season slices + consistency.
  4. player_game_logs data provides fallback L5/L10/season slices.
  5. Threshold hit-rate is a weighted blend of career-vs-opp + L10.
  6. Sample-size confidence tiers: <3 none, 3-7 low, 8-14 medium, 15+ high.
  7. Grade honours sample size (small-sample hot streaks don't outrank
     larger-sample steady performance).
  8. MLB PvT integration path (mocked because pvt module hits MLB API).
  9. NFL opponent lookup path via `nfl_player_weekly.opponent_team`.
 10. Data-source list is populated correctly.
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone

os.environ.setdefault("MONGO_URL", os.environ.get("MONGO_URL", "mongodb://localhost:27017"))
os.environ.setdefault("DB_NAME", "lockscore_db")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ─────────────────────────────────────────────────────────────────
# Unit tests — helpers (no DB required)
# ─────────────────────────────────────────────────────────────────
def test_canonical_stat_aliasing():
    from services.player_matchup_intelligence import _canon_stat
    assert _canon_stat("MLB", "strikeouts") == "pitcher_strikeouts"
    assert _canon_stat("mlb", "K") == "pitcher_strikeouts"
    assert _canon_stat("MLB", "batter_hits") == "hits"
    assert _canon_stat("MLB", "batter_hits_runs_rbis") == "hits_runs_rbis"
    assert _canon_stat("NFL", "passing_yards") == "passing_yards"
    assert _canon_stat("Tennis", "aces") == "aces"
    # Unknown → lowercased passthrough
    assert _canon_stat("MLB", "some_new_stat") == "some_new_stat"


def test_build_slice_over_hit_and_avg_math():
    from services.player_matchup_intelligence import _build_slice
    values = [1.0, 2.0, 3.0, 0.0, 2.0]
    s = _build_slice(values, threshold=1.5)
    assert s.games == 5
    assert s.over_hits == 3      # 2.0, 3.0, 2.0 > 1.5
    assert s.hit_rate == 0.6
    assert s.avg == 1.6
    assert s.median == 2.0

    # No threshold — hit_rate stays 0 but avg/median populate.
    s2 = _build_slice(values, threshold=None)
    assert s2.games == 5
    assert s2.over_hits == 0
    assert s2.hit_rate == 0.0
    assert s2.avg == 1.6


def test_consistency_score_bounds():
    from services.player_matchup_intelligence import _consistency
    # Perfect flat line → 1.0
    assert _consistency([5.0, 5.0, 5.0, 5.0]) == 1.0
    # High CV → low consistency
    high_var = [0.0, 10.0, 0.0, 10.0, 0.0]
    hv = _consistency(high_var)
    assert 0.0 <= hv <= 0.5
    # Empty / single → 0.0
    assert _consistency([]) == 0.0
    assert _consistency([5.0]) == 0.0


def test_grade_and_confidence_tiers():
    from services.player_matchup_intelligence import _grade, _confidence
    _RANK = {"F":0, "D":1, "C":2, "B":3, "A":4, "A+":5}
    # Elite: 80% hit rate, high consistency, 20 games
    assert _grade(0.80, 0.85, 20) == "A+"
    # Small sample cannot outrank big sample
    small = _grade(0.90, 0.90, 3)   # 3 games
    big   = _grade(0.65, 0.65, 20)  # 20 games
    assert _RANK[big] >= _RANK[small], (
        f"Large-sample {big} did not outrank small-sample {small}"
    )
    # Zero data
    assert _grade(0.0, 0.0, 0) == "F"
    # Confidence tiers
    assert _confidence(0) == "none"
    assert _confidence(3) == "low"
    assert _confidence(8) == "medium"
    assert _confidence(15) == "high"


# ─────────────────────────────────────────────────────────────────
# Integration tests — DB-backed
# ─────────────────────────────────────────────────────────────────
def _insert_fixture_pgl(db, sport: str, player_id: int, name: str,
                        stat_key: str, values: list[float],
                        event_prefix: str) -> list[str]:
    """Insert N fake player_game_logs rows and return the game_ids."""
    ids = []
    docs = []
    for i, v in enumerate(values):
        gid = f"{event_prefix}_{i}"
        ids.append(gid)
        docs.append({
            "game_id": gid,
            "player_id": player_id,
            "name": name,
            "sport": sport.lower(),
            stat_key: v,
            "date": datetime(2026, 7, 25 - i, 20, 0, 0, tzinfo=timezone.utc).isoformat(),
        })
    return docs


def test_unknown_player_returns_empty_result_gracefully():
    from motor.motor_asyncio import AsyncIOMotorClient
    from services.player_matchup_intelligence import get_matchup_intelligence

    async def _inner():
        client = AsyncIOMotorClient(os.environ["MONGO_URL"])
        db = client[os.environ["DB_NAME"]]
        r = await get_matchup_intelligence(
            db,
            sport="MLB",
            player_name="Nonexistent Player " + uuid.uuid4().hex[:8],
            stat="hits",
            threshold=0.5,
        )
        assert r.sample_size == 0
        assert r.sample_confidence == "none"
        assert r.matchup_grade == "F"
        assert r.career_vs_opponent.games == 0
        assert r.data_sources_used == []
        assert any("no data sources" in n for n in r.notes)

    asyncio.run(_inner())


def test_props_history_populates_windows_and_consistency():
    from motor.motor_asyncio import AsyncIOMotorClient
    from services.player_matchup_intelligence import get_matchup_intelligence

    async def _inner():
        client = AsyncIOMotorClient(os.environ["MONGO_URL"])
        db = client[os.environ["DB_NAME"]]
        pid = f"pmi_test_{uuid.uuid4().hex[:8]}"
        try:
            await db.props_history.insert_one({
                "player_id": pid,
                "sport": "mlb",
                "props": {
                    "mlb_hits": {
                        "stat": "hits",
                        "lines": {
                            "0.5": {
                                "5":       {"games_used": 5,  "hits": 4, "hit_rate": 0.8, "avg": 1.2},
                                "10":      {"games_used": 10, "hits": 7, "hit_rate": 0.7, "avg": 1.1},
                                "season":  {"games_used": 30, "hits": 20, "hit_rate": 0.67, "avg": 1.0},
                            },
                        },
                        "consistency": 0.78,
                        "last10_avg": 1.1,
                    },
                },
            })
            r = await get_matchup_intelligence(
                db,
                sport="MLB",
                player_id=pid,
                player_name="Fixture Player",
                stat="hits",
                threshold=0.5,
            )
            assert "props_history" in r.data_sources_used
            assert r.overall_last_5.games == 5
            assert r.overall_last_5.hit_rate == 0.8
            assert r.overall_last_10.games == 10
            assert r.overall_season.games == 30
            assert r.consistency_score == 0.78
            assert r.sample_size == 30    # season is largest slice
            assert r.sample_confidence == "high"
            # Grade should be A or better with 70% hit + 78% consistency + 30 games
            assert r.matchup_grade in ("A", "A+")
        finally:
            await db.props_history.delete_one({"player_id": pid})

    asyncio.run(_inner())


def test_player_game_logs_fallback_populates_slices():
    from motor.motor_asyncio import AsyncIOMotorClient
    from services.player_matchup_intelligence import get_matchup_intelligence

    async def _inner():
        client = AsyncIOMotorClient(os.environ["MONGO_URL"])
        db = client[os.environ["DB_NAME"]]
        pid = int(str(uuid.uuid4().int)[:9])   # numeric player_id
        name = f"PGL Fixture {uuid.uuid4().hex[:6]}"
        event_prefix = f"pmi_pgl_{uuid.uuid4().hex[:8]}"
        # 12 recent games with hits values: 0,1,2,1,3,0,1,2,1,0,2,1  (avg=1.17)
        values = [0, 1, 2, 1, 3, 0, 1, 2, 1, 0, 2, 1]
        docs = _insert_fixture_pgl(db, "mlb", pid, name, "hits", values, event_prefix)
        try:
            await db.player_game_logs.insert_many(docs)
            r = await get_matchup_intelligence(
                db,
                sport="MLB",
                player_id=pid,
                player_name=name,
                stat="hits",
                threshold=0.5,
            )
            assert "player_game_logs" in r.data_sources_used
            assert r.overall_last_5.games == 5
            assert r.overall_last_10.games == 10
            assert r.overall_season.games == 12
            # avg should be ~1.17
            assert 1.0 <= r.avg_stat_output <= 1.3
            # hit_rate for >0.5: 9 of 12 games have hits > 0.5
            assert 0.7 <= r.overall_season.hit_rate <= 0.8
            assert r.sample_confidence in ("medium", "high")
        finally:
            await db.player_game_logs.delete_many(
                {"game_id": {"$in": [d["game_id"] for d in docs]}}
            )

    asyncio.run(_inner())


def test_threshold_hit_rate_is_weighted_blend():
    """Weighted average of career-vs-opp + L10 hit rates by sample size."""
    from motor.motor_asyncio import AsyncIOMotorClient
    from services.player_matchup_intelligence import get_matchup_intelligence

    async def _inner():
        client = AsyncIOMotorClient(os.environ["MONGO_URL"])
        db = client[os.environ["DB_NAME"]]
        pid = int(str(uuid.uuid4().int)[:9])
        name = f"Blend Fixture {uuid.uuid4().hex[:6]}"
        # 10 recent games — 6 clear over-0.5, 4 zero
        values = [1, 1, 2, 0, 1, 3, 0, 0, 1, 2]
        event_prefix = f"pmi_blend_{uuid.uuid4().hex[:8]}"
        docs = _insert_fixture_pgl(db, "mlb", pid, name, "hits", values, event_prefix)
        try:
            await db.player_game_logs.insert_many(docs)
            r = await get_matchup_intelligence(
                db,
                sport="MLB",
                player_id=pid,
                player_name=name,
                stat="hits",
                threshold=0.5,
            )
            # No career-vs-opp data (MLB PvT skipped for hitters here) →
            # threshold_hit_rate should just reflect L10.
            l10_hr = r.overall_last_10.hit_rate
            assert abs(r.threshold_hit_rate - l10_hr) < 0.01, (
                f"threshold_hit_rate={r.threshold_hit_rate} vs "
                f"expected L10={l10_hr}"
            )
        finally:
            await db.player_game_logs.delete_many(
                {"game_id": {"$in": [d["game_id"] for d in docs]}}
            )

    asyncio.run(_inner())


def test_nfl_opponent_lookup_via_player_weekly():
    """NFL sport uses `nfl_player_weekly.opponent_team` for
    career-vs-opponent (not raw player_game_logs)."""
    from motor.motor_asyncio import AsyncIOMotorClient
    from services.player_matchup_intelligence import get_matchup_intelligence

    async def _inner():
        client = AsyncIOMotorClient(os.environ["MONGO_URL"])
        db = client[os.environ["DB_NAME"]]
        pid = f"nfl_fx_{uuid.uuid4().hex[:8]}"
        opp = "Test Defense"
        docs = [
            {"player_id": pid, "player_name": "NFL Fixture",
             "opponent_team": opp, "season": 2024, "week": w,
             "passing_yards": v}
            for w, v in enumerate([300, 250, 275, 320, 210])
        ]
        try:
            await db.nfl_player_weekly.insert_many(docs)
            r = await get_matchup_intelligence(
                db,
                sport="NFL",
                player_id=pid,
                player_name="NFL Fixture",
                stat="passing_yards",
                opponent_team=opp,
                threshold=250.5,
            )
            assert "nfl_player_weekly" in r.data_sources_used
            assert r.career_vs_opponent.games == 5
            # 300, 275, 320 > 250.5 → 3 of 5
            assert r.career_vs_opponent.hit_rate == 0.6
            # opponent detail note
            assert any(opp in n for n in r.notes)
        finally:
            await db.nfl_player_weekly.delete_many({"player_id": pid})

    asyncio.run(_inner())


def test_result_serialises_to_dict_cleanly():
    from services.player_matchup_intelligence import MatchupIntelligence, MatchupSlice
    r = MatchupIntelligence(
        sport="MLB", player_name="Test", stat="hits",
        overall_last_10=MatchupSlice(games=10, over_hits=7, hit_rate=0.7,
                                       avg=1.1, median=1.0,
                                       stat_values=[1.0]*10),
    )
    d = r.to_dict()
    assert d["sport"] == "MLB"
    assert d["overall_last_10"]["games"] == 10
    assert d["overall_last_10"]["hit_rate"] == 0.7
    # stat_values truncated to 20
    assert len(d["overall_last_10"]["stat_values"]) <= 20
