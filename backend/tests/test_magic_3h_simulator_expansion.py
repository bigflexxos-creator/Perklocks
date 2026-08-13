"""MAGIC 3H — Missing sport-specific simulator expansion tests.

NFL simulator: full validation.
CFB/UFC/NHL: honest UNAVAILABLE + reason surfaced.

All simulators MUST use the persistence contract from Magic 3B.
No fabrication of probabilities.  Missing input → UNAVAILABLE.
"""
from __future__ import annotations

import asyncio
import pytest

from services.magic.simulators.nfl_simulator import (
    SIMULATOR_NAME, SIMULATOR_VERSION, SIMULATOR_TYPE,
    run_nfl_simulation, _nfl_stat, _deterministic_seed,
    cfb_simulator_status, ufc_simulator_status, nhl_simulator_status,
)
from services.magic.sim_cal_store import (
    build_simulator_output_doc, build_input_fingerprint,
)


# ── DB fake ──────────────────────────────────────────────────────

class _Coll:
    def __init__(self, docs=None): self._docs=list(docs or [])
    def find(self, q=None, projection=None):
        q=q or {}; m=[d for d in self._docs if self._match(d,q)]
        class _C:
            def __init__(self,a): self.a=a; self.i=0
            def sort(self,s):
                k,dr=s[0]; self.a=sorted(self.a,key=lambda d:d.get(k) or "",reverse=(dr==-1))
                return self
            def limit(self,n): self.a=self.a[:n]; return self
            def __aiter__(self): return self
            async def __anext__(self):
                if self.i>=len(self.a): raise StopAsyncIteration
                d=self.a[self.i]; self.i+=1; return d
        return _C(m)
    @staticmethod
    def _match(d,q):
        for k,v in q.items():
            dv = d.get(k)
            if isinstance(v,dict):
                if "$lt" in v and not (dv is not None and dv < v["$lt"]): return False
            else:
                if dv != v: return False
        return True


class _DB:
    def __init__(self, colls=None): self._c = colls or {}
    def __getattr__(self, n): return self._c.setdefault(n, _Coll())
    def __getitem__(self, n): return self._c.setdefault(n, _Coll())


def _nfl_row(cpid, event_time, actuals):
    return {"sport":"nfl","canonical_player_id":cpid,
             "event_time":event_time,"actuals":actuals}


# ═══════════════════════════════════════════════════════════════════
# NFL simulator
# ═══════════════════════════════════════════════════════════════════
def test_nfl_sim_passing_yards_returns_valid_payload():
    db = _DB({"player_game_actuals": _Coll([
        _nfl_row("qb1", f"2025-09-{d:02d}T17:00:00Z",
                 {"passing_yards": yds})
        for d, yds in ((10, 275), (17, 240), (24, 310), (30, 220), (7, 260))
    ])})
    pick = {"id":"p1","sport":"NFL","canonical_player_id":"qb1",
             "canonical_event_id":"e1","market":"Passing Yards",
             "line":250.5,"side":"over","event_time":"2025-10-01T17:00:00Z"}
    sim = asyncio.run(run_nfl_simulation(db, pick, runs=2000))
    assert sim is not None
    assert sim["simulator_name"] == SIMULATOR_NAME
    assert sim["simulator_version"] == SIMULATOR_VERSION
    assert sim["simulator_type"] == SIMULATOR_TYPE
    assert sim["sim_runs"] == 2000
    assert 0.0 <= sim["sim_win_probability"] <= 1.0
    assert sim["sim_stat"] == "passing_yards"
    assert sim["sim_distribution"] == "lognormal"
    # Quantiles must be ordered.
    assert (sim["sim_q10"] <= sim["sim_q25"] <= sim["sim_median"]
            <= sim["sim_q75"] <= sim["sim_q90"])
    assert sim["sim_sample_size"] == 5


def test_nfl_sim_exact_threshold_produces_different_probabilities():
    """Different line → different simulated probability + different
    fingerprint."""
    db = _DB({"player_game_actuals": _Coll([
        _nfl_row("qb1", f"2025-09-{d:02d}T17:00:00Z", {"passing_yards": y})
        for d, y in ((10,275),(17,240),(24,310),(30,220),(7,260))
    ])})
    def pk(line):
        return {"id":f"p_{line}","sport":"NFL","canonical_player_id":"qb1",
                 "canonical_event_id":"e1","market":"Passing Yards",
                 "line":line,"side":"over",
                 "event_time":"2025-10-01T17:00:00Z"}
    s200 = asyncio.run(run_nfl_simulation(db, pk(200), runs=3000))
    s250 = asyncio.run(run_nfl_simulation(db, pk(250), runs=3000))
    s300 = asyncio.run(run_nfl_simulation(db, pk(300), runs=3000))
    assert s200["sim_win_probability"] > s250["sim_win_probability"] \
        > s300["sim_win_probability"]
    # Fingerprints must differ.
    fp200 = build_input_fingerprint(pk(200))
    fp250 = build_input_fingerprint(pk(250))
    fp300 = build_input_fingerprint(pk(300))
    assert fp200 != fp250 != fp300


def test_nfl_sim_deterministic_with_same_pick():
    """Same identity + same line + same version → identical p_hit."""
    db = _DB({"player_game_actuals": _Coll([
        _nfl_row("qb1", f"2025-09-{d:02d}T17:00:00Z", {"passing_yards": y})
        for d, y in ((10,275),(17,240),(24,310),(30,220),(7,260))
    ])})
    pick = {"id":"p1","sport":"NFL","canonical_player_id":"qb1",
             "canonical_event_id":"e1","market":"Passing Yards",
             "line":250.5,"side":"over","event_time":"2025-10-01T17:00:00Z"}
    a = asyncio.run(run_nfl_simulation(db, pick, runs=3000))
    b = asyncio.run(run_nfl_simulation(db, pick, runs=3000))
    assert a["sim_win_probability"] == b["sim_win_probability"]
    assert a["seed"] == b["seed"]


def test_nfl_sim_atd_bernoulli():
    """ATD should return Bernoulli-style p_hit close to empirical rate."""
    db = _DB({"player_game_actuals": _Coll([
        _nfl_row("rb1", f"2025-09-{d:02d}T17:00:00Z",
                 {"rushing_tds":r, "receiving_tds":0})
        for d, r in ((3,1),(10,0),(17,1),(24,1),(30,0))
    ])})
    pick = {"id":"p_atd","sport":"NFL","canonical_player_id":"rb1",
             "canonical_event_id":"e1","market":"Anytime Touchdown",
             "line":None,"side":None,
             "event_time":"2025-10-05T17:00:00Z"}
    sim = asyncio.run(run_nfl_simulation(db, pick, runs=3000))
    assert sim is not None
    assert sim["sim_stat"] == "atd"
    assert sim["sim_distribution"] == "bernoulli"
    # empirical 3/5 with shrinkage (3+1)/(5+3) = 0.5 → allow band.
    assert 0.35 < sim["sim_win_probability"] < 0.70


def test_nfl_sim_under_direction():
    db = _DB({"player_game_actuals": _Coll([
        _nfl_row("qb1", f"2025-09-{d:02d}T17:00:00Z", {"passing_yards": y})
        for d, y in ((10,180),(17,160),(24,140),(30,170),(7,150))
    ])})
    pick = {"id":"p_u","sport":"NFL","canonical_player_id":"qb1",
             "canonical_event_id":"e1","market":"Passing Yards",
             "line":200,"side":"under",
             "event_time":"2025-10-05T17:00:00Z"}
    sim = asyncio.run(run_nfl_simulation(db, pick, runs=3000))
    assert sim["sim_win_probability"] > 0.60


# ═══════════════════════════════════════════════════════════════════
# UNAVAILABLE / rejection paths
# ═══════════════════════════════════════════════════════════════════
def test_nfl_sim_unavailable_without_cpid():
    db = _DB()
    r = asyncio.run(run_nfl_simulation(db, {
        "sport":"NFL","market":"Passing Yards",
        "line":250,"side":"over",
        "event_time":"2025-10-01T17:00:00Z"}))
    assert r is None


def test_nfl_sim_unavailable_for_unmapped_market():
    r = asyncio.run(run_nfl_simulation(_DB(), {
        "sport":"NFL","canonical_player_id":"qb1",
        "market":"Something Random",
        "event_time":"2025-10-01T17:00:00Z"}))
    assert r is None


def test_nfl_sim_partial_when_small_sample():
    db = _DB({"player_game_actuals": _Coll([
        _nfl_row("qb1","2025-09-10T17:00:00Z",{"passing_yards":275}),
        _nfl_row("qb1","2025-09-17T17:00:00Z",{"passing_yards":240}),
    ])})
    r = asyncio.run(run_nfl_simulation(_DB({"player_game_actuals":
        _Coll(db.player_game_actuals._docs)}), {
        "id":"p1","sport":"NFL","canonical_player_id":"qb1",
        "canonical_event_id":"e1","market":"Passing Yards",
        "line":250,"side":"over","event_time":"2025-10-01T17:00:00Z"}))
    # < 3 pre-cutoff samples → UNAVAILABLE (None)
    assert r is None


def test_nfl_sim_excludes_future_samples():
    db = _DB({"player_game_actuals": _Coll([
        _nfl_row("qb1","2026-06-01T17:00:00Z",{"passing_yards":500}),   # future
        _nfl_row("qb1","2025-09-10T17:00:00Z",{"passing_yards":210}),
        _nfl_row("qb1","2025-09-17T17:00:00Z",{"passing_yards":220}),
        _nfl_row("qb1","2025-09-24T17:00:00Z",{"passing_yards":215}),
    ])})
    pick = {"id":"p1","sport":"NFL","canonical_player_id":"qb1",
             "canonical_event_id":"e1","market":"Passing Yards",
             "line":250,"side":"over","event_time":"2025-10-01T17:00:00Z"}
    sim = asyncio.run(run_nfl_simulation(db, pick, runs=3000))
    assert sim is not None
    assert sim["sim_sample_size"] == 3
    # 500 excluded — mean draws should NOT climb near line 250
    assert sim["sim_win_probability"] < 0.30


def test_nfl_sim_wrong_cpid_returns_unavailable():
    db = _DB({"player_game_actuals": _Coll([
        _nfl_row("real-qb", f"2025-09-{d:02d}T17:00:00Z",
                 {"passing_yards": 275}) for d in (10,17,24,30,7)
    ])})
    r = asyncio.run(run_nfl_simulation(db, {
        "id":"p1","sport":"NFL","canonical_player_id":"wrong-qb",
        "canonical_event_id":"e1","market":"Passing Yards",
        "line":250,"side":"over","event_time":"2025-10-01T17:00:00Z"}))
    assert r is None


def test_nfl_sim_requires_line_and_side_for_non_atd():
    db = _DB({"player_game_actuals": _Coll([
        _nfl_row("qb1", f"2025-09-{d:02d}T17:00:00Z",
                 {"passing_yards": 275}) for d in (10,17,24,30,7)
    ])})
    r = asyncio.run(run_nfl_simulation(db, {
        "id":"p1","sport":"NFL","canonical_player_id":"qb1",
        "canonical_event_id":"e1","market":"Passing Yards",
        # no line, no side
        "event_time":"2025-10-01T17:00:00Z"}))
    assert r is None


# ═══════════════════════════════════════════════════════════════════
# Persistence contract (Magic 3B)
# ═══════════════════════════════════════════════════════════════════
def test_nfl_sim_output_fits_persistence_contract():
    """The sim payload must produce a valid persistence document via
    Magic 3B's `build_simulator_output_doc`."""
    db = _DB({"player_game_actuals": _Coll([
        _nfl_row("qb1", f"2025-09-{d:02d}T17:00:00Z",
                 {"passing_yards": y})
        for d, y in ((10,275),(17,240),(24,310),(30,220),(7,260))
    ])})
    pick = {"id":"p1","sport":"NFL","canonical_player_id":"qb1",
             "canonical_event_id":"e1","market":"Passing Yards",
             "line":250.5,"side":"over","event_time":"2025-10-01T17:00:00Z",
             "league":"NFL"}
    sim = asyncio.run(run_nfl_simulation(db, pick, runs=2000))
    doc = build_simulator_output_doc(pick, sim)
    assert doc is not None
    assert doc["simulator_name"] == SIMULATOR_NAME
    assert doc["simulator_version"] == SIMULATOR_VERSION
    assert doc["sport"] == "NFL"
    assert doc["pick_id"] == "p1"
    assert doc["line"] == 250.5
    assert doc["side"] == "over"
    assert doc["p_hit"] == round(sim["sim_win_probability"], 4)
    assert doc["input_fingerprint"] and len(doc["input_fingerprint"]) == 64
    # Quantiles preserved
    assert doc["q10"] is not None and doc["q90"] is not None


def test_nfl_sim_fingerprint_changes_when_line_changes():
    db = _DB({"player_game_actuals": _Coll([
        _nfl_row("qb1", f"2025-09-{d:02d}T17:00:00Z",
                 {"passing_yards": y})
        for d, y in ((10,275),(17,240),(24,310),(30,220),(7,260))
    ])})
    def pk(line):
        return {"id":"p_x","sport":"NFL","canonical_player_id":"qb1",
                 "canonical_event_id":"e1","market":"Passing Yards",
                 "line":line,"side":"over",
                 "event_time":"2025-10-01T17:00:00Z"}
    d200 = build_simulator_output_doc(
        pk(200), asyncio.run(run_nfl_simulation(_DB({"player_game_actuals":
            _Coll(db.player_game_actuals._docs)}), pk(200), runs=1500)))
    d250 = build_simulator_output_doc(
        pk(250), asyncio.run(run_nfl_simulation(_DB({"player_game_actuals":
            _Coll(db.player_game_actuals._docs)}), pk(250), runs=1500)))
    assert d200["input_fingerprint"] != d250["input_fingerprint"]


# ═══════════════════════════════════════════════════════════════════
# CFB / UFC / NHL honest UNAVAILABLE
# ═══════════════════════════════════════════════════════════════════
def test_cfb_ufc_nhl_report_unavailable_with_reason():
    for f in (cfb_simulator_status, ufc_simulator_status,
              nhl_simulator_status):
        s = f()
        assert s["status"] == "UNAVAILABLE"
        assert len(s["reason"]) > 20


def test_model_probability_cannot_become_simulator_probability():
    """A pick with only model_probability must NOT produce a simulator
    output — the runner requires real historical inputs."""
    db = _DB()   # no history
    r = asyncio.run(run_nfl_simulation(db, {
        "id":"p1","sport":"NFL","canonical_player_id":"qb1",
        "canonical_event_id":"e1","market":"Passing Yards",
        "line":250,"side":"over","event_time":"2025-10-01T17:00:00Z",
        "model_probability": 0.72}))   # ignored
    assert r is None


# ═══════════════════════════════════════════════════════════════════
# Locked constants
# ═══════════════════════════════════════════════════════════════════
def test_magic_3h_does_not_change_locked_constants():
    from brain.sim_runner import SIM_RESIDUAL_MAX, MIN_RUNS_FOR_ANCHOR
    from brain.calibration import MIN_SAMPLE_FOR_OVERRIDE, MAX_OPTIMISM_BUFFER
    assert SIM_RESIDUAL_MAX == 3.0
    assert MIN_RUNS_FOR_ANCHOR == 10_000
    assert MIN_SAMPLE_FOR_OVERRIDE == 20
    assert MAX_OPTIMISM_BUFFER == 5.0
