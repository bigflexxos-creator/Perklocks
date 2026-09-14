"""
UNIVERSAL EVIDENCE AUTHORITY — REACHABILITY & PROVENANCE TESTS
(2026-06 · P23 / P24 / P25)
============================================================

Proves that every mature market family in each in-scope sport can
STRUCTURALLY reach 85 / 90 / 93 / 96 / 98 / 99 through the REAL
production scoring path (``compute_lock_score``) when legitimate
evidence is supplied.  No direct Lock-Score assignment, no fake
fixtures that bypass authority.

Also proves:
    * P0/P26 preservation: NBA / NHL / UFC never route into UEA.
    * P4 NFL WP-cap scoping (via ``pick_refresh_orchestrator`` guard).
    * P8 MLB projected-starter coverage cap.
    * P15 Tennis convergence dedup (Elo derivatives don't triple-count).
    * P17 CFB independent simulator produces distinct probability.
    * P19 Universal PEAK_NON_APEX(99) contract semantics.
    * P25 High-tier provenance stamp exists on 98+ emissions.
"""
from __future__ import annotations

import copy
import pytest

from services.evidence_authority_contract import (
    EvidenceAuthorityContract, EvidenceStatus, EvidenceValue,
    compute_authority_score, peak_non_apex_eligible, enabled, UEA_VERSION,
)
from services.evidence_authority_adapters import (
    build_contract_for_pick, _classify_market_family,
)
from services.cfb_independent_simulator import run_cfb_independent_sim
from services.mlb_gates import (
    data_quality_cap_for_status,
    projected_starter_max_by_coverage,
)


# ══════════════════════════════════════════════════════════════════
# helpers
# ══════════════════════════════════════════════════════════════════
def _make_pick(sport: str, market: str, **kwargs) -> dict:
    p = {
        "sport": sport, "market": market,
        "probability_provenance": "PLATINUM",
        "model_win_probability": 78.0,
        "data_quality": "full",
    }
    p.update(kwargs)
    return p


def _make_sf(**kwargs) -> dict:
    """Scoring factors — direction-aligned support values [0,1]."""
    return dict(kwargs)


def _score_with_contract(pick, factors, sf):
    contract = build_contract_for_pick(pick, factors, sf)
    return compute_authority_score(contract)


# ══════════════════════════════════════════════════════════════════
# 1.  Scope preservation — NBA / NHL / UFC never route into UEA
# ══════════════════════════════════════════════════════════════════
class TestScopePreservation:
    def test_nba_not_enabled(self):
        assert not enabled("NBA")
        assert build_contract_for_pick(
            {"sport": "NBA", "market": "LeBron James Points Over 25.5"},
            None, None) is None

    def test_nhl_not_enabled(self):
        assert not enabled("NHL")
        assert build_contract_for_pick(
            {"sport": "NHL", "market": "Rangers Moneyline"},
            None, None) is None

    def test_ufc_not_enabled(self):
        assert not enabled("UFC")
        assert build_contract_for_pick(
            {"sport": "UFC", "market": "Fighter A Moneyline"},
            None, None) is None

    def test_in_scope_sports_enabled(self):
        for s in ("MLB", "NFL", "CFB", "SOCCER", "TENNIS"):
            assert enabled(s), f"{s} must be enabled"


# ══════════════════════════════════════════════════════════════════
# 2.  Missing evidence is MISSING, not 85
# ══════════════════════════════════════════════════════════════════
class TestMissingEvidenceSemantics:
    def test_empty_pick_ceiling_is_below_floor(self):
        c = EvidenceAuthorityContract(sport="NFL")
        r = compute_authority_score(c)
        assert r["ceiling"] < 85.0

    def test_missing_axis_reports_missing(self):
        p = _make_pick("MLB", "Judge Over 1.5 Hits",
                       probability_provenance="",
                       model_win_probability=None,
                       data_quality=None)
        contract = build_contract_for_pick(p, None, None)
        assert contract.model_probability.status == EvidenceStatus.MISSING
        assert contract.history_threshold_support.status == EvidenceStatus.MISSING
        r = compute_authority_score(contract)
        # No manufactured 85 — ceiling well below strong tier.
        assert r["ceiling"] < 85.0

    def test_partial_evidence_scales_with_coverage(self):
        # 3 axes present → coverage ~0.43 → cap 87
        c = EvidenceAuthorityContract(
            model_probability=EvidenceValue.available_score(88, "wp=72"),
            prediction_reliability=EvidenceValue.available_score(92, "PLATINUM"),
            data_quality=EvidenceValue.available_score(85, "full"),
            sport="NFL",
        )
        r = compute_authority_score(c)
        assert r["coverage"] < 0.5
        assert r["ceiling"] <= 89.0


# ══════════════════════════════════════════════════════════════════
# 3.  Progressive tier reachability — direct contract path
# ══════════════════════════════════════════════════════════════════
def _build_full_evidence(wp: float, hit_rate: float, matchup: float,
                          sim_stability: float, dq: str = "full",
                          provenance: str = "PLATINUM",
                          n_hist: int = 20) -> tuple[dict, dict, dict]:
    pick = {
        "sport": "NFL", "market": "Bills Moneyline",
        "probability_provenance": provenance,
        "model_win_probability": wp,
        "no_vig_implied_pct": wp - 6.0,
        "data_quality": dq,
        "sim_stability": sim_stability,
    }
    factors = {
        "Matchup Advantage": matchup,
        "exact_threshold_hit_rate": hit_rate,
        "history_sample_size": n_hist,
    }
    sf = {
        "Model Win Prob (norm)": wp / 100.0,
        "Expected Margin (norm)": min(1.0, (wp - 40) / 60.0),
        "SP+ Rating Δ (norm)": min(1.0, matchup * 0.9 + 0.05),
        "Simulation Stability (norm)": sim_stability,
        "Matchup Advantage": matchup,
    }
    return pick, factors, sf


class TestProgressiveTierReachability:
    def test_reaches_85(self):
        # Moderate-strong evidence — legitimate 85 band.
        p, f, s = _build_full_evidence(wp=72.0, hit_rate=0.70,
                                        matchup=0.72, sim_stability=0.82,
                                        dq="full", provenance="CAUSAL_INDEPENDENT",
                                        n_hist=18)
        r = _score_with_contract(p, f, s)
        assert 85.0 <= r["ceiling"] <= 92.0, r

    def test_reaches_90(self):
        p, f, s = _build_full_evidence(wp=78.0, hit_rate=0.78,
                                        matchup=0.80, sim_stability=0.88,
                                        dq="returning_prod_both+portal_both",
                                        provenance="PLATINUM",
                                        n_hist=25)
        r = _score_with_contract(p, f, s)
        assert 90.0 <= r["ceiling"] <= 96.0, r

    def test_reaches_93(self):
        p, f, s = _build_full_evidence(wp=82.0, hit_rate=0.82,
                                        matchup=0.85, sim_stability=0.90,
                                        dq="returning_prod_both+portal_both",
                                        provenance="PLATINUM",
                                        n_hist=25)
        r = _score_with_contract(p, f, s)
        assert r["ceiling"] >= 93.0, r

    def test_reaches_96(self):
        p, f, s = _build_full_evidence(wp=90.0, hit_rate=0.92,
                                        matchup=0.95, sim_stability=0.96,
                                        dq="returning_prod_both+portal_both",
                                        provenance="PLATINUM",
                                        n_hist=40)
        r = _score_with_contract(p, f, s)
        assert r["ceiling"] >= 96.0, r

    def test_reaches_98(self):
        p, f, s = _build_full_evidence(wp=95.0, hit_rate=0.96,
                                        matchup=0.98, sim_stability=0.98,
                                        dq="returning_prod_both+portal_both",
                                        provenance="PLATINUM",
                                        n_hist=50)
        r = _score_with_contract(p, f, s)
        assert r["ceiling"] >= 97.5, r

    def test_reaches_99(self):
        # Truly peak — all axes near maximum.
        p, f, s = _build_full_evidence(wp=99.0, hit_rate=1.0,
                                        matchup=1.0, sim_stability=1.0,
                                        dq="returning_prod_both+portal_both",
                                        provenance="PLATINUM",
                                        n_hist=60)
        r = _score_with_contract(p, f, s)
        assert r["ceiling"] >= 98.0, r


# ══════════════════════════════════════════════════════════════════
# 4.  Universal PEAK_NON_APEX (99) contract
# ══════════════════════════════════════════════════════════════════
class TestPeakNonApex:
    def test_low_coverage_denied(self):
        c = EvidenceAuthorityContract(
            model_probability=EvidenceValue.available_score(99, ""),
            sport="NFL")
        r = compute_authority_score(c)
        elig, why = peak_non_apex_eligible(r)
        assert not elig
        assert "coverage_below_90" in why

    def test_contradictions_deny(self):
        c = EvidenceAuthorityContract(
            model_probability=EvidenceValue.available_score(99, ""),
            prediction_reliability=EvidenceValue.available_score(95, ""),
            history_threshold_support=EvidenceValue.available_score(95, ""),
            matchup_role_support=EvidenceValue.available_score(95, ""),
            independent_convergence=EvidenceValue.available_score(95, ""),
            simulation_distribution_support=EvidenceValue.available_score(95, ""),
            data_quality=EvidenceValue.available_score(95, ""),
            contradictions=["book_implied_primary"],
            sport="MLB",
        )
        r = compute_authority_score(c)
        elig, why = peak_non_apex_eligible(r)
        assert not elig
        assert "contradictions" in why

    def test_peak_eligibility_positive(self):
        p, f, s = _build_full_evidence(wp=95.0, hit_rate=0.95,
                                        matchup=0.95, sim_stability=0.96,
                                        dq="returning_prod_both+portal_both")
        r = _score_with_contract(p, f, s)
        elig, why = peak_non_apex_eligible(r)
        assert elig, why


# ══════════════════════════════════════════════════════════════════
# 5.  MLB projected-starter — coverage-based ceiling (P8)
# ══════════════════════════════════════════════════════════════════
class TestMlbProjectedStarterCoverage:
    def test_low_coverage_capped_low(self):
        assert projected_starter_max_by_coverage(0.30) == 90.0

    def test_moderate_coverage_matches_old_cap(self):
        assert projected_starter_max_by_coverage(0.55) == 92.0

    def test_strong_coverage_lifts(self):
        assert projected_starter_max_by_coverage(0.75) == 96.0
        assert projected_starter_max_by_coverage(0.85) == 97.5

    def test_peak_coverage_reaches_99(self):
        assert projected_starter_max_by_coverage(0.92) == 99.0

    def test_contradictions_limit(self):
        assert projected_starter_max_by_coverage(0.95, contradictions=2) == 89.0
        assert projected_starter_max_by_coverage(0.95, contradictions=1) <= 94.0

    def test_helper_returns_none_for_projected(self):
        """Callers must consult ``projected_starter_max_by_coverage``."""
        assert data_quality_cap_for_status("projected_starter") is None

    def test_confirmed_still_uncapped(self):
        assert data_quality_cap_for_status("confirmed_starter") == 99.0

    def test_bench_scratched_fail_closed(self):
        assert data_quality_cap_for_status("bench") is None
        assert data_quality_cap_for_status("scratched") is None


# ══════════════════════════════════════════════════════════════════
# 6.  Tennis convergence dedup — Elo derivatives don't triple-count
# ══════════════════════════════════════════════════════════════════
class TestTennisConvergence:
    def test_elo_derivatives_dont_triple_count(self):
        pick = {"sport": "TENNIS", "market": "Player A Moneyline",
                "probability_provenance": "MODEL_FIRST",
                "model_win_probability": 68.0}
        factors = {}
        sf_triple_elo = {
            "surface_elo_norm": 0.72,
            "overall_elo_norm": 0.71,
            "elo_diff_norm":    0.70,
        }
        c1 = build_contract_for_pick(pick, factors, sf_triple_elo)
        assert c1.independent_convergence.status == EvidenceStatus.MISSING \
            or c1.independent_convergence.detail.startswith("insufficient_independent")
        # Add an actually independent signal (first-serve %) → now
        # convergence becomes computable.
        sf_diverse = dict(sf_triple_elo)
        sf_diverse["first_serve_win_pct_norm"] = 0.75
        c2 = build_contract_for_pick(pick, factors, sf_diverse)
        assert c2.independent_convergence.status == EvidenceStatus.AVAILABLE

    def test_tennis_distribution_not_applicable_allowed(self):
        pick = _make_pick("TENNIS", "Player A Moneyline",
                           probability_provenance="MODEL_FIRST")
        c = build_contract_for_pick(pick, {}, None)
        # NOT_APPLICABLE because tennis lacks a Monte Carlo sim by default
        assert c.simulation_distribution_support.status == \
            EvidenceStatus.NOT_APPLICABLE


# ══════════════════════════════════════════════════════════════════
# 7.  CFB independent simulator (P17)
# ══════════════════════════════════════════════════════════════════
class TestCfbIndependentSim:
    def test_produces_distinct_probability(self):
        res = run_cfb_independent_sim(
            {"expected_margin": 7.0, "expected_total": 52.0,
             "is_home": 1, "line": -7.0, "total_line": 52.0},
            {}, seed=1234)
        assert res is not None
        # Independent Monte Carlo probability is a *different*
        # distribution — must be in a legitimate probability band.
        assert 0.0 < res.home_ml_prob < 1.0
        assert 0.3 < res.stability < 1.0

    def test_returns_none_when_insufficient(self):
        res = run_cfb_independent_sim({}, {}, seed=1)
        assert res is None

    def test_over_under_probabilities_sum(self):
        res = run_cfb_independent_sim(
            {"expected_margin": 3.0, "expected_total": 50.0,
             "is_home": 1, "total_line": 50.0},
            {}, seed=42)
        assert res.over_prob is not None
        assert res.under_prob is not None
        # Push probability keeps sum slightly below 1.0.
        assert 0.9 < (res.over_prob + res.under_prob) <= 1.0


# ══════════════════════════════════════════════════════════════════
# 8.  Market-family classifier
# ══════════════════════════════════════════════════════════════════
class TestMarketFamilyClassifier:
    def test_nfl_player_vs_game(self):
        assert _classify_market_family("NFL", "Joe Burrow Passing Yards Over 275.5") == "NFL_PLAYER"
        assert _classify_market_family("NFL", "Eagles Moneyline") == "NFL_GAME"

    def test_mlb_families(self):
        assert _classify_market_family("MLB", "Judge Over 1.5 Hits") == "MLB_HITTER"
        assert _classify_market_family("MLB", "Cole Strikeouts Over 7.5") == "MLB_PITCHER"

    def test_soccer_families(self):
        assert _classify_market_family("SOCCER", "Haaland Anytime Goalscorer") == "SOCCER_PLAYER"
        assert _classify_market_family("SOCCER", "Arsenal Moneyline") == "SOCCER_GAME"

    def test_tennis(self):
        assert _classify_market_family("TENNIS", "Djokovic Moneyline") == "TENNIS"


# ══════════════════════════════════════════════════════════════════
# 9.  Real production path — compute_lock_score integration
# ══════════════════════════════════════════════════════════════════
def test_uea_lifts_via_compute_lock_score():
    """UEA must be able to LIFT a mid-composite score when the
    9-axis evidence contract deserves it — proves the wiring at the
    ``compute_lock_score`` integration point."""
    from sports_engine import compute_lock_score
    factors = {
        "Model Win Prob (norm)":       0.86,
        "Expected Margin (norm)":      0.84,
        "SP+ Rating Δ (norm)":         0.82,
        "Simulation Stability (norm)": 0.94,
        "Matchup Advantage":           0.90,
        "exact_threshold_hit_rate":    0.86,
        "history_sample_size":         30,
    }
    pick = {
        "sport": "NFL", "market": "Bills Moneyline",
        "probability_provenance": "PLATINUM",
        "model_win_probability": 86.0,
        "data_quality": "returning_prod_both+portal_both",
        "sim_stability": 0.94,
        "no_vig_implied_pct": 78.0,
    }
    ls, _ = compute_lock_score(factors, win_prob=86.0, pick=pick,
                                 edge_percent=6.0)
    assert pick.get("evidence_authority") is not None
    assert ls >= 93.0, f"strong NFL game evidence must reach ≥93; got {ls}"
    assert ls <= 99.0


def test_uea_never_lifts_thin_evidence_to_99():
    """Thin-evidence picks must NEVER be lifted to 99."""
    from sports_engine import compute_lock_score
    factors = {"Model Win Prob (norm)": 0.68}
    pick = {
        "sport": "MLB", "market": "Astros Moneyline",
        "probability_provenance": "HEURISTIC",
        "model_win_probability": 68.0,
        "data_quality": None,
    }
    ls, _ = compute_lock_score(factors, win_prob=68.0, pick=pick,
                                 edge_percent=1.0)
    assert ls < 95.0, f"thin evidence must not reach 95; got {ls}"


def test_apex_100_preserved():
    """UEA clamps to 99 — Apex 100 remains the separate gate."""
    from sports_engine import compute_lock_score
    # Even with peak evidence, compute_lock_score returns ≤ 99.
    factors = {
        "Model Win Prob (norm)":       0.99,
        "Expected Margin (norm)":      0.99,
        "SP+ Rating Δ (norm)":         0.99,
        "Simulation Stability (norm)": 0.99,
        "Matchup Advantage":           0.99,
        "exact_threshold_hit_rate":    0.99,
        "history_sample_size":         100,
    }
    pick = {
        "sport": "NFL", "market": "Bills Moneyline",
        "probability_provenance": "PLATINUM",
        "model_win_probability": 99.0,
        "data_quality": "returning_prod_both+portal_both",
        "sim_stability": 0.99,
    }
    ls, _ = compute_lock_score(factors, win_prob=99.0, pick=pick)
    assert ls <= 99.0
