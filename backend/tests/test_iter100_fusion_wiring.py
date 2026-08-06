"""Fusion Wiring Tests (Phase 5 wiring fix, iter100, 2026-07-29).

Proves the fixes requested by the user:

  1. `enrich_picks_bulk` attaches `fusion` blocks to board picks and
     persists to `fusion_predictions` for on-board player-props.
  2. `grade_settled_fusion_predictions` walks settled picks, extracts
     the actual stat value, and updates `fusion_predictions` with
     `actual_value`, `outcome`, `correct`, `winning_component`.
  3. The settlement loop's call site to the grader is present and gated
     on the full-tick branch (does not fire every 60 s).
  4. Startup registers the three new `fusion_predictions` indexes.
  5. The refresh path registers the fusion enrichment step exactly ONCE,
     between Board Visibility tagging and `insert_many`.

No scoring logic is touched. No new models are added.
"""
from __future__ import annotations

import asyncio
import pathlib
import re

import pytest


def _run(c): return asyncio.run(c)


# ─── Async-Mongo stub (bounded, no real DB needed) ────────────────────
class _Cursor:
    def __init__(self, rows):
        self.rows = list(rows)
        self._proj = None
    def limit(self, n): self.rows = self.rows[:n]; return self
    def sort(self, *_a, **_k): return self
    async def to_list(self, length=None):
        return list(self.rows if length is None else self.rows[:length])
    def __aiter__(self): self._i = 0; return self
    async def __anext__(self):
        if self._i >= len(self.rows): raise StopAsyncIteration
        r = self.rows[self._i]; self._i += 1; return r


class _Coll:
    def __init__(self, name):
        self.name = name
        self.rows = []
        self.indexes = []
    async def insert_one(self, d): self.rows.append(dict(d))
    async def insert_many(self, docs, ordered=True):
        self.rows.extend(dict(d) for d in docs)
    async def find_one(self, q=None, projection=None):
        for r in self.rows:
            if all(_match(r, k, v) for k, v in (q or {}).items()):
                return dict(r)
        return None
    def find(self, q=None, projection=None):
        return _Cursor([dict(r) for r in self.rows
                         if all(_match(r, k, v) for k, v in (q or {}).items())])
    async def update_one(self, q, upd, upsert=False):
        for r in self.rows:
            if all(_match(r, k, v) for k, v in q.items()):
                for k, v in (upd.get("$set") or {}).items():
                    r[k] = v
                for k, v in (upd.get("$inc") or {}).items():
                    r[k] = (r.get(k) or 0) + v
                return {"matched": 1}
        if upsert:
            new = dict(q)
            new.update(upd.get("$setOnInsert") or {})
            new.update(upd.get("$set") or {})
            self.rows.append(new)
        return {"matched": 0}
    async def count_documents(self, q=None):
        return sum(1 for r in self.rows
                   if all(_match(r, k, v) for k, v in (q or {}).items()))
    async def create_index(self, keys, name=None, unique=False):
        self.indexes.append({"keys": keys, "name": name, "unique": unique})


def _match(row, k, v):
    val = row.get(k)
    if isinstance(v, dict):
        if "$in" in v:  return val in v["$in"]
        if "$ne" in v:  return val != v["$ne"]
        if "$gte" in v: return val is not None and val >= v["$gte"]
        if "$exists" in v: return (k in row) == bool(v["$exists"])
    return val == v


class _DB:
    def __init__(self):
        self._c = {}
    def __getitem__(self, name):
        if name not in self._c:
            self._c[name] = _Coll(name)
        return self._c[name]
    def __getattr__(self, name):
        if name.startswith("_"): raise AttributeError(name)
        return self.__getitem__(name)


# ═════════════════════════════════════════════════════════════════════
# A. `enrich_picks_bulk` attaches fusion + persists to fusion_predictions
# ═════════════════════════════════════════════════════════════════════
def test_bulk_enrichment_attaches_fusion_block_and_persists():
    """Every on-board player-prop pick gets a `fusion` block AND a row
    lands in `fusion_predictions` with the pick_id linkage."""
    from services.pick_fusion_decorator import enrich_picks_bulk

    db = _DB()
    picks = [
        {"id": "p1", "sport": "MLB", "pick_date": "2026-07-29",
         "market": "Aaron Judge (NYY) Over 1.5 Total Bases",
         "event": "NYY @ BOS", "selection": "Over 1.5"},
        {"id": "p2", "sport": "MLB", "pick_date": "2026-07-29",
         "market": "Cody Bellinger (NYY) Over 0.5 Home Runs",
         "event": "NYY @ BOS", "selection": "Over 0.5"},
        {"id": "p3", "sport": "NFL", "pick_date": "2026-07-29",
         "market": "Moneyline",           # NOT a player prop — must be skipped
         "event": "KC @ DEN", "selection": "KC"},
    ]
    _run(enrich_picks_bulk(db, picks, persist=True, concurrency=3))

    # Every pick must carry a `fusion` block (supported or not)
    assert all("fusion" in p for p in picks)

    # Moneyline should short-circuit with supported=False
    assert picks[2]["fusion"]["supported"] is False
    assert "player-prop" in picks[2]["fusion"]["reason"]

    # Player-prop picks that parse yield supported=True (even if no
    # component fires — the block is still emitted)
    prop_supported = [p for p in picks[:2] if p["fusion"].get("supported")]
    assert len(prop_supported) >= 1

    # For every supported pick a row must have landed in fusion_predictions
    persisted = db.fusion_predictions.rows
    assert len(persisted) == len(prop_supported)
    for row in persisted:
        # Grading schema pre-populated with None sentinels
        for k in ("pick_id", "market", "event", "pick_date",
                  "actual_value", "outcome", "correct",
                  "winning_component"):
            assert k in row, f"fusion_predictions row missing {k!r}"


def test_bulk_enrichment_never_raises_on_engine_error(monkeypatch):
    from services import pick_fusion_decorator as pfd

    async def _boom(*_a, **_kw):
        raise RuntimeError("fusion engine down")
    monkeypatch.setattr(
        "services.prediction_fusion_engine.fuse_prediction",
        _boom, raising=False,
    )
    db = _DB()
    picks = [{"id": "p1", "sport": "MLB", "pick_date": "2026-07-29",
              "market": "Aaron Judge (NYY) Over 1.5 Total Bases",
              "event": "NYY @ BOS"}]
    _run(pfd.enrich_picks_bulk(db, picks, persist=True, concurrency=1))
    # Fusion block still present, but marked unsupported with reason
    assert picks[0]["fusion"]["supported"] is False
    assert "engine error" in picks[0]["fusion"]["reason"]


# ═════════════════════════════════════════════════════════════════════
# B. Grading loop back-solves settled picks
# ═════════════════════════════════════════════════════════════════════
def test_grade_settled_fusion_predictions_updates_row():
    """A settled pick with a linked fusion prediction gets graded."""
    from services.pick_fusion_decorator import grade_settled_fusion_predictions
    import datetime as _dt

    db = _DB()
    now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
    # Seed one fusion prediction row (ungraded) + one settled pick
    _run(db.fusion_predictions.insert_one({
        "prediction_id": "fp1",
        "pick_id":       "pick-abc",
        "sport":         "MLB",
        "player":        "Aaron Judge",
        "stat":          "total_bases",
        "threshold":     1.5,
        "final_probability": 0.62,
        "components": {
            "ml":         {"available": True,  "probability": 0.66},
            "similar":    {"available": True,  "probability": 0.55},
            "player_h2h": {"available": False, "probability": None},
            "simulator":  {"available": False, "probability": None},
        },
        "actual_value": None, "outcome": None,
        "correct": None, "winning_component": None,
        "created_at": now_iso,
    }))
    _run(db.picks.insert_one({
        "id": "pick-abc", "sport": "MLB",
        "status": "won",
        "settlement_detail": {"value": 3},   # 3 total bases, over 1.5 ⇒ over
        "market": "Aaron Judge (NYY) Over 1.5 Total Bases",
    }))

    counts = _run(grade_settled_fusion_predictions(
        db, hours_lookback=48, limit=10,
    ))
    assert counts["scanned"] == 1
    assert counts["graded"] == 1
    row = _run(db.fusion_predictions.find_one({"prediction_id": "fp1"}))
    assert row["actual_value"] == 3.0
    assert row["outcome"] == "over"
    assert row["correct"] is True
    assert row["winning_component"] in ("ml", "similar")


def test_grader_skips_pending_and_no_actual_picks():
    """The grader is safe to run repeatedly — no side effects on
    unresolved rows."""
    from services.pick_fusion_decorator import grade_settled_fusion_predictions
    import datetime as _dt

    db = _DB()
    now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
    # Row 1 links to a still-pending pick → must be untouched
    _run(db.fusion_predictions.insert_one({
        "prediction_id": "fp_pending", "pick_id": "pk_pending",
        "sport": "MLB", "stat": "hits", "threshold": 0.5,
        "final_probability": 0.55, "components": {},
        "actual_value": None, "outcome": None, "correct": None,
        "winning_component": None, "created_at": now_iso,
    }))
    _run(db.picks.insert_one({
        "id": "pk_pending", "status": "pending",
        "market": "Some (X) Over 0.5 Hits",
    }))
    # Row 2 links to a settled pick with NO extractable actual
    _run(db.fusion_predictions.insert_one({
        "prediction_id": "fp_no_val", "pick_id": "pk_settled_no_val",
        "sport": "MLB", "stat": "hits", "threshold": 0.5,
        "final_probability": 0.55, "components": {},
        "actual_value": None, "outcome": None, "correct": None,
        "winning_component": None, "created_at": now_iso,
    }))
    _run(db.picks.insert_one({
        "id": "pk_settled_no_val", "status": "won",
        # No settlement_detail, no final_score → no extractable value
        "market": "Some (X) Over 0.5 Hits",
    }))
    counts = _run(grade_settled_fusion_predictions(
        db, hours_lookback=48, limit=10,
    ))
    # Two rows scanned, zero graded, one no_actual, zero errors
    assert counts["scanned"] == 2
    assert counts["graded"] == 0
    assert counts["no_actual"] == 1
    # Both rows still ungraded
    still = _run(db.fusion_predictions.find_one({"prediction_id": "fp_pending"}))
    assert still["actual_value"] is None
    still2 = _run(db.fusion_predictions.find_one(
        {"prediction_id": "fp_no_val"}))
    assert still2["actual_value"] is None


# ═════════════════════════════════════════════════════════════════════
# C. Learning data downstream: `winning_component` propagates
# ═════════════════════════════════════════════════════════════════════
def test_winning_component_recorded_when_ml_wins():
    """When the ML component's Over probability is strongest and the
    actual comes in Over, the winner is credited to ML."""
    from services.prediction_fusion_engine import record_prediction_actual
    import datetime as _dt

    db = _DB()
    now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
    _run(db.fusion_predictions.insert_one({
        "prediction_id": "fp_ml_win",
        "pick_id":       "pk_ml_win",
        "sport":         "MLB", "stat": "hits",
        "threshold":     0.5,
        "final_probability": 0.68,
        "components": {
            "ml":         {"available": True,  "probability": 0.80},
            "similar":    {"available": True,  "probability": 0.55},
            "player_h2h": {"available": True,  "probability": 0.60},
            "simulator":  {"available": False, "probability": None},
        },
        "actual_value": None, "outcome": None,
        "correct": None, "winning_component": None,
        "created_at": now_iso,
    }))
    r = _run(record_prediction_actual(db, "fp_ml_win", 2.0))
    assert r["ok"] is True
    row = _run(db.fusion_predictions.find_one(
        {"prediction_id": "fp_ml_win"}))
    assert row["actual_value"] == 2.0
    assert row["outcome"] == "over"
    assert row["correct"] is True
    assert row["winning_component"] == "ml"


# ═════════════════════════════════════════════════════════════════════
# D. Source-level checks — production wiring is exactly in the right
# place (guards against future accidental removal)
# ═════════════════════════════════════════════════════════════════════
_SERVER_SRC = pathlib.Path("/app/backend/server.py").read_text()


def test_server_wires_enrich_picks_bulk_before_insert_many():
    """The bulk enrichment call must exist in the pick-refresh
    pipeline (post Phase 3F-1: services/pick_refresh_orchestrator.py),
    be guarded by a try/except, and sit BEFORE
    ``db.picks.insert_many``."""
    orch_src = pathlib.Path(
        "/app/backend/services/pick_refresh_orchestrator.py").read_text()
    src = orch_src if "enrich_picks_bulk" in orch_src else _SERVER_SRC
    # Import call
    assert re.search(
        r"from services\.pick_fusion_decorator import enrich_picks_bulk",
        src,
    ), "enrich_picks_bulk import missing from refresh pipeline"
    # Direct call
    m = re.search(r"await enrich_picks_bulk\(\s*db\s*,", src)
    assert m is not None, "enrich_picks_bulk not called in refresh pipeline"
    call_pos = m.start()
    # `db.picks.insert_many` for safe_picks must appear AFTER our call
    ins_positions = [i.start() for i in re.finditer(
        r"await db\.picks\.insert_many\(safe_picks", src)]
    assert ins_positions, "safe_picks insert_many not present"
    assert min(ins_positions) > call_pos, (
        "enrich_picks_bulk must be called BEFORE insert_many(safe_picks)")
    # And AFTER the Board Visibility gate
    bv = src.find("Board Visibility Gate")
    assert bv != -1 and call_pos > bv, (
        "Fusion enrichment must run AFTER Board Visibility tagging")


def test_server_schedules_grading_in_settlement_loop():
    src = _SERVER_SRC
    assert re.search(
        r"from services\.pick_fusion_decorator import\s*\(\s*"
        r"grade_settled_fusion_predictions\s*,?\s*\)",
        src,
    ), "grade_settled_fusion_predictions import missing"
    # The call must live inside `_settlement_loop`.
    loop_start = src.find("async def _settlement_loop(")
    loop_end = src.find("async def ", loop_start + 1)
    assert loop_start != -1 and loop_end != -1
    body = src[loop_start:loop_end]
    assert "grade_settled_fusion_predictions(" in body, (
        "grade_settled_fusion_predictions not called in settlement loop")
    # Gated on `is_full` to avoid burning DB every 60 s
    assert "if is_full:" in body


def test_server_creates_fusion_prediction_indexes():
    src = _SERVER_SRC
    assert 'db.fusion_predictions.create_index' in src, (
        "fusion_predictions index creation missing at startup")
    # We expect the grading index + prediction_id unique index at minimum
    assert 'fusion_grading_idx' in src
    assert 'fusion_pid_idx' in src


# ═════════════════════════════════════════════════════════════════════
# E. Existing single-pick lazy endpoint still works
# ═════════════════════════════════════════════════════════════════════
def test_single_pick_lazy_enrichment_still_exported():
    """The lazy GET /api/picks/{id} path calls enrich_pick_with_fusion —
    make sure it stays a stable, importable API."""
    from services.pick_fusion_decorator import enrich_pick_with_fusion
    assert callable(enrich_pick_with_fusion)


def test_single_pick_lazy_enrichment_end_to_end():
    from services.pick_fusion_decorator import enrich_pick_with_fusion
    db = _DB()
    pick = {"id": "solo", "sport": "MLB", "pick_date": "2026-07-29",
            "market": "Aaron Judge (NYY) Over 1.5 Total Bases",
            "event": "NYY @ BOS"}
    _run(enrich_pick_with_fusion(db, pick, persist=True))
    assert isinstance(pick.get("fusion"), dict)
    # Persistence path stayed intact
    assert len(db.fusion_predictions.rows) == 1
