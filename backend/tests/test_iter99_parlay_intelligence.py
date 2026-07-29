"""Parlay Intelligence Engine tests (Phase 5, iter99, 2026-06-30).

Covers:
  A. Smart Leg Ranker — signal extraction, grading, risk mapping.
  B. Correlation Engine — positive / negative / same-game / usage / player.
  C. Mode Profiles — safe / balanced / aggressive tuning.
  D. Parlay Backtester — win-rate, best combos, losing legs, calibration.
  E. Learning Loop — event recording + reliability aggregation + failure
     attribution.
  F. Ensures NO sportsbook odds are consumed anywhere in the engine.
"""
from __future__ import annotations

import asyncio
import ast
import inspect

import pytest


def _run(coro):
    return asyncio.run(coro)


# ─────────────────────────────────────────────────────────────────────
# Async DB stubs (same shape used across iter93+ tests)
# ─────────────────────────────────────────────────────────────────────
class _AsyncColl:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
    async def insert_one(self, doc):
        self.rows.append(dict(doc))
    async def find_one(self, q=None, *_a, **_kw):
        for r in self.rows:
            if all(r.get(k) == v for k, v in (q or {}).items()):
                return dict(r)
        return None
    async def update_one(self, q, update, upsert=False):
        match = None
        for r in self.rows:
            if all(r.get(k) == v for k, v in q.items()):
                match = r
                break
        if match is None:
            if not upsert:
                return
            new = dict(q)
            for k, v in (update.get("$setOnInsert") or {}).items():
                new[k] = v
            for k, v in (update.get("$set") or {}).items():
                new[k] = v
            for k, v in (update.get("$inc") or {}).items():
                new[k] = (new.get(k) or 0) + v
            self.rows.append(new)
            return
        for k, v in (update.get("$set") or {}).items():
            match[k] = v
        for k, v in (update.get("$inc") or {}).items():
            match[k] = (match.get(k) or 0) + v
    def find(self, q=None, *_a, **_kw):
        rows = []
        for r in self.rows:
            ok = True
            for k, v in (q or {}).items():
                if isinstance(v, dict) and "$in" in v:
                    if r.get(k) not in v["$in"]:
                        ok = False; break
                elif isinstance(v, dict) and "$gte" in v:
                    if not (r.get(k) and r.get(k) >= v["$gte"]):
                        ok = False; break
                elif r.get(k) != v:
                    ok = False; break
            if ok:
                rows.append(dict(r))
        return _AsyncCursor(rows)


class _AsyncCursor:
    def __init__(self, rows):
        self.rows = list(rows); self._i = 0
    def sort(self, *_a, **_kw): return self
    def limit(self, *_a, **_kw): return self
    def __aiter__(self): self._i = 0; return self
    async def __anext__(self):
        if self._i >= len(self.rows): raise StopAsyncIteration
        v = self.rows[self._i]; self._i += 1; return v


class _StubDB:
    def __init__(self):
        self._colls = {}
    def __getitem__(self, name):
        if name not in self._colls:
            self._colls[name] = _AsyncColl()
        return self._colls[name]
    def __getattr__(self, name):
        return self.__getitem__(name)


# ═════════════════════════════════════════════════════════════════════
# A. Leg Ranker
# ═════════════════════════════════════════════════════════════════════
def test_leg_ranker_uses_fusion_when_available():
    from services.parlay_intelligence import rank_leg
    pick = {
        "id": "p1",
        "fusion": {
            "final_probability": 0.72,
            "agreement_score": 0.85,
            "components": {"ml": 0.7, "similar": 0.68,
                          "player_h2h": 0.75, "simulator": 0.71},
        },
        "win_probability": 68,
        "lock_score": 90,
        "edge_percent": 5.2,
        "matchup_intel": {"grade": "A", "score": 88},
        "sample_size": 40,
        "player_recent": [100, 105, 98, 102, 99],
    }
    r = rank_leg(pick)
    assert r.pick_id == "p1"
    assert r.parlay_score > 65
    assert r.confidence_grade in {"A+", "A", "B"}
    assert r.risk_level in {"safe", "balanced"}
    assert r.components["_fused_source"] == "fusion"


def test_leg_ranker_falls_back_gracefully_without_fusion():
    from services.parlay_intelligence import rank_leg
    pick = {"id": "p2", "win_probability": 62, "lock_score": 86,
            "edge_percent": 3.2}
    r = rank_leg(pick)
    assert r.components["_fused_source"] == "win_probability"
    # Basic pick should still get a mid-tier grade
    assert r.confidence_grade in {"A+", "A", "B", "C"}


def test_leg_ranker_penalises_weak_matchup_and_thin_sample():
    from services.parlay_intelligence import rank_leg
    strong = rank_leg({
        "id": "s", "win_probability": 70, "lock_score": 90, "edge_percent": 4,
        "matchup_intel": {"grade": "A+", "score": 95},
        "sample_size": 60,
    })
    weak = rank_leg({
        "id": "w", "win_probability": 70, "lock_score": 90, "edge_percent": 4,
        "matchup_intel": {"grade": "D", "score": 30},
        "sample_size": 3,
    })
    assert strong.parlay_score > weak.parlay_score + 8


def test_leg_ranker_handles_invalid_input():
    from services.parlay_intelligence import rank_leg
    r = rank_leg(None)
    assert r.parlay_score == 0.0
    assert r.confidence_grade == "F"


def test_rank_legs_returns_sorted_desc():
    from services.parlay_intelligence import rank_legs
    picks = [
        {"id": "a", "win_probability": 55, "lock_score": 82, "edge_percent": 2},
        {"id": "b", "win_probability": 72, "lock_score": 94, "edge_percent": 6,
         "matchup_intel": {"grade": "A", "score": 90}},
        {"id": "c", "win_probability": 60, "lock_score": 88, "edge_percent": 3},
    ]
    ranked = rank_legs(picks)
    assert ranked[0].pick_id == "b"
    scores = [r.parlay_score for r in ranked]
    assert scores == sorted(scores, reverse=True)


def test_grade_and_risk_boundaries():
    from services.parlay_intelligence import grade_from_score, risk_level_from_score
    assert grade_from_score(90) == "A+"
    assert grade_from_score(78) == "A"
    assert grade_from_score(60) == "C"
    assert grade_from_score(0)  == "F"
    assert risk_level_from_score(90) == "safe"
    assert risk_level_from_score(60) == "balanced"
    assert risk_level_from_score(30) == "risky"


# ═════════════════════════════════════════════════════════════════════
# B. Correlation Engine
# ═════════════════════════════════════════════════════════════════════
def test_positive_correlation_qb_wr_same_team():
    from services.parlay_intelligence import analyze_correlations
    legs = [
        {"id": "qb", "sport": "NFL", "team": "KC",
         "event_id": "kcvsden_wk10",
         "market": "Passing Yards (KC)", "selection": "Over 275.5"},
        {"id": "wr", "sport": "NFL", "team": "KC",
         "event_id": "kcvsden_wk10",
         "market": "Receiving Yards (KC)", "selection": "Over 85.5"},
    ]
    r = analyze_correlations(legs)
    assert r.positive_pairs, "Expected QB+WR positive correlation"
    assert r.pairs[0].kind == "positive"
    assert r.pairs[0].correlation >= 0.4
    assert r.downweight_factor < 1.0


def test_same_player_blocks_pair():
    from services.parlay_intelligence import analyze_correlations
    legs = [
        {"id": "a", "sport": "NFL", "player_name": "Lamar Jackson",
         "market": "Passing Yards (BAL)", "selection": "Over 249.5"},
        {"id": "b", "sport": "NFL", "player_name": "Lamar Jackson",
         "market": "Rushing Yards (BAL)", "selection": "Over 55.5"},
    ]
    r = analyze_correlations(legs)
    assert r.blocked_pairs, "Same-player pair must block"
    assert any("Lamar" in w or "same-player" in w.lower() for w in r.warnings)


def test_usage_conflict_two_rbs_same_team():
    from services.parlay_intelligence import analyze_correlations
    legs = [
        {"id": "rb1", "sport": "NFL", "team": "SF",
         "market": "Rushing Yards (SF)", "selection": "Over 65.5"},
        {"id": "rb2", "sport": "NFL", "team": "SF",
         "market": "Rushing Yards (SF)", "selection": "Over 40.5"},
    ]
    r = analyze_correlations(legs)
    # Same-team same market with usage-conflict rule → negative correlation
    kinds = [p.kind for p in r.pairs]
    assert "usage_conflict" in kinds
    idx = kinds.index("usage_conflict")
    assert r.pairs[idx].correlation < 0


def test_same_game_dependency_two_moneylines():
    from services.parlay_intelligence import analyze_correlations
    legs = [
        {"id": "a", "sport": "NFL", "event_id": "g1",
         "market": "Moneyline", "selection": "KC", "pick_side": "KC"},
        {"id": "b", "sport": "NFL", "event_id": "g1",
         "market": "Spread -3.5", "selection": "KC", "pick_side": "KC"},
    ]
    r = analyze_correlations(legs)
    assert r.pairs[0].kind == "same_game"
    assert r.pairs[0].correlation > 0


def test_negative_two_rbs_different_teams_same_game_opp_script():
    from services.parlay_intelligence import analyze_correlations
    legs = [
        {"id": "a", "sport": "NFL", "team": "SF", "event_id": "sfvsphi",
         "market": "Rushing Yards (SF)", "selection": "Over 80"},
        {"id": "b", "sport": "NFL", "team": "PHI", "event_id": "sfvsphi",
         "market": "Rushing Yards (PHI)", "selection": "Over 80"},
    ]
    r = analyze_correlations(legs)
    kinds = [p.kind for p in r.pairs]
    assert "opposite_script" in kinds


def test_report_with_single_leg_is_empty():
    from services.parlay_intelligence import analyze_correlations
    r = analyze_correlations([{"id": "a"}])
    assert r.pairs == [] and r.tier == "none"


def test_combine_with_guard_returns_legacy_shape():
    from services.parlay_intelligence import combine_with_guard
    legs = [
        {"id": "a", "sport": "NFL", "team": "KC", "event_id": "g",
         "market": "Passing Yards (KC)", "selection": "Over 250"},
        {"id": "b", "sport": "NFL", "team": "KC", "event_id": "g",
         "market": "Receiving Yards (KC)", "selection": "Over 80"},
    ]
    out = combine_with_guard(legs)
    for key in ("warnings", "blocked_pairs", "downweight_factor",
                "correlation_tier", "pairs", "positive_pairs",
                "negative_pairs", "correlation_score"):
        assert key in out


# ═════════════════════════════════════════════════════════════════════
# C. Mode Profiles
# ═════════════════════════════════════════════════════════════════════
def test_mode_profiles_are_ordered_correctly():
    from services.parlay_intelligence import MODE_PROFILES
    s = MODE_PROFILES["safe"]
    b = MODE_PROFILES["balanced"]
    a = MODE_PROFILES["aggressive"]
    assert s.min_lock > b.min_lock > a.min_lock
    assert s.min_edge_pct > b.min_edge_pct > a.min_edge_pct
    assert s.max_legs < b.max_legs < a.max_legs
    assert s.min_parlay_score > b.min_parlay_score > a.min_parlay_score


def test_resolve_mode_maps_legacy_names():
    from services.parlay_intelligence import resolve_mode
    assert resolve_mode("standard") == "balanced"
    assert resolve_mode("high_risk") == "aggressive"
    assert resolve_mode("today_window") == "safe"
    assert resolve_mode("safer") == "safe"
    assert resolve_mode(None) == "balanced"
    assert resolve_mode("garbage") == "balanced"


def test_leg_passes_profile_hard_filters():
    from services.parlay_intelligence.parlay_modes import (
        leg_passes_profile, profile_for,
    )
    from services.parlay_intelligence import rank_leg
    safe = profile_for("safe")
    weak = {"id": "w", "lock_score": 85, "edge_percent": 2,
            "win_probability": 55}
    strong = {"id": "s", "lock_score": 95, "edge_percent": 5,
              "win_probability": 75, "matchup_intel": {"score": 90},
              "sample_size": 40}
    r_w = rank_leg(weak)
    r_s = rank_leg(strong)
    ok_w, _ = leg_passes_profile(weak, r_w, safe)
    ok_s, _ = leg_passes_profile(strong, r_s, safe)
    assert not ok_w
    assert ok_s


# ═════════════════════════════════════════════════════════════════════
# D. Parlay Backtester
# ═════════════════════════════════════════════════════════════════════
def test_backtester_empty_history_returns_zero_shape():
    from services.parlay_intelligence import backtest_parlays
    db = _StubDB()
    report = _run(backtest_parlays(db, days=60))
    assert report["n_parlays"] == 0
    assert report["by_leg_count"] == {}


def test_backtester_aggregates_by_leg_count_and_combos():
    import datetime as _dt
    from services.parlay_intelligence import backtest_parlays
    db = _StubDB()
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    # 3 winning 3-leg NFL parlays with QB pass + WR rec + rush combos
    for i in range(3):
        _run(db["parlay_history"].insert_one({
            "signature": f"w{i}", "status": "won", "leg_count": 3,
            "survival_pct": 45.0, "shown_at": now,
            "legs": [
                {"pick_id": f"a{i}", "sport": "nfl",
                 "market_family": "qb_pass_yards"},
                {"pick_id": f"b{i}", "sport": "nfl",
                 "market_family": "rec_yards"},
                {"pick_id": f"c{i}", "sport": "nfl",
                 "market_family": "rush_yards"},
            ],
        }))
    # 2 losing 5-leg soccer W-o-D parlays
    for i in range(2):
        _run(db["parlay_history"].insert_one({
            "signature": f"l{i}", "status": "lost", "leg_count": 5,
            "survival_pct": 30.0, "shown_at": now,
            "legs": [
                {"pick_id": f"la{i}", "sport": "soccer",
                 "market_family": "win_or_draw"} for _ in range(5)
            ],
        }))
    r = _run(backtest_parlays(db, days=60))
    assert r["n_parlays"] == 5
    assert r["wins"] == 3 and r["losses"] == 2
    # 3-leg bucket 100% wr, 5-leg bucket 0% wr
    assert r["by_leg_count"]["3"]["win_rate"] == 1.0
    assert r["by_leg_count"]["5"]["win_rate"] == 0.0
    # Losing legs should surface soccer/win_or_draw
    losers = r["common_losing_legs"]
    assert any(x["sport"] == "soccer" and x["family"] == "win_or_draw"
               for x in losers)


def test_backtester_confidence_calibration_bins():
    import datetime as _dt
    from services.parlay_intelligence import backtest_parlays
    db = _StubDB()
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    # High survival + won → 90-bucket
    for i in range(4):
        _run(db["parlay_history"].insert_one({
            "signature": f"hw{i}", "status": "won", "leg_count": 3,
            "survival_pct": 92.0, "shown_at": now,
            "legs": [{"pick_id": "p", "sport": "nfl",
                      "market_family": "rec_yards"}],
        }))
    r = _run(backtest_parlays(db, days=60))
    conf = r["confidence_accuracy"]
    assert conf, "expected calibration bins populated"
    assert any(b["bucket_pct"] == 90 for b in conf)


def test_summarize_backtest_returns_useful_bullets():
    from services.parlay_intelligence import summarize_backtest
    fake_report = {
        "n_parlays": 25, "wins": 12, "losses": 12, "pushes": 1,
        "win_rate": 0.48, "lookback_days": 60,
        "best_combos": [{
            "sport_a": "nfl", "family_a": "qb_pass_yards",
            "sport_b": "nfl", "family_b": "rec_yards",
            "win_rate": 0.75, "n": 8, "wins": 6, "losses": 2,
        }],
        "common_losing_legs": [{
            "sport": "soccer", "family": "win_or_draw",
            "win_rate": 0.30, "lose_rate": 0.70, "n": 10,
            "wins": 3, "losses": 7,
        }],
    }
    bullets = summarize_backtest(fake_report)
    assert any("hit rate" in b.lower() for b in bullets)
    assert any("Top combo" in b for b in bullets)


# ═════════════════════════════════════════════════════════════════════
# E. Learning Loop
# ═════════════════════════════════════════════════════════════════════
def test_infer_failure_reason_flags_weak_matchup():
    from services.parlay_intelligence import infer_failure_reason
    leg = {"lock_score": 90, "win_probability": 72, "sport": "nfl",
           "market_family": "rec_yards"}
    ranking = {"components": {"matchup": 40, "sample_confidence": 80,
                              "model_agreement": 78,
                              "fused_probability": 70}}
    reason = infer_failure_reason(leg, ranking)
    assert "matchup" in reason.lower()


def test_infer_failure_reason_fallback_when_no_pregame_weakness():
    from services.parlay_intelligence import infer_failure_reason
    leg = {"lock_score": 95, "win_probability": 78}
    ranking = {"components": {"matchup": 85, "sample_confidence": 82,
                              "model_agreement": 80,
                              "fused_probability": 74}}
    reason = infer_failure_reason(leg, ranking)
    assert "variance" in reason.lower() or "no obvious" in reason.lower()


def test_record_completed_parlay_persists_event_and_reliability():
    from services.parlay_intelligence import (
        record_completed_parlay, get_leg_reliability,
    )
    db = _StubDB()
    parlay = {
        "signature": "sig1", "status": "lost", "leg_count": 3,
        "survival_pct": 42, "mode": "balanced",
        "legs": [
            {"pick_id": "p1", "sport": "nfl", "market_family": "rec_yards"},
            {"pick_id": "p2", "sport": "nfl", "market_family": "rush_yards"},
            {"pick_id": "p3", "sport": "mlb", "market_family": "pitcher_ks"},
        ],
    }
    ranking_snapshot = {
        "p1": {"parlay_score": 72, "components": {"matchup": 30,
              "sample_confidence": 60, "model_agreement": 70,
              "fused_probability": 60}},
        "p2": {"parlay_score": 65, "components": {"matchup": 55}},
        "p3": {"parlay_score": 60, "components": {"matchup": 50}},
    }
    pick_statuses = {"p1": "lost", "p2": "won", "p3": "won"}
    event = _run(record_completed_parlay(
        db, parlay,
        pick_statuses=pick_statuses,
        ranking_snapshot=ranking_snapshot,
    ))
    assert event is not None
    assert event["outcome"] == "lost"
    assert event["failed_leg"] and event["failed_leg"]["pick_id"] == "p1"
    assert "matchup" in event["failed_leg"]["reason"].lower()
    # Reliability rows created for each unique (sport, family)
    reliab_rows = list(db["parlay_leg_reliability"].rows)
    assert len(reliab_rows) == 3
    # Bump the same combo twice more (as wins) to cross the min-sample gate
    parlay2 = dict(parlay, signature="sig2", status="won")
    _run(record_completed_parlay(db, parlay2,
                                 pick_statuses={"p1": "won", "p2": "won",
                                               "p3": "won"},
                                 ranking_snapshot=ranking_snapshot))
    parlay3 = dict(parlay, signature="sig3", status="won")
    _run(record_completed_parlay(db, parlay3,
                                 pick_statuses={"p1": "won", "p2": "won",
                                               "p3": "won"},
                                 ranking_snapshot=ranking_snapshot))
    parlay4 = dict(parlay, signature="sig4", status="won")
    _run(record_completed_parlay(db, parlay4,
                                 pick_statuses={"p1": "won", "p2": "won",
                                               "p3": "won"},
                                 ranking_snapshot=ranking_snapshot))
    parlay5 = dict(parlay, signature="sig5", status="won")
    _run(record_completed_parlay(db, parlay5,
                                 pick_statuses={"p1": "won", "p2": "won",
                                               "p3": "won"},
                                 ranking_snapshot=ranking_snapshot))
    rel = _run(get_leg_reliability(db, "nfl", "rec_yards", min_samples=3))
    assert rel is not None
    assert rel["n_total"] >= 3
    # 1 loss + 4 wins → hit_rate 0.8
    assert 0.7 <= rel["hit_rate"] <= 0.9


def test_record_completed_parlay_returns_none_when_pending():
    from services.parlay_intelligence import record_completed_parlay
    db = _StubDB()
    parlay = {"signature": "x", "status": "pending", "legs": []}
    assert _run(record_completed_parlay(db, parlay)) is None


def test_get_leg_reliability_below_sample_returns_none():
    from services.parlay_intelligence import get_leg_reliability
    db = _StubDB()
    assert _run(get_leg_reliability(db, "nfl", "rec_yards")) is None


# ═════════════════════════════════════════════════════════════════════
# F. No sportsbook odds in ANY source file
# ═════════════════════════════════════════════════════════════════════
def test_no_sportsbook_odds_in_parlay_intelligence():
    """The engine must never consume sportsbook market-side data.

    We scan for *code-shape* terms that would only appear as feature
    references — variable/attribute names — not the prose word
    'sportsbook' in docstrings.
    """
    import services.parlay_intelligence as pkg
    import pathlib
    root = pathlib.Path(pkg.__file__).parent
    banned = ("book_odds", "consensus_odds", "closing_line", "opening_line",
              "steam_move", "book_line", "market_price", "vig_pct",
              "bookmaker_edge")
    for py in root.glob("*.py"):
        src = py.read_text()
        for term in banned:
            assert term not in src, (
                f"Banned market-side term '{term}' found in {py.name}"
            )


# ═════════════════════════════════════════════════════════════════════
# G. Package importable + everything wired
# ═════════════════════════════════════════════════════════════════════
def test_package_public_api_is_stable():
    import services.parlay_intelligence as pkg
    for name in ("rank_leg", "rank_legs", "grade_from_score",
                 "risk_level_from_score", "analyze_correlations",
                 "pairwise_correlation", "combine_with_guard",
                 "MODE_PROFILES", "profile_for", "resolve_mode",
                 "backtest_parlays", "summarize_backtest",
                 "record_completed_parlay", "get_leg_reliability",
                 "infer_failure_reason"):
        assert hasattr(pkg, name), f"parlay_intelligence.{name} missing"
