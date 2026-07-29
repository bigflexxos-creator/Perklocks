"""Lock Score Tier Performance tests (Phase 5, iter102, 2026-07-29).

Proves the redesigned Lock Score interpretation:

  1. `lock_score` and `win_probability` are SEPARATE fields — no code
     path forces `lock_score == win_probability`.
  2. The bucket aggregator uses the exact tiers requested by the user
     (99, 95-98, 90-94, 85-89, 80-84).
  3. Bucket rollups compute n_picks, wins, losses, pushes, voids,
     win_pct, ROI, avg_lock, avg_win_prob correctly.
  4. ROI honours American odds; pushes/voids don't inflate PnL; missing
     odds fall back to -110.
  5. The API exposes explicit `field_semantics` clarifying that
     `lock_score` is NOT a probability.
  6. The endpoint is admin-gated (same posture as other analytics routes).
  7. `include_off_board=False` skips hidden picks; `days` clamps window.

No models retrained. Existing calibration logic untouched.
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
    async def to_list(self, length=None): return list(self.rows[:length] if length else self.rows)
    def __aiter__(self): self._i = 0; return self
    async def __anext__(self):
        if self._i >= len(self.rows): raise StopAsyncIteration
        r = self.rows[self._i]; self._i += 1; return r


class _Coll:
    def __init__(self): self.rows = []
    async def insert_one(self, d): self.rows.append(dict(d))
    async def insert_many(self, docs, **_): self.rows.extend(dict(d) for d in docs)
    def find(self, q=None, projection=None):
        matched = []
        for r in self.rows:
            ok = True
            for k, v in (q or {}).items():
                val = r.get(k)
                if isinstance(v, dict):
                    if "$in" in v and val not in v["$in"]: ok = False; break
                    if "$ne" in v and val == v["$ne"]:     ok = False; break
                    if "$gte" in v and (val is None or val < v["$gte"]): ok = False; break
                elif val != v:
                    ok = False; break
            if ok: matched.append(dict(r))
        return _Cursor(matched)


class _DB:
    def __init__(self): self._c = {"picks": _Coll()}
    def __getitem__(self, name): return self._c.setdefault(name, _Coll())
    def __getattr__(self, name):
        if name.startswith("_"): raise AttributeError(name)
        return self.__getitem__(name)


def _seed(db, *, lock, wp, status, odds=-110, off_board=False,
           sport="MLB", pick_date=None, extra=None):
    pick_date = pick_date or _dt.datetime.now(
        _dt.timezone.utc).strftime("%Y-%m-%d")
    row = {"lock_score": lock, "win_probability": wp,
           "status": status, "american_odds": odds,
           "off_board": off_board, "sport": sport,
           "pick_date": pick_date}
    if extra: row.update(extra)
    return _run(db.picks.insert_one(row))


# ═════════════════════════════════════════════════════════════════════
# A. Bucket schema is exactly what the user asked for
# ═════════════════════════════════════════════════════════════════════
def test_lock_buckets_schema_matches_spec():
    from services.lock_score_performance import LOCK_BUCKETS
    labels = [b[0] for b in LOCK_BUCKETS]
    for required in ("99", "95-98", "90-94", "85-89", "80-84"):
        assert required in labels, (
            f"required bucket '{required}' missing — got {labels}")


def test_bucket_for_boundaries():
    """Boundary values fall into the expected bucket."""
    from services.lock_score_performance import _bucket_for
    assert _bucket_for(99)   == "99"
    assert _bucket_for(99.9) == "99"
    assert _bucket_for(98)   == "95-98"
    assert _bucket_for(95)   == "95-98"
    assert _bucket_for(94.9) == "90-94"
    assert _bucket_for(90)   == "90-94"
    assert _bucket_for(89.9) == "85-89"
    assert _bucket_for(85)   == "85-89"
    assert _bucket_for(80)   == "80-84"
    assert _bucket_for(79.9) == "70-79"
    assert _bucket_for(69.9) == "<70"
    assert _bucket_for(None) is None


# ═════════════════════════════════════════════════════════════════════
# B. Bucket aggregation math
# ═════════════════════════════════════════════════════════════════════
def test_win_pct_and_roi_computed_correctly():
    from services.lock_score_performance import compute_bucket_performance
    db = _DB()
    # 4 wins @ -110 = 4 × 0.9091 = 3.636u
    # 6 losses     = -6u → net = -2.364u over 10 → ROI = -23.6%
    for _ in range(4): _seed(db, lock=99, wp=80, status="won",  odds=-110)
    for _ in range(6): _seed(db, lock=99, wp=80, status="lost", odds=-110)
    r = _run(compute_bucket_performance(db))
    b99 = next(b for b in r["buckets"] if b["label"] == "99")
    assert b99["n"] == 10 and b99["wins"] == 4 and b99["losses"] == 6
    assert b99["win_pct"] == 40.0
    assert -23.7 < b99["roi_pct"] < -23.5   # -0.2364 ± rounding


def test_pushes_and_voids_do_not_count_in_win_pct():
    from services.lock_score_performance import compute_bucket_performance
    db = _DB()
    _seed(db, lock=95, wp=70, status="won")
    _seed(db, lock=95, wp=70, status="push")
    _seed(db, lock=95, wp=70, status="void")
    _seed(db, lock=95, wp=70, status="lost")
    r = _run(compute_bucket_performance(db))
    b = next(x for x in r["buckets"] if x["label"] == "95-98")
    assert b["n"] == 4
    assert b["wins"] == 1 and b["losses"] == 1
    assert b["pushes"] == 1 and b["voids"] == 1
    # Win pct denominator is won+lost only
    assert b["win_pct"] == 50.0


def test_positive_odds_pay_more_than_negative():
    """A +200 win pays 2 units; a -200 win pays 0.5 units."""
    from services.lock_score_performance import compute_bucket_performance
    db = _DB()
    _seed(db, lock=99, wp=50, status="won", odds=+200)
    _seed(db, lock=99, wp=50, status="lost", odds=+200)
    r = _run(compute_bucket_performance(db))
    b = next(x for x in r["buckets"] if x["label"] == "99")
    # net pnl = 2u - 1u = 1u over 2 = 50%
    assert 49.5 <= b["roi_pct"] <= 50.5

    db2 = _DB()
    _seed(db2, lock=99, wp=50, status="won", odds=-200)
    _seed(db2, lock=99, wp=50, status="lost", odds=-200)
    r2 = _run(compute_bucket_performance(db2))
    b2 = next(x for x in r2["buckets"] if x["label"] == "99")
    # net pnl = 0.5u - 1u = -0.5u over 2 = -25%
    assert -25.5 <= b2["roi_pct"] <= -24.5


def test_missing_odds_fall_back_to_minus_110():
    from services.lock_score_performance import _pick_pnl
    # No odds → -110 fallback → win = 0.9091, loss = -1.0
    assert abs(_pick_pnl("won", None) - 0.9091) < 0.01
    assert _pick_pnl("lost", None) == -1.0


def test_off_board_picks_excluded_by_default():
    from services.lock_score_performance import compute_bucket_performance
    db = _DB()
    _seed(db, lock=99, wp=80, status="won", off_board=True)
    _seed(db, lock=99, wp=80, status="won", off_board=False)
    r = _run(compute_bucket_performance(db))
    b = next(x for x in r["buckets"] if x["label"] == "99")
    assert b["n"] == 1

    r2 = _run(compute_bucket_performance(db, include_off_board=True))
    b2 = next(x for x in r2["buckets"] if x["label"] == "99")
    assert b2["n"] == 2


def test_sport_filter():
    from services.lock_score_performance import compute_bucket_performance
    db = _DB()
    _seed(db, lock=99, wp=80, status="won", sport="MLB")
    _seed(db, lock=99, wp=80, status="won", sport="NFL")
    r = _run(compute_bucket_performance(db, sport="MLB"))
    b = next(x for x in r["buckets"] if x["label"] == "99")
    assert b["n"] == 1


def test_days_lookback_clamps_window():
    from services.lock_score_performance import compute_bucket_performance
    db = _DB()
    old_date = ((_dt.datetime.now(_dt.timezone.utc)
                  - _dt.timedelta(days=200))
                 .strftime("%Y-%m-%d"))
    _seed(db, lock=99, wp=80, status="won", pick_date=old_date)
    _seed(db, lock=99, wp=80, status="won")   # today
    r_all = _run(compute_bucket_performance(db))
    r_7d = _run(compute_bucket_performance(db, days=7))
    b_all = next(x for x in r_all["buckets"] if x["label"] == "99")
    b_7d = next(x for x in r_7d["buckets"] if x["label"] == "99")
    assert b_all["n"] == 2
    assert b_7d["n"] == 1


def test_empty_bucket_returns_null_metrics():
    from services.lock_score_performance import compute_bucket_performance
    db = _DB()
    _seed(db, lock=95, wp=70, status="won")
    r = _run(compute_bucket_performance(db))
    b99 = next(x for x in r["buckets"] if x["label"] == "99")
    assert b99["n"] == 0
    assert b99["win_pct"] is None
    assert b99["roi_pct"] is None
    assert b99["avg_lock"] is None


# ═════════════════════════════════════════════════════════════════════
# C. Semantic separation of lock_score vs win_probability
# ═════════════════════════════════════════════════════════════════════
def test_response_documents_lock_score_is_not_a_probability():
    """`field_semantics` MUST clarify lock_score ≠ win_probability."""
    from services.lock_score_performance import compute_bucket_performance
    r = _run(compute_bucket_performance(_DB()))
    fs = r["field_semantics"]
    for key in ("lock_score", "win_probability", "roi_pct"):
        assert key in fs, f"field_semantics.{key} missing"
    # Explicit "NOT a probability" language on lock_score
    assert "not a" in fs["lock_score"].lower() \
        and "probability" in fs["lock_score"].lower()


def test_lock_score_and_win_probability_are_independent_fields():
    """A seeded pick with lock_score != win_probability must retain
    both values on the response (avg_lock ≠ avg_win_prob)."""
    from services.lock_score_performance import compute_bucket_performance
    db = _DB()
    for _ in range(5): _seed(db, lock=99, wp=72, status="won")
    r = _run(compute_bucket_performance(db))
    b = next(x for x in r["buckets"] if x["label"] == "99")
    assert b["avg_lock"] == 99.0
    assert b["avg_win_prob"] == 72.0
    assert b["avg_lock"] != b["avg_win_prob"], (
        "Aggregator collapsed lock_score into win_probability — the "
        "two must remain independent")


def test_99_lock_is_not_forced_to_99_percent():
    """Regression: a 99 Lock tier that only hit 60% of the time must
    REPORT 60%, not be silently bumped to 99%."""
    from services.lock_score_performance import compute_bucket_performance
    db = _DB()
    # 6 W / 4 L → 60% actual on a lock-99 bucket
    for _ in range(6): _seed(db, lock=99, wp=88, status="won")
    for _ in range(4): _seed(db, lock=99, wp=88, status="lost")
    r = _run(compute_bucket_performance(db))
    b = next(x for x in r["buckets"] if x["label"] == "99")
    assert b["win_pct"] == 60.0
    # The bucket's avg lock is 99.0 but win% is 60.0 — proving the
    # system LEARNS real performance instead of forcing lock == win%.
    assert b["avg_lock"] == 99.0
    assert b["win_pct"] < b["avg_lock"], (
        "System is forcing lock_score to equal win_pct — must show the "
        "real delta between the confidence rank and observed accuracy.")


# ═════════════════════════════════════════════════════════════════════
# D. Route wiring
# ═════════════════════════════════════════════════════════════════════
def test_analytics_route_registered_and_admin_gated():
    """The new `/api/analytics/lock-tiers` endpoint must be mounted
    on the admin router — same posture as the other calibration routes."""
    import routes.analytics_routes as ar
    # Match on any suffix — the router mounts under /api prefix.
    matched = [r for r in ar.router.routes
                if getattr(r, "path", "").endswith("/analytics/lock-tiers")]
    assert matched, (
        "/analytics/lock-tiers route not registered on router — got "
        f"{[getattr(r,'path','') for r in ar.router.routes if 'analytics' in getattr(r,'path','')]}"
    )
    route = matched[0]
    import inspect
    sig = inspect.signature(route.endpoint)
    params = list(sig.parameters.keys())
    assert "user" in params, "endpoint must take a `user` dependency"
    src = inspect.getsource(route.endpoint)
    assert "current_admin" in src, (
        "endpoint must be gated with `current_admin` (same as siblings)")


def test_endpoint_returns_expected_shape():
    """End-to-end shape: buckets[], summary{}, field_semantics{}."""
    from services.lock_score_performance import compute_bucket_performance
    db = _DB()
    _seed(db, lock=93, wp=71, status="won")
    r = _run(compute_bucket_performance(db))
    for k in ("buckets", "summary", "field_semantics"):
        assert k in r, f"missing top-level key {k!r}"
    assert isinstance(r["buckets"], list) and len(r["buckets"]) >= 5
    assert "generated_at" in r["summary"]
    assert "n_scored" in r["summary"]


# ═════════════════════════════════════════════════════════════════════
# E. Original scoring untouched
# ═════════════════════════════════════════════════════════════════════
def test_lock_calibration_module_unchanged_public_api():
    """No accidental breakage of the isotonic calibrator's public API
    while we were adding tier stats."""
    import lock_calibration as lc
    for name in ("compute_display_lock_score", "apply_calibration",
                 "fit_from_db", "load_curve", "maybe_recalibrate",
                 "calibration_report"):
        assert hasattr(lc, name), f"lock_calibration.{name} disappeared"
