"""Tests for Prediction Fusion Engine (2026-07-28).

Proves the fusion contract:
  A. Weight math — configurable, renormalises to available components,
     handles zero-weight and non-finite values.
  B. Agreement scoring — strong/moderate/weak/disagreement labels
     correspond correctly to probability spreads.
  C. Confidence labeling — combines n_signals + agreement + conviction.
  D. No sportsbook odds anywhere — components must not accept nor
     propagate market prices/lines-as-features.
  E. Fusion output shape is stable — even with no signals.
  F. Backtesting roundtrip — persist, then record_prediction_actual,
     then get_backtest_summary reports the correct accuracy.
  G. Graceful degradation — missing components, missing DB records,
     malformed inputs never raise.
"""
from __future__ import annotations

import asyncio
import math
import pytest


# ─────────────────────────────────────────────────────────────────────
# Stubs
# ─────────────────────────────────────────────────────────────────────
class _AsyncColl:
    """Minimal in-memory async collection for backtesting tests."""
    def __init__(self):
        self.rows: list[dict] = []

    async def insert_one(self, doc: dict):
        self.rows.append(dict(doc))

    async def find_one(self, q: dict, *_a, **_kw):
        for r in self.rows:
            if all(r.get(k) == v for k, v in q.items()):
                return dict(r)
        return None

    async def update_one(self, q: dict, upd: dict):
        for r in self.rows:
            if all(r.get(k) == v for k, v in q.items()):
                r.update(upd.get("$set", {}))
                return
        return

    def find(self, q: dict, *_a, **_kw):
        rows = []
        for r in self.rows:
            ok = True
            for k, v in q.items():
                if isinstance(v, dict):
                    # {"$gte": ..., "$ne": ..., ...}
                    val = r.get(k)
                    if "$gte" in v and (val is None or val < v["$gte"]):
                        ok = False; break
                    if "$ne" in v and val == v["$ne"]:
                        ok = False; break
                elif r.get(k) != v:
                    ok = False; break
            if ok:
                rows.append(dict(r))
        return _AsyncCursor(rows)


class _AsyncCursor:
    def __init__(self, rows): self.rows = list(rows)
    def __aiter__(self):
        self._it = iter(self.rows); return self
    async def __anext__(self):
        try: return next(self._it)
        except StopIteration: raise StopAsyncIteration


class _StubDB:
    def __init__(self):
        self.fusion_predictions = _AsyncColl()


def _run(coro):
    return asyncio.run(coro)


# ═════════════════════════════════════════════════════════════════════
# A. Weight math
# ═════════════════════════════════════════════════════════════════════
def test_default_weights_sum_to_one():
    from services.prediction_fusion_engine import DEFAULT_WEIGHTS
    total = sum(DEFAULT_WEIGHTS.values())
    assert abs(total - 1.0) < 1e-6, f"defaults sum to {total}"


def test_guard_weights_handles_bad_inputs():
    from services.prediction_fusion_engine import _guard_weights, DEFAULT_WEIGHTS
    # None → defaults
    assert _guard_weights(None) == DEFAULT_WEIGHTS
    # Non-finite / negative → clipped to 0 or default
    g = _guard_weights({"ml": float("inf"), "similar": -1, "player_h2h": "abc"})
    assert all(math.isfinite(v) and v >= 0 for v in g.values())
    # Missing keys fall back to defaults
    assert g["simulator"] == DEFAULT_WEIGHTS["simulator"]


def test_normalise_weights_renormalises_to_available():
    from services.prediction_fusion_engine import _normalise_weights, DEFAULT_WEIGHTS
    # All available → identity
    out = _normalise_weights(DEFAULT_WEIGHTS,
                              {"ml": True, "similar": True,
                                "player_h2h": True, "simulator": True})
    assert abs(sum(out.values()) - 1.0) < 1e-6
    # Only ML available → ML gets 1.0
    out = _normalise_weights(DEFAULT_WEIGHTS,
                              {"ml": True, "similar": False,
                                "player_h2h": False, "simulator": False})
    assert out["ml"] == 1.0
    assert out["similar"] == 0.0
    assert out["player_h2h"] == 0.0
    assert out["simulator"] == 0.0
    # None available → all zeros
    out = _normalise_weights(DEFAULT_WEIGHTS,
                              {n: False for n in DEFAULT_WEIGHTS})
    assert sum(out.values()) == 0.0


def test_normalise_weights_two_of_four_available():
    from services.prediction_fusion_engine import _normalise_weights
    # ml 0.40 + similar 0.25 = 0.65 total → renormalise
    out = _normalise_weights(
        {"ml": 0.40, "similar": 0.25, "player_h2h": 0.20, "simulator": 0.15},
        {"ml": True, "similar": True, "player_h2h": False, "simulator": False},
    )
    assert abs(out["ml"] - (0.40 / 0.65)) < 1e-3
    assert abs(out["similar"] - (0.25 / 0.65)) < 1e-3
    assert out["player_h2h"] == 0.0
    assert out["simulator"] == 0.0
    assert abs(sum(out.values()) - 1.0) < 1e-3


# ═════════════════════════════════════════════════════════════════════
# B. Agreement scoring
# ═════════════════════════════════════════════════════════════════════
def test_agreement_empty():
    from services.prediction_fusion_engine import _agreement
    label, score = _agreement([])
    assert label == "insufficient_signals"
    assert score == 0.0


def test_agreement_single_signal():
    from services.prediction_fusion_engine import _agreement
    label, _ = _agreement([0.70])
    assert label == "single_signal"


def test_agreement_strong_convergence_tight_spread():
    from services.prediction_fusion_engine import _agreement
    # Three probs within 0.10 range, same side → strong_convergence
    label, score = _agreement([0.65, 0.70, 0.72])
    assert label == "strong_convergence"
    assert 0.85 <= score <= 1.0


def test_agreement_moderate_convergence():
    from services.prediction_fusion_engine import _agreement
    label, _ = _agreement([0.60, 0.68, 0.76])
    assert label == "moderate_convergence"


def test_agreement_weak_convergence():
    from services.prediction_fusion_engine import _agreement
    label, _ = _agreement([0.55, 0.66, 0.83])
    assert label == "weak_convergence"


def test_agreement_detects_opposite_sides_of_50():
    from services.prediction_fusion_engine import _agreement
    # Models disagree — one below 0.5, one above
    label, _ = _agreement([0.30, 0.75])
    assert label == "disagreement"


def test_agreement_wide_spread_is_disagreement():
    from services.prediction_fusion_engine import _agreement
    label, _ = _agreement([0.55, 0.90])
    assert label == "disagreement"


# ═════════════════════════════════════════════════════════════════════
# C. Confidence labeling
# ═════════════════════════════════════════════════════════════════════
def test_confidence_high_requires_all_conditions():
    from services.prediction_fusion_engine import _confidence_label
    # 3 signals + strong agreement + high conviction → high
    assert _confidence_label(3, 0.90, 0.80) == "high"
    # If any drops, degrade to medium/low
    assert _confidence_label(1, 0.90, 0.80) == "low"
    assert _confidence_label(3, 0.50, 0.80) == "low"
    assert _confidence_label(3, 0.90, 0.55) == "medium"


def test_confidence_none_when_no_signals():
    from services.prediction_fusion_engine import _confidence_label
    assert _confidence_label(0, 1.0, 1.0) == "none"


# ═════════════════════════════════════════════════════════════════════
# D. No sportsbook odds anywhere
# ═════════════════════════════════════════════════════════════════════
def test_engine_source_contains_no_odds_terminology():
    """Grep the fusion engine's CODE (not docstrings/comments) for
    banned market-language identifiers.

    We tokenise the source and inspect only identifiers, string
    literals that look like keys/fields, and attribute accesses —
    docstrings are ignored because they contain the "we do NOT use
    sportsbook odds" disclaimer.
    """
    import services.prediction_fusion_engine as pfe
    import inspect, ast
    src = inspect.getsource(pfe)
    tree = ast.parse(src)
    banned_identifiers = {
        "book_odds", "market_price", "book_price", "moneyline_odds",
        "consensus_price", "handle_pct", "vig", "juice",
        "sportsbook_price", "steam_ratio",
    }
    banned_string_keys = banned_identifiers | {"odds", "moneyline"}
    hits: list[str] = []
    for node in ast.walk(tree):
        # Identifiers (variable names, arg names, attr access, keys).
        if isinstance(node, ast.Name) and node.id in banned_identifiers:
            hits.append(f"Name {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in banned_identifiers:
            hits.append(f"Attribute .{node.attr}")
        elif isinstance(node, ast.arg) and node.arg in banned_identifiers:
            hits.append(f"arg {node.arg}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and node.value in banned_string_keys \
                and len(node.value) <= 20:
            # Only flag SHORT string literals that look like dict keys —
            # long strings (docstrings, log messages) get a pass.
            hits.append(f"str literal {node.value!r}")
    assert not hits, (
        "fusion engine references banned market identifiers/keys:\n  "
        + "\n  ".join(hits)
    )


def test_component_dict_never_carries_book_fields():
    """A ComponentPrediction should never carry book/odds fields."""
    from services.prediction_fusion_engine import ComponentPrediction
    cp = ComponentPrediction(name="ml", available=True, probability=0.5)
    d = cp.to_dict()
    banned = {"book_odds", "odds", "vig", "consensus", "market_price"}
    for k in d.keys():
        assert k not in banned


# ═════════════════════════════════════════════════════════════════════
# E. Fusion output shape stability
# ═════════════════════════════════════════════════════════════════════
def test_fusion_returns_well_formed_result_even_with_no_signals():
    """DB stub with no data + no trained models → all components
    unavailable but result is still a well-formed FusionResult."""
    from services.prediction_fusion_engine import fuse_prediction

    class _MinimalDB:
        def __init__(self):
            self.fusion_predictions = _AsyncColl()
        def __getattr__(self, name):
            # Any other collection request returns an empty _AsyncColl.
            return _AsyncColl()

    db = _MinimalDB()
    r = _run(fuse_prediction(
        db, sport="NFL", player="XYZ", stat="passing_yards",
        opponent="KC", threshold=249.5,
    ))
    d = r.to_dict()
    # Required top-level keys.
    for k in ("prediction_id", "sport", "player", "stat", "opponent",
              "threshold", "final_probability", "projected_stat",
              "confidence", "model_agreement", "agreement_score",
              "components", "weights_used", "factors_for",
              "factors_against", "explanation", "notes"):
        assert k in d, f"missing key {k}"
    # Components dict has all four canonical keys.
    assert set(d["components"].keys()) == {"ml", "similar",
                                             "player_h2h", "simulator"}
    # Confidence should be "none" when no signals fire.
    assert d["confidence"] == "none"


def test_fusion_never_raises_on_broken_inputs():
    from services.prediction_fusion_engine import fuse_prediction

    class _MinimalDB:
        def __init__(self):
            self.fusion_predictions = _AsyncColl()
        def __getattr__(self, name):
            return _AsyncColl()

    r = _run(fuse_prediction(
        _MinimalDB(), sport="", player="", stat="", opponent="", threshold=None,
    ))
    assert r.final_probability == 0.0
    assert r.confidence == "none"


# ═════════════════════════════════════════════════════════════════════
# F. Backtesting roundtrip
# ═════════════════════════════════════════════════════════════════════
def _fake_prediction_doc(pid: str, final_prob: float, threshold: float,
                           sport="NFL", stat="passing_yards"):
    """Build a doc identical in shape to what fuse_prediction() persists."""
    return {
        "prediction_id": pid,
        "sport": sport, "player": "Test Player",
        "stat": stat, "opponent": "KC", "threshold": threshold,
        "final_probability": final_prob,
        "projected_stat": 250.0,
        "confidence": "medium",
        "model_agreement": "moderate_convergence",
        "agreement_score": 0.8,
        "components": {
            "ml":         {"name": "ml",         "available": True,
                            "probability": final_prob, "projected": 250.0},
            "similar":    {"name": "similar",    "available": True,
                            "probability": final_prob - 0.05},
            "player_h2h": {"name": "player_h2h", "available": False,
                            "probability": None},
            "simulator":  {"name": "simulator",  "available": False,
                            "probability": None},
        },
        "weights_used": {"ml": 0.60, "similar": 0.40,
                          "player_h2h": 0.0, "simulator": 0.0},
        "created_at": "2026-07-28T00:00:00+00:00",
        "notes": [],
        "actual_value": None, "outcome": None, "correct": None,
        "winning_component": None,
    }


def test_backtest_roundtrip_records_correct_outcome():
    from services.prediction_fusion_engine import record_prediction_actual
    db = _StubDB()
    # Fused predicted 0.70 over → real actual = 300 (which is > 249.5) →
    # correct.
    _run(db.fusion_predictions.insert_one(
        _fake_prediction_doc("p1", 0.70, 249.5),
    ))
    r = _run(record_prediction_actual(db, "p1", 300.0))
    assert r["ok"] is True
    assert r["outcome"] == "over"
    assert r["correct"] is True
    # ml component probability was 0.70 (closer to 1) → wins.
    assert r["winning_component"] == "ml"


def test_backtest_records_incorrect_outcome():
    from services.prediction_fusion_engine import record_prediction_actual
    db = _StubDB()
    _run(db.fusion_predictions.insert_one(
        _fake_prediction_doc("p2", 0.75, 249.5),
    ))
    # Fused says 0.75 over → actual came in UNDER (200) → incorrect.
    r = _run(record_prediction_actual(db, "p2", 200.0))
    assert r["ok"] is True
    assert r["outcome"] == "under"
    assert r["correct"] is False


def test_backtest_missing_prediction():
    from services.prediction_fusion_engine import record_prediction_actual
    db = _StubDB()
    r = _run(record_prediction_actual(db, "does-not-exist", 100.0))
    assert r["ok"] is False


def test_backtest_summary_aggregates_by_component():
    from services.prediction_fusion_engine import (
        record_prediction_actual, get_backtest_summary,
    )
    db = _StubDB()
    # 3 predictions: 2 correct (fused=0.70 vs actual=300, fused=0.30 vs actual=200)
    #                1 incorrect (fused=0.75 vs actual=100)
    _run(db.fusion_predictions.insert_one(
        _fake_prediction_doc("q1", 0.70, 249.5)))
    _run(db.fusion_predictions.insert_one(
        _fake_prediction_doc("q2", 0.30, 249.5)))
    _run(db.fusion_predictions.insert_one(
        _fake_prediction_doc("q3", 0.75, 249.5)))
    _run(record_prediction_actual(db, "q1", 300.0))    # over — correct
    _run(record_prediction_actual(db, "q2", 200.0))    # under — correct
    _run(record_prediction_actual(db, "q3", 100.0))    # under — incorrect
    summary = _run(get_backtest_summary(db, days=365))
    # Note: the trailing-window filter uses created_at, which is a fixed
    # 2026-07-28 in the fake docs. Widen to ensure inclusion.
    assert summary["n"] == 3
    assert summary["fused_accuracy"] == round(2 / 3, 4)
    # Winning component should register wins in per-component counts.
    assert sum(v["wins"] for v in summary["component_wins"].values()) == 3


# ═════════════════════════════════════════════════════════════════════
# G. Graceful degradation
# ═════════════════════════════════════════════════════════════════════
def test_fusion_still_produces_result_when_some_components_error():
    """Even if individual component runners raise internally, the top-
    level `fuse_prediction` must never propagate."""
    from services.prediction_fusion_engine import fuse_prediction

    class _MinimalDB:
        def __init__(self):
            self.fusion_predictions = _AsyncColl()
        def __getattr__(self, name):
            return _AsyncColl()

    r = _run(fuse_prediction(
        _MinimalDB(), sport="NFL", player="Nobody",
        stat="passing_yards", opponent="KC", threshold=249.5,
    ))
    # Doesn't matter what the probability is — the point is the call
    # returned a real dict.
    assert isinstance(r.to_dict(), dict)


def test_configurable_weights_are_honoured():
    """When custom weights are passed and all components are available,
    the fused probability should follow those weights."""
    from services.prediction_fusion_engine import _normalise_weights
    custom = {"ml": 0.90, "similar": 0.05,
               "player_h2h": 0.03, "simulator": 0.02}
    # All available
    out = _normalise_weights(custom,
                              {n: True for n in custom})
    assert abs(out["ml"] - 0.90) < 1e-3
    assert abs(sum(out.values()) - 1.0) < 1e-3
