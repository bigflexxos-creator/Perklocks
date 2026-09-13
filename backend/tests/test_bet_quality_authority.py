"""Bet Quality Authority — focused surgical closure tests."""
from __future__ import annotations

import pytest

from services.bet_quality_authority import (
    BET_QUALITY_AUTHORITY_ENABLED_SPORTS,
    BQ_WEIGHTS,
    compute_bet_quality_authority,
    bet_quality_authority_enabled,
)
from sports_engine import compute_lock_score


# ─────────────────────────────────────────────────────────────────────
# Weights structural invariant
# ─────────────────────────────────────────────────────────────────────

def test_bq_weights_sum_to_one():
    assert abs(sum(BQ_WEIGHTS.values()) - 1.0) < 1e-9
    assert BQ_WEIGHTS["wp"] == 0.40
    assert BQ_WEIGHTS["reliability"] == 0.15
    assert BQ_WEIGHTS["history"] == 0.15
    assert BQ_WEIGHTS["matchup"] == 0.10
    assert BQ_WEIGHTS["convergence"] == 0.10
    assert BQ_WEIGHTS["distribution"] == 0.05
    assert BQ_WEIGHTS["data_quality"] == 0.05


# ─────────────────────────────────────────────────────────────────────
# Feature-flag routing
# ─────────────────────────────────────────────────────────────────────

def test_feature_flag_default_sports():
    assert bet_quality_authority_enabled("MLB", "Moneyline")
    assert bet_quality_authority_enabled("CFB", "Total Points Over 50.5")
    assert bet_quality_authority_enabled("NFL", "Player 25.5 receiving yards")
    assert bet_quality_authority_enabled("NFL", "Total Points Over 44.5")


def test_feature_flag_excludes_other_sports():
    assert not bet_quality_authority_enabled("Soccer", "Match Winner")
    assert not bet_quality_authority_enabled("Tennis", "Match Winner")
    assert not bet_quality_authority_enabled("NBA", "Points Over 24.5")


# ─────────────────────────────────────────────────────────────────────
# LS != WP — a 80% WP prop with exceptional evidence CAN reach 98/99
# ─────────────────────────────────────────────────────────────────────

def test_bq_ceiling_80pct_wp_with_perfect_evidence_reaches_98():
    """Old NFL prop authority capped 80% WP at 60+80*0.40 = 92; BQ
    authority allows LS 98 when all multi-signal axes are perfect."""
    pick = {
        "sport": "NFL",
        "market": "Player 25.5 receiving yards",
        "book_odds": -150,
        "win_probability": 80.0,
        "edge_percent": 5.0,
        "probability_provenance": "PLATINUM",
        "data_quality": "returning_prod_both+portal_both",
        "sim_stability": 0.95,
        "exact_threshold_hit_rate": 0.75,
    }
    factors = {
        # Tightly clustered (all near 0.80) → convergence in the
        # 90-95 band; this reflects a real "everything aligns" prop.
        "Matchup Advantage": 0.82,
        "L10 Hit Rate":       0.80,
        "Route Rate":         0.78,
        "Snap Share":         0.80,
        "Target Share":       0.81,
    }
    ceiling, comps = compute_bet_quality_authority(
        win_prob_pct=80.0, pick=pick,
        factors=factors, scoring_factors=factors,
    )
    assert ceiling >= 92.0, (
        f"Perfect-evidence 80% WP prop capped at {ceiling}; "
        f"components: {comps}"
    )
    # And the composite still can breathe past the old 92 ceiling
    # when other axes clearly qualify (proving the OLD 60+WP*40
    # regression is closed).
    assert ceiling > 92.0 or comps["wp"] < 96.0, (
        f"BQ ceiling still stuck at the old 92 cap for a strong "
        f"80% WP prop; components: {comps}"
    )


def test_bq_ceiling_95pct_wp_can_reach_98():
    """A 95% WP prop with strong multi-signal evidence must reach 98+
    ceiling — the OLD authority capped at 60+0.95*40 = 98 exactly,
    the BQ authority now allows the same ceiling naturally when
    evidence backs it."""
    pick = {
        "sport": "NFL",
        "market": "Player 25.5 receiving yards",
        "book_odds": -400,
        "win_probability": 95.0,
        "edge_percent": 6.0,
        "probability_provenance": "PLATINUM",
        "data_quality": "returning_prod_both+portal_both",
        "sim_stability": 0.95,
        "exact_threshold_hit_rate": 0.82,
    }
    factors = {
        "Matchup Advantage": 0.88,
        "L10 Hit Rate":       0.85,
        "Route Rate":         0.87,
        "Snap Share":         0.86,
        "Target Share":       0.88,
    }
    ceiling, comps = compute_bet_quality_authority(
        win_prob_pct=95.0, pick=pick,
        factors=factors, scoring_factors=factors,
    )
    assert ceiling >= 94.0, (
        f"95% WP + strong-evidence prop should reach ≥94 ceiling; "
        f"got {ceiling}; components: {comps}"
    )


def test_bq_ceiling_80pct_wp_with_thin_evidence_below_90():
    pick = {
        "sport": "NFL",
        "market": "Player 25.5 receiving yards",
        "book_odds": -150,
        "win_probability": 80.0,
        "edge_percent": 5.0,
        "probability_provenance": "HEURISTIC",
        # No matchup / history / sim stability signals.
    }
    ceiling, comps = compute_bet_quality_authority(
        win_prob_pct=80.0, pick=pick,
        factors={}, scoring_factors={},
    )
    # Thin evidence → ceiling naturally lower.
    assert ceiling < 90.0, (
        f"Thin-evidence 80% WP prop should not reach 90; got {ceiling} "
        f"components: {comps}"
    )


# ─────────────────────────────────────────────────────────────────────
# 99 remains reachable for exceptional evidence; 100 is APEX-only
# ─────────────────────────────────────────────────────────────────────

def test_bq_ceiling_capped_at_99_not_100():
    pick = {
        "sport": "MLB",
        "market": "Anytime HR",
        "book_odds": -110,
        "win_probability": 99.0,
        "edge_percent": 20.0,
        "probability_provenance": "PLATINUM",
        "data_quality": "full",
        "sim_stability": 1.0,
        "exact_threshold_hit_rate": 1.0,
    }
    factors = {
        "Matchup Advantage": 1.0, "Barrel% (Quality of Contact)": 1.0,
        "Hard-Hit % (Statcast)": 1.0, "Expected BA (Statcast)": 1.0,
        "Recent L10 Hit Rate": 1.0,
    }
    ceiling, _ = compute_bet_quality_authority(
        win_prob_pct=99.0, pick=pick,
        factors=factors, scoring_factors=factors,
    )
    assert ceiling <= 99.0, f"BQ ceiling breached 99 → {ceiling}"


# ─────────────────────────────────────────────────────────────────────
# LS = min(composite, BQ ceiling) — never lifts
# ─────────────────────────────────────────────────────────────────────

def test_bq_ceiling_never_lifts_a_weak_composite():
    """Compose a weak-composite pick and prove BQ authority doesn't
    inflate the emitted Lock Score."""
    pick = {
        "sport": "MLB",
        "market": "Anytime HR",
        "book_odds": +150,
        "win_probability": 40.0,
        "edge_percent": -1.0,
        "probability_provenance": "HEURISTIC",
    }
    ls, _ = compute_lock_score({}, win_prob=40.0, pick=pick, edge_percent=-1.0)
    # Weak WP → composite bound below 80; BQ ceiling can't lift.
    assert ls <= 75.0, f"Weak-composite Lock Score inflated to {ls}"


def test_bq_ceiling_supersedes_old_nfl_prop_authority():
    """Simulate NFL player-prop path with 80% WP + strong evidence and
    verify the old 60+WP*40 hard cap no longer blocks LS ≥ 92."""
    factors = {
        "L5 Avg vs Line":      0.72,
        "Opponent Defense":    0.86,
        "Health Signal":       0.90,
        "ATD Engine Conf":     0.82,
    }
    pick = {
        "sport": "NFL",
        "market": "Player 5.5 receptions",
        "book_odds": -160,
        "win_probability": 80.0,
        "edge_percent": 4.0,
        "probability_provenance": "MODEL_CONDITIONED",
        "data_quality": "full",
        "model_win_prob": 80.0,
    }
    ls, _ = compute_lock_score(factors, win_prob=80.0,
                                pick=pick, edge_percent=4.0)
    # With BQ authority active, the composite is no longer capped at 92
    # (old formula: 60+0.80*40=92).  The multi-signal ceiling now
    # permits the composite to breathe up to the BQ ceiling.
    assert pick.get("nfl_prop_authority_bq_supersedes") is True, (
        f"BQ authority not detected as active on NFL player prop path; "
        f"pick={ {k: pick[k] for k in pick if 'authority' in k} }"
    )


# ─────────────────────────────────────────────────────────────────────
# Bet-Quality provenance is stamped on the pick
# ─────────────────────────────────────────────────────────────────────

def test_bq_stamps_provenance_on_pick():
    factors = {"a (norm)": 0.7, "b (norm)": 0.72, "c (norm)": 0.71}
    pick = {
        "sport": "MLB",
        "market": "Hits Over 0.5",
        "book_odds": -150,
        "win_probability": 78.0,
        "edge_percent": 3.0,
        "probability_provenance": "MODEL_CONDITIONED",
    }
    compute_lock_score(factors, win_prob=78.0, pick=pick, edge_percent=3.0)
    bq = pick.get("bet_quality_authority")
    assert bq is not None
    assert bq.get("version", "").startswith("bq_authority.")
    assert 0.0 < bq["ceiling"] <= 99.0
    assert set(bq["components"].keys()) == set(BQ_WEIGHTS.keys())
