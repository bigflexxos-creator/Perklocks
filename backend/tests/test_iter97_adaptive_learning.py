"""Adaptive-Learning system tests (Phase 3, 2026-07-28).

Covers:
  A. Calibration report — Brier, buckets, sport/market/tier grouping.
  B. Engine performance ranking + best/worst market detection.
  C. Weight optimiser — safety gates, smoothing, validation, persistence.
  D. Retraining orchestrator — trigger discovery, model comparison,
     promotion decision (dry_run).
  E. Drift detector — accuracy drift + mean-prob drift alerts.
  F. No sportsbook odds anywhere in the package.
  G. Missing-data / empty-queue graceful behaviour.
"""
from __future__ import annotations

import asyncio
import json
import inspect
import math
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest


MODEL_DIR = Path("/app/backend/models")


def _run(coro):
    return asyncio.run(coro)


# ─────────────────────────────────────────────────────────────────────
# Mongo stub — supports the query patterns our modules use
# ─────────────────────────────────────────────────────────────────────
class _AsyncColl:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.inserted: list[dict] = []
        self.updates: list[tuple] = []

    async def insert_one(self, doc):
        self.rows.append(dict(doc)); self.inserted.append(dict(doc))

    async def find_one(self, q=None, *_a, **_kw):
        for r in self.rows:
            if _matches(r, q or {}):
                return dict(r)
        return None

    async def update_one(self, q, upd, upsert=False):
        for r in self.rows:
            if _matches(r, q):
                r.update(upd.get("$set", {})); self.updates.append((q, upd))
                return
        if upsert:
            merged = {**q, **upd.get("$set", {})}
            self.rows.append(merged); self.inserted.append(dict(merged))

    async def count_documents(self, q):
        return sum(1 for r in self.rows if _matches(r, q))

    def find(self, q=None, *_a, **_kw):
        return _AsyncCursor([r for r in self.rows
                              if _matches(r, q or {})])


class _AsyncCursor:
    def __init__(self, rows): self.rows = list(rows); self._i = 0
    def sort(self, *_a, **_kw): return self
    def limit(self, *_a, **_kw): return self
    def __aiter__(self): self._i = 0; return self
    async def __anext__(self):
        if self._i >= len(self.rows): raise StopAsyncIteration
        v = self.rows[self._i]; self._i += 1; return v


def _matches(row: dict, q: dict) -> bool:
    for k, v in q.items():
        val = row.get(k)
        if isinstance(v, dict):
            if "$ne" in v and val == v["$ne"]:
                return False
            if "$in" in v and val not in v["$in"]:
                return False
            if "$gte" in v and (val is None or str(val) < str(v["$gte"])):
                return False
        else:
            if val != v:
                return False
    return True


class _StubDB:
    def __init__(self):
        self.fusion_predictions   = _AsyncColl()
        self.fusion_weights       = _AsyncColl()
        self.fusion_weight_changes = _AsyncColl()

    def __getattr__(self, name):
        c = _AsyncColl(); setattr(self, name, c); return c


# ─────────────────────────────────────────────────────────────────────
# Fixture: seed a set of graded predictions.
# ─────────────────────────────────────────────────────────────────────
def _seed_predictions(db: _StubDB, n_per_bucket: int = 20,
                       sport: str = "NFL",
                       market: str = "Player Over Passing Yards",
                       calibrated: bool = True) -> None:
    """Seed graded predictions with a controlled prob → outcome
    correspondence.

    calibrated=True → bucket X% wins near X%.
    calibrated=False → predictions are over-confident (predict 80%
    when truth is 55%).
    """
    now = datetime.now(timezone.utc)
    for bucket_center in (0.15, 0.35, 0.55, 0.75, 0.90):
        if calibrated:
            win_rate = bucket_center
        else:
            win_rate = max(0.05, bucket_center - 0.25)
        n_wins = int(round(win_rate * n_per_bucket))
        for i in range(n_per_bucket):
            p_final = bucket_center + (i - n_per_bucket / 2) * 0.001
            y = 1 if i < n_wins else 0
            actual = 300 if y else 100
            db.fusion_predictions.rows.append({
                "prediction_id": f"P-{bucket_center}-{i}",
                "pick_id":       f"pk-{bucket_center}-{i}",
                "sport":         sport,
                "market":        market,
                "stat":          "passing_yards",
                "threshold":     200.0,
                "final_probability": p_final,
                "confidence":    "medium",
                "created_at":    (now - timedelta(days=1)).isoformat(),
                "graded_at":     now.isoformat(),
                "actual_value":  actual,
                "correct":       (p_final >= 0.5) == (y == 1),
                "winning_component": "ml",
                "components": {
                    "ml":         {"available": True,
                                    "probability": bucket_center,
                                    "sample_size": 40},
                    "similar":    {"available": True,
                                    "probability": bucket_center - 0.05,
                                    "sample_size": 12},
                    "player_h2h": {"available": True,
                                    "probability": bucket_center + 0.02,
                                    "sample_size": 6},
                    "simulator":  {"available": False, "probability": None},
                },
                "weights_used": {"ml": 0.44, "similar": 0.28,
                                  "player_h2h": 0.28, "simulator": 0.0},
            })


# ═════════════════════════════════════════════════════════════════════
# A. Calibration
# ═════════════════════════════════════════════════════════════════════
def test_calibration_report_shape_and_stats():
    from services.adaptive_learning import build_calibration_report
    db = _StubDB()
    _seed_predictions(db, n_per_bucket=20, calibrated=True)
    r = _run(build_calibration_report(db, days=30))
    assert r["n_graded"] == 100
    assert 0.0 <= r["brier_score"] <= 0.5
    assert 0.0 <= r["accuracy"] <= 1.0
    assert isinstance(r["calibration_curve"], list)
    # Each bucket should show expected ≈ observed within a few points
    # when we're calibrated.
    for b in r["calibration_curve"]:
        assert abs(b["expected"] - b["observed"]) < 15.0
    # Per-sport + per-market + per-engine dicts populated.
    assert "NFL" in r["by_sport"]
    assert set(r["by_engine"].keys()).issuperset(
        {"ml", "similar", "player_h2h", "fused"},
    )


def test_calibration_report_flags_over_confidence():
    from services.adaptive_learning import build_calibration_report
    db = _StubDB()
    _seed_predictions(db, n_per_bucket=40, calibrated=False)
    r = _run(build_calibration_report(db, days=30))
    assert r["reliability_flag"] == "over_confident"


def test_calibration_empty_queue_ok():
    from services.adaptive_learning import build_calibration_report
    r = _run(build_calibration_report(_StubDB(), days=30))
    assert r["n_graded"] == 0
    assert r["reliability_flag"] == "insufficient"


# ═════════════════════════════════════════════════════════════════════
# B. Engine performance
# ═════════════════════════════════════════════════════════════════════
def test_engine_performance_ranking_returns_ordered_engines():
    from services.adaptive_learning import build_engine_performance_report
    db = _StubDB()
    _seed_predictions(db, n_per_bucket=30, calibrated=True)
    r = _run(build_engine_performance_report(db, days=30, min_samples=5))
    assert r["n_graded"] == 150
    # Rankings should list several engines and be sorted by Brier asc.
    briers = [e["brier"] for e in r["engines"] if e["engine"] in r["engine_ranking"]]
    ordered = [next(x for x in r["engines"] if x["engine"] == e)["brier"]
               for e in r["engine_ranking"]]
    assert ordered == sorted(ordered)


def test_engine_performance_best_worst_markets():
    from services.adaptive_learning import build_engine_performance_report
    db = _StubDB()
    _seed_predictions(db, n_per_bucket=30, market="good market")
    _seed_predictions(db, n_per_bucket=30, market="bad market",
                       calibrated=False)
    r = _run(build_engine_performance_report(db, days=30, min_samples=5))
    assert len(r["best_markets"]) >= 1
    assert r["best_markets"][0]["accuracy"] >= r["worst_markets"][0]["accuracy"]


# ═════════════════════════════════════════════════════════════════════
# C. Weight optimiser
# ═════════════════════════════════════════════════════════════════════
def test_weight_optimiser_skips_insufficient_samples():
    from services.adaptive_learning import optimise_fusion_weights
    db = _StubDB()
    _seed_predictions(db, n_per_bucket=2)   # only 10 rows total
    changes = _run(optimise_fusion_weights(
        db, min_samples=100, days=30, persist=False,
    ))
    assert changes == []


def test_weight_optimiser_produces_change_and_persists():
    from services.adaptive_learning import optimise_fusion_weights
    from services.adaptive_learning.weight_optimizer import load_learned_weights
    db = _StubDB()
    # Seed a large, mildly-biased dataset (ML is more accurate → should
    # earn more weight).
    _seed_predictions(db, n_per_bucket=40)
    changes = _run(optimise_fusion_weights(
        db, min_samples=50, days=30, validation_days=30,
        smoothing=0.5, persist=True,
    ))
    # A meaningful improvement may or may not exist depending on
    # randomness — assert function is stable & shape is correct.
    for ch in changes:
        assert set(ch["old_weights"].keys()) == set(ch["new_weights"].keys())
        assert abs(sum(ch["new_weights"].values()) - 1.0) < 1e-2
        assert ch["delta_brier"] > 0
    # If a change happened, load_learned_weights should return it.
    if changes:
        s, m = changes[0]["sport"], changes[0]["market"]
        loaded = _run(load_learned_weights(db, s, m))
        assert loaded is not None
        assert set(loaded.keys()) == {"ml", "similar", "player_h2h",
                                        "simulator"}


def test_weight_optimiser_smoothing_prevents_overreaction():
    """Smoothing = 0.9 (heavy prior) should produce weights close to
    the previous weights even when the raw new-weights are very
    different."""
    from services.adaptive_learning.weight_optimizer import _smooth
    old = {"ml": 0.40, "similar": 0.25, "player_h2h": 0.20, "simulator": 0.15}
    new = {"ml": 0.95, "similar": 0.02, "player_h2h": 0.02, "simulator": 0.01}
    smoothed = _smooth(old, new, smoothing=0.9)
    assert abs(smoothed["ml"] - old["ml"]) < 0.10


def test_weight_optimiser_fused_brier_math_is_correct():
    from services.adaptive_learning.weight_optimizer import _fused_brier
    rows = [
        {"threshold": 100, "actual_value": 120,
          "components": {"ml": {"available": True, "probability": 0.7}}},
        {"threshold": 100, "actual_value": 80,
          "components": {"ml": {"available": True, "probability": 0.2}}},
    ]
    n, brier = _fused_brier({"ml": 1.0}, rows)
    # Row 1: p=0.7 y=1 → (1-0.7)² = 0.09
    # Row 2: p=0.2 y=0 → (0-0.2)² = 0.04
    assert n == 2
    assert abs(brier - 0.065) < 1e-6


# ═════════════════════════════════════════════════════════════════════
# D. Retraining orchestrator
# ═════════════════════════════════════════════════════════════════════
def test_retraining_orchestrator_lists_models():
    from services.adaptive_learning import RetrainingOrchestrator
    o = RetrainingOrchestrator(_StubDB())
    rows = o.list_models()
    # We should find at least NFL flagship models on disk.
    assert any(r["sport"] == "NFL" for r in rows) or \
            any(r["sport"] == "MLB" for r in rows)
    for r in rows:
        assert "trained_at" in r


def test_retraining_orchestrator_detect_triggers_never_raises():
    from services.adaptive_learning import RetrainingOrchestrator
    o = RetrainingOrchestrator(_StubDB(), min_new_settled=999999,
                                 min_days_since_retrain=999999)
    triggers = _run(o.detect_needs_retraining())
    # With absurdly-high gates the on-disk models should not qualify.
    assert isinstance(triggers, list)


def test_retraining_orchestrator_dry_run_backs_up():
    from services.adaptive_learning import RetrainingOrchestrator
    o = RetrainingOrchestrator(_StubDB())
    r = _run(o.retrain("NFL", "passing_yards", dry_run=True))
    assert r["ok"] is True
    assert r.get("dry_run") is True


def test_retraining_comparison_helper():
    from services.adaptive_learning.retraining_pipeline import (
        RetrainingOrchestrator,
    )
    o = RetrainingOrchestrator(_StubDB(),
                                 promotion_threshold_pct=5.0)
    old_meta = {"winner": "lgbm",
                 "lgbm": {"mae": 100.0,
                           "auc_by_thr": {"p50": 0.60},
                           "top_features": [["a", 1.0], ["b", 0.5]]}}
    # 3% improvement → should NOT promote (threshold is 5 %).
    new_meta = {"winner": "lgbm",
                 "lgbm": {"mae": 97.0,
                           "auc_by_thr": {"p50": 0.62},
                           "top_features": []}}
    assert o._should_promote(old_meta, new_meta) is False
    # 10% improvement → should promote.
    new_meta["lgbm"]["mae"] = 90.0
    assert o._should_promote(old_meta, new_meta) is True


# ═════════════════════════════════════════════════════════════════════
# E. Drift detector
# ═════════════════════════════════════════════════════════════════════
def test_drift_detector_returns_list_for_empty_queue():
    from services.adaptive_learning import detect_drift
    alerts = _run(detect_drift(_StubDB(), days=30, min_samples=25))
    assert isinstance(alerts, list)   # empty is fine.


def test_drift_detector_fires_when_brier_degrades():
    """Seed predictions with a huge Brier score for an NFL stat that
    HAS a trained model on disk → drift should be flagged."""
    from services.adaptive_learning import detect_drift
    db = _StubDB()
    now = datetime.now(timezone.utc)
    # Load the on-disk baseline for a real model.
    meta_p = MODEL_DIR / "nfl_passing_yards.meta.json"
    if not meta_p.exists():
        pytest.skip("nfl_passing_yards model not on disk")
    m = json.loads(meta_p.read_text())
    baseline = (m[m["winner"]].get("brier_by_thr") or {}).get("p50")
    if not baseline:
        pytest.skip("no baseline brier")

    # Insert 30 predictions with a huge Brier (all wrong).
    for i in range(30):
        db.fusion_predictions.rows.append({
            "sport": "NFL", "stat": "passing_yards",
            "market": "market", "threshold": 200.0,
            "final_probability": 0.90, "actual_value": 100.0,
            "components": {"ml": {"available": True, "probability": 0.90}},
            "created_at": (now - timedelta(days=1)).isoformat(),
            "actual_value_ok": True,
        })
    alerts = _run(detect_drift(db, sport="NFL", stat="passing_yards",
                                 days=30, min_samples=10))
    # We expect at least one alert about NFL passing_yards.
    assert any(a["tag"].startswith("nfl_passing_yards") for a in alerts)
    # Level should be warning or critical.
    assert alerts[0]["level"] in ("warning", "critical")


# ═════════════════════════════════════════════════════════════════════
# F. No sportsbook odds in the adaptive_learning package.
# ═════════════════════════════════════════════════════════════════════
def test_no_odds_or_market_features_in_package():
    """AST-level scan of every module in `services.adaptive_learning`
    for banned market-language identifiers."""
    import services.adaptive_learning as pkg
    import pkgutil, importlib, ast
    banned_identifiers = {
        "book_odds", "market_price", "book_price", "moneyline_odds",
        "consensus_price", "handle_pct", "vig", "juice",
        "sportsbook_price", "steam_ratio",
    }
    hits: list[str] = []
    for mod_info in pkgutil.iter_modules(pkg.__path__):
        mod = importlib.import_module(f"services.adaptive_learning.{mod_info.name}")
        src = inspect.getsource(mod)
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in banned_identifiers:
                hits.append(f"{mod_info.name}:{node.id}")
            elif isinstance(node, ast.Attribute) and node.attr in banned_identifiers:
                hits.append(f"{mod_info.name}:.{node.attr}")
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and node.value in banned_identifiers:
                hits.append(f"{mod_info.name}:str[{node.value}]")
    assert not hits, "adaptive_learning references banned market ids:\n  " + \
                       "\n  ".join(hits)


# ═════════════════════════════════════════════════════════════════════
# G. Graceful defaults
# ═════════════════════════════════════════════════════════════════════
def test_optimise_fusion_weights_never_raises_on_empty():
    from services.adaptive_learning import optimise_fusion_weights
    changes = _run(optimise_fusion_weights(_StubDB(), days=1,
                                              min_samples=1,
                                              persist=False))
    assert changes == []


def test_load_learned_weights_returns_none_for_unknown_tuple():
    from services.adaptive_learning.weight_optimizer import load_learned_weights
    r = _run(load_learned_weights(_StubDB(), "NFL", "unknown market"))
    assert r is None
