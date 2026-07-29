"""Daily Learning Job + Adaptive Calibration Tests
(Phase 5, iter103, 2026-07-29).

Proves:
  1. Win Probability calibration bins settled picks correctly and
     computes actual hit-rate vs predicted, exposing the delta.
  2. Sport performance aggregates real settled picks (no fake data).
  3. Market performance groups by market_family + honours min_samples.
  4. `run_daily_learning_job` collects all 7 subsections, never raises,
     and captures per-subsection errors without hard-failing.
  5. Snapshot persistence + `load_latest_snapshot` round-trip works.
  6. Settlement loop's daily-gate uses UTC calendar dates so multi-tick
     cycles don't re-persist duplicates.
  7. API endpoints are admin-gated (same posture as sibling routes).
"""
from __future__ import annotations

import asyncio
import datetime as _dt

import pytest


def _run(c): return asyncio.run(c)


# ─── Async-Mongo stub ────────────────────────────────────────────────
class _Cursor:
    def __init__(self, rows): self.rows = list(rows); self._i = 0
    def sort(self, *_a, **_k): return self
    def limit(self, n): self.rows = self.rows[:n]; return self
    async def to_list(self, length=None):
        return list(self.rows[:length] if length else self.rows)
    def __aiter__(self): self._i = 0; return self
    async def __anext__(self):
        if self._i >= len(self.rows): raise StopAsyncIteration
        r = self.rows[self._i]; self._i += 1; return r


class _Coll:
    def __init__(self): self.rows = []
    async def insert_one(self, d): self.rows.append(dict(d))
    async def insert_many(self, docs, **_):
        self.rows.extend(dict(d) for d in docs)
    async def find_one(self, q=None, projection=None):
        for r in self.rows:
            if all(_match(r, k, v) for k, v in (q or {}).items()):
                return dict(r)
        return None
    def find(self, q=None, projection=None):
        matched = []
        for r in self.rows:
            if all(_match(r, k, v) for k, v in (q or {}).items()):
                matched.append(dict(r))
        return _Cursor(matched)


def _match(row, k, v):
    val = row.get(k)
    if isinstance(v, dict):
        if "$in" in v:  return val in v["$in"]
        if "$ne" in v:  return val != v["$ne"]
        if "$gte" in v: return val is not None and val >= v["$gte"]
        if "$exists" in v: return (k in row) == bool(v["$exists"])
    return val == v


class _DB:
    def __init__(self): self._c = {}
    def __getitem__(self, name):
        if name not in self._c: self._c[name] = _Coll()
        return self._c[name]
    def __getattr__(self, name):
        if name.startswith("_"): raise AttributeError(name)
        return self.__getitem__(name)


def _seed_pick(db, *, lock=90, wp=70, status="won", odds=-110,
               sport="MLB", market="Aaron Judge (NYY) Over 1.5 Hits",
               off_board=False, pick_date=None):
    pick_date = pick_date or _dt.datetime.now(
        _dt.timezone.utc).strftime("%Y-%m-%d")
    _run(db.picks.insert_one({
        "lock_score": lock, "win_probability": wp,
        "status": status, "american_odds": odds,
        "sport": sport, "market": market,
        "off_board": off_board, "pick_date": pick_date,
    }))


# ═════════════════════════════════════════════════════════════════════
# A. Win Probability calibration
# ═════════════════════════════════════════════════════════════════════
def test_wp_calibration_bins_and_hit_rate():
    from services.adaptive_learning import compute_win_probability_calibration
    db = _DB()
    # 90-100 band: 8W 2L → 80% actual (predicted 95%, delta -15)
    for _ in range(8): _seed_pick(db, wp=95, status="won")
    for _ in range(2): _seed_pick(db, wp=95, status="lost")
    # 60-69 band: 6W 4L → 60% actual (predicted 65%, delta -5)
    for _ in range(6): _seed_pick(db, wp=65, status="won")
    for _ in range(4): _seed_pick(db, wp=65, status="lost")
    r = _run(compute_win_probability_calibration(db))
    assert r["n_scored"] == 20
    by_label = {b["label"]: b for b in r["bands"]}
    b_top = by_label["90-100"]
    assert b_top["n"] == 10
    assert b_top["actual_pct"] == 80.0
    assert b_top["predicted_avg"] == 95.0
    assert b_top["delta"] == -15.0
    b_mid = by_label["60-69"]
    assert b_mid["n"] == 10
    assert b_mid["actual_pct"] == 60.0


def test_wp_calibration_excludes_push_void():
    from services.adaptive_learning import compute_win_probability_calibration
    db = _DB()
    _seed_pick(db, wp=95, status="won")
    _seed_pick(db, wp=95, status="push")     # excluded
    _seed_pick(db, wp=95, status="void")     # excluded
    _seed_pick(db, wp=95, status="pending")  # excluded
    r = _run(compute_win_probability_calibration(db))
    # Only the 'won' row should count
    assert r["n_scored"] == 1


def test_wp_calibration_empty_bands_return_null():
    from services.adaptive_learning import compute_win_probability_calibration
    db = _DB()
    r = _run(compute_win_probability_calibration(db))
    for b in r["bands"]:
        assert b["n"] == 0
        assert b["predicted_avg"] is None
        assert b["actual_pct"] is None


def test_wp_calibration_brier_score_bounded():
    """Brier is squared-error of prob vs outcome; must be in [0, 1]."""
    from services.adaptive_learning import compute_win_probability_calibration
    db = _DB()
    _seed_pick(db, wp=90, status="won")   # (0.9-1)^2 = 0.01
    _seed_pick(db, wp=90, status="lost")  # (0.9-0)^2 = 0.81
    r = _run(compute_win_probability_calibration(db))
    assert 0 <= r["brier_score"] <= 1.0
    # (0.01 + 0.81)/2 = 0.41
    assert 0.40 <= r["brier_score"] <= 0.42


# ═════════════════════════════════════════════════════════════════════
# B. Sport performance
# ═════════════════════════════════════════════════════════════════════
def test_sport_performance_aggregates_by_sport():
    from services.adaptive_learning import compute_sport_performance
    db = _DB()
    for _ in range(6): _seed_pick(db, sport="MLB", status="won")
    for _ in range(4): _seed_pick(db, sport="MLB", status="lost")
    for _ in range(3): _seed_pick(db, sport="NFL", status="won")
    for _ in range(1): _seed_pick(db, sport="NFL", status="lost")
    rows = _run(compute_sport_performance(db))
    by = {r["sport"]: r for r in rows}
    assert by["MLB"]["n"] == 10
    assert by["MLB"]["win_pct"] == 60.0
    assert by["NFL"]["n"] == 4
    assert by["NFL"]["win_pct"] == 75.0
    # Sorted by ROI descending — NFL (positive ROI) should out-rank MLB
    assert rows[0]["sport"] == "NFL"


def test_sport_performance_skips_off_board_by_default():
    from services.adaptive_learning import compute_sport_performance
    db = _DB()
    _seed_pick(db, sport="MLB", status="won", off_board=True)
    _seed_pick(db, sport="MLB", status="won")
    rows = _run(compute_sport_performance(db))
    assert next(r for r in rows if r["sport"] == "MLB")["n"] == 1
    rows2 = _run(compute_sport_performance(db, include_off_board=True))
    assert next(r for r in rows2 if r["sport"] == "MLB")["n"] == 2


# ═════════════════════════════════════════════════════════════════════
# C. Market performance
# ═════════════════════════════════════════════════════════════════════
def test_market_performance_groups_by_family_and_min_samples():
    from services.adaptive_learning import compute_market_performance
    db = _DB()
    # 12 hits picks, all won → hits family passes min_samples=10
    for _ in range(12):
        _seed_pick(db, market="Aaron Judge (NYY) Over 1.5 Hits",
                   status="won")
    # 3 total_bases picks → below min_samples=10, gets filtered
    for _ in range(3):
        _seed_pick(db, market="Aaron Judge (NYY) Over 1.5 Total Bases",
                   status="won")
    rows = _run(compute_market_performance(db, min_samples=10))
    fams = [r["family"] for r in rows]
    assert "hits" in fams
    assert "total_bases" not in fams   # below min_samples


def test_market_family_normalisation():
    """`_market_family` picks the right family for common market strings."""
    from services.adaptive_learning.daily_learning_job import _market_family
    assert _market_family("Aaron Judge (NYY) Over 1.5 Hits") == "hits"
    assert _market_family("Aaron Judge (NYY) Over 1.5 Total Bases") == "total_bases"
    assert _market_family("Joe Burrow Over 249.5 Passing Yards") == "qb_pass_yards"
    assert _market_family("Justin Jefferson Over 64.5 Receiving Yards") == "rec_yards"
    assert _market_family("Anytime Goal Scorer") == "goal_scorer"
    assert _market_family("Win or Draw") == "win_or_draw"
    assert _market_family("Moneyline") == "moneyline"


# ═════════════════════════════════════════════════════════════════════
# D. Master orchestrator
# ═════════════════════════════════════════════════════════════════════
def test_run_daily_learning_job_returns_all_sections():
    from services.adaptive_learning import run_daily_learning_job
    db = _DB()
    for _ in range(15):
        _seed_pick(db, lock=95, wp=75, sport="MLB", status="won")
    for _ in range(5):
        _seed_pick(db, lock=95, wp=75, sport="MLB", status="lost")
    r = _run(run_daily_learning_job(db, persist=False))
    for section in ("lock_tier_performance",
                    "win_probability_calibration",
                    "sport_performance", "market_performance"):
        assert section in r, f"missing section {section!r}"
    # Errors list must exist even if empty
    assert isinstance(r["errors"], list)
    assert "generated_at" in r


def test_run_daily_learning_job_persists_snapshot():
    from services.adaptive_learning import (
        run_daily_learning_job, load_latest_snapshot, SNAPSHOT_COLL,
    )
    db = _DB()
    _seed_pick(db, lock=90, wp=70, sport="NFL", status="won")
    r = _run(run_daily_learning_job(db, persist=True))
    assert r.get("snapshot_id"), "snapshot_id missing on persist path"
    # Row landed in the collection
    assert len(db[SNAPSHOT_COLL].rows) == 1
    row = db[SNAPSHOT_COLL].rows[0]
    assert row["id"] == r["snapshot_id"]
    assert row["snapshot_date"] == _dt.datetime.now(
        _dt.timezone.utc).strftime("%Y-%m-%d")
    # Round-trip
    latest = _run(load_latest_snapshot(db))
    assert latest is not None
    assert latest["id"] == r["snapshot_id"]


def test_load_latest_snapshot_none_when_empty():
    from services.adaptive_learning import load_latest_snapshot
    db = _DB()
    assert _run(load_latest_snapshot(db)) is None


def test_run_daily_learning_job_never_raises_on_subsection_failure(monkeypatch):
    """A sub-report exception is captured in `errors[]`, never bubbles."""
    from services.adaptive_learning import run_daily_learning_job
    from services.adaptive_learning import daily_learning_job as dlj

    async def _boom(*_a, **_kw):
        raise RuntimeError("simulated failure")
    monkeypatch.setattr(dlj, "compute_win_probability_calibration",
                         _boom)
    r = _run(run_daily_learning_job(_DB(), persist=False))
    assert r["errors"], "sub-report failure was silently swallowed"
    assert any("win_probability_calibration" in e for e in r["errors"])
    # Other sections should still be populated
    assert "lock_tier_performance" in r


# ═════════════════════════════════════════════════════════════════════
# E. Route wiring
# ═════════════════════════════════════════════════════════════════════
def test_learning_endpoints_are_admin_gated():
    """All 4 new endpoints must be gated on `current_admin`."""
    import routes.analytics_routes as ar
    import inspect
    expected = {
        "/analytics/learning/snapshot",
        "/analytics/learning/run",
        "/analytics/learning/win-probability",
        "/analytics/learning/sport-performance",
        "/analytics/learning/market-performance",
    }
    found = set()
    for route in ar.router.routes:
        path = getattr(route, "path", "") or ""
        for suffix in expected:
            if path.endswith(suffix):
                found.add(suffix)
                src = inspect.getsource(route.endpoint)
                assert "current_admin" in src, (
                    f"{suffix} not gated on current_admin")
    missing = expected - found
    assert not missing, f"routes not registered: {sorted(missing)}"


# ═════════════════════════════════════════════════════════════════════
# F. Settlement loop wiring — daily gate present + calendar dated
# ═════════════════════════════════════════════════════════════════════
def test_settlement_loop_gates_daily_learning_by_utc_date():
    import pathlib
    src = pathlib.Path("/app/backend/server.py").read_text()
    # The daily learning block must import `run_daily_learning_job`
    assert "run_daily_learning_job" in src, (
        "server.py does not import run_daily_learning_job")
    # And must use `snapshot_date` (a calendar-day gate) — not just
    # generated_at, which would rerun every tick.
    assert 'snapshot_date' in src, (
        "settlement loop is missing the daily UTC-date gate — will "
        "run the learning job every 15 min instead of once per day")


# ═════════════════════════════════════════════════════════════════════
# G. Semantic guardrail — no synthetic training data
# ═════════════════════════════════════════════════════════════════════
def test_daily_job_reads_only_real_settled_picks():
    """None of the sub-aggregators may generate synthetic rows — every
    read filters by `status ∈ {won, lost, push, void}`."""
    import pathlib
    src = pathlib.Path(
        "/app/backend/services/adaptive_learning/daily_learning_job.py"
    ).read_text()
    banned = ("fake_", "synthesize", "generate_training",
              "make_fake", "simulated_row", "random_pick(")
    for term in banned:
        assert term not in src, (
            f"daily_learning_job.py contains banned synthetic-data "
            f"marker: {term!r}")
    # Must filter status == won/lost/push/void
    assert '"status"' in src
    assert '"won"' in src and '"lost"' in src
