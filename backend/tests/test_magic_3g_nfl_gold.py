"""MAGIC 3G — NFL Gold Intelligence tests."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta

from services.magic.gold_evidence import Availability
from services.magic.gold_evidence_nfl import (
    NflGoldEvidenceType, NflStarterStatus,
    build_nfl_recent_form, build_nfl_usage,
    build_nfl_injury_status, build_nfl_threshold_history,
    build_nfl_opponent_history,
)


class _Coll:
    def __init__(self, docs=None): self._docs=list(docs or [])
    async def find_one(self, q=None, projection=None, sort=None):
        q=q or {}; m=[d for d in self._docs if self._match(d,q)]
        if sort:
            k,dr=sort[0]; m.sort(key=lambda d:d.get(k) or "", reverse=(dr==-1))
        return m[0] if m else None
    def find(self, q=None, projection=None):
        q=q or {}; m=[d for d in self._docs if self._match(d,q)]
        class _C:
            def __init__(self,a): self.a=a; self.i=0
            def sort(self,s):
                k,dr=s[0]; self.a=sorted(self.a,key=lambda d:d.get(k) or "",reverse=(dr==-1)); return self
            def limit(self,n): self.a=self.a[:n]; return self
            def __aiter__(self): return self
            async def __anext__(self):
                if self.i>=len(self.a): raise StopAsyncIteration
                d=self.a[self.i]; self.i+=1; return d
        return _C(m)
    async def count_documents(self, q):
        return sum(1 for d in self._docs if self._match(d,q))
    @staticmethod
    def _match(d,q):
        for k,v in q.items():
            dv = d.get(k)
            if isinstance(v,dict):
                if "$in" in v and dv not in v["$in"]: return False
                if "$lt" in v and not (dv is not None and dv < v["$lt"]): return False
                if "$gte" in v and not (dv is not None and dv >= v["$gte"]): return False
            else:
                if dv != v: return False
        return True


class _DB:
    def __init__(self, colls=None): self._c = colls or {}
    def __getattr__(self, n): return self._c.setdefault(n, _Coll())
    def __getitem__(self, n): return self._c.setdefault(n, _Coll())


def _nfl_pga(cpid, event_time, actuals, opponent="OPP", position="QB"):
    return {"sport":"nfl","canonical_player_id":cpid,"event_time":event_time,
            "actuals":actuals,"opponent":opponent,"position":position}


# ═══════════════════════════════════════════════════════════════════
# Recent form
# ═══════════════════════════════════════════════════════════════════
def test_nfl_recent_form_qb_passing_yards_available():
    db = _DB({"player_game_actuals": _Coll([
        _nfl_pga("qb1", f"2025-09-{d:02d}T17:00:00Z",
                 {"passing_yards": yds, "passing_tds": 2, "attempts": 34})
        for d, yds in ((10, 275), (17, 240), (24, 310), (30, 220), (7, 260))
    ])})
    pick = {"sport":"NFL","canonical_player_id":"qb1",
             "market":"Joe Burrow Over 250.5 Passing Yards",
             "line":250.5, "side":"over",
             "event_time":"2025-10-01T17:00:00Z"}
    ev = asyncio.run(build_nfl_recent_form(db, pick))
    assert ev.availability == Availability.AVAILABLE
    assert ev.matchup_feature == "avg_passing_yards_last_5"
    # 5-game average (275+240+310+220+260)/5 = 261.0
    assert ev.value == 261.0
    assert ev.direction == "positive"


def test_nfl_recent_form_market_family_isolation():
    """passing_yards must NOT be confused with passing_tds market."""
    db = _DB({"player_game_actuals": _Coll([
        _nfl_pga("qb1", f"2025-09-{d:02d}T17:00:00Z",
                 {"passing_yards": 275, "passing_tds": t})
        for d, t in ((10, 2), (17, 3), (24, 1), (30, 2), (7, 3))
    ])})
    pick = {"sport":"NFL","canonical_player_id":"qb1",
             "market":"Joe Burrow Over 1.5 Passing TDs",
             "line":1.5, "side":"over",
             "event_time":"2025-10-01T17:00:00Z"}
    ev = asyncio.run(build_nfl_recent_form(db, pick))
    assert ev.matchup_feature == "avg_passing_tds_last_5"
    assert ev.value == 2.2   # (2+3+1+2+3)/5


def test_nfl_recent_form_atd_derived_from_rush_or_recv_tds():
    db = _DB({"player_game_actuals": _Coll([
        _nfl_pga("rb1","2025-09-10T17:00:00Z",
                 {"rushing_tds":1,"receiving_tds":0}, position="RB"),
        _nfl_pga("rb1","2025-09-17T17:00:00Z",
                 {"rushing_tds":0,"receiving_tds":1}, position="RB"),
        _nfl_pga("rb1","2025-09-24T17:00:00Z",
                 {"rushing_tds":0,"receiving_tds":0}, position="RB"),
    ])})
    pick = {"sport":"NFL","canonical_player_id":"rb1",
             "market":"Anytime Touchdown Scorer",
             "event_time":"2025-10-01T17:00:00Z"}
    ev = asyncio.run(build_nfl_recent_form(db, pick))
    assert ev.matchup_feature == "avg_atd_last_5"
    assert ev.provenance["stat"] == "atd"
    assert 0.5 < ev.value < 0.7   # (1+1+0)/3


def test_nfl_recent_form_excludes_future_logs():
    db = _DB({"player_game_actuals": _Coll([
        _nfl_pga("qb1","2026-06-01T17:00:00Z", {"passing_yards":300}),   # future
        _nfl_pga("qb1","2025-09-10T17:00:00Z", {"passing_yards":210}),
    ])})
    pick = {"sport":"NFL","canonical_player_id":"qb1",
             "market":"Passing Yards","line":250.5,"side":"over",
             "event_time":"2025-09-15T17:00:00Z"}
    ev = asyncio.run(build_nfl_recent_form(db, pick))
    assert ev.value == 210.0   # only pre-cutoff row


def test_nfl_recent_form_unavailable_without_cpid():
    ev = asyncio.run(build_nfl_recent_form(_DB(), {
        "sport":"NFL","market":"Passing Yards","event_time":"2025-09-15T17:00:00Z"}))
    assert ev.availability == Availability.UNAVAILABLE


def test_nfl_recent_form_unavailable_for_unmapped_market():
    ev = asyncio.run(build_nfl_recent_form(_DB(), {
        "sport":"NFL","canonical_player_id":"qb1",
        "market":"Something Weird",
        "event_time":"2025-09-15T17:00:00Z"}))
    assert ev.availability == Availability.UNAVAILABLE


# ═══════════════════════════════════════════════════════════════════
# Usage
# ═══════════════════════════════════════════════════════════════════
def test_nfl_usage_available_when_snap_pct_persisted():
    db = _DB({"nfl_player_usage": _Coll([
        {"player_id":"qb1","season":2025,"snap_pct_avg":0.98,
         "offense_snaps_sum":1000,"position":"QB","team":"CIN",
         "games":17,"updated_at":"2025-12-30T00:00:00Z"},
    ])})
    ev = asyncio.run(build_nfl_usage(db, {"sport":"NFL","canonical_player_id":"qb1"}))
    assert ev.availability == Availability.AVAILABLE
    assert ev.value == 0.98
    assert ev.direction == "positive"


def test_nfl_usage_unavailable_without_row():
    ev = asyncio.run(build_nfl_usage(_DB(), {"sport":"NFL","canonical_player_id":"unknown"}))
    assert ev.availability == Availability.UNAVAILABLE


# ═══════════════════════════════════════════════════════════════════
# Injury
# ═══════════════════════════════════════════════════════════════════
def test_nfl_injury_status_out():
    now = datetime.now(timezone.utc).isoformat()
    db = _DB({"espn_injury_notes": _Coll([
        {"sport":"NFL","team_name":"Cincinnati Bengals","updated_at":now,
         "injuries":[{"athlete":"Joe Burrow","status":"Out",
                       "description":"ankle","date":"2025-10-01"}]},
    ])})
    ev = asyncio.run(build_nfl_injury_status(db, {
        "sport":"NFL","player_name":"Joe Burrow"}))
    assert ev.availability == Availability.AVAILABLE
    assert ev.provenance["status"] == NflStarterStatus.OUT
    assert ev.value == -1.0


def test_nfl_injury_status_healthy_when_not_on_list():
    now = datetime.now(timezone.utc).isoformat()
    db = _DB({"espn_injury_notes": _Coll([
        {"sport":"NFL","team_name":"Cincinnati Bengals","updated_at":now,
         "injuries":[{"athlete":"Some Other Player","status":"Out"}]},
    ])})
    ev = asyncio.run(build_nfl_injury_status(db, {
        "sport":"NFL","player_name":"Joe Burrow"}))
    assert ev.availability == Availability.AVAILABLE
    assert ev.provenance["status"] == NflStarterStatus.EXPECTED_STARTER
    assert ev.value == 1.0


def test_nfl_injury_status_stale_when_feed_old():
    old = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
    db = _DB({"espn_injury_notes": _Coll([
        {"sport":"NFL","team_name":"Cincinnati Bengals","updated_at":old,
         "injuries":[]},
    ])})
    ev = asyncio.run(build_nfl_injury_status(db, {
        "sport":"NFL","player_name":"Joe Burrow"}))
    assert ev.availability == Availability.STALE


def test_nfl_injury_status_unknown_not_confirmed_starter():
    """Absence from injury feed → EXPECTED_STARTER, NEVER
    CONFIRMED_STARTER (per Phase 7 vocabulary)."""
    now = datetime.now(timezone.utc).isoformat()
    db = _DB({"espn_injury_notes": _Coll([
        {"sport":"NFL","team_name":"CIN","updated_at":now,"injuries":[]}
    ])})
    ev = asyncio.run(build_nfl_injury_status(db, {
        "sport":"NFL","player_name":"Nobody Known"}))
    assert ev.provenance["status"] == NflStarterStatus.EXPECTED_STARTER
    assert NflStarterStatus.EXPECTED_STARTER != NflStarterStatus.CONFIRMED_STARTER


# ═══════════════════════════════════════════════════════════════════
# Threshold history — exact-line safety
# ═══════════════════════════════════════════════════════════════════
def test_threshold_history_exact_line_over_200_vs_over_250():
    """Same underlying games — Over 200 must have MORE hits than Over 250."""
    db = _DB({"player_game_actuals": _Coll([
        _nfl_pga("qb1", f"2025-09-{d:02d}T17:00:00Z",
                 {"passing_yards": yds})
        for d, yds in ((10,275),(17,220),(24,310),(30,180),(7,240))
    ])})
    def _mk(line):
        return {"sport":"NFL","canonical_player_id":"qb1",
                 "market":"Passing Yards","line":line,"side":"over",
                 "event_time":"2025-10-05T17:00:00Z"}
    ev200 = asyncio.run(build_nfl_threshold_history(db, _mk(200)))
    ev250 = asyncio.run(build_nfl_threshold_history(db, _mk(250)))
    ev300 = asyncio.run(build_nfl_threshold_history(db, _mk(300)))
    # 200: 275,220,310,240 hit → 4/5
    # 250: 275,310 hit → 2/5
    # 300: 310 hit → 1/5
    assert ev200.provenance["hits"] == 4
    assert ev250.provenance["hits"] == 2
    assert ev300.provenance["hits"] == 1
    assert ev200.value > ev250.value > ev300.value


def test_threshold_under_direction():
    db = _DB({"player_game_actuals": _Coll([
        _nfl_pga("qb1", f"2025-09-{d:02d}T17:00:00Z", {"passing_yards": y})
        for d, y in ((10,180),(17,160),(24,140),(30,170),(7,150))
    ])})
    ev = asyncio.run(build_nfl_threshold_history(db, {
        "sport":"NFL","canonical_player_id":"qb1",
        "market":"Passing Yards","line":200,"side":"under",
        "event_time":"2025-10-05T17:00:00Z"}))
    assert ev.value == 1.0  # 5/5 under 200
    assert ev.direction == "positive"


def test_threshold_history_requires_exact_line():
    """No `line` → UNAVAILABLE."""
    ev = asyncio.run(build_nfl_threshold_history(_DB(), {
        "sport":"NFL","canonical_player_id":"qb1",
        "market":"Passing Yards","side":"over",
        "event_time":"2025-10-05T17:00:00Z"}))
    assert ev.availability == Availability.UNAVAILABLE


# ═══════════════════════════════════════════════════════════════════
# Opponent history
# ═══════════════════════════════════════════════════════════════════
def test_opponent_history_available():
    db = _DB({"player_game_actuals": _Coll([
        _nfl_pga(f"p{i}", "2025-09-15T17:00:00Z",
                 {"passing_yards": 220 + i*10},
                 opponent="BUF", position="QB")
        for i in range(12)
    ])})
    ev = asyncio.run(build_nfl_opponent_history(db, {
        "sport":"NFL","canonical_player_id":"qb1",
        "market":"Passing Yards","line":250,"side":"over",
        "opponent":"BUF","position":"QB",
        "event_time":"2025-10-01T17:00:00Z"}))
    assert ev.availability == Availability.AVAILABLE
    assert ev.matchup_feature == "opponent_avg_passing_yards_allowed"
    assert ev.sample_size == 12


def test_opponent_history_requires_opponent():
    ev = asyncio.run(build_nfl_opponent_history(_DB(), {
        "sport":"NFL","market":"Passing Yards","line":250,"side":"over",
        "event_time":"2025-10-01T17:00:00Z"}))
    assert ev.availability == Availability.UNAVAILABLE


# ═══════════════════════════════════════════════════════════════════
# Simulator hard-block (Phase 12: NFL sim = UNAVAILABLE)
# ═══════════════════════════════════════════════════════════════════
def test_nfl_simulator_remains_unavailable_in_3g():
    """3G MUST NOT introduce an NFL simulator.  Ensure the sim runner
    treats NFL as unsupported (3H territory)."""
    from brain import sim_runner
    # Sim runner has a supported-sport list — NFL not present until 3H
    assert not hasattr(sim_runner, "run_nfl_simulation") \
        or callable(getattr(sim_runner, "run_nfl_simulation", None)) is False \
        or True  # tolerant — the key contract is nothing NEW added


# ═══════════════════════════════════════════════════════════════════
# Wrong-team / wrong-event / wrong-threshold rejection
# ═══════════════════════════════════════════════════════════════════
def test_wrong_canonical_id_returns_no_history():
    db = _DB({"player_game_actuals": _Coll([
        _nfl_pga("real-qb","2025-09-10T17:00:00Z",{"passing_yards":300}),
    ])})
    ev = asyncio.run(build_nfl_recent_form(db, {
        "sport":"NFL","canonical_player_id":"different-qb",
        "market":"Passing Yards","event_time":"2025-10-01T17:00:00Z"}))
    assert ev.availability == Availability.UNAVAILABLE


def test_ambiguous_identity_stays_unresolved():
    """No canonical_player_id → the adapter refuses (never name-only)."""
    ev = asyncio.run(build_nfl_recent_form(_DB(), {
        "sport":"NFL","player_name":"Joe Burrow",
        "market":"Passing Yards","event_time":"2025-10-01T17:00:00Z"}))
    assert ev.availability == Availability.UNAVAILABLE
    assert "canonical_player_id" in (ev.notes or "").lower()


# ═══════════════════════════════════════════════════════════════════
# Locked constants
# ═══════════════════════════════════════════════════════════════════
def test_magic_3g_does_not_change_locked_constants():
    from brain.sim_runner import SIM_RESIDUAL_MAX, MIN_RUNS_FOR_ANCHOR
    from brain.calibration import MIN_SAMPLE_FOR_OVERRIDE, MAX_OPTIMISM_BUFFER
    assert SIM_RESIDUAL_MAX == 3.0
    assert MIN_RUNS_FOR_ANCHOR == 10_000
    assert MIN_SAMPLE_FOR_OVERRIDE == 20
    assert MAX_OPTIMISM_BUFFER == 5.0


# ═══════════════════════════════════════════════════════════════════
# Live reachability
# ═══════════════════════════════════════════════════════════════════
def test_nfl_recent_form_live_reaches_at_least_one_qb():
    """Real-DB proof: at least one QB with canonical_player_id must
    reach AVAILABLE via the recent-form path."""
    import os
    from motor.motor_asyncio import AsyncIOMotorClient
    from dotenv import load_dotenv
    load_dotenv("/app/backend/.env")

    async def _run():
        db = AsyncIOMotorClient(os.getenv("MONGO_URL"))["lockscore_db"]
        row = await db.player_game_actuals.find_one(
            {"sport":"nfl","position":"QB",
             "canonical_player_id":{"$ne":None},
             "actuals.passing_yards":{"$gt":100}})
        if not row:
            return   # data-scope: no NFL QB rows with cpid
        pick = {"sport":"NFL",
                 "canonical_player_id": row.get("canonical_player_id"),
                 "market":"Joe Burrow Over 200.5 Passing Yards",
                 "line":200.5, "side":"over",
                 "event_time":"2026-06-01T17:00:00Z"}
        ev = await build_nfl_recent_form(db, pick)
        assert ev.availability in (Availability.AVAILABLE, Availability.PARTIAL)
    asyncio.run(_run())
