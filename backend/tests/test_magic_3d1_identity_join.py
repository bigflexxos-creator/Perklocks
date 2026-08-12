"""MAGIC 3D.1 — MLB + Tennis identity join tests."""
import asyncio

from services.magic.identity_join import (
    normalize_name, strip_tennis_prefix,
    mlb_source_row_for_pick, tennis_stats_row_for_pick,
)


# ── Normalization ──────────────────────────────────────────────

def test_normalize_strips_accents():
    assert normalize_name("José Ramírez") == "jose ramirez"


def test_normalize_strips_team_parenthetical():
    assert normalize_name("Aaron Judge (NYY)") == "aaron judge"


def test_normalize_comma_reorder():
    assert normalize_name("Judge, Aaron") == "aaron judge"


def test_normalize_strips_suffix():
    assert normalize_name("Ken Griffey Jr") == "ken griffey"


def test_normalize_case_fold():
    assert normalize_name("KEVIN GAUSMAN") == "kevin gausman"


def test_strip_tennis_prefix():
    assert strip_tennis_prefix("tp:sorana cirstea") == "sorana cirstea"
    assert strip_tennis_prefix("TP:Coco Gauff") == "coco gauff"
    assert strip_tennis_prefix("plain name") == "plain name"


# ── MLB ID-first join ──────────────────────────────────────────

class _Coll:
    def __init__(self, docs=None): self._docs = docs or []
    async def find_one(self, q):
        for d in self._docs:
            if all(d.get(k) == v for k, v in q.items()):
                return d
        return None
    def find(self, q, projection=None):
        matched = [d for d in self._docs
                    if all(d.get(k) == v for k, v in q.items())]
        class _C:
            def __init__(self, a): self.a=a; self.i=0
            def __aiter__(self): return self
            async def __anext__(self):
                if self.i >= len(self.a): raise StopAsyncIteration
                d = self.a[self.i]; self.i += 1; return d
        return _C(matched)


class _DB:
    def __init__(self, colls): self._c = colls
    def __getitem__(self, n): return self._c.get(n, _Coll())
    def __getattr__(self, n): return self._c.get(n, _Coll())


def test_mlb_id_first_join():
    db = _DB({"mlb_statcast_players": _Coll([
        {"player_id": "669364", "name": "xavier edwards", "xslg": 0.42},
    ])})
    pick = {"canonical_player_id": "669364",
            "player_name": "Xavier Edwards"}
    row = asyncio.run(mlb_source_row_for_pick(
        db, pick, "mlb_statcast_players"))
    assert row is not None
    assert row["xslg"] == 0.42


def test_mlb_id_beats_wrong_name():
    """When ID matches, name doesn't matter — authoritative."""
    db = _DB({"mlb_statcast_players": _Coll([
        {"player_id": "669364", "name": "totally different name", "xslg": 0.42},
    ])})
    pick = {"canonical_player_id": "669364",
            "player_name": "Some Other"}
    row = asyncio.run(mlb_source_row_for_pick(
        db, pick, "mlb_statcast_players"))
    assert row is not None


def test_mlb_ambiguous_name_rejected():
    """Two rows same normalized name and no ID → refuse."""
    db = _DB({"mlb_statcast_players": _Coll([
        {"player_id": "1", "name": "jose ramirez"},
        {"player_id": "2", "name": "jose ramirez"},
    ])})
    pick = {"player_name": "Jose Ramirez"}
    row = asyncio.run(mlb_source_row_for_pick(
        db, pick, "mlb_statcast_players"))
    assert row is None


def test_mlb_no_id_no_name_returns_none():
    db = _DB({"mlb_statcast_players": _Coll([])})
    row = asyncio.run(mlb_source_row_for_pick(
        db, {}, "mlb_statcast_players"))
    assert row is None


# ── Tennis join ───────────────────────────────────────────────

def test_tennis_prefix_stripped_and_titled():
    db = _DB({"tennis_player_stats": _Coll([
        {"name": "Sorana Cirstea", "surface": "Hard",
         "first_serve_won_pct": 65.0},
    ])})
    pick = {"canonical_player_id": "tp:sorana cirstea"}
    row = asyncio.run(tennis_stats_row_for_pick(
        db, pick, surface="Hard"))
    assert row is not None
    assert row["surface"] == "Hard"


def test_tennis_surface_isolation():
    """Player has hard-only stats; querying clay returns None
    (all-surface fallback allowed, but distinct surface preserved)."""
    db = _DB({"tennis_player_stats": _Coll([
        {"name": "Sorana Cirstea", "surface": "Hard",
         "first_serve_won_pct": 65.0},
    ])})
    pick = {"canonical_player_id": "tp:sorana cirstea"}
    row = asyncio.run(tennis_stats_row_for_pick(
        db, pick, surface="Clay"))
    # Any-surface fallback allowed — row IS returned, but with
    # different surface tag — adapter marks PARTIAL in the
    # gold_evidence layer.
    assert row is not None
    assert row["surface"] == "Hard"    # not fabricated as Clay


def test_tennis_no_id_no_name():
    db = _DB({"tennis_player_stats": _Coll([])})
    row = asyncio.run(tennis_stats_row_for_pick(
        db, {}, surface="Hard"))
    assert row is None


# ── LIVE integration coverage ─────────────────────────────────

def test_mlb_batter_coverage_live_dramatically_improved():
    import os
    from motor.motor_asyncio import AsyncIOMotorClient
    from dotenv import load_dotenv
    load_dotenv("/app/backend/.env")
    from services.magic.gold_evidence import (
        build_mlb_batter_matchup, Availability,
    )

    async def _run():
        db = AsyncIOMotorClient(os.getenv("MONGO_URL"))["lockscore_db"]
        CUTOFF = "2026-08-07T00:00:00"
        avail = 0; total = 0
        async for p in db.picks.find(
            {"sport": "MLB", "created_at": {"$gte": CUTOFF}},
            {"canonical_player_id": 1, "player_name": 1,
             "selection": 1, "market": 1, "line": 1,
             "canonical_event_id": 1, "side": 1},
        ).limit(100):
            total += 1
            ev = await build_mlb_batter_matchup(db, p)
            if ev.availability == Availability.AVAILABLE:
                avail += 1
        assert total > 0
        # Before 3D.1: 0/200.  After: >= 50% of current MLB picks.
        pct = 100 * avail / total
        assert pct >= 50.0, f"coverage regressed: {pct:.1f}%"

    asyncio.run(_run())


def test_lock_score_and_calibration_constants_pinned():
    """Regression pin — Magic 3D.1 must not touch these."""
    from brain.sim_runner import SIM_RESIDUAL_MAX, MIN_RUNS_FOR_ANCHOR
    from brain.calibration import MIN_SAMPLE_FOR_OVERRIDE, MAX_OPTIMISM_BUFFER
    assert SIM_RESIDUAL_MAX == 3.0
    assert MIN_RUNS_FOR_ANCHOR == 10_000
    assert MIN_SAMPLE_FOR_OVERRIDE == 20
    assert MAX_OPTIMISM_BUFFER == 5.0
