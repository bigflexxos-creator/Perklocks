"""Discovery layer tests (Phase 4, 2026-07-28).

Covers all six subsystems:
  A. Confidence system — Wilson bound math, small-sample gate, grades
  B. Threshold discovery — ladder analysis + strongest/safest picks
  C. Pattern discovery — factor splits + sample gate + lift filter
  D. Situation clustering — k-means outputs stable & target found
  E. Alt-line recommendations — safest / strongest / best_value logic
  F. Magic Finder — combines all four into a single response
  G. No sportsbook odds anywhere in the discovery package
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import math

import pytest


def _run(coro):
    return asyncio.run(coro)


# ─────────────────────────────────────────────────────────────────────
# Async stubs (reuse the shape used by adaptive_learning tests)
# ─────────────────────────────────────────────────────────────────────
class _AsyncColl:
    def __init__(self, rows=None): self.rows = list(rows or [])
    async def insert_one(self, doc): self.rows.append(dict(doc))
    async def find_one(self, q=None, *_a, **_kw):
        for r in self.rows:
            if all(r.get(k) == v for k, v in (q or {}).items()):
                return dict(r)
        return None
    def find(self, q=None, *_a, **_kw):
        rows = []
        for r in self.rows:
            ok = True
            for k, v in (q or {}).items():
                if isinstance(v, dict):
                    val = r.get(k)
                    if "$or" in v and not any(_match(r, x) for x in v["$or"]):
                        ok = False; break
                elif k == "$or":
                    if not any(_match(r, x) for x in v):
                        ok = False; break
                elif r.get(k) != v:
                    ok = False; break
            if ok:
                rows.append(dict(r))
        return _AsyncCursor(rows)
    def aggregate(self, *a, **k):
        return _AsyncCursor([])


def _match(row, q):
    for k, v in q.items():
        if row.get(k) != v:
            return False
    return True


class _AsyncCursor:
    def __init__(self, rows): self.rows = list(rows); self._i = 0
    def sort(self, *_a, **_kw): return self
    def limit(self, *_a, **_kw): return self
    def __aiter__(self): self._i = 0; return self
    async def __anext__(self):
        if self._i >= len(self.rows): raise StopAsyncIteration
        v = self.rows[self._i]; self._i += 1; return v


class _StubDB:
    def __init__(self): self.nfl_player_weekly = _AsyncColl()
    def __getattr__(self, name):
        c = _AsyncColl(); setattr(self, name, c); return c


# ═════════════════════════════════════════════════════════════════════
# A. Confidence primitives
# ═════════════════════════════════════════════════════════════════════
def test_wilson_lower_bound_penalises_small_samples():
    from services.discovery import wilson_lower_bound
    # 5/5 must NOT return 1.0 — small sample protection.
    assert wilson_lower_bound(5, 5) < 0.60
    # 50/50 gets much higher lower bound.
    assert wilson_lower_bound(50, 50) > 0.90


def test_wilson_lb_zero_denominator_ok():
    from services.discovery import wilson_lower_bound
    assert wilson_lower_bound(0, 0) == 0.0


def test_confidence_grade_scales_with_sample_and_rate():
    from services.discovery import confidence_grade
    # 5/5 (small sample even though 100 %) → weak grade.
    assert confidence_grade(5, 5) in {"D", "F"}
    # 50/60 (strong hit rate + big sample) → strong grade.
    assert confidence_grade(50, 60) in {"A+", "A", "B"}
    # 30/60 (weak lift over baseline) → mid or worse.
    assert confidence_grade(30, 60) in {"C", "D", "F"}


def test_confidence_label_tiers():
    from services.discovery import confidence_label
    assert confidence_label(0) == "insufficient"
    assert confidence_label(4) == "insufficient"
    assert confidence_label(5) == "low"
    assert confidence_label(20) == "medium"
    assert confidence_label(45) == "high"


def test_passes_sample_gate():
    from services.discovery import passes_sample_gate
    assert not passes_sample_gate(14)
    assert passes_sample_gate(15)


def test_consistency_score_bounded():
    from services.discovery import consistency_score
    assert consistency_score([]) == 0.0
    assert 0.0 <= consistency_score([100, 100, 100]) <= 1.0
    # High variance → low score.
    hi = consistency_score([0, 50, 100, 150, 200])
    lo = consistency_score([100, 100, 100, 100, 100])
    assert lo >= hi


# ═════════════════════════════════════════════════════════════════════
# B. Threshold discovery
# ═════════════════════════════════════════════════════════════════════
def test_threshold_analysis_uses_supplied_values():
    from services.discovery import analyse_thresholds
    vals = [200, 220, 240, 260, 280, 300, 320, 340,
            180, 210, 230, 250, 270, 290, 310, 330,
            200, 220, 240, 260, 280, 300, 320, 340]
    r = _run(analyse_thresholds(
        _StubDB(), sport="NFL", player="X",
        stat="passing_yards",
        thresholds=[199.5, 249.5, 299.5],
        values=vals,
    ))
    assert r["games_used"] == len(vals)
    # 199.5: hits = count(> 199.5) → should be 22 of 24.
    row_199 = r["thresholds"][0]
    assert row_199["threshold"] == 199.5
    assert row_199["hits"] == sum(1 for v in vals if v > 199.5)
    # Strongest should be the lowest-threshold row (highest lb95).
    assert r["strongest"]["threshold"] == 199.5


def test_threshold_analysis_empty_data():
    from services.discovery import analyse_thresholds
    r = _run(analyse_thresholds(_StubDB(), sport="NFL",
                                  player="Nobody", stat="passing_yards"))
    assert r["games_used"] == 0
    assert r["thresholds"] == []


def test_threshold_analysis_uses_default_ladder():
    from services.discovery import analyse_thresholds
    r = _run(analyse_thresholds(_StubDB(), sport="NFL",
                                  player="X", stat="passing_yards",
                                  values=[250, 300, 275, 220, 195]))
    # Should have several ladder rows even without passing thresholds.
    assert len(r["thresholds"]) >= 5


# ═════════════════════════════════════════════════════════════════════
# C. Pattern discovery
# ═════════════════════════════════════════════════════════════════════
def test_pattern_discovery_empty_when_no_rows():
    from services.discovery import discover_patterns
    r = _run(discover_patterns(_StubDB(), sport="NFL",
                                 player="Nobody", stat="passing_yards"))
    assert r == []


def test_pattern_discovery_returns_list_on_synthetic_data():
    """Seed a StubDB with enough rows that home_away split fires."""
    from services.discovery import discover_patterns
    db = _StubDB()
    # 40 games — 20 home, 20 away — home mean 320, away mean 220.
    for i in range(40):
        home = i % 2 == 0
        pyds = 320 if home else 220
        team = "CIN"; opp = "KC" if i < 20 else "BUF"
        away_team = opp if home else team
        home_team = team if home else opp
        db.nfl_player_weekly.rows.append({
            "player_display_name": "Joe Burrow",
            "player_id": "burrow",
            "team": team, "opponent_team": opp,
            "season": 2023, "week": (i % 17) + 1,
            "game_id": f"2023_{i:02d}_{away_team}_{home_team}",
            "passing_yards": pyds,
            "position": "QB",
        })
    patterns = _run(discover_patterns(
        db, sport="NFL", player="Joe Burrow",
        stat="passing_yards", min_samples=5, min_lift_pct=10,
    ))
    assert isinstance(patterns, list)
    # Home/away split should register — average diff is ±32%.
    factors = {p["factor"] for p in patterns}
    assert "home_away" in factors


def test_pattern_discovery_respects_sample_gate():
    """With min_samples=1000, no patterns should qualify."""
    from services.discovery import discover_patterns
    db = _StubDB()
    for i in range(20):
        db.nfl_player_weekly.rows.append({
            "player_display_name": "X",
            "player_id": "x", "team": "CIN",
            "opponent_team": "KC", "season": 2023,
            "week": i + 1, "game_id": f"2023_{i}_A_B",
            "passing_yards": 250, "position": "QB",
        })
    patterns = _run(discover_patterns(
        db, sport="NFL", player="X", stat="passing_yards",
        min_samples=1000, min_lift_pct=0,
    ))
    assert patterns == []


def test_pattern_discovery_non_nfl_returns_empty():
    from services.discovery import discover_patterns
    r = _run(discover_patterns(_StubDB(), sport="Tennis",
                                 player="Alcaraz", stat="aces"))
    assert r == []


# ═════════════════════════════════════════════════════════════════════
# D. Situation clustering
# ═════════════════════════════════════════════════════════════════════
def test_kmeans_produces_stable_clusters():
    from services.discovery.situation_clustering import _kmeans
    profiles = {
        "A": [0, 0], "B": [0.1, 0.05], "C": [10, 10], "D": [10.1, 10.2],
    }
    a = _kmeans(profiles, k=2, seed=0)
    # A,B same cluster; C,D same cluster.
    assert a["A"] == a["B"]
    assert a["C"] == a["D"]
    assert a["A"] != a["C"]


def test_situation_clustering_empty_db_returns_notes():
    from services.discovery import find_similar_situations
    r = _run(find_similar_situations(
        _StubDB(), sport="NFL", player="X",
        stat="passing_yards", opponent="KC", threshold=200.0,
    ))
    assert isinstance(r["clusters"], list)
    assert isinstance(r["notes"], list)
    # No profiles → some note reason is present.
    assert len(r["notes"]) > 0


# ═════════════════════════════════════════════════════════════════════
# E. Alt-line intelligence
# ═════════════════════════════════════════════════════════════════════
def test_alt_line_returns_ladder_and_recommendations():
    from services.discovery.alt_line_intelligence import (
        recommend_alt_lines,
    )
    from services.discovery.threshold_discovery import (
        analyse_thresholds,
    )
    # Wrap analyse_thresholds so callers get a pre-supplied `values`
    # list (no DB). Use `await` directly — no nested asyncio.run.
    async def _fake_analyse(db, **kw):
        return await analyse_thresholds(
            db,
            sport=kw.get("sport"), player=kw.get("player"),
            stat=kw.get("stat"),
            thresholds=kw.get("thresholds"),
            values=[220, 250, 280, 260, 275, 200, 290, 300, 240, 260,
                     280, 270, 240, 250, 260, 290, 240, 220, 260, 280],
        )
    # Monkey-patch analyse_thresholds inside alt_line_intelligence.
    import services.discovery.alt_line_intelligence as mod
    orig = mod.analyse_thresholds
    mod.analyse_thresholds = _fake_analyse
    try:
        r = _run(recommend_alt_lines(
            _StubDB(), sport="NFL", player="X",
            stat="passing_yards",
        ))
    finally:
        mod.analyse_thresholds = orig
    assert r["games_used"] == 20
    assert len(r["ladder"]) > 0
    assert r["safest"] is not None
    assert r["strongest"] is not None
    assert r["best_value"] is not None
    for key in ("threshold", "hit_rate", "grade"):
        assert key in r["safest"]


def test_alt_line_insufficient_games_flags_note():
    from services.discovery.alt_line_intelligence import recommend_alt_lines
    async def _fake_empty(db, **kw):
        return {"games_used": 3, "average_output": 200,
                "median_output": 200, "stdev": 0,
                "consistency_score": 1.0, "thresholds": [],
                "strongest": None, "safest": None, "notes": []}
    import services.discovery.alt_line_intelligence as mod
    orig = mod.analyse_thresholds
    mod.analyse_thresholds = _fake_empty
    try:
        r = _run(recommend_alt_lines(_StubDB(), sport="NFL",
                                       player="X", stat="passing_yards",
                                       min_games=10))
    finally:
        mod.analyse_thresholds = orig
    assert any("insufficient" in n for n in r["notes"])


# ═════════════════════════════════════════════════════════════════════
# F. Magic Finder
# ═════════════════════════════════════════════════════════════════════
def test_magic_finder_returns_full_payload_shape():
    from services.discovery import magic_find
    r = _run(magic_find(_StubDB(), sport="NFL", player="Nobody",
                          stat="passing_yards", opponent="KC",
                          threshold=249.5))
    for k in ("player", "stat", "sport", "opponent", "threshold",
              "generated_at", "threshold_analysis",
              "alt_line_recommendation", "patterns",
              "similar_situations", "explanations", "notes"):
        assert k in r


def test_magic_finder_never_raises_on_broken_inputs():
    from services.discovery import magic_find
    r = _run(magic_find(_StubDB(), sport="", player="",
                          stat="", opponent=None))
    assert isinstance(r, dict)
    assert r["explanations"] == [] or isinstance(r["explanations"], list)


# ═════════════════════════════════════════════════════════════════════
# G. No sportsbook odds anywhere in the discovery package
# ═════════════════════════════════════════════════════════════════════
def test_no_odds_or_market_features_in_discovery_package():
    import services.discovery as pkg
    import pkgutil, importlib
    banned = {
        "book_odds", "market_price", "book_price", "moneyline_odds",
        "consensus_price", "handle_pct", "vig", "juice",
        "sportsbook_price", "steam_ratio",
    }
    hits: list[str] = []
    for mod_info in pkgutil.iter_modules(pkg.__path__):
        mod = importlib.import_module(f"services.discovery.{mod_info.name}")
        src = inspect.getsource(mod)
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in banned:
                hits.append(f"{mod_info.name}:{node.id}")
            elif isinstance(node, ast.Attribute) and node.attr in banned:
                hits.append(f"{mod_info.name}:.{node.attr}")
            elif isinstance(node, ast.Constant) and \
                    isinstance(node.value, str) and node.value in banned:
                hits.append(f"{mod_info.name}:str[{node.value}]")
    assert not hits, "banned market identifier(s):\n  " + "\n  ".join(hits)
