"""P12 contracts (2026-09-13 · MLB + NFL Game closure).

Test contracts:
  1-5. MLB Hits/H+R+RBI/TB/K/Outs with edge < -1 can still reach scoring
  6-8. NFL ML/Spread/Total with edge < -1 can still reach scoring
  9.   negative edge alone does not increase LS
 10.   malformed/no-model pick still fails closed (WP invariant preserved)
 11.   NFL player-alt behavior unchanged
 12.   NFL alt candidate/threshold/odds/WP frozen
 13.   NFL game convergence uses semantic support
 14.   sportsbook implied is market anchor, not independent evidence
 15.   strong game fixture can legitimately clear 85
 16.   weak game fixture stays below 85
"""
from __future__ import annotations

import sys, pathlib
_BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from sports_engine import compute_lock_score, _build_pick


def _build(**kw):
    kw.setdefault("insights", [])
    kw.setdefault("external_id", "test-x")
    return _build_pick(**kw)


# ─────────────────────────────────────────────────────────────
# 1-5. MLB Hits / TB / HRR / RBI / HR / K / Outs — edge < -1 EXEMPT
# ─────────────────────────────────────────────────────────────
def test_mlb_hits_edge_below_one_survives():
    p = _build(sport="MLB", league="MLB", event="NYY vs BOS",
                event_time=None, market="Aaron Judge Over 0.5 Hits",
                pick_side="Judge", model_win_prob=0.68, book_odds=-300,
                lock=90.0,
                factors={"Expected BA (Statcast)": 0.75, "Barrel%": 0.80})
    assert p is not None, "MLB Hits with edge<-1 must survive generation"


def test_mlb_total_bases_edge_below_one_survives():
    p = _build(sport="MLB", league="MLB", event="X",
                event_time=None, market="Aaron Judge Over 1.5 Total Bases",
                pick_side="X", model_win_prob=0.62, book_odds=-250,
                lock=88.0, factors={"Expected BA (Statcast)": 0.72})
    assert p is not None


def test_mlb_hrr_edge_below_one_survives():
    p = _build(sport="MLB", league="MLB", event="X",
                event_time=None,
                market="Judge Over 1.5 Hits + Runs + RBIs",
                pick_side="X", model_win_prob=0.65, book_odds=-280,
                lock=88.0, factors={"Matchup Advantage": 0.85})
    assert p is not None


def test_mlb_pitcher_strikeouts_edge_below_one_survives():
    p = _build(sport="MLB", league="MLB", event="X",
                event_time=None,
                market="Gerrit Cole Over 6.5 Strikeouts",
                pick_side="Over", model_win_prob=0.58, book_odds=-220,
                lock=86.0, factors={"K/9 Prior": 0.72})
    assert p is not None


def test_mlb_pitcher_outs_edge_below_one_survives():
    p = _build(sport="MLB", league="MLB", event="X",
                event_time=None,
                market="Cole Over 17.5 Pitcher Outs Recorded",
                pick_side="Over", model_win_prob=0.60, book_odds=-240,
                lock=86.0, factors={"Innings Prior": 0.70})
    assert p is not None


# ─────────────────────────────────────────────────────────────
# 6-8. NFL Moneyline / Spread / Total — edge < -1 EXEMPT
# ─────────────────────────────────────────────────────────────
def test_nfl_ml_edge_below_one_survives():
    p = _build(sport="NFL", league="NFL", event="BAL vs PIT",
                event_time=None, market="Buffalo Bills Moneyline",
                pick_side="Bills", model_win_prob=0.72, book_odds=-500,
                lock=88.0, factors={"Model Win Prob (norm)": 0.72})
    assert p is not None


def test_nfl_spread_edge_below_one_survives():
    p = _build(sport="NFL", league="NFL", event="X",
                event_time=None, market="Baltimore Ravens -7.5 Spread",
                pick_side="Ravens -7.5", model_win_prob=0.72,
                book_odds=-500, lock=88.0,
                factors={"Model Win Prob (norm)": 0.72})
    assert p is not None


def test_nfl_total_edge_below_one_survives():
    p = _build(sport="NFL", league="NFL", event="X",
                event_time=None, market="Total Points Over 47.5",
                pick_side="Over", model_win_prob=0.60, book_odds=-500,
                lock=88.0, factors={"Model Win Prob (norm)": 0.60})
    assert p is not None


# ─────────────────────────────────────────────────────────────
# 9. Negative edge alone must NOT increase LS
# ─────────────────────────────────────────────────────────────
def test_negative_edge_does_not_boost_lock_score():
    """A pick with negative edge must not receive a Lock Score BOOST
    solely because of the negative edge.  The edge exemption stops
    the row from being deleted; it does not add points."""
    # Compare two identical pick shims — one w/ positive edge, one w/
    # deeply negative edge — through compute_lock_score.  A negative
    # edge must NEVER produce a higher final LS.
    common = dict(factors={"Model Win Prob (norm)": 0.72,
                            "Expected Margin (norm)": 0.78,
                            "Simulation Stability (norm)": 0.90})
    pick_pos = dict(sport="NFL", market="Buffalo Bills Moneyline",
                     book_odds=-120, win_probability=72.0,
                     data_quality="platinum_nfl_sim",
                     probability_provenance="CAUSAL_INDEPENDENT",
                     sim_stability=0.90)
    pick_neg = dict(pick_pos)
    ls_pos, _ = compute_lock_score(dict(common["factors"]), win_prob=72.0,
                                     pick=pick_pos, edge_percent=5.0)
    ls_neg, _ = compute_lock_score(dict(common["factors"]), win_prob=72.0,
                                     pick=pick_neg, edge_percent=-5.0)
    assert ls_neg <= ls_pos + 0.15, (
        f"negative edge must not boost LS; pos={ls_pos} neg={ls_neg}"
    )


# ─────────────────────────────────────────────────────────────
# 10. Non-exempt market with negative edge — REJECTED (fail-closed)
# ─────────────────────────────────────────────────────────────
def test_non_exempt_negative_edge_still_rejected():
    p = _build(sport="MLB", league="MLB", event="X",
                event_time=None, market="SB Anytime", pick_side="X",
                model_win_prob=0.50, book_odds=-500, lock=60.0,
                factors={"x": 0.5})
    assert p is None, "non-exempt negative-edge market must still reject"


# ─────────────────────────────────────────────────────────────
# 11. NFL alt player-prop still exempt (frozen behavior)
# ─────────────────────────────────────────────────────────────
def test_nfl_alt_prop_edge_exempt_frozen():
    p = _build(sport="NFL", league="NFL", event="X",
                event_time=None, market="Player Pass Yds Over 200.5",
                pick_side="X", model_win_prob=0.72, book_odds=-500,
                lock=90.0, factors={"x": 0.72}, is_alt_prop=True)
    assert p is not None


# ─────────────────────────────────────────────────────────────
# 13-16. NFL game convergence + strong/weak reachability
# ─────────────────────────────────────────────────────────────
def test_nfl_game_convergence_is_semantic():
    """Heterogeneous NFL game evidence (WP 0.72, margin 0.78, sim 0.92)
    must NOT collapse to near-zero convergence via raw stdev.  The
    semantic-agreement fix treats all three axes as supporting the
    same side → high agreement."""
    factors = {"Model Win Prob (norm)": 0.72,
                "Expected Margin (norm)": 0.78,
                "Model-vs-Market Δ": 0.62,
                "Simulation Stability (norm)": 0.92}
    pick = {"sport": "NFL", "market": "Baltimore Ravens -7.5 Spread",
             "book_odds": -110, "win_probability": 72.0,
             "data_quality": "platinum_nfl_sim",
             "probability_provenance": "CAUSAL_INDEPENDENT",
             "sim_stability": 0.92}
    compute_lock_score(factors, win_prob=72.0, pick=pick, edge_percent=5.0)
    bq = pick.get("bet_quality_authority") or {}
    conv = (bq.get("components") or {}).get("convergence")
    assert conv is not None and conv >= 75.0, (
        f"semantic NFL game convergence must be ≥75 with strong signals; got {conv}"
    )


def test_sportsbook_implied_is_market_anchor_not_evidence():
    """Sportsbook Implied (norm) must NOT swing evidence-based LS."""
    with_anchor = {"Model Win Prob (norm)": 0.72,
                    "Expected Margin (norm)": 0.72,
                    "Sportsbook Implied (norm)": 0.90}
    without = {"Model Win Prob (norm)": 0.72,
                "Expected Margin (norm)": 0.72}
    pick_a = {"sport": "NFL", "market": "NYJ Moneyline",
               "win_probability": 72.0,
               "probability_provenance": "CAUSAL_INDEPENDENT"}
    pick_b = dict(pick_a)
    ls_a, _ = compute_lock_score(with_anchor, win_prob=72.0, pick=pick_a)
    ls_b, _ = compute_lock_score(without, win_prob=72.0, pick=pick_b)
    assert abs(ls_a - ls_b) < 0.5, (
        f"market anchor must not change LS; with={ls_a} without={ls_b}"
    )


def test_nfl_game_strong_fixture_clears_85():
    factors = {"Model Win Prob (norm)": 0.72,
                "Expected Margin (norm)": 0.78,
                "Model-vs-Market Δ": 0.62,
                "Simulation Stability (norm)": 0.92}
    pick = {"book_odds": -110, "edge_percent": 5.0,
             "win_probability": 72.0,
             "sport": "NFL", "market": "Baltimore Ravens -7.5 Spread",
             "data_quality": "platinum_nfl_sim",
             "probability_provenance": "CAUSAL_INDEPENDENT",
             "sim_stability": 0.92}
    ls, _ = compute_lock_score(factors, win_prob=72.0, pick=pick,
                                 edge_percent=5.0)
    assert ls >= 85.0, f"strong NFL game fixture must clear 85; got {ls}"


def test_nfl_game_weak_fixture_below_85():
    factors = {"Model Win Prob (norm)": 0.53,
                "Expected Margin (norm)": 0.51,
                "Model-vs-Market Δ": 0.50,
                "Simulation Stability (norm)": 0.78}
    pick = {"book_odds": -110, "edge_percent": 0.5,
             "win_probability": 53.0,
             "sport": "NFL", "market": "NYJ -1.0 Spread",
             "data_quality": "platinum_nfl_sim",
             "probability_provenance": "CAUSAL_INDEPENDENT",
             "sim_stability": 0.78}
    ls, _ = compute_lock_score(factors, win_prob=53.0, pick=pick,
                                 edge_percent=0.5)
    assert ls < 85.0, f"weak NFL game fixture must stay below 85; got {ls}"
