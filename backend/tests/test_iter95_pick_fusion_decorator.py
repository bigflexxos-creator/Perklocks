"""Integration tests for Phase-1 fusion wire-up (2026-07-28).

Covers:
  A. Fusion decorator handles all pick shapes:
     • player-prop pick (MLB / NFL / Tennis) → supported=True with
       full fusion block.
     • team/moneyline pick → supported=False with reason.
  B. Non-supported markets are skipped (never raise, never persist).
  C. "Why This Pick" payload has all required fields.
  D. Bulk enrichment runs concurrently with graceful failure.
  E. Actual-value extraction handles both settlement_detail and
     final_score payload shapes.
  F. Post-settlement grading job walks the queue, grades what it can,
     and never raises on missing data.
  G. Fusion telemetry doc contains every field the spec requires:
     prediction_id, sport, market, final_probability, individual
     engine probabilities, agreement, confidence, timestamp, pick_id.
  H. Never crashes existing pick pipeline on engine error.
"""
from __future__ import annotations

import asyncio
import pytest


# ─────────────────────────────────────────────────────────────────────
# Stubs
# ─────────────────────────────────────────────────────────────────────
class _AsyncColl:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.inserted: list[dict] = []
        self.updates: list[tuple] = []
    async def insert_one(self, doc):
        self.rows.append(dict(doc))
        self.inserted.append(dict(doc))
    async def find_one(self, q, *_a, **_kw):
        for r in self.rows:
            if all(r.get(k) == v for k, v in q.items()
                    if not isinstance(v, dict)):
                return dict(r)
        return None
    async def update_one(self, q, upd):
        for r in self.rows:
            if all(r.get(k) == v for k, v in q.items()
                    if not isinstance(v, dict)):
                r.update(upd.get("$set", {}))
                self.updates.append((q, upd))
                return
    def find(self, q, *_a, **_kw):
        rows = []
        for r in self.rows:
            ok = True
            for k, v in q.items():
                val = r.get(k)
                if isinstance(v, dict):
                    if "$ne" in v and val == v["$ne"]:
                        ok = False; break
                    if "$in" in v and val not in v["$in"]:
                        ok = False; break
                    if "$gte" in v and (val is None or val < v["$gte"]):
                        ok = False; break
                elif val != v:
                    ok = False; break
            if ok:
                rows.append(dict(r))
        return _AsyncCursor(rows)


class _AsyncCursor:
    def __init__(self, rows): self.rows = list(rows); self._i = 0
    def limit(self, *_a, **_kw): return self
    def sort(self, *_a, **_kw): return self
    def __aiter__(self):
        self._i = 0; return self
    async def __anext__(self):
        if self._i >= len(self.rows):
            raise StopAsyncIteration
        v = self.rows[self._i]; self._i += 1
        return v


class _StubDB:
    def __init__(self):
        self.fusion_predictions = _AsyncColl()
        self.picks              = _AsyncColl()
    def __getattr__(self, name):
        # Any other collection → empty async coll (safe default).
        empty = _AsyncColl()
        setattr(self, name, empty)
        return empty


def _run(coro):
    return asyncio.run(coro)


# ═════════════════════════════════════════════════════════════════════
# A. Pick parsing
# ═════════════════════════════════════════════════════════════════════
def test_parse_moneyline_returns_none():
    from services.pick_fusion_decorator import _parse_pick
    p = {"sport": "MLB", "market": "Miami Marlins Moneyline",
         "selection": "Miami Marlins", "event": "X @ Y"}
    assert _parse_pick(p) is None


def test_parse_mlb_strikeout_market():
    from services.pick_fusion_decorator import _parse_pick
    p = {
        "sport": "MLB",
        "market": "Zack Wheeler (PHI) Over 6.5 Strikeouts",
        "selection": "Zack Wheeler",
        "event": "Philadelphia Phillies @ Miami Marlins",
    }
    got = _parse_pick(p)
    assert got is not None
    assert got["player"] == "Zack Wheeler"
    assert got["stat"] == "strikeouts"
    assert got["threshold"] == 6.5
    assert got["opponent"] == "Miami Marlins"


def test_parse_nfl_prop_market():
    from services.pick_fusion_decorator import _parse_pick
    p = {"sport": "NFL", "market": "Joe Burrow Over 249.5 Passing Yards",
         "selection": "Joe Burrow",
         "event": "Cincinnati Bengals @ Kansas City Chiefs"}
    got = _parse_pick(p)
    assert got is not None
    assert got["stat"] == "passing_yards"
    assert got["threshold"] == 249.5


def test_parse_unknown_stat_returns_none():
    from services.pick_fusion_decorator import _parse_pick
    p = {"sport": "MLB", "market": "Some Player Over 99 Widgets",
         "selection": "Some Player", "event": "X @ Y"}
    assert _parse_pick(p) is None


# ═════════════════════════════════════════════════════════════════════
# B. Enrichment for supported vs unsupported markets
# ═════════════════════════════════════════════════════════════════════
def test_enrich_moneyline_marks_unsupported():
    from services.pick_fusion_decorator import enrich_pick_with_fusion
    db = _StubDB()
    p = {"id": "m1", "sport": "MLB", "market": "Miami Marlins Moneyline",
         "selection": "Miami Marlins",
         "event": "Arizona Diamondbacks @ Miami Marlins"}
    out = _run(enrich_pick_with_fusion(db, p, persist=True))
    assert out is p
    assert out["fusion"]["supported"] is False
    assert "player-prop" in out["fusion"]["reason"]
    # No telemetry row for unsupported markets.
    assert len(db.fusion_predictions.inserted) == 0


def test_enrich_supported_market_attaches_full_block():
    from services.pick_fusion_decorator import enrich_pick_with_fusion
    db = _StubDB()
    p = {"id": "s1", "sport": "NFL",
         "market": "Joe Burrow Over 249.5 Passing Yards",
         "selection": "Joe Burrow",
         "event": "Cincinnati Bengals @ Kansas City Chiefs",
         "pick_date": "2026-07-28", "league": "NFL"}
    out = _run(enrich_pick_with_fusion(db, p, persist=True))
    # `fusion` must be present with a stable schema even if the sub-
    # engines can't hit the (empty) stub DB.
    assert out["fusion"]["supported"] is True
    assert "prediction_id" in out["fusion"]
    assert "final_probability" in out["fusion"]
    assert "why_this_pick" in out["fusion"]
    assert "components" in out["fusion"]
    assert "weights_used" in out["fusion"]


def test_enrich_never_raises_on_broken_pick():
    from services.pick_fusion_decorator import enrich_pick_with_fusion
    out = _run(enrich_pick_with_fusion(_StubDB(), {}))
    assert "fusion" in out


# ═════════════════════════════════════════════════════════════════════
# C. "Why This Pick" schema
# ═════════════════════════════════════════════════════════════════════
def test_why_this_pick_schema_is_stable():
    from services.pick_fusion_decorator import _build_why_this_pick
    # Simulate a fully-populated fusion result dict.
    fake = {
        "final_probability": 0.62,
        "confidence": "medium",
        "model_agreement": "moderate_convergence",
        "agreement_score": 0.83,
        "factors_for": ["direct H2H: 6 games, hit rate 67%"],
        "factors_against": [],
        "explanation": "fused 3 signals",
        "components": {
            "ml":         {"available": True, "probability": 0.60,
                            "projected": 260.0, "sample_size": None},
            "similar":    {"available": True, "probability": 0.58,
                            "projected": 265.0, "sample_size": 14},
            "player_h2h": {"available": True, "probability": 0.67,
                            "projected": 275.0, "sample_size": 6},
            "simulator":  {"available": False, "probability": None},
        },
    }
    wtp = _build_why_this_pick(fake)
    for k in ("final_probability", "confidence_level",
              "agreement_label", "agreement_score",
              "engines_agreed", "engines_disagreed",
              "matchup_summary", "similar_matchup_summary",
              "monte_carlo_summary", "trained_model_summary",
              "top_factors", "counter_factors",
              "sample_sizes", "explanation"):
        assert k in wtp, f"missing WTP key: {k}"
    # All three signals agree on OVER — engines_agreed has 3 members.
    assert set(wtp["engines_agreed"]) == {"ml", "similar", "player_h2h"}
    assert wtp["engines_disagreed"] == []
    # Sample-size dict lists the two engines that supply it.
    assert wtp["sample_sizes"]["similar"] == 14
    assert wtp["sample_sizes"]["player_h2h"] == 6


def test_why_this_pick_flags_disagreement():
    from services.pick_fusion_decorator import _build_why_this_pick
    fake = {
        "final_probability": 0.52,      # slight over lean
        "confidence": "low",
        "model_agreement": "disagreement",
        "components": {
            "ml":         {"available": True, "probability": 0.72},
            "similar":    {"available": True, "probability": 0.34},
            "player_h2h": {"available": False, "probability": None},
            "simulator":  {"available": False, "probability": None},
        },
    }
    wtp = _build_why_this_pick(fake)
    assert "ml" in wtp["engines_agreed"]         # 0.72 aligns with over lean
    assert "similar" in wtp["engines_disagreed"] # 0.34 disagrees


# ═════════════════════════════════════════════════════════════════════
# D. Bulk enrichment
# ═════════════════════════════════════════════════════════════════════
def test_bulk_enrichment_handles_mixed_batch():
    from services.pick_fusion_decorator import enrich_picks_bulk
    db = _StubDB()
    picks = [
        {"id": "a", "sport": "MLB", "market": "Miami Marlins Moneyline",
         "selection": "Miami Marlins", "event": "X @ Y"},
        {"id": "b", "sport": "NFL", "market": "Joe Burrow Over 249.5 Passing Yards",
         "selection": "Joe Burrow", "event": "Cincinnati Bengals @ KC"},
        {"id": "c"},                       # broken pick
    ]
    out = _run(enrich_picks_bulk(db, picks, persist=True, concurrency=3))
    assert len(out) == 3
    assert out[0]["fusion"]["supported"] is False
    assert out[1]["fusion"]["supported"] is True
    assert "fusion" in out[2]


# ═════════════════════════════════════════════════════════════════════
# E. Actual value extraction
# ═════════════════════════════════════════════════════════════════════
def test_extract_actual_from_settlement_detail():
    from services.pick_fusion_decorator import _extract_actual_from_pick
    p = {"settlement_detail": {"player": "Aaron Nola", "stat": "strikeOuts",
                                "value": 8.0, "line": 4.5}}
    assert _extract_actual_from_pick(p) == 8.0


def test_extract_actual_from_final_score():
    from services.pick_fusion_decorator import _extract_actual_from_pick
    p = {"final_score": {"Aaron Nola Strikeouts": 7.0, "Line": 4.5}}
    assert _extract_actual_from_pick(p) == 7.0


def test_extract_actual_returns_none_when_missing():
    from services.pick_fusion_decorator import _extract_actual_from_pick
    assert _extract_actual_from_pick({}) is None
    assert _extract_actual_from_pick({"final_score": {"Line": 4.5}}) is None


# ═════════════════════════════════════════════════════════════════════
# F. Grading job
# ═════════════════════════════════════════════════════════════════════
def test_grading_job_records_actuals_for_settled_picks():
    from datetime import datetime, timezone
    from services.pick_fusion_decorator import grade_settled_fusion_predictions
    db = _StubDB()
    # A fusion prediction linked to a settled pick.
    now = datetime.now(timezone.utc).isoformat()
    db.fusion_predictions.rows.append({
        "prediction_id": "pred-1",
        "pick_id": "pick-1",
        "threshold": 249.5,
        "final_probability": 0.70,
        "actual_value": None,
        "created_at": now,
        "components": {
            "ml": {"probability": 0.70},
            "similar": {"probability": 0.66},
        },
    })
    db.picks.rows.append({
        "id": "pick-1", "status": "won",
        "settlement_detail": {"value": 300.0},
    })
    counts = _run(grade_settled_fusion_predictions(db, hours_lookback=48))
    assert counts["scanned"] == 1
    assert counts["graded"] == 1


def test_grading_job_skips_unsettled_and_missing_picks():
    from datetime import datetime, timezone
    from services.pick_fusion_decorator import grade_settled_fusion_predictions
    db = _StubDB()
    now = datetime.now(timezone.utc).isoformat()
    db.fusion_predictions.rows.append({
        "prediction_id": "pred-2", "pick_id": "not-yet-settled",
        "threshold": 100.0, "actual_value": None,
        "final_probability": 0.5,
        "created_at": now, "components": {},
    })
    db.fusion_predictions.rows.append({
        "prediction_id": "pred-3", "pick_id": None,
        "threshold": 100.0, "actual_value": None,
        "final_probability": 0.5,
        "created_at": now, "components": {},
    })
    counts = _run(grade_settled_fusion_predictions(db))
    # `pred-3` has no pick_id → filtered by the query itself (pick_id != None)
    # so only 1 row is scanned; 0 graded.
    assert counts["graded"] == 0


def test_grading_job_never_raises_on_empty_queue():
    from services.pick_fusion_decorator import grade_settled_fusion_predictions
    counts = _run(grade_settled_fusion_predictions(_StubDB()))
    assert counts["graded"] == 0


# ═════════════════════════════════════════════════════════════════════
# G. Telemetry schema completeness
# ═════════════════════════════════════════════════════════════════════
def test_telemetry_doc_includes_all_required_fields():
    """When persist=True, the persisted doc must include every field
    listed in the Phase-1 spec: prediction_id, sport, market,
    final_probability, per-engine probs, agreement, confidence,
    winning_engine placeholder, timestamp, pick_id."""
    from services.pick_fusion_decorator import enrich_pick_with_fusion
    db = _StubDB()
    p = {"id": "px1", "sport": "NFL",
         "market": "Joe Burrow Over 249.5 Passing Yards",
         "selection": "Joe Burrow",
         "event": "CIN @ KC", "pick_date": "2026-07-28", "league": "NFL"}
    _run(enrich_pick_with_fusion(db, p, persist=True))
    assert len(db.fusion_predictions.inserted) == 1
    doc = db.fusion_predictions.inserted[0]
    for k in ("prediction_id", "sport", "market", "pick_id",
              "final_probability", "confidence", "model_agreement",
              "components", "weights_used", "created_at",
              "actual_value", "correct", "winning_component"):
        assert k in doc, f"missing telemetry field: {k}"
    assert doc["pick_id"] == "px1"
    assert doc["market"] == "Joe Burrow Over 249.5 Passing Yards"
    # Winning_component is None until graded.
    assert doc["winning_component"] is None


# ═════════════════════════════════════════════════════════════════════
# H. Never crashes the caller
# ═════════════════════════════════════════════════════════════════════
def test_enrichment_survives_engine_exceptions(monkeypatch):
    """Force the fusion engine to raise — decorator must still return
    a well-formed fusion block with supported=False."""
    import services.pick_fusion_decorator as pfd
    from services.prediction_fusion_engine import FusionResult
    async def _boom(*a, **kw):
        raise RuntimeError("simulated engine failure")
    monkeypatch.setattr("services.prediction_fusion_engine.fuse_prediction",
                         _boom)
    db = _StubDB()
    p = {"id": "e1", "sport": "NFL",
         "market": "Joe Burrow Over 249.5 Passing Yards",
         "selection": "Joe Burrow", "event": "CIN @ KC"}
    out = _run(pfd.enrich_pick_with_fusion(db, p))
    assert out["fusion"]["supported"] is False
    assert "engine error" in out["fusion"]["reason"]
